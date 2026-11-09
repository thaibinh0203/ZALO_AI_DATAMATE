"""Processing policies: proposal -> checks -> decision -> executor (design §6).

Only operations implemented here can run. A policy runs only when its decision is
``enabled``; code decides that from preconditions/check results, never from a grain
label or a model's confidence. Each decision keeps its evidence and check references.
"""
from decimal import Decimal
import hashlib

from ..normalization import NUMERIC, parse_number
from ..parsers import dumps
from ..pipeline import Conversions, name_key, unit_key
from .contracts import stable_id

FILLABLE_ROLES = {'order_id', 'customer_id', 'customer_name', 'date', 'timestamp', 'status', 'store_id', 'text'}


def _pre(condition, passed, detail=None, check_id=None):
    return {'condition': condition, 'status': 'pass' if passed else 'fail', 'detail': detail, 'check_id': check_id}


def policy_record(table, operation, *, columns=(), key_columns=(), mode=None, method, evidence_refs=(),
                  preconditions=(), decision_status, reason, extra=None):
    record = {'policy_id': stable_id('P', table, operation, list(columns), list(key_columns), mode),
              'operation': operation, 'table': table, 'columns': list(columns), 'key_columns': list(key_columns),
              'mode': mode, 'method': method, 'evidence_refs': list(evidence_refs),
              'preconditions': list(preconditions), 'decision_status': decision_status, 'reason': reason}
    if extra:
        record.update(extra)
    return record


def evaluate_fill_forward(table, columns, proposal, check, *, rule_ref=None):
    """Enable only if continuation is observed with no conflicting values.

    ``proposal`` = {columns, key_columns, mode, evidence_refs, method}. Evidence must
    cite the rulebook group-continuation clause or README; checks cite data.
    """
    roles = {c['name']: c['role'] for c in columns}
    fill = proposal['columns'];keys = proposal['key_columns'];result = check['result']
    pre = [_pre('fill_columns_are_group_attributes', all(roles[c] in FILLABLE_ROLES for c in fill),
                {c: roles[c] for c in fill}),
           _pre('key_columns_filled_with_group', proposal['mode'] == 'block' or set(keys) <= set(fill),
                {'key_columns': keys}),
           _pre('continuation_rows_observed', result['continuation_rows'] > 0, result['continuation_rows'], check['check_id']),
           _pre('no_conflicting_values_in_continuation', not result['conflicting_values'], result['conflicting_values'], check['check_id']),
           _pre('continuation_rows_have_line_cells', result['continuation_without_line_cells'] == 0,
                result['continuation_without_line_cells'], check['check_id']),
           _pre('source_or_rule_evidence', bool(proposal['evidence_refs']), proposal['evidence_refs'])]
    failed = [p['condition'] for p in pre if p['status'] == 'fail']
    if result['continuation_rows'] == 0 and not result['explicit_key_blank_cells']:
        status, reason = 'disabled', 'no continuation pattern in data; nothing to fill'
    elif failed:
        status, reason = 'needs_review', 'preconditions failed: '+', '.join(failed)
    else:
        status, reason = 'enabled', 'continuation observed without conflicts; fill raw-empty cells inside group only'
    evidence = list(proposal['evidence_refs'])+([rule_ref] if rule_ref and rule_ref not in proposal['evidence_refs'] else [])
    return policy_record(table, 'fill_forward', columns=fill, key_columns=keys, mode=proposal['mode'],
                         method=proposal['method'], evidence_refs=evidence, preconditions=pre,
                         decision_status=status, reason=reason,
                         extra={'reset_on': ['source_part_change', 'blank_row', 'non_data_or_unknown_row',
                                             'new_explicit_group_key'],
                                'fills_only': 'raw_empty_cells_not_parse_errors'})


def evaluate_duplicates(table, columns, proposal, check, *, rule_ref=None, no_order_rule_ref=None):
    """Same-group identical rows are export duplicates only with rule/README evidence."""
    keys = proposal['key_columns'];roles = {c['name']: c['role'] for c in columns};result = check['result']
    count = result['identical_same_part']+result['identical_cross_part']
    pre = [_pre('group_key_is_order_identifier', bool(keys) and all(roles[k] == 'order_id' for k in keys), keys),
           _pre('rule_or_source_evidence', bool(proposal['evidence_refs'] or rule_ref), proposal['evidence_refs'] or rule_ref),
           _pre('identical_rows_observed', count > 0, count, check['check_id'])]
    if not keys:
        status = 'disabled'
        reason = 'table is not recorded by order: identical rows are separate records'+(f' ({no_order_rule_ref})' if no_order_rule_ref else '')
    elif count == 0:
        status, reason = 'disabled', 'no identical rows within the same order'
    elif all(p['status'] == 'pass' for p in pre):
        status, reason = 'enabled', 'rows identical in every cell within one order counted once; raw rows kept with duplicate_of'
    else:
        status, reason = 'needs_review', 'insufficient evidence; identical rows kept and reported'
    evidence = list(proposal['evidence_refs'])+([rule_ref] if rule_ref and rule_ref not in proposal['evidence_refs'] else [])
    return policy_record(table, 'mark_duplicates', columns=[], key_columns=keys, mode='same_group_identical',
                         method=proposal['method'], evidence_refs=evidence, preconditions=pre,
                         decision_status=status, reason=reason, extra={'potential_duplicates': count})


def evaluate_base_quantity(table, columns, *, conversion_sources, default_rule_ref=None):
    roles = {c['role'] for c in columns}
    quantity = 'quantity' in roles or 'base_quantity' in roles
    product = bool({'product_id', 'product_name'} & roles)
    pre = [_pre('quantity_operand_present', quantity), _pre('product_identifier_present', product),
           _pre('conversion_evidence_present', bool(conversion_sources or default_rule_ref),
                {'sources': conversion_sources, 'default_rule': default_rule_ref})]
    status = 'enabled' if all(p['status'] == 'pass' for p in pre) else 'disabled'
    return policy_record(table, 'derive_base_quantity', columns=[c['name'] for c in columns if c['role'] in {'quantity', 'base_quantity', 'unit'}],
                         method='code_rule', evidence_refs=[r for r in [default_rule_ref] if r],
                         preconditions=pre, decision_status=status,
                         reason='derived __quantity_base from scoped conversions; conflicts stay unresolved' if status == 'enabled'
                         else 'missing operands or conversion evidence')


class ConversionRegistry(Conversions):
    """v4 conversion registry (store/product/unit scoped, conflicts -> None)."""


def build_fill_state():
    return {'group': None, 'head': {}, 'head_temporal': {}, 'head_row': None, 'part': None}


def apply_fill_forward(policy, columns, record, typed, quality, state, kind, temporal=None):
    """Fill raw-empty cells inside the current group. Mutates typed/quality/temporal.

    Parse errors are never "repaired" from a previous row, and the group state resets
    at source-part changes, blank rows, any non-data/unknown row and new group keys.
    """
    temporal = temporal if temporal is not None else {}
    index = {c['name']: i for i, c in enumerate(columns)}
    fill = [index[c] for c in policy['columns']];keys = [index[c] for c in policy['key_columns']]
    raw = record['raw']
    if record['part'] != state['part'] or record.get('after_blank') or kind != 'data':
        part = record['part'];state.update(build_fill_state());state['part'] = part
        if kind != 'data':
            return

    def empty(value):
        return value is None or isinstance(value, str) and not value.strip()

    def remember(i):
        if typed[i] is not None and i not in state['head']:
            state['head'][i] = typed[i]
            if i in temporal:
                state['head_temporal'][i] = temporal[i]

    def fill_cell(i):
        if typed[i] is None and empty(raw[i]) and i in state['head']:
            typed[i] = state['head'][i]
            if i in state['head_temporal']:
                temporal[i] = {**state['head_temporal'][i], 'filled': True}
            quality['notes'][columns[i]['name']] = {'status': 'filled_within_group', 'policy_id': policy['policy_id'],
                                                   'source_row_id': state['head_row']}
    if policy['mode'] == 'key':
        if all(not empty(raw[i]) for i in keys):
            group = tuple(str(raw[i]).strip() for i in keys)
            if group != state['group']:
                state.update(group=group, head={}, head_temporal={}, head_row=record['row_id'])
            for i in fill:
                remember(i)
                fill_cell(i)
            return
        if state['group'] is None:
            return
    else:
        anchor = fill[0]
        if not empty(raw[anchor]):
            state.update(group=('block', record['row_id']), head={}, head_temporal={}, head_row=record['row_id'])
            for i in fill:
                remember(i)
            return
        if state['group'] is None:
            return
    for i in fill:
        fill_cell(i)


def duplicate_signature(policy, columns, record, typed):
    index = {c['name']: i for i, c in enumerate(columns)}
    group = [typed[index[k]] for k in policy['key_columns']]
    if any(v is None for v in group):
        return None
    store = [typed[i] for i, c in enumerate(columns) if c['role'] == 'store_id']
    return hashlib.sha256(dumps([store, group, record['raw']]).encode()).hexdigest()


def derive_base_quantity(columns, typed, quality, conversions, *, default_rule):
    """Port of the v4 derived base quantity with explicit audit notes.

    Returns (quantity_base or None, unit_base or None)."""
    def single(role):
        values = [typed[i] for i, c in enumerate(columns) if c['role'] == role and typed[i] is not None]
        return values[0] if len(values) == 1 else None
    quantity = single('quantity');existing = single('base_quantity');unit = single('unit')
    extracted = [n.get('unit') for n in quality['notes'].values() if isinstance(n, dict) and n.get('unit')]
    if not unit and len(set(extracted)) == 1:
        unit = extracted[0]
    if unit and extracted and any(unit_key(u) != unit_key(unit) for u in extracted):
        quality['errors']['__quantity_base'] = 'quantity_unit_conflict'
        return None, None
    store = single('store_id')
    products = [p for p in [single('product_id'), single('product_name')] if p]
    conversion = conversions.resolve(products, unit, store) if unit else None
    if conversion is None and unit and default_rule and conversions.default_base_is_consistent(products, unit, store):
        conversion = ('1', unit)
        quality['notes']['unit_default_rule'] = {'status': 'factor_1_from_current_rulebook'}
    as_decimal = lambda v: v if isinstance(v, Decimal) else parse_number(v)  # noqa: E731
    derived = as_decimal(quantity)*Decimal(conversion[0]) if quantity is not None and conversion else None
    base = None;unit_base = conversion[1] if conversion else None
    if existing is not None:
        base = as_decimal(existing)
        if unit and quantity is not None and as_decimal(quantity) != 0 and as_decimal(existing) == as_decimal(quantity):
            unit_base = unit
        if derived is not None and as_decimal(existing) != derived:
            quality['errors']['__quantity_base'] = 'supplied_and_computed_base_quantity_disagree'
    elif derived is not None:
        base = derived
    if quantity is not None and as_decimal(quantity) < 0:
        quality['notes']['negative_quantity_preserved'] = True
    return base, unit_base


def conversions_from_tables(conversions, tables_typed):
    """Register conversions from unit-conversion rows: (columns, typed rows, source)."""
    for columns, rows, source in tables_typed:
        roles = {}
        for i, c in enumerate(columns):
            roles.setdefault(c['role'], []).append(i)
        if 'conversion_factor' not in roles:
            continue
        for typed in rows:
            def one(role):
                values = [typed[i] for i in roles.get(role, []) if typed[i] is not None]
                return values[0] if len(values) == 1 else None
            factor = one('conversion_factor');unit = one('unit');base = one('base_unit')
            if factor is None or not unit:
                continue
            factor = factor if isinstance(factor, Decimal) else parse_number(factor)
            if factor <= 0:
                continue
            for product in (one('product_id'), one('product_name')):
                if product:
                    if base:
                        conversions.add(product, unit, factor, base, source, one('store_id'))
                    else:
                        conversions.pending.append((product, unit, factor, source, one('store_id')))
            if one('base_unit'):
                for product in (one('product_id'), one('product_name')):
                    if product:
                        conversions.bases.setdefault((one('store_id'), name_key(product)), set()).add(unit_key(one('base_unit')))
    for product, unit, factor, source, store in conversions.pending:
        bases = conversions.bases.get((store, name_key(product)), set())
        if len(bases) == 1:
            conversions.add(product, unit, factor, next(iter(bases)), source, store)
    conversions.pending = []
    return conversions


__all__ = ['evaluate_fill_forward', 'evaluate_duplicates', 'evaluate_base_quantity', 'apply_fill_forward',
           'duplicate_signature', 'derive_base_quantity', 'conversions_from_tables', 'build_fill_state',
           'ConversionRegistry', 'policy_record', 'NUMERIC']
