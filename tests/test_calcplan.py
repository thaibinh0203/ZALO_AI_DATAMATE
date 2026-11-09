"""Synthetic checks for the calculation-plan baseline (design §13.2).

Fake models/executors check contracts, routing, budgets and gates; they do not show
that the real local model follows the contracts or that answers are correct.
"""
from copy import deepcopy
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1];sys.path.insert(0, str(ROOT/'src'));sys.path.insert(0, str(Path(__file__).parent))
from datamate_baseline.calcplan import contracts, plan_sql  # noqa: E402
from datamate_baseline.calcplan.prompts import BIND_SYSTEM, SQL_SYSTEM, UNDERSTAND_SYSTEM  # noqa: E402
from datamate_baseline.calcplan.runner import answer_question  # noqa: E402
from datamate_baseline.calcplan.time_resolver import TimeResolver, extract_time  # noqa: E402
from datamate_baseline.calcplan.tools import ToolBox, ToolBudgetExceeded  # noqa: E402
from datamate_baseline.query_tool import checked_sql  # noqa: E402
from datamate_data.semplan.contracts import EvidenceCatalog  # noqa: E402
from datamate_data.semplan.quality import make_issue  # noqa: E402

RULEBOOK = (ROOT/'docs/train/rulebook.md').read_text(encoding='utf-8')
README = '# Demo\nFile bán hàng: mỗi dòng một mặt hàng của đơn; tổng tiền đơn lặp lại ở mọi dòng.\n- Không có dữ liệu giá vốn.\n'
CONFIG = json.loads((ROOT/'configs/baseline_calcplan.json').read_text())
CALENDAR = ('date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n2026-02-10,2,23,12,2025,false,\n'
            '2026-02-26,4,10,1,2026,false,\n2025-01-22,3,23,12,2024,false,\n2025-02-07,5,10,1,2025,false,\n')
SALES = {'ma_don': 'TEXT', 'ngay': 'DATE', 'ten_hang': 'TEXT', 'so_luong': 'NUMERIC', 'don_gia': 'NUMERIC',
         'thanh_tien': 'NUMERIC', 'tong_don': 'NUMERIC', 'trang_thai': 'TEXT', 'trang_thai__raw': 'TEXT',
         '__quantity_base': 'NUMERIC', '__unit_base': 'TEXT'}
QUESTION = 'Tháng 3/2026 có bao nhiêu đơn có doanh thu từ 55.000 đồng trở lên?'
ANALYSIS = {'rewrite': {'text': None, 'changed': False}, 'target': {'entity': 'đơn hàng', 'attribute': 'số lượng'},
            'metric': {'phrase': 'doanh thu', 'concept': 'revenue', 'meaning': 'doanh thu đơn theo rulebook'},
            'operation': 'count',
            'filters': [{'phrase': 'từ 55.000 đồng trở lên', 'field': 'doanh thu của đơn', 'operator': '>=', 'value': 55000, 'level': 'group'}],
            'time': [{'phrase': 'Tháng 3/2026', 'kind': 'month', 'month': 3, 'year': 2026, 'role': 'filter'}],
            'grouping': [], 'answer': {'type': 'number', 'target': 'số đơn', 'granularity': 'một số'}, 'unresolved': []}


def make_plan(view='sales_clean', date_col='ngay', order='ma_don', amount='thanh_tien', status='trang_thai',
              statuses=('cancelled', 'returned'), having=True, extra_filters=()):
    s1 = {'id': 's1', 'from': view, 'joins': [], 'time': [{'interval': 'T1', 'column': date_col}],
          'filters': [{'column': status, 'op': 'not_in', 'value': list(statuses), 'evidence': ['B3']}, *extra_filters],
          'group_by': [order], 'aggregates': [{'name': 'order_total', 'fn': 'sum', 'column': amount}],
          'having': [{'aggregate': 'order_total', 'op': '>=', 'value': 55000}] if having else []}
    if not having:
        s1['filters'].append({'column': amount, 'op': '>=', 'value': 55000, 'evidence': []})
    return {'answer': {'type': 'number', 'shape': 'scalar', 'columns': None},
            'stages': [s1, {'id': 's2', 'from': 's1', 'joins': [], 'time': [], 'filters': [], 'group_by': [],
                            'aggregates': [{'name': 'value', 'fn': 'count', 'column': '*'}], 'having': []}],
            'output': {'select': [{'name': 'value', 'expr': 's2.value'}], 'order_by': [], 'limit': None, 'round': None},
            'evidence': ['B3'], 'feasibility': {'status': 'feasible', 'missing': []}}


def good_sql(view='sales_clean', date_col='ngay', order='ma_don', amount='thanh_tien', status='trang_thai', end='2026-04-01'):
    return (f"WITH s1 AS (SELECT {order}, SUM({amount}) AS order_total FROM {view} WHERE {date_col} >= '2026-03-01' "
            f"AND {date_col} < '{end}' AND {status} NOT IN ('cancelled', 'returned') GROUP BY {order} "
            f"HAVING SUM({amount}) >= 55000) SELECT COUNT(*) AS value FROM s1")


def make_context(quality=None, bounds=('2026-01-01', '2026-04-12'), products_unique=False):
    measures = {('sales_clean', 'thanh_tien'): {'column': 'thanh_tien', 'measure_scope': 'row', 'aggregation': 'sum', 'group_columns': []},
                ('sales_clean', 'don_gia'): {'column': 'don_gia', 'measure_scope': 'row', 'aggregation': 'none', 'group_columns': []},
                ('sales_clean', 'tong_don'): {'column': 'tong_don', 'measure_scope': 'order', 'aggregation': 'sum_once_per_group',
                                              'group_columns': ['ma_don']}}
    roles = {'ma_don': 'order_id', 'ngay': 'date', 'ten_hang': 'product_name', 'so_luong': 'quantity', 'don_gia': 'unit_price',
             'thanh_tien': 'line_amount', 'tong_don': 'money', 'trang_thai': 'status'}
    columns = [{'name': n, 'source': n, 'type': t, 'canonical_role': roles.get(n), 'semantic_role': None,
                'meaning': None, 'has_data': True, 'note': None, 'profile': None} for n, t in SALES.items()]
    mapping = {'domain': 'order', 'status': 'enabled', 'unresolved': [],
               'mappings': {'Hoàn thành': {'target': 'completed'}, 'Đã huỷ': {'target': 'cancelled'}, 'Trả hàng': {'target': 'returned'}}}
    tables = {'sales_clean': {'view': 'sales_clean', 'grain': {'kind': 'order_line', 'description': 'Một mặt hàng của đơn'},
                              'row_identity': {'status': 'not_proposed'},
                              'group_keys': [{'name': 'order', 'columns': ['ma_don'], 'status': 'candidate_checked'}],
                              'measures': [m for (v, _), m in measures.items()], 'joins': [], 'status_mappings': {'trang_thai': mapping},
                              'columns': columns, 'clean_rows': 10, 'date_bounds': {'ngay': list(bounds)}},
              'products_clean': {'view': 'products_clean', 'grain': {'kind': 'product', 'description': 'Một sản phẩm'},
                                 'row_identity': {'status': 'not_proposed'}, 'group_keys': [], 'measures': [], 'joins': [],
                                 'status_mappings': {}, 'clean_rows': 3, 'date_bounds': {},
                                 'columns': [{'name': 'ten_hang', 'source': 'Tên hàng', 'type': 'TEXT', 'canonical_role': 'product_name',
                                              'semantic_role': None, 'meaning': None, 'has_data': True, 'note': None, 'profile': None},
                                             {'name': 'nhom', 'source': 'Nhóm', 'type': 'TEXT', 'canonical_role': None,
                                              'semantic_role': 'product_group', 'meaning': 'nhóm hàng', 'has_data': True, 'note': None, 'profile': None}]}}
    allowed = {'sales_clean': dict(SALES), 'products_clean': {'ten_hang': 'TEXT', 'nhom': 'TEXT'}}
    ddl = '\n'.join('CREATE TABLE "'+v+'" ('+', '.join(f'"{c}" {t}' for c, t in cols.items())+');' for v, cols in allowed.items())
    relation = {'left': 'sales_clean', 'right': 'products_clean', 'left_columns': ['ten_hang'], 'right_columns': ['ten_hang'],
                'target_unique': products_unique, 'status': 'candidate_checked'}

    def find(left, right, lcols, rcols):
        if (left, right, lcols, rcols) == ('sales_clean', 'products_clean', ['ten_hang'], ['ten_hang']):
            return relation
        return None
    evidence = EvidenceCatalog.from_sources(README, RULEBOOK)
    return {'business_id': 'biz_demo', 'pg_schema': 'demo', 'manifest_sha256': 'm', 'semantic_revision': 'plan-x',
            'fingerprint': 'f', 'readme': README, 'rulebook': RULEBOOK, 'evidence': evidence,
            'rules_detected': {'outside_coverage': 'B59'}, 'calendar_path': None, 'declared_coverage_end': None,
            'tables': tables, 'allowed': allowed, 'ddl': ddl, 'quality': quality or {'issues': []},
            'relationships': [relation], 'relation': find, 'measure': lambda view, col: measures.get((view, col))}


class FakeTimeout(Exception):
    sqlstate = '57014'


class FakeExecutor:
    """Same AST gates as the real executor; canned results; no database."""

    def __init__(self, results=None, values=()):
        self.results = list(results or []);self.executed = [];self.explained = [];self.values = list(values)

    def execute_sql(self, context, query, max_rows=None):
        executed = checked_sql(query, context['ddl'], context['allowed']);self.executed.append(query)
        item = self.results.pop(0) if self.results else {'columns': ['value'], 'rows': [(Decimal(2),)]}
        if isinstance(item, Exception):
            raise item
        return {'columns': item['columns'], 'rows': item['rows'], 'truncated': False, 'sql_executed': executed, 'elapsed_ms': 0.1}

    def explain(self, context, query):
        checked_sql(query, context['ddl'], context['allowed']);self.explained.append(query)
        return {'node_type': 'Aggregate', 'total_cost': 1.0, 'plan_rows': 1, 'analyze': False}

    def fixed(self, context, query, params=(), max_rows=None):
        text = query.as_string(None)
        if 'count(DISTINCT' in text:
            return {'columns': ['count'], 'rows': [(len(self.values),)], 'truncated': False}
        if text.startswith('SELECT DISTINCT'):
            return {'columns': ['v'], 'rows': [(v,) for v in self.values], 'truncated': False}
        return {'columns': ['v', 'n'], 'rows': [(v, 1) for v in self.values], 'truncated': False}


class FakeModel:
    """Scripted by stage: understand / bind / sql, with *_repair when the payload has repair."""

    def __init__(self, script):
        self.script = script;self.calls = []

    def metadata(self):
        return {'model': 'fake'}

    def load(self):
        return self

    def generate(self, messages):
        system = messages[0]['content'];payload = json.loads(messages[1]['content'])
        stage = {UNDERSTAND_SYSTEM: 'understand', BIND_SYSTEM: 'bind', SQL_SYSTEM: 'sql'}[system]
        if 'repair' in payload:
            stage += '_repair'
        self.calls.append((stage, payload))
        out = self.script(stage, payload)
        return {'raw_output': out if isinstance(out, str) else json.dumps(out, ensure_ascii=False),
                'prompt_tokens': 1, 'output_tokens': 1, 'generation_ms': 0.0, 'hit_token_limit': False}


def script(analysis=ANALYSIS, plans=None, sqls=None):
    plans = list(plans or [{'action': 'plan', 'plan': make_plan()}]);sqls = list(sqls or [good_sql()])

    def respond(stage, payload):
        if stage == 'understand':
            return analysis
        if stage.startswith('bind'):
            return deepcopy(plans.pop(0) if len(plans) > 1 else plans[0])
        return {'sql': sqls.pop(0) if len(sqls) > 1 else sqls[0]}
    return respond


def run(question=QUESTION, as_of='2026-04-15', *, model, context=None, executor=None, config=None):
    context = context or make_context();executor = executor or FakeExecutor();config = {**CONFIG, **(config or {})}
    trace = {};resolver = TimeResolver(None)
    toolbox = ToolBox(context, executor, caps=config, resolver=resolver, trace=trace)
    answer = answer_question({'id': 'q1', 'business_id': 'biz_demo', 'question': question, 'as_of_date': as_of},
                             context=context, model=model, toolbox=toolbox, resolver=resolver, config=config, trace=trace)
    return answer, trace, executor


class TimeResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();path = Path(self.temp.name)/'calendar.csv';path.write_text(CALENDAR)
        self.r = TimeResolver(path)

    def tearDown(self):
        self.temp.cleanup()

    def window(self, question, as_of='2026-05-25', index=0):
        result = self.r.resolve(extract_time(question)[index], as_of)
        return result['status'], result['start'], result['end_exclusive']

    def test_rulebook_periods_from_as_of_date_not_host_clock(self):
        cases = {'tuần này': ('2026-05-25', '2026-05-26'), 'tuần trước': ('2026-05-18', '2026-05-25'),
                 'tháng này': ('2026-05-01', '2026-05-26'), 'tháng trước': ('2026-04-01', '2026-05-01'),
                 '7 ngày gần nhất': ('2026-05-19', '2026-05-26'), 'tháng 9': ('2025-09-01', '2025-10-01'),
                 'tháng 3': ('2026-03-01', '2026-04-01'), 'quý 1': ('2026-01-01', '2026-04-01'),
                 'từ đầu năm tới nay': ('2026-01-01', '2026-05-26'), 'hôm qua': ('2026-05-24', '2026-05-25'),
                 'từ 01/03/2026 đến 05/03/2026': ('2026-03-01', '2026-03-06'), 'đến hết ngày 15/05/2026': (None, '2026-05-16'),
                 'thang 2/2025': ('2025-02-01', '2025-03-01')}
        for phrase, (start, end) in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(self.window(phrase), ('resolved', start, end))
        self.assertEqual(self.window('tháng này', as_of='2024-02-29'), ('resolved', '2024-02-01', '2024-03-01'))

    def test_windows_are_clamped_to_as_of_and_future_windows_are_not_guessed(self):
        result = self.r.resolve(extract_time('tháng 5/2026')[0], '2026-05-25')
        self.assertEqual((result['end_exclusive'], result['clamped']), ('2026-05-26', True))
        self.assertEqual(self.window('tháng 7/2026')[0], 'ambiguous')

    def test_month_to_date_comparison_uses_same_number_of_days(self):
        spec = {'kind': 'month', 'relative': 'this'}
        base = self.r.resolve(spec, '2026-03-31')
        previous = self.r.previous(base, spec, '2026-03-31')
        self.assertEqual((previous['start'], previous['end_exclusive']), ('2026-02-01', '2026-03-01'))
        base = self.r.resolve(spec, '2026-05-25')
        self.assertEqual(self.r.previous(base, spec, '2026-05-25')['end_exclusive'], '2026-04-26')

    def test_tet_uses_lunar_calendar_and_before_tet_has_same_length(self):
        specs = contracts.reconcile_time(extract_time('Tết 2026 bán bao nhiêu so với trước Tết'), [])[0]
        tet = self.r.resolve(specs[0], '2026-05-25');before = self.r.resolve(specs[1], '2026-05-25')
        self.assertEqual((tet['start'], tet['end_exclusive']), ('2026-02-10', '2026-02-27'))
        self.assertEqual((before['start'], before['end_exclusive']), ('2026-01-24', '2026-02-10'))
        self.assertEqual(self.r.resolve({'kind': 'tet', 'lunar_year': 2030}, '2030-05-01')['status'], 'unsupported')

    def test_extraction_handles_missing_diacritics_and_shift(self):
        spec = extract_time('thang 7/2026 co bao nhiu don co dthu tu 55.000 dong tro len')[0]
        self.assertEqual((spec['kind'], spec['month'], spec['year']), ('month', 7, 2026))
        self.assertEqual([s['kind'] for s in extract_time('Ca sáng hôm qua bán gì')], ['shift', 'day'])


class AnalysisContractTests(unittest.TestCase):
    def test_strict_and_inclusive_thresholds_follow_the_question_wording(self):
        value = deepcopy(ANALYSIS);value['filters'][0].update(phrase='trên 55.000 đồng', operator='>=')
        analysis, corrections = contracts.validate_analysis(value, 'Có bao nhiêu đơn trên 55.000 đồng trong tháng 3/2026?')
        self.assertEqual(analysis['filters'][0]['operator'], '>')
        self.assertEqual(corrections[0]['operator_to'], '>')
        analysis, _ = contracts.validate_analysis(deepcopy(ANALYSIS), QUESTION)
        self.assertEqual(analysis['filters'][0]['operator'], '>=')

    def test_invented_threshold_or_answer_type_is_a_contract_error(self):
        value = deepcopy(ANALYSIS);value['filters'][0]['value'] = 50000
        with self.assertRaisesRegex(ValueError, 'not a number stated'):
            contracts.validate_analysis(value, QUESTION)
        value = deepcopy(ANALYSIS);value['answer']['type'] = 'customer_name'
        with self.assertRaises(ValueError):
            contracts.validate_analysis(value, QUESTION)

    def test_rewrite_may_not_change_numbers_years_or_threshold_words(self):
        value = deepcopy(ANALYSIS)
        value['rewrite'] = {'text': 'Tháng 3/2026 có bao nhiêu đơn có doanh thu trên 55.000 đồng?', 'changed': True}
        analysis, _ = contracts.validate_analysis(value, QUESTION)
        self.assertTrue(analysis['rewrite_status'].startswith('rejected:'))
        self.assertEqual(analysis['original_question'], QUESTION)
        value['rewrite'] = {'text': 'Tháng 3/2026 có bao nhiêu đơn có doanh thu từ 55.000 đồng trở lên?', 'changed': True}
        self.assertEqual(contracts.validate_analysis(value, 'thang 3/2026 co bao nhieu don co dthu tu 55.000 dong tro len')[0]['rewrite_status'], 'accepted')

    def test_year_not_in_question_is_removed_and_extractor_wins(self):
        value = deepcopy(ANALYSIS);value['time'] = [{'phrase': 'tháng 3', 'kind': 'month', 'month': 3, 'year': 2025, 'role': 'filter'}]
        question = 'Tháng 3 có bao nhiêu đơn có doanh thu từ 55.000 đồng trở lên?'
        analysis, corrections = contracts.validate_analysis(value, question)
        self.assertIsNone(analysis['time'][0]['year']);self.assertTrue(any('removed' in c for c in corrections))
        specs, conflicts = contracts.reconcile_time(extract_time(question), [{'phrase': 'tháng 3', 'kind': 'quarter', 'quarter': 1}])
        self.assertEqual([s['kind'] for s in specs], ['month']);self.assertEqual(conflicts[0]['model_kind'], 'quarter')


class PlanValidationTests(unittest.TestCase):
    def setUp(self):
        self.ctx = make_context();self.analysis = contracts.validate_analysis(deepcopy(ANALYSIS), QUESTION)[0]
        self.intervals = {'T1': TimeResolver(None).resolve({'kind': 'month', 'month': 3, 'year': 2026}, '2026-04-15')}

    def validate(self, plan, analysis=None):
        return contracts.validate_calc_plan(deepcopy(plan), self.ctx, analysis or self.analysis, self.intervals)

    def codes(self, plan, analysis=None):
        return {i['code'] for i in self.validate(plan, analysis)[1]}

    def test_valid_plan_yields_dependencies_with_operations(self):
        plan, issues, deps = self.validate(make_plan())
        self.assertEqual(issues, [])
        self.assertEqual(plan['stages'][0]['time'][0]['start'], '2026-03-01')
        self.assertEqual(deps[0]['columns']['ngay'], ['time_filter']);self.assertIn('sum', deps[0]['columns']['thanh_tien'])

    def test_group_threshold_must_be_having_and_must_be_preserved(self):
        self.assertIn('PLAN_THRESHOLD_LEVEL_MISMATCH', self.codes(make_plan(having=False)))
        plan = make_plan();plan['stages'][0]['having'][0]['value'] = 50000
        self.assertIn('PLAN_THRESHOLD_NOT_PRESERVED', self.codes(plan))

    def test_prices_and_repeated_group_values_are_not_summed(self):
        plan = make_plan();plan['stages'][0]['aggregates'][0]['column'] = 'don_gia'
        self.assertIn('PLAN_NON_ADDITIVE_SUM', self.codes(plan))
        plan = make_plan();plan['stages'][0]['group_by'] = ['ten_hang'];plan['stages'][0]['aggregates'][0]['column'] = 'tong_don'
        self.assertIn('PLAN_REPEATED_GROUP_VALUE_SUM', self.codes(plan))
        plan = make_plan();plan['stages'][0]['aggregates'][0]['column'] = 'tong_don'
        self.assertNotIn('PLAN_REPEATED_GROUP_VALUE_SUM', self.codes(plan))

    def test_join_fanout_self_join_and_unknown_interval(self):
        plan = make_plan();plan['stages'][0]['joins'] = [{'table': 'products_clean', 'on': [['ten_hang', 'ten_hang']], 'type': 'inner'}]
        self.assertIn('PLAN_FANOUT_RISK', self.codes(plan))
        self.ctx = make_context(products_unique=True)
        self.assertNotIn('PLAN_FANOUT_RISK', self.codes(plan))
        plan['stages'][0]['joins'] = [{'table': 'sales_clean', 'on': [['ma_don', 'ma_don']], 'type': 'inner'}]
        self.assertIn('SELF_JOIN_UNSUPPORTED', self.codes(plan))
        plan = make_plan();plan['stages'][0]['time'][0]['interval'] = 'T9'
        self.assertIn('PLAN_UNKNOWN_INTERVAL', self.codes(plan))

    def test_projection_and_contract_errors(self):
        plan = make_plan();plan['output']['select'].append({'name': 'x', 'expr': 's2.value'})
        self.assertIn('PLAN_PROJECTION_COUNT', self.codes(plan))
        plan = make_plan();plan['output']['select'] = [{'name': 'value', 'expr': 's1.order_total'}]
        self.assertIn('PLAN_SCALAR_FROM_GROUPED_STAGE', self.codes(plan))
        with self.assertRaises(ValueError):
            self.validate({**make_plan(), 'sql': 'SELECT 1'})


class PlanSqlTests(unittest.TestCase):
    def setUp(self):
        self.ctx = make_context();analysis = contracts.validate_analysis(deepcopy(ANALYSIS), QUESTION)[0]
        intervals = {'T1': TimeResolver(None).resolve({'kind': 'month', 'month': 3, 'year': 2026}, '2026-04-15')}
        self.plan = contracts.validate_calc_plan(make_plan(), self.ctx, analysis, intervals)[0]

    def status(self, sql):
        qualified = checked_sql(sql, self.ctx['ddl'], self.ctx['allowed'])
        checks = plan_sql.check_sql(qualified, self.plan, answer_shape='scalar', allowed=self.ctx['allowed'])
        return plan_sql.summarize(checks)[0], {c['check'] for c in checks if c['status'] != 'pass'}

    def test_equivalent_forms_pass(self):
        self.assertEqual(self.status(good_sql()), ('pass', set()))
        between = ("WITH s1 AS (SELECT ma_don, SUM(thanh_tien) AS order_total FROM sales_clean WHERE ngay BETWEEN '2026-03-01' "
                   "AND DATE '2026-03-31' AND NOT trang_thai IN ('returned', 'cancelled') GROUP BY ma_don) "
                   "SELECT COUNT(*) AS value FROM s1 WHERE 55000 <= order_total")
        self.assertEqual(self.status(between), ('pass', set()))

    def test_wrong_bounds_row_level_threshold_and_projection_fail(self):
        self.assertEqual(self.status(good_sql(end='2026-03-31')), ('fail', {'time_bounds'}))
        row_level = ("WITH s1 AS (SELECT ma_don, SUM(thanh_tien) AS order_total FROM sales_clean WHERE ngay >= '2026-03-01' "
                     "AND ngay < '2026-04-01' AND trang_thai NOT IN ('cancelled','returned') AND thanh_tien >= 55000 GROUP BY ma_don) "
                     "SELECT COUNT(*) AS value FROM s1")
        self.assertEqual(self.status(row_level), ('fail', {'having'}))
        two = good_sql().replace('SELECT COUNT(*) AS value FROM s1', 'SELECT COUNT(*) AS value, 1 AS extra FROM s1')
        self.assertEqual(self.status(two), ('fail', {'projection'}))
        joined = good_sql().replace('FROM sales_clean', 'FROM sales_clean JOIN products_clean USING (ten_hang)')
        self.assertEqual(self.status(joined)[0], 'fail')

    def test_unsupported_critical_checks_are_not_silently_passed(self):
        checks = [{'check': 'filters', 'status': 'unsupported', 'detail': {}}]
        self.assertEqual(plan_sql.summarize(checks)[0], 'unverified')
        self.assertEqual(plan_sql.summarize(checks, unverified_policy='warn')[0], 'pass')


class RunnerTests(unittest.TestCase):
    def test_happy_path_budgets_and_v2_compatible_trace(self):
        model = FakeModel(script());answer, trace, executor = run(model=model)
        self.assertEqual((answer['type'], answer['answer']), ('number', 2))
        self.assertEqual([c[0] for c in model.calls], ['understand', 'bind', 'sql'])
        self.assertEqual(trace['budgets']['model_calls_used'], 3);self.assertEqual(trace['budgets']['repairs'], [])
        self.assertEqual(len(executor.explained), 1);self.assertEqual(len(executor.executed), 1)
        for key in ('raw_output', 'json_valid', 'contract_valid', 'sql_valid', 'sql_executed', 'question_analysis',
                    'schema_linking', 'plan_sql_checks', 'quality_gate', 'tool_calls', 'time_resolution'):
            self.assertIn(key, trace)
        self.assertEqual(trace['time_resolution']['intervals'][0]['start'], '2026-03-01')
        sql_payload = model.calls[-1][1]
        self.assertEqual(sql_payload['plan']['stages'][0]['time'][0]['end_exclusive'], '2026-04-01')

    def test_sql_is_repaired_without_changing_the_plan(self):
        model = FakeModel(script(sqls=[good_sql(end='2026-03-31'), good_sql()]))
        answer, trace, _ = run(model=model)
        self.assertEqual(answer['answer'], 2)
        self.assertEqual([r['layer'] for r in trace['budgets']['repairs']], ['sql'])
        self.assertEqual(model.calls[-1][0], 'sql_repair')
        self.assertEqual(model.calls[-1][1]['plan'], model.calls[-2][1]['plan'])

    def test_plan_is_repaired_when_threshold_level_is_wrong(self):
        model = FakeModel(script(plans=[{'action': 'plan', 'plan': make_plan(having=False)}, {'action': 'plan', 'plan': make_plan()}]))
        answer, trace, _ = run(model=model)
        self.assertEqual(answer['answer'], 2)
        self.assertEqual([r['layer'] for r in trace['budgets']['repairs']], ['plan'])
        self.assertEqual(trace['budgets']['repairs'][0]['issues'][0]['code'], 'PLAN_THRESHOLD_LEVEL_MISMATCH')

    def test_repair_budget_is_hard(self):
        model = FakeModel(script(plans=[{'action': 'plan', 'plan': {'bad': 1}}]))
        answer, trace, _ = run(model=model)
        self.assertEqual((answer['type'], answer['reason']), ('abstain', 'PLAN_INVALID'))
        self.assertEqual(trace['budgets']['repair_rounds_used'], CONFIG['max_repair_rounds'])
        self.assertLessEqual(trace['budgets']['model_calls_used'], CONFIG['max_model_calls'])
        endless = FakeModel(script(plans=[{'action': 'tools', 'requests': [
            {'tool': 'describe_tables', 'args': {}}, {'tool': 'get_source_context', 'args': {'search': 'đơn'}},
            {'tool': 'execute_sql', 'args': {'sql_text': 'SELECT 1'}}]}]))
        answer, trace, executor = run(model=endless)
        self.assertEqual(answer['reason'], 'BUDGET_EXHAUSTED');self.assertEqual(executor.executed, [])
        self.assertLessEqual(trace['budgets']['tool_calls_used'], CONFIG['max_tool_calls'])
        rejected = [t for t in trace['tool_calls'] if t['tool'] == 'execute_sql']
        self.assertTrue(rejected and all(t['result']['status'] == 'error' for t in rejected))

    def test_status_literals_are_grounded_and_unknown_labels_abstain(self):
        model = FakeModel(script(plans=[{'action': 'plan', 'plan': make_plan(statuses=('Đã huỷ', 'Trả hàng'))}]))
        answer, trace, _ = run(model=model)
        self.assertEqual(answer['answer'], 2)
        self.assertEqual(trace['schema_linking']['stages'][0]['filters'][0]['value'], ['cancelled', 'returned'])
        model = FakeModel(script(plans=[{'action': 'plan', 'plan': make_plan(statuses=('Hoàn tất',))}]))
        answer, _, _ = run(model=model)
        self.assertEqual(answer['reason'], 'AMBIGUOUS_LITERAL')

    def test_text_literals_need_exact_or_normalized_match(self):
        extra = ({'column': 'ten_hang', 'op': '=', 'value': 'nuoc', 'evidence': []},)
        model = FakeModel(script(plans=[{'action': 'plan', 'plan': make_plan(extra_filters=extra)}],
                                 sqls=[good_sql().replace("GROUP BY", "AND ten_hang = 'Nước' GROUP BY")]))
        answer, trace, _ = run(model=model, executor=FakeExecutor(values=['Nước', 'Bánh']))
        self.assertEqual(answer['answer'], 2)
        self.assertEqual(trace['schema_linking']['stages'][0]['filters'][1]['value'], 'Nước')
        answer, _, _ = run(model=FakeModel(script(plans=[{'action': 'plan', 'plan': make_plan(extra_filters=extra)}])),
                           executor=FakeExecutor(values=['Nước ngọt', 'Nước suối']))
        self.assertEqual(answer['reason'], 'AMBIGUOUS_LITERAL')

    def test_outside_coverage_is_unanswerable_but_quality_uncertainty_abstains(self):
        question = 'Tháng 5/2026 có bao nhiêu đơn có doanh thu từ 55.000 đồng trở lên?'
        analysis = deepcopy(ANALYSIS);analysis['time'][0].update(phrase='Tháng 5/2026', month=5)
        answer, trace, _ = run(question, '2026-06-10', model=FakeModel(script(analysis=analysis)))
        self.assertEqual((answer['type'], answer['reason']), ('unanswerable', 'OUTSIDE_DATA_COVERAGE'))
        issue = make_issue('unknown_rows', business_id='b', table='t', view='sales_clean', row_count=2, unknown_bounds={'time': True})
        answer, trace, _ = run(model=FakeModel(script()), context=make_context(quality={'issues': [issue]}))
        self.assertEqual((answer['type'], answer['reason']), ('abstain', 'QUALITY_BLOCKED'))

    def test_missing_data_needs_inventory_and_source_evidence(self):
        claim = {'action': 'missing_data', 'missing': [{'requirement': 'giá vốn', 'concepts': ['giá vốn', 'cogs'], 'evidence': ['R3']}],
                 'reason': 'không ghi giá vốn'}
        answer, _, _ = run(model=FakeModel(script(plans=[claim])))
        self.assertEqual((answer['type'], answer['reason']), ('unanswerable', 'MISSING_DATA'))
        wrong = {'action': 'missing_data', 'missing': [{'requirement': 'thành tiền', 'concepts': ['thành tiền'], 'evidence': ['R2']}],
                 'reason': 'x'}
        answer, trace, _ = run(model=FakeModel(script(plans=[wrong])))
        self.assertEqual((answer['type'], answer['reason']), ('abstain', 'AMBIGUOUS_QUESTION'))
        self.assertTrue(trace['missing_data_check']['columns_with_data'])

    def test_empty_sum_is_zero_but_null_average_is_not(self):
        plan = make_plan();plan['stages'] = [{'id': 's1', 'from': 'sales_clean', 'joins': [], 'time': [{'interval': 'T1', 'column': 'ngay'}],
                                              'filters': [], 'group_by': [], 'aggregates': [{'name': 'value', 'fn': 'sum', 'column': 'thanh_tien'}],
                                              'having': []}]
        plan['output']['select'] = [{'name': 'value', 'expr': 's1.value'}]
        analysis = deepcopy(ANALYSIS);analysis.update(filters=[], operation='sum')
        question = 'Tháng 3/2026 bán được bao nhiêu tiền?'
        sql = "SELECT SUM(thanh_tien) AS value FROM sales_clean WHERE ngay >= '2026-03-01' AND ngay < '2026-04-01'"
        answer, trace, _ = run(question, model=FakeModel(script(analysis=analysis, plans=[{'action': 'plan', 'plan': plan}], sqls=[sql])),
                               executor=FakeExecutor([{'columns': ['value'], 'rows': [(None,)]}]))
        self.assertEqual((answer['type'], answer['answer']), ('number', 0));self.assertIn('empty_population', trace)
        plan['stages'][0]['aggregates'][0]['fn'] = 'avg'
        avg_sql = sql.replace('SUM(', 'AVG(')
        answer, trace, _ = run(question, model=FakeModel(script(analysis=analysis, plans=[{'action': 'plan', 'plan': plan}], sqls=[avg_sql])),
                               executor=FakeExecutor([{'columns': ['value'], 'rows': [(None,)]}]*3))
        self.assertEqual(answer['reason'], 'RESULT_SHAPE')

    def test_timeout_shift_and_rounding(self):
        answer, trace, _ = run(model=FakeModel(script()), executor=FakeExecutor([FakeTimeout('canceling statement due to statement timeout')]))
        self.assertEqual(answer['reason'], 'QUERY_TIMEOUT');self.assertEqual(trace['budgets']['repairs'], [])
        shift = deepcopy(ANALYSIS);shift.update(filters=[], operation='sum')
        model = FakeModel(script(analysis=shift))
        answer, _, _ = run('Ca sáng tháng 3/2026 bán được bao nhiêu tiền?', model=model)
        self.assertEqual(answer['reason'], 'CAPABILITY_UNSUPPORTED');self.assertEqual([c[0] for c in model.calls], ['understand'])
        plan = make_plan();plan['answer']['type'] = 'percent'
        analysis = deepcopy(ANALYSIS);analysis['answer']['type'] = 'percent'
        answer, _, _ = run(model=FakeModel(script(analysis=analysis, plans=[{'action': 'plan', 'plan': plan}])),
                           executor=FakeExecutor([{'columns': ['value'], 'rows': [(Decimal('12.345'),)]}]))
        self.assertEqual(answer['answer'], Decimal('12.35'))
        analysis = deepcopy(ANALYSIS);analysis['metric'] = {'phrase': 'giá trị trung bình một đơn', 'concept': 'average_order_value', 'meaning': ''}
        answer, _, _ = run(model=FakeModel(script(analysis=analysis)),
                           executor=FakeExecutor([{'columns': ['value'], 'rows': [(Decimal('51666.5'),)]}]))
        self.assertEqual(answer['answer'], 51667)

    def test_tool_budget_counts_every_request(self):
        trace = {};box = ToolBox(make_context(), FakeExecutor(), caps={**CONFIG, 'max_tool_calls': 2}, resolver=TimeResolver(None), trace=trace)
        box.call('describe_tables', {}, by='model');box.call('execute_sql', {'sql_text': 'SELECT 1'}, by='model')
        with self.assertRaises(ToolBudgetExceeded):
            box.call('describe_tables', {}, by='model')
        self.assertEqual(box.used['rejected'], 1)


@unittest.skipUnless(os.environ.get('DATAMATE_TEST_PG_DSN'), 'set isolated test PostgreSQL DSN')
class PublishedDatabaseTests(unittest.TestCase):
    CLEAN = ('Mã hóa đơn,Ngày bán,Khách hàng,Tên hàng,Số lượng,Đơn vị tính,Thành tiền,Trạng thái\n'
             'A1,03/03/2026,Lan,Nước,2,thùng,100000,Hoàn thành\nA1,03/03/2026,Lan,Bánh,1,chai,20000,Hoàn thành\n'
             'A2,04/03/2026,,Kẹo,1,gói,5000,Đã huỷ\nA3,05/03/2026,Minh,Nước,1,thùng,60000,Hoàn thành\n'
             'A4,10/04/2026,Lan,Nước,1,thùng,50000,Hoàn thành\n')

    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        from test_semplan_pipeline import Fixture
        cls.admin = os.environ['DATAMATE_TEST_PG_DSN']
        cls.fx = Fixture({'sales.csv': cls.CLEAN}, readme='# Demo\nFile bán hàng: mỗi dòng một mặt hàng của đơn.\n- Nước: 1 thùng = 24 chai\n')
        cls.document, cls.out = cls.fx.build(dsn=cls.admin)
        cls.schema = cls.document['database']['schema'];cls.role = 'datamate_test_'+uuid.uuid4().hex[:12]
        with psycopg.connect(cls.admin, autocommit=True) as conn:
            conn.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT').format(sql.Identifier(cls.role)))
            conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only='on'").format(sql.Identifier(cls.role)))
            conn.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(cls.schema), sql.Identifier(cls.role)))
            for view in cls.document['database']['clean_views']:
                conn.execute(sql.SQL('GRANT SELECT ON {}.{} TO {}').format(sql.Identifier(cls.schema), sql.Identifier(view), sql.Identifier(cls.role)))
        kwargs = conninfo_to_dict(cls.admin);kwargs['user'] = cls.role;kwargs.pop('password', None)
        cls.reader = make_conninfo(**kwargs)
        from datamate_baseline.calcplan.context import PublishedContextLoader
        from datamate_baseline.calcplan.tools import ReadOnlyExecutor
        cls.loader = PublishedContextLoader(cls.fx.root/'out', cls.fx.source)
        cls.context = cls.loader.load('biz_demo');cls.view = next(iter(cls.context['tables']))
        cls.executor = ReadOnlyExecutor(cls.reader, timeout_ms=5000, max_rows=100)

    @classmethod
    def tearDownClass(cls):
        import psycopg
        from psycopg import sql
        cls.executor.close()
        with psycopg.connect(cls.admin, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(cls.schema)))
            conn.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(cls.role)))
            conn.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(cls.role)))
        cls.fx.close()

    def toolbox(self, **caps):
        trace = {}
        return ToolBox(self.context, self.executor, caps={**CONFIG, **caps}, resolver=TimeResolver(None), trace=trace), trace

    def test_value_tools_never_turn_fuzzy_candidates_into_matches(self):
        box, _ = self.toolbox()
        results = {lit: box.call('resolve_value', {'table': self.view, 'column': 'ten_hang', 'literal': lit}, by='model')
                   for lit in ('Nước', 'NƯỚC', 'nuoc', 'Sữa', 'a')}
        self.assertEqual(results['Nước']['status'], 'exact')
        self.assertEqual((results['NƯỚC']['status'], results['NƯỚC']['value']), ('normalized_unique', 'Nước'))
        self.assertEqual((results['nuoc']['status'], results['nuoc']['value']), ('normalized_unique', 'Nước'))
        self.assertEqual(results['Sữa']['status'], 'not_found_complete')
        self.assertEqual((results['a']['status'], results['a']['value']), ('ambiguous', None))
        values = box.call('get_column_values', {'table': self.view, 'column': 'trang_thai'}, by='model')
        self.assertEqual(values['status_domain'], 'order')
        self.assertEqual(sorted(v[0] for v in values['values']), ['cancelled', 'completed'])
        preview = box.call('preview_rows', {'table': self.view, 'columns': ['ma_hoa_don', 'ten_hang'], 'key_columns': ['ma_hoa_don']}, by='model')
        self.assertTrue(preview['sample_only']);self.assertTrue(1 <= len(preview['rows']) <= 10)
        self.assertTrue(all(r[0] == 'A1' for r in preview['rows']))

    def test_explain_never_executes_and_errors_roll_back(self):
        box, _ = self.toolbox()
        boom = f'SELECT 1/(count(*)-count(*)) AS v FROM {self.view}'
        explained = box.call('explain_sql', {'sql_text': boom})
        self.assertEqual((explained['status'], explained['analyze']), ('ok', False))
        failed = box.call('execute_sql', {'sql_text': boom})
        self.assertEqual(failed['status'], 'error');self.assertIn('division', failed['error'])
        ok = box.call('execute_sql', {'sql_text': f'SELECT count(*) AS n FROM {self.view}'})
        self.assertEqual(ok['rows'], [(5,)])
        rejected = box.call('execute_sql', {'sql_text': f'DELETE FROM {self.view}'})
        self.assertEqual(rejected['status'], 'error')
        import psycopg
        from psycopg import sql
        with psycopg.connect(self.reader, autocommit=True) as conn, self.assertRaises(psycopg.Error):
            conn.execute(sql.SQL('UPDATE {}.{} SET thanh_tien = 0').format(sql.Identifier(self.schema), sql.Identifier(self.view)))

    def scripted(self, extra_filters=(), analysis=ANALYSIS):
        view = self.view

        def respond(stage, payload):
            if stage == 'understand':
                return analysis
            if stage.startswith('bind'):
                return {'action': 'plan', 'plan': make_plan(view, 'ngay_ban', 'ma_hoa_don', extra_filters=list(extra_filters))}
            plan = payload['plan'];extra = ''
            for f in plan['stages'][0]['filters'][1:]:
                extra += f" AND {f['column_name']} = '{f['value']}'"
            return {'sql': good_sql(view, 'ngay_ban', 'ma_hoa_don').replace(' GROUP BY', extra+' GROUP BY')}
        return FakeModel(respond)

    def test_end_to_end_with_real_sql_and_grounded_literal(self):
        trace = {};resolver = TimeResolver(self.context['calendar_path'])
        box = ToolBox(self.context, self.executor, caps=CONFIG, resolver=resolver, trace=trace)
        answer = answer_question({'id': 'q1', 'business_id': 'biz_demo', 'question': QUESTION, 'as_of_date': '2026-04-15'},
                                 context=self.context, model=self.scripted(), toolbox=box, resolver=resolver, config=CONFIG, trace=trace)
        self.assertEqual((answer['type'], answer['answer']), ('number', 2), trace.get('decision'))
        self.assertEqual(trace['quality_gate']['quality']['decision'], 'pass')
        question = 'Tháng 3/2026 có bao nhiêu đơn mua nuoc có doanh thu từ 55.000 đồng trở lên?'
        trace = {};box = ToolBox(self.context, self.executor, caps=CONFIG, resolver=resolver, trace=trace)
        model = self.scripted(extra_filters=[{'column': 'ten_hang', 'op': '=', 'value': 'nuoc', 'evidence': []}])
        answer = answer_question({'id': 'q2', 'business_id': 'biz_demo', 'question': question, 'as_of_date': '2026-04-15'},
                                 context=self.context, model=model, toolbox=box, resolver=resolver, config=CONFIG, trace=trace)
        self.assertEqual(answer['answer'], 2, trace.get('decision'))
        self.assertIn("ten_hang = 'Nước'", trace['plan']['sql'])

    def test_batch_outputs_work_with_existing_report_scripts(self):
        from datamate_baseline.calcplan.runner import run_batch
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'run'
            requests = [{'id': 'q1', 'business_id': 'biz_demo', 'question': QUESTION, 'as_of_date': '2026-04-15'},
                        {'id': 'q2', 'business_id': 'biz_demo', 'question': 'Ca sáng tháng 3/2026 bán bao nhiêu?', 'as_of_date': '2026-04-15'}]
            manifest = run_batch(requests, model=self.scripted(), executor=self.executor, loader=self.loader,
                                 output_dir=output, config=CONFIG)
            self.assertEqual((manifest['status'], manifest['outcomes']), ('complete', {'number': 1, 'abstain': 1}))
            predictions = [json.loads(l) for l in (output/'predictions.jsonl').read_text().splitlines()]
            self.assertEqual(predictions[0], {'id': 'q1', 'type': 'number', 'answer': 2})
            result = subprocess.run([sys.executable, str(ROOT/'scripts/summarize_baseline.py'), '--run-dir', str(output)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((output/'report.html').exists())


if __name__ == '__main__':
    unittest.main()
