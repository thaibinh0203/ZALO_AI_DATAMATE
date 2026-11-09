"""Plan-SQL conformance checks on the SQL AST (design §11.5).

Each check reports pass/fail/not_checked/unsupported with the evidence used. Checks
look for the plan's predicates/aggregations in any equivalent supported form; they do
not prove business correctness, and anything outside the supported forms is reported
as unsupported rather than silently passed.
"""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import sqlglot
from sqlglot import exp

CRITICAL = {'tables_used', 'time_bounds', 'filters', 'having', 'aggregates', 'projection'}
FLIP = {'>': '<', '>=': '<=', '<': '>', '<=': '>=', '=': '=', '<>': '<>'}
OPS = {exp.GT: '>', exp.GTE: '>=', exp.LT: '<', exp.LTE: '<=', exp.EQ: '=', exp.NEQ: '<>'}


def literal(node):
    """Python value of a literal-like node or None."""
    while isinstance(node, (exp.Cast, exp.Paren, exp.TryCast)) or node.__class__.__name__ in {'TsOrDsToDate', 'StrToDate'}:
        node = node.this
    if isinstance(node, exp.Neg):
        inner = literal(node.this)
        return -inner if isinstance(inner, Decimal) else None
    if isinstance(node, exp.Literal):
        if node.is_string:
            return node.this
        try:
            return Decimal(node.this)
        except InvalidOperation:
            return None
    if isinstance(node, exp.Boolean):
        return node.this
    return None


def column_name(node):
    while isinstance(node, (exp.Cast, exp.Paren)):
        node = node.this
    return node.name if isinstance(node, exp.Column) else None


def comparisons(tree):
    """Yield (column_or_alias, op, literal) for simple comparisons/BETWEEN/IN anywhere."""
    for node in tree.walk():
        if type(node) in OPS:
            left, right = node.this, node.expression;op = OPS[type(node)]
            lc, rc = column_name(left), column_name(right)
            lv, rv = literal(left), literal(right)
            if lc is not None and rv is not None:
                yield lc, op, rv, node
            elif rc is not None and lv is not None:
                yield rc, FLIP[op], lv, node
            elif rv is not None and isinstance(left, exp.AggFunc):
                yield ('agg:'+left.key, op, rv, node)
            elif lv is not None and isinstance(right, exp.AggFunc):
                yield ('agg:'+right.key, FLIP[op], lv, node)
        elif isinstance(node, exp.Between):
            c = column_name(node.this);low = literal(node.args.get('low'));high = literal(node.args.get('high'))
            if c is not None and low is not None and high is not None:
                yield c, '>=', low, node
                yield c, '<=', high, node
        elif isinstance(node, exp.In):
            c = column_name(node.this);values = [literal(v) for v in node.expressions]
            if c is not None and values and all(v is not None for v in values):
                negated = isinstance(node.parent, exp.Not)
                yield c, 'not_in' if negated else 'in', values, node


def _as_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _same(a, b):
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except InvalidOperation:
        return str(a) == str(b)


def check_time(comps, column, start, end_exclusive, sql_type):
    lower = upper = False;seen = False
    for col, op, value, _ in comps:
        if col != column:
            continue
        seen = True;d = _as_date(value)
        if d is None:
            continue
        if start and ((op == '>=' and d == date.fromisoformat(start)) or (op == '>' and d == date.fromisoformat(start)-timedelta(days=1))):
            lower = True
        if end_exclusive:
            end = date.fromisoformat(end_exclusive)
            if (op == '<' and d == end) or (op == '<=' and d == end-timedelta(days=1) and sql_type == 'DATE'):
                upper = True
    need_lower = start is not None;need_upper = end_exclusive is not None
    if (not need_lower or lower) and (not need_upper or upper):
        return 'pass', None
    return 'fail', 'missing or different bounds' if seen else 'time column not filtered'


def check_filter(comps, f):
    column = f['column_name'];op = f['op'];value = f['value']
    for col, cop, cval, _ in comps:
        if col != column:
            continue
        if op in {'in', 'not_in'} and cop == op and isinstance(cval, list) and sorted(map(str, cval)) == sorted(map(str, value)):
            return 'pass'
        if op == '=' and cop == 'in' and isinstance(cval, list) and len(cval) == 1 and _same(cval[0], value):
            return 'pass'
        if op == 'in' and cop == '=' and isinstance(value, list) and len(value) == 1 and _same(cval, value[0]):
            return 'pass'
        if op == cop and not isinstance(cval, list) and _same(cval, value):
            return 'pass'
        if op == 'between' and cop in {'>=', '<='} and isinstance(value, list):
            return 'pass' if _same(cval, value[0 if cop == '>=' else 1]) else 'fail'
    if op in {'is_null', 'is_not_null', 'contains'}:
        return 'unsupported'
    return 'fail'


def check_sql(sql, plan, *, answer_shape, allowed):
    """Return list of checks for qualified SQL against the validated plan."""
    tree = sqlglot.parse_one(sql, read='postgres');checks = []
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    used = {t.name for t in tree.find_all(exp.Table) if t.name not in ctes}
    planned = set()
    for s in plan['stages']:
        if s['from'] in allowed:
            planned.add(s['from'])
        planned |= {j['table'] for j in s['joins']}
    status = 'pass' if used == planned else 'fail'
    checks.append({'check': 'tables_used', 'status': status, 'detail': {'sql': sorted(used), 'plan': sorted(planned)}})
    comps = list(comparisons(tree))
    for s in plan['stages']:
        for t in s['time']:
            if 'start' not in t:
                continue
            sql_type = allowed[t['view']][t['column']]
            st, detail = check_time(comps, t['column'], t['start'], t['end_exclusive'], sql_type)
            checks.append({'check': 'time_bounds', 'status': st, 'detail': {'stage': s['id'], 'column': t['column'],
                           'start': t['start'], 'end_exclusive': t['end_exclusive'], 'note': detail}})
        for f in s['filters']:
            if 'column_name' not in f:
                continue
            checks.append({'check': 'filters', 'status': check_filter(comps, f),
                           'detail': {'stage': s['id'], 'column': f['column_name'], 'op': f['op'], 'value': f['value']}})
        aggregates = {a['name']: a for a in s['aggregates']}
        for h in s['having']:
            a = aggregates[h['aggregate']]
            fn_key = {'sum': 'sum', 'count': 'count', 'count_distinct': 'count', 'avg': 'avg', 'min': 'min', 'max': 'max'}.get(a['fn'])
            ok = any(((col == h['aggregate']) or (col == 'agg:'+str(fn_key))) and op == h['op'] and not isinstance(v, list) and _same(v, h['value'])
                     for col, op, v, _ in comps)
            checks.append({'check': 'having', 'status': 'pass' if ok else 'fail',
                           'detail': {'stage': s['id'], 'aggregate': h['aggregate'], 'op': h['op'], 'value': h['value']}})
        group_cols = {column_name(g) for node in tree.find_all(exp.Group) for g in node.expressions}
        group_cols |= {column_name(p) for w in tree.find_all(exp.Window) for p in (w.args.get('partition_by') or [])}
        for g in s['group_by']:
            name = g.split('.')[-1]
            if s['from'] not in allowed and name in {a for st in plan['stages'] for a in [x['name'] for x in st['aggregates']]}:
                continue
            checks.append({'check': 'group_by', 'status': 'pass' if name in group_cols else 'fail',
                           'detail': {'stage': s['id'], 'column': name}})
        for a in s['aggregates']:
            checks.append({'check': 'aggregates', 'status': _aggregate_present(tree, a), 'detail': {'stage': s['id'], **a}})
    final = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    count = len(final.expressions) if final is not None else 0
    expected = len(plan['output']['select'])
    star = final is not None and any(isinstance(e, exp.Star) for e in final.expressions)
    checks.append({'check': 'projection', 'status': 'fail' if star or count != expected else 'pass',
                   'detail': {'sql_columns': count, 'plan_columns': expected, 'shape': answer_shape}})
    if plan['output']['limit'] is not None:
        limit = tree.args.get('limit')
        value = literal(limit.expression) if limit is not None else None
        checks.append({'check': 'limit', 'status': 'pass' if value is not None and _same(value, plan['output']['limit']) else 'fail',
                       'detail': {'plan': plan['output']['limit'], 'sql': None if value is None else str(value)}})
    return checks


def _aggregate_present(tree, aggregate):
    fn = aggregate['fn'];column = aggregate['column'].split('.')[-1]
    for node in tree.walk():
        if fn == 'sum' and isinstance(node, exp.Sum):
            target = node.this
        elif fn in {'count', 'count_distinct'} and isinstance(node, exp.Count):
            target = node.this
            distinct = isinstance(target, exp.Distinct)
            if fn == 'count_distinct' and not distinct:
                continue
            if fn == 'count' and distinct and column != '*':
                continue
            if distinct:
                target = target.expressions[0] if target.expressions else None
            if column == '*' and (target is None or isinstance(target, exp.Star) or literal(target) is not None):
                return 'pass'
        elif fn == 'avg' and isinstance(node, exp.Avg):
            target = node.this
        elif fn == 'min' and isinstance(node, exp.Min):
            target = node.this
        elif fn == 'max' and isinstance(node, exp.Max):
            target = node.this
        elif fn == 'weighted_avg':
            return 'unsupported'
        else:
            continue
        if target is not None and any(column_name(c) == column for c in [target, *target.find_all(exp.Column)]):
            return 'pass'
    return 'fail'


def summarize(checks, *, unverified_policy='abstain'):
    failed = [c for c in checks if c['status'] == 'fail']
    unverified = [c for c in checks if c['status'] in {'unsupported', 'not_checked'} and c['check'] in CRITICAL]
    if failed:
        return 'fail', failed
    if unverified and unverified_policy == 'abstain':
        return 'unverified', unverified
    return 'pass', unverified
