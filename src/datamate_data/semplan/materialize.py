"""P7: final normalization under frozen decisions and enabled policies only.

Typed values are recomputed from raw cells with the final column specs. Order:
typed -> scoped status mapping -> fill-forward -> duplicate marks -> derived base
quantity -> partial-date companions. Raw values and provenance are kept per row.
"""
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import re

from ..parsers import dumps
from ..pipeline import digest
from .policies import (ConversionRegistry, apply_fill_forward, build_fill_state, conversions_from_tables,
                       derive_base_quantity, duplicate_signature)
from .status import apply_mapping
from .temporal import companion_values
from .typed import typed_cell

COMPANIONS = (('__y', 'INTEGER'), ('__m', 'INTEGER'), ('__d', 'INTEGER'), ('__p', 'TEXT'), ('__r', 'TEXT'))


def companion_names(column):
    return [column+suffix for suffix, _ in COMPANIONS]


def _enabled(plan, operation):
    return [p for p in plan['policies'] if p['operation'] == operation and p['decision_status'] == 'enabled']


def _year_by_part(plan, parts):
    enabled = {p['part']: p['year_info'] for p in _enabled(plan, 'resolve_source_year')}
    return {part['part']: enabled.get(part['part'], {'status': 'disabled'}) for part in parts}


def _records(root, raw_path):
    with (Path(root)/raw_path).open(encoding='utf-8') as stream:
        for line in stream:
            yield json.loads(line)


def build_conversions(root, plans, staged, readme):
    registry = ConversionRegistry();registry.from_readme(readme)
    readme_has = bool(registry.factors)
    tables = []
    for name, plan in plans.items():
        if not any(c['role'] == 'conversion_factor' for c in plan['columns']):
            continue
        decisions = {d['row_id']: d['kind'] for d in staged['decisions_final'][name]}
        years = _year_by_part(plan, staged['tables'][name]['parts'])
        rows = []
        for record in _records(root, staged['tables'][name]['raw_path']):
            if decisions.get(record['row_id'], 'data') != 'data':
                continue
            rows.append([typed_cell(v, c, record['cells'].get(str(i)), years[record['part']])['value']
                         for i, (v, c) in enumerate(zip(record['raw'], plan['columns']))])
        tables.append((plan['columns'], rows, name))
    conversions_from_tables(registry, tables)
    return registry, readme_has or bool(tables)


def materialize_table(root, name, plan, staged_table, decisions, conversions, *, rules, business_id):
    """Write final/<name>.jsonl. Returns (table catalog entry, quality stats)."""
    root = Path(root);out_path = root/'final'/f'{name}.jsonl'
    columns = plan['columns'];by_row = {d['row_id']: d for d in decisions}
    years = _year_by_part(plan, staged_table['parts'])
    status_maps = {c: m for c, m in plan['status_mappings'].items()}
    fills = _enabled(plan, 'fill_forward');dupes = _enabled(plan, 'mark_duplicates')
    derive = bool(_enabled(plan, 'derive_base_quantity'))
    fill_states = [build_fill_state() for _ in fills];seen = {}
    stats = {'rows': 0, 'kinds': Counter(), 'errors': Counter(), 'error_rows': {}, 'missing': Counter(),
             'notes': Counter(), 'partial': Counter(), 'partial_months': {}, 'partial_rows': {},
             'ambiguous': Counter(), 'unknown_rows': [], 'unknown_dates': {'min': None, 'max': None, 'undated': 0},
             'unmapped_status': {}, 'base_unresolved': 0, 'base_conflict': 0, 'duplicates': 0,
             'clean_numeric': Counter(), 'date_bounds': {}}
    date_cols = [i for i, c in enumerate(columns) if c['sql_type'] in {'DATE', 'TIMESTAMP'}]
    errors_path = root/'final'/f'{name}.normalization_errors.jsonl'
    with out_path.open('w', encoding='utf-8') as dest, errors_path.open('w', encoding='utf-8') as errors:
        for record in _records(root, staged_table['raw_path']):
            decision = by_row.get(record['row_id'])
            kind = decision['kind'] if decision else 'data'
            typed = [];temporal = {};quality = {'errors': {}, 'notes': {}, 'source_flags': record['flags']}
            for i, (raw, column) in enumerate(zip(record['raw'], columns)):
                result = typed_cell(raw, column, record['cells'].get(str(i)), years[record['part']])
                status = result['status']
                if 'temporal' in result:
                    temporal[i] = result['temporal']
                if status.startswith('error:'):
                    code = status[6:];quality['errors'][column['name']] = code
                    if kind == 'data':
                        stats['errors'][column['name']] += 1
                        error_rows = stats['error_rows'].setdefault(column['name'], [])
                        if len(error_rows) < 20:
                            error_rows.append(record['row_id'])
                    errors.write(dumps({'table': name, 'row_id': record['row_id'], **record['source'], 'column': column['name'], 'error': code})+'\n')
                elif status == 'missing':
                    stats['missing'][column['name']] += 1
                elif status == 'partial':
                    parsed = result['temporal']
                    quality['notes'][column['name']] = {'status': 'partial_date', 'precision': parsed['precision'],
                                                        'resolution': parsed['resolution']}
                    if kind == 'data':
                        bucket = 'ambiguous' if parsed['resolution'] == 'ambiguous_day_month' else 'partial'
                        stats[bucket][column['name']] += 1
                        if parsed.get('month'):
                            stats['partial_months'].setdefault(column['name'], set()).add(parsed['month'])
                        else:
                            stats['partial_months'].setdefault(column['name'], set()).add(None)
                        rows = stats['partial_rows'].setdefault(column['name'], [])
                        if len(rows) < 20:
                            rows.append(record['row_id'])
                elif status != 'ok':
                    quality['notes'][column['name']] = {'status': status, **({'unit': result['unit']} if result.get('unit') else {})}
                    stats['notes'][status] += 1
                elif result.get('unit'):
                    quality['notes'][column['name']] = {'status': 'ok_unit_extracted', 'unit': result['unit']}
                value = result['value']
                if column['role'] == 'status':
                    value, note = apply_mapping(value, status_maps.get(column['name']))
                    if note:
                        quality['notes'][column['name']] = {'status': note}
                        if kind == 'data' and note == 'unmapped_status_preserved':
                            stats['unmapped_status'].setdefault(column['name'], Counter())[value] += 1
                typed.append(value)
            for policy, state in zip(fills, fill_states):
                apply_fill_forward(policy, columns, record, typed, quality, state, kind, temporal)
            duplicate_of = None
            if kind == 'data':
                for policy in dupes:
                    signature = duplicate_signature(policy, columns, record, typed)
                    if signature is None:
                        continue
                    if signature in seen:
                        duplicate_of = seen[signature];quality['notes']['__duplicate'] = {'status': 'same_group_identical_row', 'policy_id': policy['policy_id']}
                    else:
                        seen[signature] = record['row_id']
            quantity_base = unit_base = None
            if derive and kind == 'data':
                quantity_base, unit_base = derive_base_quantity(columns, typed, quality, conversions,
                                                                default_rule=bool(rules.get('base_unit_default_factor_1')))
                if quality['errors'].get('__quantity_base') == 'supplied_and_computed_base_quantity_disagree':
                    stats['base_conflict'] += 1
                if quantity_base is None and any(c['role'] in {'quantity', 'base_quantity'} for c in columns):
                    stats['base_unresolved'] += 1
            companions = {columns[i]['name']: companion_values(temporal[i]) for i in date_cols}
            final = {'row_id': record['row_id'], 'seq': record['seq'], 'part': record['part'], 'source': record['source'],
                     'raw': record['raw'], 'typed': typed, 'kind': kind,
                     'decision': {k: decision.get(k) for k in ('decision_status', 'method', 'rule', 'candidate_kind', 'evidence_refs')} if decision else {'decision_status': 'decided_rule', 'rule': 'default_structured_row'},
                     'quality': quality, 'duplicate_of': duplicate_of,
                     'quantity_base': None if quantity_base is None else str(quantity_base), 'unit_base': unit_base,
                     'temporal': companions,
                     'status_raw': {c['name']: (None if record['raw'][i] is None else str(record['raw'][i]).strip() or None)
                                    for i, c in enumerate(columns) if c['role'] == 'status'}}
            dest.write(dumps(final)+'\n')
            stats['rows'] += 1;stats['kinds'][kind] += 1
            if duplicate_of:
                stats['duplicates'] += 1
            if kind == 'unknown':
                if len(stats['unknown_rows']) < 20:
                    stats['unknown_rows'].append(record['row_id'])
                dates = [temporal[i]['value'] for i in date_cols if temporal.get(i, {}).get('value') is not None]
                if dates:
                    low = min(d.isoformat()[:10] for d in dates);high = max(d.isoformat()[:10] for d in dates)
                    b = stats['unknown_dates']
                    b['min'] = low if b['min'] is None or low < b['min'] else b['min']
                    b['max'] = high if b['max'] is None or high > b['max'] else b['max']
                else:
                    stats['unknown_dates']['undated'] += 1
            if kind == 'data' and duplicate_of is None:
                for i in date_cols:
                    v = temporal.get(i, {}).get('value')
                    if v is not None:
                        text = v.isoformat()[:10];b = stats['date_bounds'].setdefault(columns[i]['name'], [text, text])
                        b[0] = min(b[0], text);b[1] = max(b[1], text)
    entry = {'name': name, 'logical_name': plan['logical_name'], 'role': plan['table_role_hint'],
             'columns': columns, 'source_parts': plan['source_parts'], 'rows': stats['rows'],
             'row_kinds': dict(stats['kinds']), 'errors': dict(stats['errors']), 'missing': dict(stats['missing']),
             'normalization_notes': dict(stats['notes']) | ({'unmapped_status_preserved': sum(sum(c.values()) for c in stats['unmapped_status'].values())}
                                                            if stats['unmapped_status'] else {}),
             'duplicates': stats['duplicates'], 'derived_columns': {'__quantity_base': 'NUMERIC', '__unit_base': 'TEXT'},
             'base_quantity_unresolved': stats['base_unresolved'],
             'derived_errors': {'__quantity_base': stats['base_conflict']} if stats['base_conflict'] else {},
             'data_path': f'final/{name}.jsonl', 'data_sha256': digest(out_path),
             'grain_candidate': plan['grain'].get('description') or plan['grain']['kind'],
             'temporal_columns': {columns[i]['name']: companion_names(columns[i]['name']) for i in date_cols},
             'status_columns': [c['name'] for c in columns if c['role'] == 'status'],
             'date_bounds': stats['date_bounds'], 'plan_revision': plan['revision']}
    for column in entry['columns']:
        column['safe_for_aggregation'] = column['sql_type'] != 'NUMERIC' or stats['errors'].get(column['name'], 0) == 0
    return entry, stats


def declared_coverage_end(readme):
    from datetime import datetime
    match = re.search(r'đến hết\s+([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})', readme, re.I)
    return datetime.strptime(match[1], '%d/%m/%Y').date().isoformat() if match else None


def numeric_clean_sums(path, columns):
    """Exact Decimal sums over clean rows (data, not duplicate) for audit."""
    sums = [Decimal(0)]*len(columns);counts = [0]*len(columns);clean = 0;raw = 0;kinds = Counter()
    base = Decimal(0);base_n = 0
    numeric = [i for i, c in enumerate(columns) if c['sql_type'] == 'NUMERIC']
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            r = json.loads(line);raw += 1;kinds[r['kind']] += 1
            if r['kind'] != 'data' or r['duplicate_of'] is not None:
                continue
            clean += 1
            for i in numeric:
                v = r['typed'][i]
                if v is not None:
                    sums[i] += Decimal(str(v));counts[i] += 1
            if r['quantity_base'] is not None:
                base += Decimal(r['quantity_base']);base_n += 1
    return {'raw_rows': raw, 'clean_rows': clean, 'kinds': dict(kinds),
            'sums': {columns[i]['name']: (str(sums[i]) if counts[i] else None) for i in numeric},
            'quantity_base_sum': str(base) if base_n else None, 'quantity_base_non_null': base_n}
