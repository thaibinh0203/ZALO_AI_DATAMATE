"""Status mappings scoped by business/table/column/domain (design §8).

A dictionary entry is applied only inside its own domain: "Đã giao" of a delivery
event becomes ``delivered``; it is not evidence that an order is ``completed``.
Values without an in-domain mapping stay raw and are reported as unresolved.
"""
from ..normalization import STATUSES, key
from . import STATUS_MAPPING_VERSION
from .contracts import STATUS_DOMAINS, nfc
from .temporal import fold

DICTIONARIES = {
    # Order dictionary keeps the v4 order-context mappings whose target is one of the
    # rulebook's four order states.
    'order': {k: v for k, v in STATUSES.items() if v in STATUS_DOMAINS['order']},
    'delivery': {'dagiao': 'delivered', 'giaothanhcong': 'delivered', 'dagiaohang': 'delivered',
                 'hoanthanh': 'delivered', 'danggiao': 'delivering', 'danggiaohang': 'delivering',
                 'dangvanchuyen': 'delivering', 'chogiao': 'pending', 'choxuly': 'pending',
                 'giaothatbai': 'failed', 'khonggiaoduoc': 'failed', 'huy': 'cancelled', 'dahuy': 'cancelled',
                 'trahang': 'returned', 'hoantra': 'returned', 'datrahang': 'returned'},
    'purchase': {'danhaphang': 'received', 'danhap': 'received', 'nhapthanhcong': 'received',
                 'hoanthanh': 'received', 'chonhap': 'pending', 'huy': 'cancelled', 'dahuy': 'cancelled'},
    'payment': {'dathanhtoan': 'paid', 'chuathanhtoan': 'pending', 'huy': 'cancelled', 'dahuy': 'cancelled',
                'hoantien': 'refunded'},
    'product': {'danghoatdong': 'active', 'dangkinhdoanh': 'active', 'ngungkinhdoanh': 'inactive',
                'ngunghoatdong': 'inactive'},
    'customer': {'danghoatdong': 'active', 'ngunghoatdong': 'inactive'},
}
GRAIN_DOMAINS = {'order': 'order', 'order_line': 'order', 'delivery': 'delivery', 'delivery_item': 'delivery',
                 'purchase': 'purchase', 'purchase_line': 'purchase', 'payment': 'payment',
                 'product': 'product', 'customer': 'customer'}
ROLE_DOMAINS = {'sale_lines': 'order', 'orders_or_order_extension': 'order', 'purchase_lines': 'purchase',
                'payments': 'payment', 'products_or_product_extension': 'product'}


def infer_domain(column, *, grain_kind=None, role_hint=None, proposed=None):
    """Return (domain or None, basis). A model-proposed domain must agree with structure."""
    name = fold(column['original_name']+' '+(column.get('description') or ''))
    structural = None;basis = None
    if 'giao' in name.split() or 'giao hang' in name:
        structural, basis = 'delivery', 'column_name_mentions_delivery'
    elif grain_kind in GRAIN_DOMAINS:
        structural, basis = GRAIN_DOMAINS[grain_kind], 'table_grain_'+grain_kind
    elif role_hint in ROLE_DOMAINS:
        structural, basis = ROLE_DOMAINS[role_hint], 'table_role_hint_'+role_hint
    if proposed:
        if structural and proposed != structural:
            return None, f'model_domain_{proposed}_conflicts_with_{basis}'
        return proposed, 'model_proposal'+('_consistent_with_'+basis if basis else '')
    return structural, basis or 'no_domain_evidence'


def build_mapping(table, column, domain, basis, vocabulary, *, reviewed=None, evidence_refs=()):
    """Combine in-domain dictionary entries and validated model mappings."""
    record = {'version': STATUS_MAPPING_VERSION, 'table': table, 'column': column, 'domain': domain,
              'domain_basis': basis, 'domain_evidence_refs': list(evidence_refs),
              'vocabulary': {k: vocabulary['result'][k] for k in ('non_empty', 'truncated', 'cap')}
              | {'distinct_observed': len(vocabulary['result']['values']), 'check_id': vocabulary['check_id']},
              'mappings': {}, 'unresolved': []}
    if domain is None:
        record['unresolved'] = [v for v, _ in vocabulary['result']['values']]
        record['status'] = 'needs_review'
        return record
    dictionary = DICTIONARIES.get(domain, {})
    for value, count in vocabulary['result']['values']:
        target = dictionary.get(key(value))
        if target:
            record['mappings'][value] = {'target': target, 'method': 'dictionary_in_domain', 'rows': count,
                                         'evidence_refs': []}
        elif reviewed and value in reviewed:
            record['mappings'][value] = {**reviewed[value], 'rows': count}
        else:
            record['unresolved'].append(value)
    record['status'] = 'enabled'
    return record


def unknown_values(vocabulary, domain):
    dictionary = DICTIONARIES.get(domain, {})
    return [v for v, _ in vocabulary['result']['values'] if key(v) not in dictionary]


def apply_mapping(value, mapping):
    """Return (typed value, note). Unmapped values stay raw (never guessed)."""
    if value is None:
        return None, None
    if mapping is None or mapping.get('status') != 'enabled':
        return value, 'status_mapping_not_enabled'
    entry = mapping['mappings'].get(nfc(value))
    if entry is None:
        return value, 'unmapped_status_preserved'
    return entry['target'], None
