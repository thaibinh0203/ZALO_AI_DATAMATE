"""Queryable quality registry and per-question impact assessment (design §9).

An analytic view that excludes unknown rows is not proof the data is complete. Each
issue states which operations it can affect and what is known/unknown about its
bounds, so a calculation can be cleared only when an issue provably does not touch
its population; otherwise the answer is resolved with tools or abstained.
"""
from datetime import date, timedelta

from . import QUALITY_VERSION
from .contracts import stable_id

AGGREGATES = {'sum', 'count', 'count_distinct', 'avg', 'min_max', 'exists', 'list_values'}
KIND_DEFAULTS = {
    'unknown_rows': ('blocking', AGGREGATES),
    'parse_errors': ('blocking', {'sum', 'avg', 'min_max', 'filter', 'group_by', 'time_filter',
                                  'count_distinct', 'list_values', 'join', 'project'}),
    'partial_dates': ('blocking', {'time_filter', 'group_by', 'filter', 'min_max', 'project'}),
    'ambiguous_dates': ('blocking', {'time_filter', 'group_by', 'filter', 'min_max', 'project'}),
    'unmapped_status': ('blocking', {'filter', 'group_by'}),
    'status_mapping_needs_review': ('blocking', {'filter', 'group_by'}),
    'base_quantity_unresolved': ('blocking', {'sum', 'avg', 'min_max', 'filter', 'project'}),
    'base_quantity_conflict': ('blocking', {'sum', 'avg', 'min_max', 'filter', 'project'}),
    'potential_duplicates': ('warning', {'sum', 'count', 'avg', 'count_distinct'}),
    'fill_forward_needs_review': ('blocking', {'filter', 'group_by', 'count_distinct', 'join', 'time_filter'}),
    'column_semantics_unresolved': ('info', {'filter', 'group_by', 'sum', 'project'}),
    'conversion_conflicts': ('blocking', {'sum', 'avg'}),
}


def make_issue(kind, *, business_id, table, view, columns=(), row_count=0, row_refs=(), revision=None,
               known_bounds=None, unknown_bounds=None, evidence_refs=(), description='', details=None,
               severity=None, operations=None):
    default_severity, default_ops = KIND_DEFAULTS[kind]
    return {'issue_id': stable_id('Q', business_id, table, kind, list(columns), revision),
            'kind': kind, 'severity': severity or default_severity, 'business_id': business_id,
            'table': table, 'view': view, 'columns': list(columns), 'row_count': row_count,
            'row_refs': list(row_refs)[:20], 'stage': 'final_materialization', 'revision': revision,
            'affected_operations': sorted(operations or default_ops), 'known_bounds': known_bounds or {},
            'unknown_bounds': unknown_bounds or {}, 'evidence_refs': list(evidence_refs),
            'description': description, 'details': details or {}}


def table_issues(business_id, plan, entry, stats, view, *, conversion_conflicts=()):
    """Translate materialization statistics into located issues for one table."""
    name = plan['name'];rev = plan['revision'];issues = []
    unknown = stats['kinds'].get('unknown', 0)
    if unknown:
        b = stats['unknown_dates']
        issues.append(make_issue('unknown_rows', business_id=business_id, table=name, view=view, row_count=unknown,
            row_refs=stats['unknown_rows'], revision=rev,
            known_bounds={'time': {'min': b['min'], 'max': b['max']}} if b['min'] else {},
            unknown_bounds={'time': b['undated'] > 0 or b['min'] is None},
            description='Rows whose role (data/subtotal/note...) is unresolved; kept raw, excluded from the view.'))
    for column, count in stats['errors'].items():
        issues.append(make_issue('parse_errors', business_id=business_id, table=name, view=view, columns=[column],
            row_count=count, row_refs=stats['error_rows'].get(column, []), revision=rev, unknown_bounds={'values': True},
            description='Non-empty source cells that could not be parsed; typed value is NULL, raw kept.'))
    for bucket, kind in (('partial', 'partial_dates'), ('ambiguous', 'ambiguous_dates')):
        for column, count in stats[bucket].items():
            months = stats['partial_months'].get(column, set())
            issues.append(make_issue(kind, business_id=business_id, table=name, view=view, columns=[column],
                row_count=count, row_refs=stats['partial_rows'].get(column, []), revision=rev,
                known_bounds={'months': sorted(m for m in months if m)} if months - {None} else {},
                unknown_bounds={'year': True, 'months': None in months},
                description='Dates without a source-proven year (or day/month order); full DATE is NULL, parts kept in companions.'))
    for column, values in stats['unmapped_status'].items():
        issues.append(make_issue('unmapped_status', business_id=business_id, table=name, view=view, columns=[column],
            row_count=sum(values.values()), revision=rev, details={'values': values.most_common(20)},
            description='Status values without an in-domain mapping are kept raw; filters on canonical labels miss them.'))
    for column, mapping in plan['status_mappings'].items():
        if mapping['status'] != 'enabled':
            issues.append(make_issue('status_mapping_needs_review', business_id=business_id, table=name, view=view,
                columns=[column], revision=rev, details={'domain_basis': mapping['domain_basis']},
                description='Status domain could not be established; values are raw and unmapped.'))
    if stats['base_unresolved']:
        issues.append(make_issue('base_quantity_unresolved', business_id=business_id, table=name, view=view,
            columns=['__quantity_base'], row_count=stats['base_unresolved'], revision=rev,
            description='Rows without a base-unit quantity (missing operand, unit or conversion).'))
    if stats['base_conflict']:
        issues.append(make_issue('base_quantity_conflict', business_id=business_id, table=name, view=view,
            columns=['__quantity_base'], row_count=stats['base_conflict'], revision=rev,
            description='Supplied and computed base quantities disagree; supplied value kept.'))
    if conversion_conflicts and any(p['operation'] == 'derive_base_quantity' and p['decision_status'] == 'enabled' for p in plan['policies']):
        issues.append(make_issue('conversion_conflicts', business_id=business_id, table=name, view=view,
            columns=['__quantity_base'], row_count=len(conversion_conflicts), revision=rev,
            details={'conflicts': list(conversion_conflicts)[:20]},
            description='Conflicting conversion factors are unresolved for some product/unit pairs.'))
    for policy in plan['policies']:
        if policy['operation'] == 'mark_duplicates' and policy['decision_status'] == 'needs_review' and policy.get('potential_duplicates'):
            issues.append(make_issue('potential_duplicates', business_id=business_id, table=name, view=view,
                row_count=policy['potential_duplicates'], revision=rev, evidence_refs=policy['evidence_refs'],
                description='Identical rows kept (insufficient evidence they are export duplicates).'))
        if policy['operation'] == 'fill_forward' and policy['decision_status'] == 'needs_review':
            issues.append(make_issue('fill_forward_needs_review', business_id=business_id, table=name, view=view,
                columns=policy['columns'], revision=rev, details={'preconditions': policy['preconditions']},
                description='Blank group cells may belong to the previous row group but filling was not proven safe.'))
    unresolved = [c['name'] for c in plan['columns']
                  if (c.get('semantic') or {}).get('status') in {'unresolved', 'role_conflict', 'conflict_existing'}]
    if unresolved:
        issues.append(make_issue('column_semantics_unresolved', business_id=business_id, table=name, view=view,
            columns=unresolved, revision=rev, description='Column meaning/role not established by rules or validated proposals.'))
    return issues


def registry_document(business_id, issues, *, plan_revision):
    return {'version': QUALITY_VERSION, 'business_id': business_id, 'plan_revision': plan_revision,
            'issues': sorted(issues, key=lambda i: (i['table'] or '', i['kind'], i['issue_id'])),
            'note': 'Issues located by table/columns/rows. Not listed is not proof of completeness.'}


def _overlaps(window, low, high):
    start = window.get('start');end = window.get('end_exclusive')
    if low is None or high is None:
        return True
    if start is not None and high < start:
        return False
    if end is not None and low >= end:
        return False
    return True


def _window_months(window):
    start = window.get('start');end = window.get('end_exclusive')
    if not start or not end:
        return None
    s = date.fromisoformat(start);e = date.fromisoformat(end)-timedelta(days=1)
    if (e-s).days > 366:
        return None
    months = set();current = s.replace(day=1)
    while current <= e:
        months.add(current.month)
        current = (current.replace(day=28)+timedelta(days=4)).replace(day=1)
    return months


def assess_issue(issue, dependency):
    """Return (impact, reason) for one issue against one table dependency."""
    if issue['view'] != dependency['view']:
        return 'not_affecting', 'different_table'
    ops = set(issue['affected_operations'])
    if issue['columns']:
        used = {c: set(o) for c, o in dependency.get('columns', {}).items() if c in issue['columns']}
        hit = any(o & ops for o in used.values())
    else:
        hit = bool(set(dependency.get('table_operations', [])) & ops)
    if not hit:
        return 'not_affecting', 'issue_columns_or_operations_not_used'
    windows = dependency.get('time_filters') or []
    bounds = issue['known_bounds'].get('time')
    if windows and bounds and not issue['unknown_bounds'].get('time', True):
        if not any(_overlaps(w, bounds.get('min'), bounds.get('max')) for w in windows):
            return 'not_affecting', 'issue_rows_outside_time_window'
    months = issue['known_bounds'].get('months')
    if windows and months and not issue['unknown_bounds'].get('months', True):
        window_months = [_window_months(w) for w in windows]
        if all(m is not None and not (m & set(months)) for m in window_months):
            return 'not_affecting', 'partial_dates_months_outside_window'
    if issue['unknown_bounds'] and any(issue['unknown_bounds'].values()):
        return 'cannot_exclude', 'issue_has_unknown_bounds'
    return 'affects', 'issue_touches_used_columns_or_operations'


def assess(registry, dependencies):
    """Gate a calculation: block when a blocking issue affects or cannot be excluded."""
    items = [];decision = 'pass'
    for issue in registry.get('issues', []):
        for dependency in dependencies:
            impact, reason = assess_issue(issue, dependency)
            if impact == 'not_affecting' and reason == 'different_table':
                continue
            items.append({'issue_id': issue['issue_id'], 'kind': issue['kind'], 'severity': issue['severity'],
                          'view': issue['view'], 'columns': issue['columns'], 'impact': impact, 'reason': reason,
                          'row_count': issue['row_count']})
            if impact != 'not_affecting':
                if issue['severity'] == 'blocking':
                    decision = 'block'
                elif issue['severity'] == 'warning' and decision == 'pass':
                    decision = 'warn'
    return {'decision': decision, 'items': items}
