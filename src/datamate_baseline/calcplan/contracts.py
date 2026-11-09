"""Question analysis, calculation plan and model action contracts with validators.

Validators return structured issues (code + detail) so repairs target the right
layer. Passing a contract proves structure only, never that a metric is correct.
"""
from decimal import Decimal, InvalidOperation
import re

from datamate_b.contracts import VALUE_TYPES
from datamate_data.normalization import key, parse_number
from .time_resolver import KINDS as TIME_KINDS, fold

ANALYSIS_KEYS = {'rewrite', 'target', 'metric', 'operation', 'filters', 'time', 'grouping', 'answer', 'unresolved'}
OPERATIONS = ('count', 'sum', 'average', 'max', 'min', 'rank', 'list', 'compare', 'ratio', 'lookup', 'exists',
              'difference', 'growth', 'share', 'other')
FILTER_OPS = ('=', '<>', '>', '>=', '<', '<=', 'in', 'not_in', 'contains', 'between', 'is_null', 'is_not_null')
AGG_FNS = ('sum', 'count', 'count_distinct', 'avg', 'min', 'max', 'weighted_avg')
TIME_PARAMS = {'relative', 'day', 'month', 'year', 'quarter', 'n', 'lunar_year', 'shift', 'date', 'start', 'end'}
NAME = re.compile(r'[a-z_][a-z0-9_]{0,62}')
# Ordered: inclusive phrases are checked before their strict sub-phrases.
MARKERS = [('>=', r'\b(tro len|it nhat|toi thieu|khong duoi|khong it hon)\b'),
           ('<=', r'\b(tro xuong|toi da|khong qua|khong vuot qua|khong hon|khong nhieu hon)\b'),
           ('>', r'\b(tren|hon|qua|lon hon|nhieu hon|vuot|cao hon)\b'),
           ('<', r'\b(duoi|it hon|nho hon|chua toi|chua den|thap hon|kem hon)\b')]
REASONS = {
    'MODEL_CONTRACT', 'CONTEXT_BUDGET', 'BUDGET_EXHAUSTED', 'AMBIGUOUS_QUESTION', 'AMBIGUOUS_TIME', 'UNSUPPORTED_TIME',
    'AMBIGUOUS_LITERAL', 'LITERAL_NOT_FOUND', 'MISSING_DATA', 'OUTSIDE_DATA_COVERAGE', 'QUALITY_BLOCKED',
    'PLAN_INVALID', 'PLAN_SQL_MISMATCH', 'SQL_GATE', 'SQL_ERROR', 'QUERY_TIMEOUT', 'RESULT_SHAPE', 'NULL_RESULT',
    'SELF_JOIN_UNSUPPORTED', 'CAPABILITY_UNSUPPORTED', 'CONTEXT_CHANGED', 'UNVERIFIED_CRITICAL_CHECK'}


class Issue(dict):
    def __init__(self, code, **detail):
        super().__init__(code=code, **detail)


def question_numbers(text):
    """Numbers stated in the question (VND formats, k/tr suffixes, percents)."""
    folded = fold(text);values = set()
    for token in re.findall(r'[0-9][0-9.,]*\s*(?:tr(?:ieu)?[0-9]*|k|nghin|ngan|%|d|dong|vnd)?', folded):
        token = token.strip().replace('%', '').strip()
        for currency in (True, False):
            try:
                values.add(parse_number(token.replace('dong', 'đ').replace(' d', 'đ'), currency=currency))
            except (ValueError, InvalidOperation):
                continue
        digits = re.sub(r'[^0-9]', '', token)
        if digits:
            values.add(Decimal(digits))
    return values


def phrase_operator(phrase):
    folded = fold(phrase)
    for op, pattern in MARKERS:
        if re.search(pattern, folded):
            return op
    return None


def markers(text):
    folded = fold(text)
    return sorted(op for op, pattern in MARKERS for _ in re.finditer(pattern, folded))


def _contains(question, phrase):
    return isinstance(phrase, str) and phrase.strip() and fold(phrase) in fold(question)


def validate_analysis(value, question):
    """Return (analysis, corrections). Raise ValueError for contract errors (retryable).

    The original question is always kept. A rewrite that changes numbers, years or
    strict/inclusive wording is rejected (analysis continues on the original).
    Operators contradicting the phrase wording are corrected with a note.
    """
    if not isinstance(value, dict) or set(value) != ANALYSIS_KEYS:
        raise ValueError(f'analysis needs exactly keys {sorted(ANALYSIS_KEYS)}')
    corrections = []
    rewrite = value['rewrite']
    if not isinstance(rewrite, dict) or set(rewrite) != {'text', 'changed'}:
        raise ValueError('rewrite must be {"text": str|null, "changed": bool}')
    numbers = question_numbers(question);years = set(re.findall(r'\b(?:19|20)\d{2}\b', question))
    rewrite_status = 'kept_original'
    if rewrite['changed'] and isinstance(rewrite['text'], str) and rewrite['text'].strip():
        bad = []
        if not question_numbers(rewrite['text']) <= numbers:
            bad.append('rewrite_changes_numbers')
        if not set(re.findall(r'\b(?:19|20)\d{2}\b', rewrite['text'])) <= years:
            bad.append('rewrite_adds_year')
        if markers(rewrite['text']) != markers(question):
            bad.append('rewrite_changes_threshold_wording')
        rewrite_status = 'rejected:'+','.join(bad) if bad else 'accepted'
    for field in ('target', 'metric'):
        if not isinstance(value[field], dict):
            raise ValueError(f'{field} must be an object')
    if value['operation'] not in OPERATIONS:
        raise ValueError(f'operation must be one of {OPERATIONS}')
    answer = value['answer']
    if not isinstance(answer, dict) or set(answer) != {'type', 'target', 'granularity'} or answer['type'] not in VALUE_TYPES:
        raise ValueError('answer must be {type in value types, target, granularity}')
    if not isinstance(value['grouping'], list) or not isinstance(value['unresolved'], list):
        raise ValueError('grouping and unresolved must be lists')
    filters = []
    if not isinstance(value['filters'], list):
        raise ValueError('filters must be a list')
    for f in value['filters']:
        if not isinstance(f, dict) or set(f) != {'phrase', 'field', 'operator', 'value', 'level'}:
            raise ValueError('filter items need phrase, field, operator, value, level')
        if f['operator'] not in FILTER_OPS or f['level'] not in {'row', 'group', 'unknown'}:
            raise ValueError('invalid filter operator/level')
        f = dict(f)
        if not _contains(question, f['phrase']):
            corrections.append({'filter': f['phrase'], 'note': 'phrase_not_in_question'})
        expected = phrase_operator(f['phrase'] or '')
        if expected and f['operator'] in {'>', '>=', '<', '<='} and expected != f['operator']:
            corrections.append({'filter': f['phrase'], 'operator_from': f['operator'], 'operator_to': expected,
                                'rule': 'rulebook §2: "quá/hơn/trên/dưới N" excludes N; "từ N trở lên" includes N'})
            f['operator'] = expected
        if isinstance(f['value'], (int, float)) and not isinstance(f['value'], bool):
            if Decimal(str(f['value'])) not in numbers:
                raise ValueError(f'filter value {f["value"]} is not a number stated in the question')
        filters.append(f)
    times = []
    if not isinstance(value['time'], list):
        raise ValueError('time must be a list')
    for t in value['time']:
        if not isinstance(t, dict) or 'kind' not in t or 'phrase' not in t or t['kind'] not in TIME_KINDS:
            raise ValueError(f'time items need phrase and kind in {TIME_KINDS}')
        extra = set(t)-{'phrase', 'kind', 'role'}-TIME_PARAMS
        if extra:
            raise ValueError(f'unknown time params {sorted(extra)}')
        t = dict(t)
        for k in ('year', 'lunar_year'):
            if t.get(k) is not None and str(t[k]) not in years:
                corrections.append({'time': t['phrase'], 'removed': {k: t[k]}, 'note': 'year not stated in question'})
                t[k] = None
        times.append(t)
    analysis = {**value, 'filters': filters, 'time': times, 'rewrite_status': rewrite_status,
                'original_question': question}
    return analysis, corrections


def reconcile_time(rule_specs, model_specs):
    """Deterministic extractor wins on overlapping phrases; model fills only gaps."""
    result = [dict(s) for s in rule_specs];conflicts = []
    covered = [fold(s['phrase']) for s in rule_specs]
    for spec in model_specs:
        phrase = fold(spec.get('phrase') or '')
        overlap = [c for c in covered if phrase and (phrase in c or c in phrase)]
        if overlap:
            rule = next(s for s in rule_specs if fold(s['phrase']) == overlap[0])
            if rule['kind'] != spec['kind']:
                conflicts.append({'phrase': spec['phrase'], 'model_kind': spec['kind'], 'rule_kind': rule['kind']})
            continue
        result.append({**{k: v for k, v in spec.items() if k != 'role'}, 'source': 'model'})
    tet = next((s for s in result if s['kind'] == 'tet' and s.get('lunar_year')), None)
    for s in result:
        if s['kind'] == 'before_tet' and tet and not s.get('lunar_year'):
            s['lunar_year'] = tet['lunar_year']
    return result, conflicts


def _num(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not a number')
    return Decimal(str(value))


def _resolve_column(ref, available):
    """Resolve 'col' or 'view.col' against {qualified: (view, col, type)}; None if ambiguous."""
    if not isinstance(ref, str):
        return None
    if ref in available:
        return available[ref]
    matches = [v for k, v in available.items() if k.split('.', 1)[-1] == ref]
    return matches[0] if len(matches) == 1 else None


def validate_calc_plan(plan, ctx, analysis, intervals):
    """Structural + semantic validation. Returns (plan, issues, dependencies).

    ``intervals`` maps interval IDs to resolver output. Bounds always come from the
    resolver, never from the model.
    """
    issues = []
    keys = {'answer', 'stages', 'output', 'evidence', 'feasibility'}
    if not isinstance(plan, dict) or set(plan) != keys:
        raise ValueError(f'plan needs exactly keys {sorted(keys)}')
    answer = plan['answer']
    if not isinstance(answer, dict) or answer.get('type') not in VALUE_TYPES or answer.get('shape') not in {'scalar', 'list', 'table'}:
        raise ValueError('answer needs type in value types and shape scalar/list/table')
    if not isinstance(plan['stages'], list) or not 1 <= len(plan['stages']) <= 8:
        raise ValueError('stages must be a list of 1-8 stages')
    tables = ctx['tables'];stage_outputs = {};deps = {}
    if answer['type'] != analysis['answer']['type']:
        issues.append(Issue('PLAN_ANSWER_TYPE_DIFFERS', plan=answer['type'], analysis=analysis['answer']['type']))
    expected_shape = 'scalar' if answer['type'] not in {'list', 'table'} else answer['type']
    if answer['shape'] != expected_shape:
        issues.append(Issue('PLAN_SHAPE_TYPE_MISMATCH', shape=answer['shape'], type=answer['type']))

    def dep(view):
        return deps.setdefault(view, {'view': view, 'columns': {}, 'table_operations': set(), 'time_filters': []})
    thresholds = []
    for stage in plan['stages']:
        need = {'id', 'from', 'joins', 'time', 'filters', 'group_by', 'aggregates', 'having'}
        if not isinstance(stage, dict) or set(stage) != need:
            raise ValueError(f'stage needs exactly keys {sorted(need)}')
        sid = stage['id']
        if not isinstance(sid, str) or not re.fullmatch(r's[0-9]{1,2}', sid) or sid in stage_outputs:
            raise ValueError('stage id must be unique s1..s99')
        available = {};base_views = []
        if stage['from'] in tables:
            base_views.append(stage['from'])
            for col, typ in ctx['allowed'][stage['from']].items():
                available[f"{stage['from']}.{col}"] = (stage['from'], col, typ)
            for j in stage['joins']:
                if not isinstance(j, dict) or set(j) != {'table', 'on', 'type'} or j['table'] not in tables or j['type'] not in {'inner', 'left'}:
                    raise ValueError('join needs table (known view), on [[left,right],...], type inner/left')
                if j['table'] == stage['from'] or j['table'] in base_views:
                    issues.append(Issue('SELF_JOIN_UNSUPPORTED', table=j['table']));continue
                if not isinstance(j['on'], list) or not j['on'] or any(not isinstance(p, list) or len(p) != 2 for p in j['on']):
                    raise ValueError('join.on must be a nonempty list of [left_column, right_column]')
                right = {f"{j['table']}.{c}": (j['table'], c, t) for c, t in ctx['allowed'][j['table']].items()}
                for left_col, right_col in j['on']:
                    l = _resolve_column(left_col, available);r = _resolve_column(right_col if '.' in str(right_col) else f"{j['table']}.{right_col}", right)
                    if l is None or r is None:
                        issues.append(Issue('PLAN_UNKNOWN_JOIN_COLUMN', join=j));continue
                    if l[2] != r[2]:
                        issues.append(Issue('PLAN_JOIN_TYPE_MISMATCH', left=left_col, right=right_col, types=[l[2], r[2]]))
                    dep(l[0])['columns'].setdefault(l[1], set()).add('join');dep(r[0])['columns'].setdefault(r[1], set()).add('join')
                relation = ctx['relation'](stage['from'], j['table'], [p[0].split('.')[-1] for p in j['on']], [p[1].split('.')[-1] for p in j['on']])
                j['relation'] = relation
                if relation is None:
                    issues.append(Issue('PLAN_JOIN_NOT_IN_SEMANTIC_PLAN', join=j, severity='needs_profile'))
                base_views.append(j['table']);available.update(right)
        elif stage['from'] in stage_outputs:
            if stage['joins']:
                issues.append(Issue('PLAN_JOIN_ON_STAGE_UNSUPPORTED', stage=sid))
            for name, typ in stage_outputs[stage['from']].items():
                available[f"{stage['from']}.{name}"] = (stage['from'], name, typ)
        else:
            issues.append(Issue('PLAN_UNKNOWN_SOURCE', stage=sid, source=stage['from']));stage_outputs[sid] = {};continue
        outputs = {}
        for t in stage['time']:
            if not isinstance(t, dict) or set(t) != {'interval', 'column'}:
                raise ValueError('time items need interval and column')
            interval = intervals.get(t['interval'])
            col = _resolve_column(t['column'], available)
            if interval is None or interval['status'] != 'resolved' or interval.get('time_of_day'):
                issues.append(Issue('PLAN_UNKNOWN_INTERVAL', stage=sid, interval=t['interval']));continue
            if col is None or col[2] not in {'DATE', 'TIMESTAMP'} or col[0] not in tables:
                issues.append(Issue('PLAN_TIME_COLUMN_INVALID', stage=sid, column=t['column']));continue
            t['view'], t['column'] = col[0], col[1];t['start'] = interval['start'];t['end_exclusive'] = interval['end_exclusive']
            d = dep(col[0]);d['columns'].setdefault(col[1], set()).add('time_filter')
            d['time_filters'].append({'column': col[1], 'start': interval['start'], 'end_exclusive': interval['end_exclusive']})
        for f in stage['filters']:
            if not isinstance(f, dict) or set(f) != {'column', 'op', 'value', 'evidence'}:
                raise ValueError('filters need column, op, value, evidence')
            col = _resolve_column(f['column'], available)
            if col is None:
                issues.append(Issue('PLAN_UNKNOWN_COLUMN', stage=sid, column=f['column']));continue
            if f['op'] not in FILTER_OPS:
                raise ValueError('invalid filter op')
            f['view'], f['column_name'], f['sql_type'] = col
            problem = _value_problem(f['op'], f['value'], col[2])
            if problem:
                issues.append(Issue('PLAN_FILTER_VALUE_TYPE', stage=sid, column=f['column'], problem=problem))
            if col[0] in tables:
                dep(col[0])['columns'].setdefault(col[1], set()).add('filter')
            if f['op'] in {'>', '>=', '<', '<='} and col[2] == 'NUMERIC':
                thresholds.append((f['op'], f['value'], 'row'))
        for g in stage['group_by']:
            col = _resolve_column(g, available)
            if col is None:
                issues.append(Issue('PLAN_UNKNOWN_COLUMN', stage=sid, column=g));continue
            outputs[col[1]] = col[2]
            if col[0] in tables:
                dep(col[0])['columns'].setdefault(col[1], set()).add('group_by')
        for a in stage['aggregates']:
            if not isinstance(a, dict) or set(a) != {'name', 'fn', 'column'} or a['fn'] not in AGG_FNS or not NAME.fullmatch(str(a['name'])):
                raise ValueError('aggregates need name (snake_case), fn, column')
            if a['name'] in outputs:
                raise ValueError('duplicate output name in stage '+sid)
            if a['column'] == '*' and a['fn'] == 'count':
                outputs[a['name']] = 'NUMERIC'
                for v in base_views:
                    dep(v)['table_operations'].add('count')
                continue
            col = _resolve_column(a['column'], available)
            if col is None:
                issues.append(Issue('PLAN_UNKNOWN_COLUMN', stage=sid, column=a['column']));continue
            outputs[a['name']] = 'NUMERIC' if a['fn'] in {'sum', 'count', 'count_distinct', 'avg', 'weighted_avg'} else col[2]
            if col[0] in tables:
                op = {'sum': 'sum', 'count': 'count', 'count_distinct': 'count_distinct', 'avg': 'avg',
                      'min': 'min_max', 'max': 'min_max', 'weighted_avg': 'avg'}[a['fn']]
                dep(col[0])['columns'].setdefault(col[1], set()).add(op);dep(col[0])['table_operations'].add(op)
                measure = ctx['measure'](col[0], col[1])
                if measure and a['fn'] == 'sum':
                    if measure['aggregation'] == 'none':
                        issues.append(Issue('PLAN_NON_ADDITIVE_SUM', stage=sid, column=col[1], measure=measure))
                    if measure['aggregation'] == 'sum_once_per_group' and not set(measure.get('group_columns') or []) <= set(stage['group_by']):
                        issues.append(Issue('PLAN_REPEATED_GROUP_VALUE_SUM', stage=sid, column=col[1], measure=measure))
                if stage['joins'] and a['fn'] in {'sum', 'count', 'avg'} and col[0] == stage['from']:
                    risky = [j for j in stage['joins'] if not (j.get('relation') or {}).get('target_unique', False)]
                    if risky:
                        issues.append(Issue('PLAN_FANOUT_RISK', stage=sid, column=col[1], joins=[j['table'] for j in risky]))
        for h in stage['having']:
            if not isinstance(h, dict) or set(h) != {'aggregate', 'op', 'value'} or h['aggregate'] not in outputs:
                raise ValueError('having needs aggregate (defined in this stage), op, value')
            problem = _value_problem(h['op'], h['value'], 'NUMERIC')
            if problem:
                issues.append(Issue('PLAN_FILTER_VALUE_TYPE', stage=sid, having=h['aggregate'], problem=problem))
            if h['op'] in {'>', '>=', '<', '<='}:
                thresholds.append((h['op'], h['value'], 'group'))
        stage_outputs[sid] = outputs
    output = plan['output']
    if not isinstance(output, dict) or set(output) != {'select', 'order_by', 'limit', 'round'}:
        raise ValueError('output needs select, order_by, limit, round')
    refs = set()
    for item in output['select']:
        if not isinstance(item, dict) or set(item) != {'name', 'expr'}:
            raise ValueError('select items need name and expr')
        for ref in re.findall(r'\b(s[0-9]{1,2})\.([a-z_][a-z0-9_]*)', item['expr']):
            if ref[1] not in stage_outputs.get(ref[0], {}):
                issues.append(Issue('PLAN_UNKNOWN_OUTPUT_REF', ref='.'.join(ref)))
            refs.add(ref[0])
        if re.sub(r'\b(s[0-9]{1,2})\.([a-z_][a-z0-9_]*)|[0-9.]+|[-+*/() ]', '', item['expr']):
            issues.append(Issue('PLAN_UNSUPPORTED_EXPRESSION', expr=item['expr']))
    count = len(output['select'])
    if answer['shape'] in {'scalar', 'list'} and count != 1:
        issues.append(Issue('PLAN_PROJECTION_COUNT', shape=answer['shape'], count=count))
    if answer['shape'] == 'scalar':
        grouped = [s for s in plan['stages'] if s['id'] in refs and s['group_by']]
        if grouped and output['limit'] != 1:
            issues.append(Issue('PLAN_SCALAR_FROM_GROUPED_STAGE', stages=[s['id'] for s in grouped]))
    if analysis['operation'] in {'list', 'rank'} or answer['shape'] == 'list':
        for v in deps.values():
            v['table_operations'].add('list_values')
    stated = [(f['operator'], f['value'], f['level']) for f in analysis['filters'] if f['operator'] in {'>', '>=', '<', '<='}
              and isinstance(f['value'], (int, float)) and not isinstance(f['value'], bool)]
    for op, val, level in stated:
        matches = [lv for o, v, lv in thresholds if o == op and _same_number(v, val)]
        if not matches:
            issues.append(Issue('PLAN_THRESHOLD_NOT_PRESERVED', operator=op, value=val))
        elif level == 'group' and 'group' not in matches:
            issues.append(Issue('PLAN_THRESHOLD_LEVEL_MISMATCH', operator=op, value=val,
                                note='condition on a per-entity total must be a HAVING on the grouped stage'))
    feasibility = plan['feasibility']
    if not isinstance(feasibility, dict) or feasibility.get('status') not in {'feasible', 'missing_data', 'ambiguous'}:
        raise ValueError('feasibility.status must be feasible/missing_data/ambiguous')
    if not isinstance(plan['evidence'], list):
        raise ValueError('evidence must be a list of IDs')
    unknown_refs = [r for r in plan['evidence'] if not ctx['evidence'].has(r) and not str(r).startswith('tool:')]
    if unknown_refs:
        issues.append(Issue('PLAN_UNKNOWN_EVIDENCE', refs=unknown_refs))
    dependencies = [{**d, 'columns': {c: sorted(o) for c, o in d['columns'].items()},
                     'table_operations': sorted(d['table_operations'])} for d in deps.values()]
    return plan, issues, dependencies


def _same_number(a, b):
    try:
        return _num(a) == _num(b)
    except (ValueError, InvalidOperation):
        return False


def _value_problem(op, value, sql_type):
    if op in {'is_null', 'is_not_null'}:
        return None if value is None else 'null checks take value null'
    values = value if op in {'in', 'not_in', 'between'} else [value]
    if op in {'in', 'not_in'} and (not isinstance(value, list) or not value):
        return 'in/not_in need a nonempty list'
    if op == 'between' and (not isinstance(value, list) or len(value) != 2):
        return 'between needs [low, high]'
    for v in values:
        if sql_type in {'NUMERIC', 'INTEGER'}:
            try:
                _num(v)
            except (ValueError, InvalidOperation):
                return f'{v!r} is not numeric'
        elif sql_type in {'DATE', 'TIMESTAMP'}:
            if not isinstance(v, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', v):
                return 'date filters use ISO dates (prefer time intervals)'
        elif sql_type == 'BOOLEAN':
            if not isinstance(v, bool):
                return 'boolean filter needs true/false'
        elif not isinstance(v, (str, int)) or isinstance(v, bool):
            return 'text filters need string values'
    return None


def string_filters(plan):
    """(stage, filter) pairs comparing text columns with literal strings."""
    out = []
    for stage in plan['stages']:
        for f in stage['filters']:
            if f.get('sql_type') == 'TEXT' and f['op'] in {'=', '<>', 'in', 'not_in'}:
                out.append((stage, f))
    return out


def concept_present(concepts, ctx):
    """Inventory check for missing_data claims: columns whose role/meaning/name match."""
    hits = []
    wanted = [key(c) for c in concepts if isinstance(c, str) and c.strip()]
    for view, table in ctx['tables'].items():
        for col in table['columns']:
            text = key(' '.join(str(col.get(k) or '') for k in ('name', 'source', 'canonical_role', 'semantic_role', 'meaning')))
            if any(w and w in text for w in wanted):
                hits.append({'view': view, 'column': col['name'], 'has_data': col.get('has_data')})
    return hits
