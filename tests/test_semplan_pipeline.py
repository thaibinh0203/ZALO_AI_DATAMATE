"""Synthetic contracts for the opt-in semantic-plan pipeline (design §13.1).

A scripted fake model checks contracts, refinement, budgets and replay; these tests do
not show that the real local model follows the contracts.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1];sys.path.insert(0, str(ROOT/'src'))
from datamate_data.semplan import contracts, quality  # noqa: E402
from datamate_data.semplan.build import build_business  # noqa: E402
from datamate_data.semplan.checks import TableState, compute_checks  # noqa: E402
from datamate_data.semplan.contracts import EvidenceCatalog, rule_units  # noqa: E402
from datamate_data.semplan.model import PlanModel  # noqa: E402
from datamate_data.semplan.publish import load_manifest  # noqa: E402
from datamate_data.semplan.rows import classify, finalize_part  # noqa: E402
from datamate_data.semplan.status import build_mapping, infer_domain  # noqa: E402
from datamate_data.semplan.temporal import infer_day_month_order, parse_temporal, year_evidence  # noqa: E402

RULEBOOK = (ROOT/'docs/train/rulebook.md').read_text(encoding='utf-8')
CONFIG = json.loads((ROOT/'configs/pipeline_semplan.json').read_text(encoding='utf-8'))
ORDERS = ('Mã hóa đơn,Ngày bán,Khách hàng,Tên hàng,Số lượng,Đơn vị tính,Thành tiền,Trạng thái\n'
          'A1,03/03/2026,Lan,Nước,2,thùng,100000,Hoàn thành\n'
          ',,,Bánh,1,chai,20000,\n'
          'A2,04/03,,Tổng hợp rau,1,chai,5000,Đã huỷ\n'
          'A2,04/03,,Tổng hợp rau,1,chai,5000,Đã huỷ\n'
          'Tổng ngày 04/03,,,,,,10000,\n'
          'Ghi chú: số liệu tạm,,,,,,,\n'
          'A3,05/03/2026,Minh,Phí giao hàng,,,15000,Chờ xác nhận\n'
          ',,,,,,125000,\n')
COLUMNS = [{'name': n, 'original_name': n, 'role': r, 'sql_type': 'TEXT'} for n, r in
           [('ma', 'order_id'), ('ngay', 'date'), ('ten', 'product_name'), ('sl', 'quantity'), ('tien', 'line_amount')]]


class ScriptedModel:
    def __init__(self, handler):
        self.handler = handler;self.calls = []

    def metadata(self):
        return {'model': 'scripted-fake'}

    def load(self):
        return self

    def generate(self, messages):
        payload = json.loads(messages[1]['content']);self.calls.append(payload)
        out = self.handler(payload)
        return {'raw_output': out if isinstance(out, str) else json.dumps(out, ensure_ascii=False),
                'prompt_tokens': 0, 'output_tokens': 0, 'generation_ms': 0.0, 'hit_token_limit': False}


def unknown_plan():
    return {'grain': {'kind': 'unknown', 'semantic_kind': None, 'description': '', 'identity_columns': [], 'evidence': []},
            'group_keys': [], 'measures': [], 'joins': [], 'status_columns': [], 'policies': []}


class Fixture:
    def __init__(self, files, readme='# Demo\nFile bán hàng: mỗi dòng một mặt hàng của đơn.\n- Nước: 1 thùng = 24 chai\n', rulebook=RULEBOOK):
        self.temp = tempfile.TemporaryDirectory();self.root = Path(self.temp.name)
        self.source = self.root/'src';self.business = self.source/'data'/'biz_demo';self.business.mkdir(parents=True)
        (self.business/'README.md').write_text(readme, encoding='utf-8')
        for name, content in files.items():
            if isinstance(content, str):
                (self.business/name).write_text(content, encoding='utf-8')
            else:
                content(self.business/name)
        (self.source/'rulebook.md').write_text(rulebook, encoding='utf-8')
        (self.source/'calendar.csv').write_text('date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n')

    def build(self, name='out', *, model=None, config=None, dsn=None, replay=None, caps=None):
        config = deepcopy(config or CONFIG)
        plan_model = None
        if model is not None:
            plan_model = PlanModel(model, audit_path=self.root/'pending.jsonl', caps=caps or config['model_caps'],
                                   cache_dir=self.root/'cache', identity={'test': name})
        return build_business(self.business, self.root/name/'biz_demo', rulebook=self.source/'rulebook.md',
                              calendar=self.source/'calendar.csv', config=config, plan_model=plan_model, dsn=dsn,
                              replay_plan=replay, log=lambda *_: None), self.root/name/'biz_demo'

    def close(self):
        for path in self.root.rglob('*'):
            if path.is_file():
                path.chmod(stat.S_IWUSR | stat.S_IRUSR)
        self.temp.cleanup()


def rows(out):
    catalog = json.loads((out/'catalog.json').read_text())
    table = next(iter(catalog['tables'].values()))
    return table, [json.loads(l) for l in (out/table['data_path']).read_text().splitlines()]


class RowClassificationTests(unittest.TestCase):
    def classify(self, values):
        return classify(values, headers=['Mã', 'Ngày', 'Tên', 'SL', 'Tiền'], columns=COLUMNS)

    def test_total_label_with_identity_is_never_dropped(self):
        self.assertEqual(self.classify(['Tổng ngày 04/03', None, None, None, '10000'])['kind'], 'subtotal')
        product = self.classify(['A1', '2026-03-04', 'Tổng hợp rau', '1', '5000'])
        self.assertEqual((product['kind'], product['rule']), ('data', 'default_structured_row'))
        named = classify(['Tổng hàng khuyến mãi', 'A1', '1', '5000'], headers=['Tên', 'Mã', 'SL', 'Tiền'],
                         columns=[COLUMNS[2], COLUMNS[0], COLUMNS[3], COLUMNS[4]])
        self.assertEqual((named['kind'], named['rule']), ('data', 'total_word_inside_product_name'))
        odd = self.classify(['Tổng', None, 'Nước', None, '10'])
        self.assertEqual((odd['kind'], odd['decision_status']), ('unknown', 'candidate'))

    def test_fee_line_without_quantity_is_data_and_unlabeled_numbers_are_candidates(self):
        self.assertEqual(self.classify(['A3', '05/03/2026', 'Phí giao hàng', None, '15000'])['kind'], 'data')
        decision = self.classify([None, None, None, None, '125000'])
        self.assertEqual((decision['kind'], decision['candidate_kind']), ('unknown', 'subtotal'))

    def test_header_note_metadata_and_ragged_rows(self):
        self.assertEqual(self.classify(['Mã', 'Ngày', 'Tên', 'SL', 'Tiền'])['kind'], 'repeated_header')
        self.assertEqual(self.classify(['Ghi chú: tạm tính', None, None, None, None])['kind'], 'note')
        self.assertEqual(self.classify(['Người lập: Lan', None, None, None, None])['kind'], 'metadata')
        long_text = self.classify([None, 'Hàng đổi trả được xử lý vào cuối tháng sau', None, None, None])
        self.assertEqual((long_text['kind'], long_text['candidate_kind']), ('unknown', 'note'))
        ragged = classify(['A', 'B'], headers=['Mã', 'Ngày'], columns=COLUMNS[:2], flags=[{'error': 'ragged_row'}])
        self.assertEqual(ragged['kind'], 'quarantine')

    def test_total_after_last_data_row_is_grand_total(self):
        decisions = [{'seq': 0, 'kind': 'data'}, {'seq': 1, 'kind': 'subtotal', 'decision_status': 'decided_rule', 'rule': 'r'},
                     {'seq': 2, 'kind': 'data'}, {'seq': 3, 'kind': 'subtotal', 'decision_status': 'decided_rule', 'rule': 'r'}]
        finalize_part(decisions)
        self.assertEqual([d['kind'] for d in decisions], ['data', 'subtotal', 'data', 'grand_total'])


class TemporalTests(unittest.TestCase):
    def test_partial_dates_keep_parts_and_never_invent_a_year(self):
        partial = parse_temporal('1/7')
        self.assertEqual((partial['value'], partial['month'], partial['day'], partial['precision'], partial['resolution']),
                         (None, 7, 1, 'month_day', 'missing_year'))
        leap = parse_temporal('29/2');self.assertEqual((leap['status'], leap['precision']), ('partial', 'month_day'))
        self.assertTrue(parse_temporal('31/2')['status'].startswith('error:'))
        sheet = {'status': 'single', 'year': 2025, 'source': 'sheet'}
        resolved = parse_temporal('1/7', year_info=sheet)
        self.assertEqual((str(resolved['value']), resolved['resolution']), ('2025-07-01', 'year_from_sheet'))
        self.assertTrue(parse_temporal('29/2', year_info=sheet)['status'].startswith('error:invalid_for_source_year'))
        conflicting = parse_temporal('1/7', year_info={'status': 'conflicting', 'year': None})
        self.assertEqual(conflicting['resolution'], 'conflicting_year')
        month = parse_temporal('07/2025');self.assertEqual((month['precision'], month['month'], month['year']), ('month', 7, 2025))

    def test_year_evidence_prefers_sheet_and_reports_conflicts(self):
        part = {'format': 'xlsx', 'source_table': 'T01_2025', 'source_file': 'So_2026.xlsx', 'header_context': [['Báo cáo'], ['Ngày']]}
        info = year_evidence(part, {'missing_year_from_sheet': 'B66'})
        self.assertEqual((info['year'], info['source'], info['conflicting_sources'], info['evidence_refs']), (2025, 'sheet', ['file'], ['B66']))
        both = year_evidence({'format': 'csv', 'source_table': 'x', 'source_file': 'a_2024.csv',
                              'header_context': [['Năm 2025'], ['Ngày']]})
        self.assertEqual((both['status'], both['year']), ('conflicting', None))

    def test_day_month_order_needs_evidence(self):
        self.assertEqual(infer_day_month_order({'dmy_only': 3})['order'], 'dmy')
        self.assertEqual(infer_day_month_order({'dmy_only': 3, 'mdy_only': 2})['order'], 'ambiguous')
        self.assertEqual(infer_day_month_order({'mdy_only': 1}, 'dmy')['order'], 'ambiguous')
        ambiguous = parse_temporal('03/04/2025', order='ambiguous')
        self.assertEqual((ambiguous['value'], ambiguous['resolution']), (None, 'ambiguous_day_month'))
        self.assertEqual(str(parse_temporal('13/04/2025', order='ambiguous')['value']), '2025-04-13')


class StatusTests(unittest.TestCase):
    def vocabulary(self, values):
        return {'check_id': 'Cx', 'result': {'values': values, 'non_empty': sum(n for _, n in values), 'truncated': False, 'cap': 200}}

    def test_delivered_event_is_not_order_completed(self):
        column = {'original_name': 'Trạng thái giao hàng', 'description': ''}
        domain, basis = infer_domain(column, grain_kind='order_line')
        self.assertEqual(domain, 'delivery')
        mapping = build_mapping('deliveries', 'tt', domain, basis, self.vocabulary([['Đã giao', 3], ['Hoàn tất', 1]]))
        self.assertEqual(mapping['mappings']['Đã giao']['target'], 'delivered')
        self.assertEqual(mapping['unresolved'], ['Hoàn tất'])
        order = build_mapping('orders', 'tt', 'order', 'grain', self.vocabulary([['Đã giao', 3]]))
        self.assertEqual(order['mappings']['Đã giao']['target'], 'completed')

    def test_model_domain_must_agree_with_structure_and_unknown_domain_maps_nothing(self):
        self.assertIsNone(infer_domain({'original_name': 'Trạng thái', 'description': ''}, grain_kind='delivery_item', proposed='order')[0])
        mapping = build_mapping('t', 'c', None, 'no_domain_evidence', self.vocabulary([['Hoàn thành', 2]]))
        self.assertEqual((mapping['status'], mapping['mappings'], mapping['unresolved']), ('needs_review', {}, ['Hoàn thành']))

    def test_status_review_rejects_targets_outside_domain(self):
        catalog = EvidenceCatalog.from_sources('# x\n- Hoàn tất: đơn đã xong\n', RULEBOOK)
        accepted, rejected = contracts.validate_status_review(
            {'mappings': [{'value': 'Hoàn tất', 'target': 'completed', 'evidence': ['R2']},
                          {'value': 'Lạ', 'target': 'delivered', 'evidence': ['R2']}], 'unresolved': []},
            ['Hoàn tất', 'Lạ'], 'order', catalog)
        self.assertEqual(accepted['Hoàn tất']['target'], 'completed');self.assertEqual(len(rejected), 1)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.catalog = EvidenceCatalog.from_sources('# README\n- Mỗi dòng là một mặt hàng\n', RULEBOOK)
        self.table = {'name': 't', 'columns': [{'name': 'don', 'role': 'order_id', 'sql_type': 'TEXT'},
                                               {'name': 'gia', 'role': 'unit_price', 'sql_type': 'NUMERIC'},
                                               {'name': 'tien', 'role': 'line_amount', 'sql_type': 'NUMERIC'}]}

    def test_prices_are_not_summed_and_other_needs_semantic_kind(self):
        value = unknown_plan();value['grain'] = {'kind': 'other', 'semantic_kind': 'delivery_item_per_lot',
                                                 'description': 'Một mặt hàng theo lô', 'identity_columns': [], 'evidence': ['R2']}
        value['measures'] = [{'column': 'gia', 'scope': 'row', 'aggregation': 'sum', 'unit': None, 'time_semantics': 'event',
                              'weights_column': None, 'evidence': []}]
        plan = contracts.validate_table_plan(value, self.table, self.catalog, {'t': self.table})
        self.assertEqual(plan['grain']['semantic_kind'], 'delivery_item_per_lot')
        self.assertEqual(plan['measures'][0]['aggregation'], 'none')
        bad = deepcopy(value);bad['grain']['semantic_kind'] = None
        with self.assertRaises(contracts.ContractError):
            contracts.validate_table_plan(bad, self.table, self.catalog, {'t': self.table})
        unknown = unknown_plan()
        self.assertEqual(contracts.validate_table_plan(unknown, self.table, self.catalog, {'t': self.table})['grain']['kind'], 'unknown')

    def test_invalid_items_are_rejected_not_repaired(self):
        value = unknown_plan()
        value['group_keys'] = [{'name': 'order', 'columns': ['nope'], 'purpose': 'x', 'evidence': []}]
        value['policies'] = [{'operation': 'drop_rows', 'columns': ['don'], 'key_columns': [], 'mode': 'key', 'evidence': ['R2']}]
        value['joins'] = [{'target': 't', 'source_columns': ['don'], 'target_columns': ['don'], 'purpose': '', 'evidence': ['R2']}]
        plan = contracts.validate_table_plan(value, self.table, self.catalog, {'t': self.table})
        self.assertEqual((plan['group_keys'], plan['policies'], plan['joins']), ([], [], []))
        self.assertEqual(len(plan['rejected']), 3)
        with self.assertRaises(contracts.ContractError):
            contracts.validate_table_plan({**unknown_plan(), 'extra': 1}, self.table, self.catalog, {'t': self.table})

    def test_rule_units_cite_verbatim_rulebook_rows(self):
        units = rule_units(RULEBOOK);detected = contracts.detect_rules(units)
        for name in ('group_blank_cells_continue', 'same_order_identical_rows_once', 'non_order_rows_counted',
                     'missing_year_from_sheet', 'base_unit_default_factor_1', 'outside_coverage'):
            self.assertIn(name, detected)
        unit = next(u for u in units if u['id'] == detected['same_order_identical_rows_once'])
        self.assertIn(unit['quote'], RULEBOOK)


class QualityAssessTests(unittest.TestCase):
    def issue(self, kind, **kw):
        return quality.make_issue(kind, business_id='b', table='t', view='t_clean', revision='r', **kw)

    def test_unknown_rows_outside_window_do_not_block_but_undated_ones_do(self):
        dated = self.issue('unknown_rows', row_count=2, known_bounds={'time': {'min': '2026-01-03', 'max': '2026-01-05'}},
                           unknown_bounds={'time': False})
        dep = {'view': 't_clean', 'columns': {'tien': ['sum']}, 'table_operations': ['sum'],
               'time_filters': [{'column': 'ngay', 'start': '2026-02-01', 'end_exclusive': '2026-03-01'}]}
        self.assertEqual(quality.assess({'issues': [dated]}, [dep])['decision'], 'pass')
        inside = {**dep, 'time_filters': [{'column': 'ngay', 'start': '2026-01-01', 'end_exclusive': '2026-02-01'}]}
        self.assertEqual(quality.assess({'issues': [dated]}, [inside])['decision'], 'block')
        undated = self.issue('unknown_rows', row_count=1, unknown_bounds={'time': True})
        result = quality.assess({'issues': [undated]}, [dep])
        self.assertEqual((result['decision'], result['items'][0]['impact']), ('block', 'cannot_exclude'))

    def test_issue_on_unused_column_or_other_table_is_not_blocking(self):
        errors = self.issue('parse_errors', columns=['ghi_chu'], row_count=3, unknown_bounds={'values': True})
        dep = {'view': 't_clean', 'columns': {'tien': ['sum']}, 'table_operations': ['sum'], 'time_filters': []}
        self.assertEqual(quality.assess({'issues': [errors]}, [dep])['decision'], 'pass')
        self.assertEqual(quality.assess({'issues': [errors]}, [{**dep, 'view': 'other_clean'}])['items'], [])

    def test_partial_date_months_outside_window_are_excluded(self):
        partial = self.issue('partial_dates', columns=['ngay'], row_count=2, known_bounds={'months': [7]},
                             unknown_bounds={'year': True, 'months': False})
        march = {'view': 't_clean', 'columns': {'ngay': ['time_filter']}, 'table_operations': ['sum'],
                 'time_filters': [{'column': 'ngay', 'start': '2026-03-01', 'end_exclusive': '2026-04-01'}]}
        self.assertEqual(quality.assess({'issues': [partial]}, [march])['decision'], 'pass')
        july = {**march, 'time_filters': [{'column': 'ngay', 'start': '2026-07-01', 'end_exclusive': '2026-08-01'}]}
        self.assertEqual(quality.assess({'issues': [partial]}, [july])['decision'], 'block')


class BuildTests(unittest.TestCase):
    def tearDown(self):
        if hasattr(self, 'fx'):
            self.fx.close()

    def test_rule_only_build_policies_quality_and_immutable_publication(self):
        self.fx = Fixture({'sales.csv': ORDERS})
        document, out = self.fx.build()
        self.assertEqual(document['status'], 'prepared_not_loaded');self.assertFalse(document['usable_by_baseline'])
        table, records = rows(out)
        kinds = [r['kind'] for r in records]
        self.assertEqual(kinds, ['data', 'data', 'data', 'data', 'subtotal', 'note', 'data', 'unknown'])
        plan = json.loads((out/'semantic_plan.json').read_text())['tables'][table['name']]
        policies = {p['operation']: p for p in plan['policies']}
        self.assertEqual(policies['fill_forward']['decision_status'], 'enabled')
        self.assertIn('B62', policies['fill_forward']['evidence_refs'])
        self.assertEqual(policies['mark_duplicates']['decision_status'], 'enabled')
        self.assertIn('B64', policies['mark_duplicates']['evidence_refs'])
        self.assertEqual(records[1]['typed'][:3], ['A1', '2026-03-03', 'Lan'])
        self.assertEqual(records[1]['quality']['notes']['ma_hoa_don']['status'], 'filled_within_group')
        self.assertEqual(records[1]['raw'][0], '')
        self.assertIsNone(records[2]['duplicate_of']);self.assertEqual(records[3]['duplicate_of'], records[2]['row_id'])
        self.assertIsNone(records[2]['typed'][2])  # walk-in head row stays without a customer
        self.assertEqual(records[2]['temporal']['ngay_ban'], [None, 3, 4, 'month_day', 'missing_year'])
        self.assertEqual(records[6]['typed'][7], 'Chờ xác nhận')
        kinds_q = {i['kind'] for i in json.loads((out/'quality_registry.json').read_text())['issues']}
        self.assertTrue({'unknown_rows', 'partial_dates', 'unmapped_status'} <= kinds_q)
        manifest = load_manifest(out)
        self.assertEqual(manifest['semantic_revision'], json.loads((out/'semantic_plan.json').read_text())['plan_revision'])
        with self.assertRaises(PermissionError):
            (out/'semantic_plan.json').write_text('{}')
        (out/'quality_registry.json').chmod(stat.S_IWUSR | stat.S_IRUSR)
        (out/'quality_registry.json').write_text('{"issues": []}')
        with self.assertRaisesRegex(ValueError, 'changed after publication'):
            load_manifest(out)
        with self.assertRaises(FileExistsError):
            self.fx.build()

    def test_fill_forward_resets_at_unknown_rows_and_never_repairs_parse_errors(self):
        content = ('Mã hóa đơn,Ngày bán,Tên hàng,Số lượng,Thành tiền\n'
                   'A1,03/03/2026,Nước,1,10\n'
                   ',,Bánh,1,20\n'
                   ',,,,30\n'
                   ',,Kẹo,1,40\n'
                   'A2,ngày-lỗi,Nước,1,50\n'
                   'A2,,Bánh,1,60\n')
        self.fx = Fixture({'sales.csv': content})
        _, out = self.fx.build()
        _, records = rows(out)
        self.assertEqual(records[1]['typed'][0], 'A1')
        self.assertEqual(records[2]['kind'], 'unknown')
        self.assertIsNone(records[3]['typed'][0], 'fill must reset after an unknown row')
        self.assertIn('ngay_ban', records[4]['quality']['errors'])
        self.assertIsNone(records[5]['typed'][1], 'a parse error in the group head is not copied')

    def test_identical_rows_without_order_key_are_separate_records(self):
        self.fx = Fixture({'events.csv': 'Ngày bán,Tên hàng,Số lượng,Thành tiền\n03/03/2026,Nước,2,10\n03/03/2026,Nước,2,10\n'})
        _, out = self.fx.build()
        table, records = rows(out)
        self.assertTrue(all(r['duplicate_of'] is None for r in records))
        plan = json.loads((out/'semantic_plan.json').read_text())['tables'][table['name']]
        dup = next(p for p in plan['policies'] if p['operation'] == 'mark_duplicates')
        self.assertEqual(dup['decision_status'], 'disabled');self.assertIn('B65', dup['evidence_refs'])
        self.assertNotIn('fill_forward', [p['operation'] for p in plan['policies']])

    def test_sheet_year_resolves_and_missing_year_stays_partial(self):
        def workbook(path):
            from openpyxl import Workbook
            wb = Workbook();ws = wb.active;ws.title = 'T07_2025'
            ws.append(['Mã hóa đơn', 'Ngày bán', 'Tên hàng', 'Số lượng', 'Thành tiền'])
            ws.append(['A1', '1/7', 'Nước', 1, 10]);ws.append(['A2', '29/2', 'Nước', 1, 10])
            other = wb.create_sheet('Khac');other.append(['Mã hóa đơn', 'Ngày bán', 'Tên hàng', 'Số lượng', 'Thành tiền'])
            other.append(['B1', '2/7', 'Nước', 1, 5])
            wb.save(path)
        self.fx = Fixture({'so_ban_hang.xlsx': workbook})
        _, out = self.fx.build()
        catalog = json.loads((out/'catalog.json').read_text())
        all_rows = {r['typed'][0]: r for t in catalog['tables'].values() for r in map(json.loads, (out/t['data_path']).read_text().splitlines())}
        self.assertEqual((all_rows['A1']['typed'][1], all_rows['A1']['temporal']['ngay_ban'][4]), ('2025-07-01', 'year_from_sheet'))
        self.assertIn('ngay_ban', all_rows['A2']['quality']['errors'])  # 29/2 does not exist in 2025
        self.assertEqual(all_rows['B1']['temporal']['ngay_ban'], [None, 7, 2, 'month_day', 'missing_year'])

    def test_formula_without_cache_is_a_parse_error_with_raw_kept(self):
        def workbook(path):
            from openpyxl import Workbook
            wb = Workbook();ws = wb.active;ws.title = 'Sheet1'
            ws.append(['Mã hóa đơn', 'Ngày bán', 'Tên hàng', 'Số lượng', 'Đơn giá', 'Thành tiền'])
            ws.append(['A1', '01/03/2026', 'Nước', 2, 10, '=D2*E2']);ws.append(['A2', '02/03/2026', 'Bánh', 1, 20, 20])
            wb.save(path)
        self.fx = Fixture({'ban_hang.xlsx': workbook})
        _, out = self.fx.build()
        _, records = rows(out)
        self.assertEqual(records[0]['quality']['errors']['thanh_tien'], 'formula_cache_missing')
        self.assertIsNone(records[0]['typed'][5]);self.assertEqual(records[0]['raw'][5], '=D2*E2')
        issues = json.loads((out/'quality_registry.json').read_text())['issues']
        self.assertTrue(any(i['kind'] == 'parse_errors' and i['columns'] == ['thanh_tien'] for i in issues))

    def test_version_mismatches_are_refused(self):
        self.fx = Fixture({'sales.csv': ORDERS})
        _, out = self.fx.build('first')
        plan = json.loads((out/'semantic_plan.json').read_text())
        plan['code']['pipeline_version'] = 'postgres-pipeline-v4-semantic'
        with self.assertRaisesRegex(ValueError, 'another pipeline version'):
            self.fx.build('second', replay=plan)
        plan['schema_version'] = 'business-semantic-plan-v0'
        with self.assertRaisesRegex(ValueError, 'schema version'):
            self.fx.build('third', replay=plan)
        manifest = out/'manifest.json';manifest.chmod(stat.S_IWUSR | stat.S_IRUSR)
        manifest.write_text(manifest.read_text().replace('published-business-v1', 'published-business-v0'))
        with self.assertRaisesRegex(ValueError, 'Unsupported manifest version'):
            load_manifest(out)

    def test_model_row_review_and_status_mapping_are_validated(self):
        def handler(payload):
            if payload['task'] == 'row_review':
                group = payload['groups'][0]
                return {'groups': [{'group': group['group_id'], 'kind': 'grand_total', 'exceptions': [],
                                    'evidence': [group['samples'][0]['ref']]}, {'group': 'G99', 'kind': 'data', 'exceptions': [], 'evidence': ['R1']}]}
            if payload['task'] == 'status_mapping':
                return {'mappings': [{'value': 'Chờ xác nhận', 'target': 'delivering', 'evidence': ['R3']},
                                     {'value': 'Chờ xác nhận', 'target': 'paid', 'evidence': ['R3']}], 'unresolved': []}
            return unknown_plan()
        readme = '# Demo\nFile bán hàng.\n- Chờ xác nhận: đơn đang được giao\n- Nước: 1 thùng = 24 chai\n'
        self.fx = Fixture({'sales.csv': ORDERS}, readme=readme)
        model = ScriptedModel(handler)
        _, out = self.fx.build(model=model)
        table, records = rows(out)
        self.assertEqual(records[-1]['kind'], 'grand_total')
        self.assertEqual(records[-1]['decision']['method'], 'model_group_review')
        self.assertEqual(records[6]['typed'][7], 'delivering')
        plan = json.loads((out/'semantic_plan.json').read_text())
        events = [e for e in plan['events'] if e['event'] == 'row_review'][0]
        self.assertEqual(len(events['rejected']), 1)
        calls = [json.loads(l) for l in (out/'model_calls.jsonl').read_text().splitlines()]
        self.assertTrue(all('messages' in c and 'raw_output' in c for c in calls))

    def test_refinement_runs_once_and_contradicted_items_are_downgraded(self):
        content = ('Mã hóa đơn,Ngày bán,Tên hàng,Số lượng,Thành tiền,Tổng tiền\n'
                   'A1,03/03/2026,Nước,1,10,30\nA1,03/03/2026,Bánh,1,20,30\n'
                   'A2,04/03/2026,Nước,1,15,40\nA2,04/03/2026,Kẹo,1,25,40\n'
                   'A3,05/03/2026,Nước,2,30,50\nA3,05/03/2026,Kẹo,1,20,50\n')

        def plan(identity, aggregation, scope):
            value = unknown_plan()
            value['grain'] = {'kind': 'order_line', 'semantic_kind': None, 'description': 'Một mặt hàng của đơn',
                              'identity_columns': identity, 'evidence': ['R2']}
            value['group_keys'] = [{'name': 'order', 'columns': ['ma_hoa_don'], 'purpose': 'đơn', 'evidence': ['R2']}]
            value['measures'] = [{'column': 'tong_tien', 'scope': scope, 'aggregation': aggregation, 'unit': 'VND',
                                  'time_semantics': 'event', 'weights_column': None, 'evidence': []},
                                 {'column': 'thanh_tien', 'scope': 'row', 'aggregation': 'sum', 'unit': 'VND',
                                  'time_semantics': 'event', 'weights_column': None, 'evidence': []}]
            return value
        seen = []

        def handler(payload):
            if payload['task'] != 'table_plan':
                return unknown_plan()
            seen.append(payload.get('refinement'))
            if payload.get('refinement'):
                return plan(['ma_hoa_don'], 'sum_once_per_group', 'order')
            return plan(['ma_hoa_don'], 'sum', 'row')
        config = deepcopy(CONFIG);config['repetition_min_groups'] = 2
        self.fx = Fixture({'sales.csv': content}, readme='# Demo\nMỗi dòng một mặt hàng; tổng tiền của đơn lặp lại ở mọi dòng.\n')
        _, out = self.fx.build(model=ScriptedModel(handler), config=config)
        table = next(iter(json.loads((out/'semantic_plan.json').read_text())['tables'].values()))
        self.assertEqual(len(seen), 2, 'one initial table plan and exactly one refinement request')
        self.assertTrue(table['refinement']['accepted'])
        failed = {f['kind'] for f in table['refinement']['failed_before']}
        self.assertEqual(failed, {'identity_not_unique_or_null', 'row_sum_but_value_repeats_within_group'})
        measures = {m['column']: m for m in table['measures']}
        self.assertEqual(measures['tong_tien']['aggregation'], 'sum_once_per_group')
        self.assertEqual(table['row_identity']['status'], 'not_established')
        self.assertEqual(table['grain']['identity_columns'], [])

    def test_budget_exhaustion_keeps_safe_fallback_and_is_logged(self):
        self.fx = Fixture({'sales.csv': ORDERS})
        caps = {**{k: 0 for k in CONFIG['model_caps']}, 'max_model_calls_per_business': 600}
        model = ScriptedModel(lambda payload: unknown_plan())
        _, out = self.fx.build(model=model, caps=caps)
        table = next(iter(json.loads((out/'semantic_plan.json').read_text())['tables'].values()))
        self.assertEqual(table['grain']['kind'], 'unknown')
        self.assertEqual(model.calls, [])
        _, records = rows(out)
        self.assertEqual(records[-1]['kind'], 'unknown')
        logged = [json.loads(l) for l in (out/'model_calls.jsonl').read_text().splitlines()]
        self.assertTrue(logged and all(r['sent'] is False and 'budget_exhausted' in r for r in logged))

    def test_replay_reuses_frozen_decisions_without_inference(self):
        def handler(payload):
            if payload['task'] == 'row_review':
                return {'groups': [{'group': g['group_id'], 'kind': 'grand_total', 'exceptions': [],
                                    'evidence': [g['samples'][0]['ref']]} for g in payload['groups']]}
            return unknown_plan()
        self.fx = Fixture({'sales.csv': ORDERS})
        first, out = self.fx.build('first', model=ScriptedModel(handler))
        replay = json.loads((out/'semantic_plan.json').read_text())
        silent = ScriptedModel(lambda payload: self.fail('replay must not call the model'))
        second, out2 = self.fx.build('second', model=silent, replay=replay)
        self.assertEqual(first['semantic_revision'], second['semantic_revision'])
        strip = lambda rs: [{k: v for k, v in r.items() if k != 'decision'} for r in rs]  # noqa: E731
        self.assertEqual(strip(rows(out)[1]), strip(rows(out2)[1]))
        self.assertEqual(rows(out2)[1][-1]['decision']['method'], 'model_group_review_replayed')
        (self.fx.business/'sales.csv').write_text(ORDERS+'A9,06/03/2026,,Nước,1,thùng,1,Hoàn thành\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'source snapshot'):
            self.fx.build('third', replay=replay)

    def test_checks_are_scoped_to_revision_and_recomputed(self):
        self.fx = Fixture({'sales.csv': ORDERS})
        from datamate_data.semplan.staging import stage_business
        out = self.fx.root/'stage'
        staged = stage_business(self.fx.business, out, readme='x', rules={})
        name = next(iter(staged['tables']))
        state = TableState(staged['tables'][name], out, staged['decisions'][name])
        first = compute_checks(state, [{'kind': 'key_stats', 'columns': ['ma_hoa_don']}])[0]
        again = compute_checks(state, [{'kind': 'key_stats', 'columns': ['ma_hoa_don']}])[0]
        self.assertIs(first, again)
        unknown = next(d for d in state.decisions.values() if d['kind'] == 'unknown')
        unknown['kind'] = 'data'
        changed = compute_checks(state, [{'kind': 'key_stats', 'columns': ['ma_hoa_don']}])[0]
        self.assertNotEqual(first['scope']['semantic_revision'], changed['scope']['semantic_revision'])
        self.assertNotEqual(first['check_id'], changed['check_id'])
        self.assertEqual(changed['result']['rows'], first['result']['rows']+1)

    def test_plan_model_cache_and_caps_count_every_attempt(self):
        temp = tempfile.TemporaryDirectory();root = Path(temp.name)
        try:
            outputs = iter(['not json', {'ok': 1}])
            model = ScriptedModel(lambda payload: next(outputs))
            plan_model = PlanModel(model, audit_path=root/'a.jsonl', caps={'s': 2, 'max_model_calls_per_business': 10},
                                   cache_dir=root/'cache')
            value, attempts = plan_model.ask('s', 'x', 'system', {'a': 1})
            self.assertEqual((value, len(attempts)), ({'ok': 1}, 2), 'retry counted as a second attempt')
            value, attempts = plan_model.ask('s', 'x', 'system', {'a': 1})
            self.assertIsNone(value);self.assertIn('budget_exhausted', attempts[0])
            cached = PlanModel(ScriptedModel(lambda p: self.fail('identical request must hit the cache')), audit_path=root/'b.jsonl',
                               caps={'s': 2, 'max_model_calls_per_business': 10}, cache_dir=root/'cache')
            cached.identity = plan_model.identity
            value, attempts = cached.ask('s', 'y', 'system', {'a': 1})
            self.assertTrue(attempts[0]['cache_hit']);self.assertEqual(cached.calls, 2, 'cache hit still counts')
            fresh = ScriptedModel(lambda p: {'ok': 2})
            changed = PlanModel(fresh, audit_path=root/'c.jsonl', caps={'s': 1, 'max_model_calls_per_business': 10},
                                cache_dir=root/'cache')
            changed.identity = plan_model.identity
            self.assertEqual(changed.ask('s', 'z', 'system', {'a': 2})[0], {'ok': 2});self.assertEqual(len(fresh.calls), 1)
        finally:
            temp.cleanup()


@unittest.skipUnless(os.environ.get('DATAMATE_TEST_PG_DSN'), 'set isolated test PostgreSQL DSN')
class PostgresPublicationTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture({'sales.csv': ORDERS});self.dsn = os.environ['DATAMATE_TEST_PG_DSN'];self.schemas = []

    def tearDown(self):
        import psycopg
        from psycopg import sql
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            for schema in self.schemas:
                conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
        self.fx.close()

    def test_load_audit_publish_and_v2_compatibility_reader(self):
        import psycopg
        from psycopg import sql
        document, out = self.fx.build(dsn=self.dsn)
        self.schemas.append(document['database']['schema'])
        self.assertEqual((document['status'], document['audit_status']), ('published', 'passed'))
        catalog = json.loads((out/'postgres_catalog.json').read_text())
        table = next(iter(catalog['tables'].values()))
        with psycopg.connect(self.dsn) as conn:
            view = sql.Identifier(catalog['schema'], table['clean_view'])
            count, total = conn.execute(sql.SQL('SELECT count(*), sum(thanh_tien) FROM {}').format(view)).fetchone()
            self.assertEqual((count, int(total)), (4, 140000))  # unknown/subtotal/note/duplicate excluded
            parts = conn.execute(sql.SQL("SELECT ngay_ban, ngay_ban__m, ngay_ban__d, ngay_ban__p FROM {} WHERE ma_hoa_don='A2'").format(view)).fetchone()
            self.assertEqual(parts, (None, 3, 4, 'month_day'))
            columns = [r[0] for r in conn.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s',
                                                  (catalog['schema'], table['clean_view'])).fetchall()]
            self.assertNotIn('__raw', columns);self.assertIn('trang_thai__raw', columns)
        # The v2 baseline can read the published business through the compatibility sidecar.
        from datamate_baseline.semantic_context import SemanticContextLoader
        loader = SemanticContextLoader(self.fx.root/'out', self.fx.source)
        context = loader.load('biz_demo')
        self.assertIn(table['clean_view'], context['allowed'])
        self.assertEqual(context['business_metadata']['compatibility_adapter']['new_inference'], False)

    def test_audit_failure_is_not_published(self):
        with mock.patch('datamate_data.semplan.postgres.audit', return_value={'status': 'failed', 'checks': [{'status': 'fail'}]}):
            document, out = self.fx.build(dsn=self.dsn)
        self.schemas.append(document['database']['schema'])
        self.assertEqual(document['status'], 'audit_failed');self.assertFalse(document['usable_by_baseline'])
        from datamate_baseline.calcplan.context import PublishedContextLoader
        with self.assertRaisesRegex(ValueError, 'not published'):
            PublishedContextLoader(self.fx.root/'out', self.fx.source).load('biz_demo')


if __name__ == '__main__':
    unittest.main()
