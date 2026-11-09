"""Scoped data checks over provisional staging (design §3.1, §5.3).

Every result records dataset stage, row selection and semantic revision. A table's
revision changes whenever row decisions or column specs change, so stale statistics
are never reused for a new plan revision. Checks are evidence, not business proof.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from ..normalization import key
from ..parsers import dumps
from ..semantic import ColumnProfile
from .contracts import stable_id
from .temporal import day_month_pattern
from .typed import typed_cell

STATUS_VOCAB_CAP = 200


def column_digest(columns):
    return [(c['name'], c['role'], c['sql_type'], c.get('date_order')) for c in columns]


class TableState:
    """Working state for one logical table during planning (P2-P6)."""

    def __init__(self, staged, root, decisions):
        self.staged = staged;self.root = Path(root);self.name = staged['name']
        self.columns = deepcopy(staged['columns']);self.parts = staged['parts']
        self.decisions = {d['row_id']: d for d in decisions}
        self.policies = [];self.cache = {}

    def kind(self, record):
        decision = self.decisions.get(record['row_id'])
        return decision['kind'] if decision else 'data'

    def revision(self):
        digest = hashlib.sha256(dumps([sorted((d['row_id'], d['kind']) for d in self.decisions.values()),
                                        column_digest(self.columns)]).encode()).hexdigest()
        return 'rev-'+digest[:16]

    def records(self):
        with (self.root/self.staged['raw_path']).open(encoding='utf-8') as stream:
            for line in stream:
                yield json.loads(line)

    def index(self, name):
        return next(i for i, c in enumerate(self.columns) if c['name'] == name)


def _norm_key(value):
    return None if value is None else ' '.join(str(value).split())


def _typed(state, record, index, cache):
    if index not in cache:
        spec = state.columns[index]
        cache[index] = typed_cell(record['raw'][index], spec, record['cells'].get(str(index)),
                                  state.parts[record['part']]['year_info'])
    return cache[index]


def _empty(value):
    return value is None or isinstance(value, str) and not value.strip()


def _scope(state, selection, stage='provisional_staging'):
    return {'dataset_stage': stage, 'row_selection': selection, 'semantic_revision': state.revision(),
            'table': state.name}


def compute_checks(state, requests, *, stage='provisional_staging'):
    """One streaming pass computing all requested checks for this table revision.

    Requests: column_profile, key_stats(columns), measure_repetition(column, group),
    continuation(columns, key_columns, mode), identical_rows(key_columns),
    status_vocabulary(column), date_format(column), key_values(columns).
    Results are memoized per (request, revision). ``key_values`` returns in-memory
    key counters for join checks and is not persisted.
    """
    revision = state.revision();results = {};todo = []
    for request in requests:
        ident = dumps(request)
        cached = state.cache.get((ident, revision))
        if cached is not None:
            results[ident] = cached
        else:
            todo.append(request)
    if not todo:
        return [results[dumps(r)] for r in requests]
    index = {c['name']: i for i, c in enumerate(state.columns)}
    profiles = None;profile_unknown = None
    acc = []
    for request in todo:
        kind = request['kind']
        if kind == 'column_profile':
            profiles = [ColumnProfile(c) for c in state.columns]
            profile_unknown = Counter();acc.append((request, None))
        elif kind in {'key_stats', 'key_values'}:
            acc.append((request, {'rows': 0, 'non_null': 0, 'groups': Counter(), 'raw_groups': Counter()}))
        elif kind == 'measure_repetition':
            acc.append((request, {}))
        elif kind == 'continuation':
            acc.append((request, {'continuation_rows': 0, 'fillable_cells': Counter(), 'conflicting_values': Counter(),
                                  'orphan_continuations': 0, 'explicit_key_blank_cells': Counter(), 'groups': 0,
                                  'line_cells_missing': 0, 'state': None, 'samples': []}))
        elif kind == 'identical_rows':
            acc.append((request, {'seen': {}, 'same_part': 0, 'cross_part': 0, 'pairs': Counter(), 'samples': [],
                                  'state': None}))
        elif kind == 'status_vocabulary':
            acc.append((request, {'values': Counter(), 'truncated': False, 'non_empty': 0}))
        elif kind == 'date_format':
            acc.append((request, {'patterns': Counter(), 'statuses': Counter(), 'precisions': Counter(),
                                  'resolutions': Counter(), 'min': None, 'max': None, 'partial_months': Counter(),
                                  'row_refs': []}))
        else:
            raise ValueError('Unknown check kind: '+kind)
    last_part = None
    for record in state.records():
        kind = state.kind(record);cache = {}
        reset = record['part'] != last_part or record.get('after_blank') or kind != 'data'
        last_part = record['part']
        if kind != 'data':
            if profiles is not None:
                profile_unknown[kind] += 1
            for request, data in acc:
                if request['kind'] in {'continuation', 'identical_rows'}:
                    data['state'] = None
            continue
        if profiles is not None:
            for i, profile in enumerate(profiles):
                profile.add(record['raw'][i], year=state.parts[record['part']]['year_info'].get('year'),
                            cell=record['cells'].get(str(i)))
        for request, data in acc:
            kind_r = request['kind']
            if kind_r in {'key_stats', 'key_values'}:
                cols = [index[c] for c in request['columns']]
                values = [_norm_key(_typed(state, record, i, cache)['value']) for i in cols]
                data['rows'] += 1
                if all(v is not None for v in values):
                    data['non_null'] += 1;data['groups'][tuple(values)] += 1
                    if kind_r == 'key_values':
                        data['raw_groups'][tuple(key(v) for v in values)] += 1
            elif kind_r == 'measure_repetition':
                group = tuple(_norm_key(_typed(state, record, index[c], cache)['value']) for c in request['group'])
                if any(v is None for v in group):
                    continue
                value = _typed(state, record, index[request['column']], cache)['value']
                entry = data.get(group)
                if entry is None:
                    data[group] = [value, False, 1]
                else:
                    entry[2] += 1
                    if value != entry[0]:
                        entry[1] = True
            elif kind_r == 'continuation':
                _continuation_step(state, record, data, request, index, reset)
            elif kind_r == 'identical_rows':
                _identical_step(state, record, data, request, index, reset, cache)
            elif kind_r == 'status_vocabulary':
                value = _typed(state, record, index[request['column']], cache)['value']
                if value is None:
                    continue
                data['non_empty'] += 1
                if value in data['values'] or len(data['values']) < STATUS_VOCAB_CAP:
                    data['values'][value] += 1
                else:
                    data['truncated'] = True
            elif kind_r == 'date_format':
                i = index[request['column']];raw = record['raw'][i]
                pattern = day_month_pattern(raw)
                if pattern:
                    data['patterns'][pattern] += 1
                elif raw is not None and not _empty(raw):
                    data['patterns']['native_or_other'] += 1
                parsed = _typed(state, record, i, cache)['temporal']
                data['statuses'][parsed['status'].split(':')[0] if parsed['status'] != 'ok' else 'ok'] += 1
                if parsed['status'].startswith('error'):
                    data['statuses'][parsed['status']] += 1
                if parsed['precision']:
                    data['precisions'][parsed['precision']] += 1
                data['resolutions'][parsed['resolution']] += 1
                value = parsed['value']
                if value is not None:
                    text = value.isoformat()
                    data['min'] = text if data['min'] is None or text < data['min'] else data['min']
                    data['max'] = text if data['max'] is None or text > data['max'] else data['max']
                elif parsed['status'] == 'partial':
                    if parsed['month']:
                        data['partial_months'][parsed['month']] += 1
                    if len(data['row_refs']) < 20:
                        data['row_refs'].append(record['row_id'])
    for request, data in acc:
        result = _finish(state, request, data, profiles, profile_unknown)
        check = {'check_id': stable_id('C', state.name, request, revision), 'kind': request['kind'],
                 'table': state.name, 'request': request, 'scope': _scope(state, 'provisional_data', stage),
                 'result': result}
        if request['kind'] == 'key_values':
            check['_keys'] = data
        state.cache[(dumps(request), revision)] = check
        results[dumps(request)] = check
    return [results[dumps(r)] for r in requests]


def _continuation_step(state, record, data, request, index, reset):
    fill = [index[c] for c in request['columns']];keys = [index[c] for c in request['key_columns']]
    raw = record['raw']
    if reset:
        data['state'] = None
    line_cells = [i for i in range(len(raw)) if i not in fill and i not in keys]
    line_present = any(not _empty(raw[i]) for i in line_cells) if line_cells else True
    if request['mode'] == 'key':
        key_present = all(not _empty(raw[i]) for i in keys)
        if key_present:
            current = tuple(str(raw[i]).strip() for i in keys)
            head_state = data['state']
            if head_state is None or head_state['key'] != current:
                data['state'] = {'key': current, 'head': {i: raw[i] for i in fill}};data['groups'] += 1
            else:
                for i in fill:
                    if _empty(raw[i]) and not _empty(head_state['head'].get(i)):
                        data['explicit_key_blank_cells'][state.columns[i]['name']] += 1
            return
        if data['state'] is None:
            data['orphan_continuations'] += 1;return
    else:
        anchor = fill[0]
        if not _empty(raw[anchor]):
            data['state'] = {'head': {i: raw[i] for i in fill}};data['groups'] += 1;return
        if data['state'] is None:
            data['orphan_continuations'] += 1;return
    if not line_present:
        data['line_cells_missing'] += 1
    data['continuation_rows'] += 1
    if len(data['samples']) < 6:
        data['samples'].append(record['row_id'])
    for i in fill:
        head = data['state']['head'].get(i)
        if _empty(raw[i]):
            if not _empty(head):
                data['fillable_cells'][state.columns[i]['name']] += 1
        elif not _empty(head) and str(raw[i]).strip() != str(head).strip():
            data['conflicting_values'][state.columns[i]['name']] += 1


def _identical_step(state, record, data, request, index, reset, cache):
    keys = [index[c] for c in request['key_columns']]
    raw = record['raw']
    if keys:
        if reset:
            data['state'] = None
        present = all(not _empty(raw[i]) for i in keys)
        if present:
            data['state'] = tuple(str(raw[i]).strip() for i in keys)
        group = data['state'] if (present or request.get('continuation')) else None
        if group is None:
            return
    else:
        group = ('__no_key__',)
    signature = hashlib.sha256(dumps([group, raw]).encode()).hexdigest()
    first = data['seen'].get(signature)
    if first is None:
        data['seen'][signature] = (record['row_id'], record['part'])
        return
    if first[1] == record['part']:
        data['same_part'] += 1
    else:
        data['cross_part'] += 1;data['pairs'][f'{first[1]}->{record["part"]}'] += 1
    if len(data['samples']) < 6:
        data['samples'].append([first[0], record['row_id']])


def _finish(state, request, data, profiles, profile_unknown):
    kind = request['kind']
    if kind == 'column_profile':
        out = {}
        for column, profile in zip(state.columns, profiles):
            finished = profile.finish();finished['scope'] = 'provisional_data_rows'
            out[column['name']] = finished
        return {'columns': out, 'excluded_row_kinds': dict(profile_unknown)}
    if kind in {'key_stats', 'key_values'}:
        groups = data['groups'];sizes = Counter(groups.values())
        multi = sum(n for size, n in sizes.items() if size > 1)
        return {'rows': data['rows'], 'non_null_rows': data['non_null'], 'distinct_groups': len(groups),
                'null_key_rows': data['rows']-data['non_null'], 'groups_with_multiple_rows': multi,
                'max_rows_per_group': max(groups.values(), default=0),
                'unique_when_non_null': data['non_null'] == len(groups),
                'row_key_supported': data['rows'] > 0 and data['non_null'] == data['rows'] == len(groups),
                'note': 'Repeated grouping keys are valid; uniqueness alone does not prove identity.'}
    if kind == 'measure_repetition':
        multi = [v for v in data.values() if v[2] > 1]
        varying = sum(1 for v in multi if v[1])
        return {'groups': len(data), 'groups_with_multiple_rows': len(multi), 'groups_varying': varying,
                'groups_constant_multi_row': len(multi)-varying,
                'note': 'Constant values within multi-row groups suggest a repeated group-level value.'}
    if kind == 'continuation':
        return {'continuation_rows': data['continuation_rows'], 'groups': data['groups'],
                'fillable_cells': dict(data['fillable_cells']), 'conflicting_values': dict(data['conflicting_values']),
                'orphan_continuations': data['orphan_continuations'],
                'explicit_key_blank_cells': dict(data['explicit_key_blank_cells']),
                'continuation_without_line_cells': data['line_cells_missing'], 'sample_row_ids': data['samples']}
    if kind == 'identical_rows':
        return {'identical_same_part': data['same_part'], 'identical_cross_part': data['cross_part'],
                'part_pairs': dict(data['pairs']), 'sample_pairs': data['samples'],
                'scope_key_columns': request['key_columns']}
    if kind == 'status_vocabulary':
        return {'values': [[v, n] for v, n in data['values'].most_common()], 'non_empty': data['non_empty'],
                'truncated': data['truncated'], 'cap': STATUS_VOCAB_CAP}
    if kind == 'date_format':
        return {'patterns': dict(data['patterns']), 'statuses': dict(data['statuses']),
                'precisions': dict(data['precisions']), 'resolutions': dict(data['resolutions']),
                'min': data['min'], 'max': data['max'], 'partial_months': sorted(data['partial_months']),
                'partial_row_refs': data['row_refs']}
    raise ValueError(kind)


def join_check(source_state, source_cols, source_keys, target_state, target_cols, target_keys):
    """Overlap/cardinality/fanout evidence for a proposed join (not semantic proof)."""
    stypes = [source_state.columns[source_state.index(c)]['sql_type'] for c in source_cols]
    ttypes = [target_state.columns[target_state.index(c)]['sql_type'] for c in target_cols]
    if stypes != ttypes:
        return {'status': 'type_mismatch', 'source_types': stypes, 'target_types': ttypes}
    s = source_keys['groups'];t = target_keys['groups']
    orphans = sum(n for k, n in s.items() if k not in t)
    raw_t = target_keys['raw_groups'];raw_s = source_keys['raw_groups']
    normalized_orphans = sum(n for k, n in raw_s.items() if k not in raw_t)
    target_unique = all(n == 1 for n in t.values())
    source_unique = all(n == 1 for n in s.values())
    return {'status': 'checked', 'source_non_null_rows': sum(s.values()), 'orphan_source_rows': orphans,
            'orphan_source_rows_accent_folded': normalized_orphans, 'matched_source_keys': sum(1 for k in s if k in t),
            'target_unique': target_unique,
            'cardinality': ('one' if source_unique else 'many')+'_to_'+('one' if target_unique else 'many'),
            'fanout_risk': not target_unique, 'business_verified': False}
