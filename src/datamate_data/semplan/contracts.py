"""Vocabularies, stable IDs, evidence references and model-output validators.

Model outputs are proposals. Validators here only check contracts and references;
data checks (``checks.py``) and policy gates (``policies.py``) decide what is used.
"""
import hashlib
import re
import unicodedata

from ..normalization import key
from . import PLAN_SCHEMA_VERSION

ROW_KINDS = ('data', 'subtotal', 'grand_total', 'repeated_header', 'note', 'metadata',
             'blank', 'quarantine', 'unknown')
REVIEWABLE_ROW_KINDS = ('data', 'subtotal', 'grand_total', 'repeated_header', 'note',
                        'metadata', 'unknown')
ROW_DECISION_STATUS = ('decided_rule', 'candidate', 'decided_model_validated', 'needs_review')
GRAIN_KINDS = ('order', 'order_line', 'product', 'customer', 'supplier', 'employee', 'store',
               'delivery', 'delivery_item', 'payment', 'purchase', 'purchase_line', 'lot',
               'stock_snapshot', 'stock_movement', 'price_change', 'unit_conversion',
               'expense', 'ledger_entry', 'other', 'unknown')
AGGREGATIONS = ('sum', 'sum_once_per_group', 'weighted_average', 'average',
                'snapshot_latest', 'min_max', 'none', 'unknown')
STANDARD_SCOPES = ('row', 'order', 'order_line', 'product', 'customer', 'lot', 'delivery',
                   'day', 'snapshot', 'unknown')
TIME_SEMANTICS = ('event', 'snapshot', 'validity_start', 'expiry', 'none', 'unknown')
POLICY_OPERATIONS = ('fill_forward', 'mark_duplicates', 'derive_base_quantity',
                     'apply_status_mapping', 'resolve_source_year')
MODEL_POLICY_OPERATIONS = ('fill_forward', 'mark_duplicates')
POLICY_STATUS = ('proposed', 'enabled', 'disabled', 'needs_review')
# Target vocabularies are scoped by the object carrying the status. The order
# vocabulary is the rulebook's four order states; other domains never borrow it.
STATUS_DOMAINS = {
    'order': ('completed', 'cancelled', 'returned', 'delivering'),
    'delivery': ('delivered', 'delivering', 'failed', 'cancelled', 'returned', 'pending'),
    'purchase': ('received', 'pending', 'cancelled'),
    'payment': ('paid', 'pending', 'cancelled', 'refunded'),
    'product': ('active', 'inactive'),
    'customer': ('active', 'inactive'),
}
SNAKE = re.compile(r'[a-z][a-z0-9_]{1,63}')
# Rulebook clauses that code may cite for policies, located by accent-folded text.
RULE_SIGNATURES = {
    'group_blank_cells_continue': ('detrongocacdongsaucuacungmotnhomdong',),
    'same_order_identical_rows_once': ('haidongcuacungmotdongiongnhauomoio',),
    'non_order_rows_counted': ('bangkhongghitheodon',),
    'missing_year_from_sheet': ('ngaykhongghinam', 'namcuasheet'),
    'order_status_vocabulary': ('trangthaidon', 'moidoncodungmottrongbontrangthai'),
    'base_unit_default_factor_1': ('donvikhongcodongquydoinaoladonvicobanheso1',),
    'outside_coverage': ('kynamhoantoanngoaikhoangngaycodulieu',),
    'records_after_today_not_happened': ('filecothecobanghingaysauhomnay',),
}


class ContractError(ValueError):
    """Model output does not follow the requested contract (retryable once)."""


def canonical(value):
    """Key-order independent JSON text for content hashes."""
    import json
    from ..parsers import json_value
    return json.dumps(value, ensure_ascii=False, default=json_value, allow_nan=False, sort_keys=True, separators=(',', ':'))


def stable_id(prefix, *parts):
    return prefix + hashlib.sha256(canonical(list(parts)).encode()).hexdigest()[:12]


def object_sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def nfc(value):
    return unicodedata.normalize('NFC', str(value)).strip()


def readme_units(readme):
    return [{'id': f'R{n}', 'source': 'readme', 'quote': line.strip(), 'location': f'README.md:{n}'}
            for n, line in enumerate(readme.splitlines(), 1) if line.strip()]


def rule_units(rulebook):
    """Split the rulebook into citable units: each table data row or paragraph.

    IDs are positional (B1..Bn) over the pinned rulebook hash, so they are stable for
    a given rulebook. The quote is verbatim source text; the heading is context only.
    """
    lines = rulebook.splitlines();units = [];heading = '';paragraph = []
    separator = re.compile(r'^\|[\s:|-]+\|\s*$')

    def flush():
        if paragraph:
            text = ' '.join(x.strip() for x in paragraph)
            units.append({'quote': text, 'context': heading})
            paragraph.clear()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('#'):
            flush();heading = stripped.lstrip('#').strip();continue
        if stripped.startswith('|'):
            flush()
            nxt = lines[index+1].strip() if index+1 < len(lines) else ''
            if separator.match(stripped) or separator.match(nxt):
                continue  # separator or table header row
            units.append({'quote': stripped, 'context': heading});continue
        if not stripped:
            flush();continue
        paragraph.append(line)
    flush()
    return [{'id': f'B{i}', 'source': 'rulebook', 'quote': u['quote'], 'context': u['context'],
             'location': f'rulebook.md:unit{i}'} for i, u in enumerate(units, 1)]


def detect_rules(units):
    """Map known rule signatures to unit IDs (code-citable rulebook evidence)."""
    found = {}
    for unit in units:
        folded = key(unit['quote'])
        for name, needles in RULE_SIGNATURES.items():
            if name not in found and all(n in folded for n in needles[:1]) and (
                    len(needles) == 1 or any(n in folded for n in needles[1:])):
                found[name] = unit['id']
    return found


class EvidenceCatalog:
    """Citable evidence: README lines, rulebook units, headers, source rows, checks."""

    def __init__(self, entries=()):
        self.entries = {}
        for entry in entries:
            self.add(**entry)

    @classmethod
    def from_sources(cls, readme, rulebook):
        return cls(readme_units(readme)+rule_units(rulebook))

    def add(self, id, source, quote, location=None, context=None):
        if not isinstance(id, str) or not id:
            raise ValueError('Evidence id required')
        self.entries[id] = {'id': id, 'source': source, 'quote': quote, 'location': location,
                            'context': context}
        return id

    def has(self, ref):
        return isinstance(ref, str) and ref in self.entries

    def get(self, ref):
        return self.entries[ref]

    def view(self, ids=None, sources=None):
        items = [e for e in self.entries.values()
                 if (ids is None or e['id'] in ids) and (sources is None or e['source'] in sources)]
        return [[e['id'], e['quote']] if not e.get('context') else [e['id'], e['context'], e['quote']]
                for e in items]

    def check_refs(self, refs, *, minimum=1, sources=None):
        """Return valid refs; raise ContractError if refs are missing or unknown."""
        if isinstance(refs, str):
            refs = [refs]
        if not isinstance(refs, list) or len(refs) < minimum:
            raise ContractError('evidence must be a list of catalog IDs')
        for ref in refs:
            if not self.has(ref):
                raise ContractError(f'unknown evidence reference: {ref}')
            if sources and self.entries[ref]['source'] not in sources:
                raise ContractError(f'evidence {ref} must come from {sorted(sources)}')
        return list(dict.fromkeys(refs))


def _columns(table):
    return {c['name']: c for c in table['columns']}


def _column_list(items, allowed, *, minimum=1, maximum=4):
    return (isinstance(items, list) and minimum <= len(items) <= maximum and len(set(items)) == len(items)
            and all(isinstance(c, str) and c in allowed for c in items))


def validate_table_plan(value, table, catalog, tables):
    """Validate a table plan proposal. Raise only for top-level contract errors.

    Invalid items are dropped and reported in ``rejected`` instead of being repaired
    silently; checks/refinement may revisit them.
    """
    keys = {'grain', 'group_keys', 'measures', 'joins', 'status_columns', 'policies'}
    if not isinstance(value, dict) or set(value) != keys:
        raise ContractError(f'expected exactly keys {sorted(keys)}')
    columns = _columns(table);allowed = set(columns) | {'__quantity_base'}
    rejected = []
    g = value['grain']
    if not isinstance(g, dict) or set(g) != {'kind', 'semantic_kind', 'description', 'identity_columns', 'evidence'}:
        raise ContractError('grain requires kind, semantic_kind, description, identity_columns, evidence')
    if g['kind'] not in GRAIN_KINDS:
        raise ContractError(f'grain.kind must be one of {GRAIN_KINDS}')
    if not isinstance(g['description'], str):
        raise ContractError('grain.description must be a string')
    grain = {'kind': g['kind'], 'semantic_kind': None, 'description': g['description'].strip(),
             'identity_columns': [], 'evidence_refs': [], 'status': 'candidate'}
    if g['kind'] == 'other':
        if not isinstance(g['semantic_kind'], str) or not SNAKE.fullmatch(g['semantic_kind']) or g['semantic_kind'] in {'unknown', 'other', 'none'}:
            raise ContractError('grain kind=other needs a specific snake_case semantic_kind')
        if not grain['description']:
            raise ContractError('grain kind=other needs a description')
        grain['semantic_kind'] = g['semantic_kind']
    elif g['semantic_kind'] is not None:
        rejected.append({'item': 'grain.semantic_kind', 'reason': 'semantic_kind only with kind=other'})
    if g['identity_columns'] not in ([], None):
        if _column_list(g['identity_columns'], allowed):
            grain['identity_columns'] = g['identity_columns']
        else:
            rejected.append({'item': 'grain.identity_columns', 'reason': 'unknown or invalid columns'})
    try:
        grain['evidence_refs'] = catalog.check_refs(g['evidence'], minimum=0 if g['kind'] == 'unknown' else 1)
    except ContractError as exc:
        if g['kind'] != 'unknown':
            raise
        rejected.append({'item': 'grain.evidence', 'reason': str(exc)})
    out = {'grain': grain, 'group_keys': [], 'measures': [], 'joins': [], 'status_columns': [],
           'policies': [], 'rejected': rejected}
    for name, cap in (('group_keys', 4), ('measures', 16), ('joins', 4), ('status_columns', 4), ('policies', 4)):
        if not isinstance(value[name], list) or len(value[name]) > cap:
            raise ContractError(f'{name} must be a list with at most {cap} items')
    names = set()
    for k in value['group_keys']:
        if not isinstance(k, dict) or set(k) != {'name', 'columns', 'purpose', 'evidence'}:
            raise ContractError('group_keys items need name, columns, purpose, evidence')
        if not isinstance(k['name'], str) or not SNAKE.fullmatch(k['name']) or k['name'] in names or not _column_list(k['columns'], allowed):
            rejected.append({'item': 'group_key', 'value': k, 'reason': 'invalid name or columns'});continue
        try:
            refs = catalog.check_refs(k['evidence'], minimum=0)
        except ContractError as exc:
            rejected.append({'item': 'group_key', 'value': k, 'reason': str(exc)});continue
        names.add(k['name'])
        out['group_keys'].append({'name': k['name'], 'columns': k['columns'], 'purpose': str(k['purpose'] or ''),
                                  'evidence_refs': refs, 'status': 'candidate'})
    seen = set()
    for m in value['measures']:
        needed = {'column', 'scope', 'aggregation', 'unit', 'time_semantics', 'weights_column', 'evidence'}
        if not isinstance(m, dict) or set(m) != needed:
            raise ContractError(f'measures items need exactly {sorted(needed)}')
        col = m['column']
        if col not in allowed or col in seen:
            rejected.append({'item': 'measure', 'value': m, 'reason': 'unknown or duplicate column'});continue
        if col != '__quantity_base' and columns[col]['sql_type'] != 'NUMERIC':
            rejected.append({'item': 'measure', 'value': m, 'reason': 'measure column is not NUMERIC'});continue
        scope = m['scope']
        if scope not in STANDARD_SCOPES and scope not in names:
            rejected.append({'item': 'measure', 'value': m, 'reason': 'scope must be standard or a declared group key'});continue
        if m['aggregation'] not in AGGREGATIONS or m['time_semantics'] not in TIME_SEMANTICS:
            rejected.append({'item': 'measure', 'value': m, 'reason': 'unsupported aggregation/time_semantics'});continue
        if m['unit'] is not None and not isinstance(m['unit'], str):
            rejected.append({'item': 'measure', 'value': m, 'reason': 'unit must be string or null'});continue
        role = columns[col]['role'] if col in columns else 'base_quantity'
        aggregation = m['aggregation'];checks = []
        if role in {'unit_price', 'unit_cogs'} and aggregation in {'sum', 'sum_once_per_group'}:
            aggregation = 'none';checks.append('price_is_not_additive:aggregation_set_none')
        if aggregation == 'sum_once_per_group' and scope not in names and scope in {'row', 'unknown'}:
            rejected.append({'item': 'measure', 'value': m, 'reason': 'sum_once_per_group requires a group scope'});continue
        if aggregation == 'weighted_average' and (m['weights_column'] not in allowed or m['weights_column'] == col):
            rejected.append({'item': 'measure', 'value': m, 'reason': 'weighted_average requires another numeric weights_column'});continue
        if aggregation == 'snapshot_latest' and m['time_semantics'] != 'snapshot':
            checks.append('snapshot_aggregation_without_snapshot_time')
        try:
            refs = catalog.check_refs(m['evidence'], minimum=0)
        except ContractError as exc:
            rejected.append({'item': 'measure', 'value': m, 'reason': str(exc)});continue
        seen.add(col)
        out['measures'].append({'column': col, 'measure_scope': scope, 'aggregation': aggregation,
            'required_group_keys': [scope] if scope in names else [], 'unit': m['unit'],
            'time_semantics': m['time_semantics'], 'weights_column': m['weights_column'] if aggregation == 'weighted_average' else None,
            'preconditions': [], 'evidence_refs': refs, 'validation_notes': checks, 'status': 'candidate'})
    for j in value['joins']:
        if not isinstance(j, dict) or set(j) != {'target', 'source_columns', 'target_columns', 'purpose', 'evidence'}:
            raise ContractError('joins items need target, source_columns, target_columns, purpose, evidence')
        target = tables.get(j['target'])
        if target is None or j['target'] == table['name']:
            rejected.append({'item': 'join', 'value': j, 'reason': 'unknown or self target'});continue
        if not _column_list(j['source_columns'], allowed) or not _column_list(j['target_columns'], set(_columns(target))) or len(j['source_columns']) != len(j['target_columns']):
            rejected.append({'item': 'join', 'value': j, 'reason': 'invalid key columns'});continue
        try:
            refs = catalog.check_refs(j['evidence'], minimum=1)
        except ContractError as exc:
            rejected.append({'item': 'join', 'value': j, 'reason': str(exc)});continue
        out['joins'].append({'target': j['target'], 'source_columns': j['source_columns'],
            'target_columns': j['target_columns'], 'purpose': str(j['purpose'] or ''),
            'evidence_refs': refs, 'status': 'candidate', 'business_verified': False})
    for s in value['status_columns']:
        if not isinstance(s, dict) or set(s) != {'column', 'domain', 'evidence'}:
            raise ContractError('status_columns items need column, domain, evidence')
        if s['column'] not in columns or s['domain'] not in STATUS_DOMAINS:
            rejected.append({'item': 'status_column', 'value': s, 'reason': 'unknown column or domain'});continue
        try:
            refs = catalog.check_refs(s['evidence'], minimum=1)
        except ContractError as exc:
            rejected.append({'item': 'status_column', 'value': s, 'reason': str(exc)});continue
        out['status_columns'].append({'column': s['column'], 'domain': s['domain'], 'evidence_refs': refs})
    for p in value['policies']:
        if not isinstance(p, dict) or set(p) != {'operation', 'columns', 'key_columns', 'mode', 'evidence'}:
            raise ContractError('policies items need operation, columns, key_columns, mode, evidence')
        if p['operation'] not in MODEL_POLICY_OPERATIONS:
            rejected.append({'item': 'policy', 'value': p, 'reason': 'operation not in model allowlist'});continue
        if p['mode'] not in {'key', 'block', 'same_group_identical'}:
            rejected.append({'item': 'policy', 'value': p, 'reason': 'unsupported mode'});continue
        if not _column_list(p['columns'], set(columns), maximum=8) or not (
                p['key_columns'] == [] or _column_list(p['key_columns'], set(columns))):
            rejected.append({'item': 'policy', 'value': p, 'reason': 'invalid columns'});continue
        try:
            refs = catalog.check_refs(p['evidence'], minimum=1)
        except ContractError as exc:
            rejected.append({'item': 'policy', 'value': p, 'reason': str(exc)});continue
        out['policies'].append({'operation': p['operation'], 'columns': p['columns'], 'key_columns': p['key_columns'],
                                'mode': p['mode'], 'evidence_refs': refs, 'method': 'model_proposal'})
    return out


def validate_row_review(value, groups, catalog):
    """Validate group-level row decisions. Exceptions may only name sampled rows."""
    if not isinstance(value, dict) or set(value) != {'groups'} or not isinstance(value['groups'], list):
        raise ContractError('expected {"groups": [...]}')
    by_id = {g['group_id']: g for g in groups};decided = {};rejected = []
    for item in value['groups']:
        if not isinstance(item, dict) or set(item) != {'group', 'kind', 'exceptions', 'evidence'}:
            raise ContractError('group decisions need group, kind, exceptions, evidence')
        group = by_id.get(item['group'])
        if group is None or item['group'] in decided:
            rejected.append({'value': item, 'reason': 'unknown or duplicate group'});continue
        if item['kind'] not in REVIEWABLE_ROW_KINDS:
            rejected.append({'value': item, 'reason': 'unsupported kind'});continue
        sampled = {s['ref']: s['row_id'] for s in group['samples']}
        exceptions = {};ok = True
        if not isinstance(item['exceptions'], list):
            raise ContractError('exceptions must be a list')
        for exc in item['exceptions']:
            if not isinstance(exc, dict) or set(exc) != {'row', 'kind'} or exc['row'] not in sampled or exc['kind'] not in REVIEWABLE_ROW_KINDS:
                ok = False;break
            exceptions[sampled[exc['row']]] = exc['kind']
        if not ok:
            rejected.append({'value': item, 'reason': 'exceptions must name sampled rows and valid kinds'});continue
        try:
            refs = catalog.check_refs(item['evidence'], minimum=1)
        except ContractError as exc:
            rejected.append({'value': item, 'reason': str(exc)});continue
        decided[item['group']] = {'kind': item['kind'], 'exceptions': exceptions, 'evidence_refs': refs}
    return decided, rejected


def validate_status_review(value, unknown_values, domain, catalog):
    if not isinstance(value, dict) or set(value) != {'mappings', 'unresolved'}:
        raise ContractError('expected {"mappings": [...], "unresolved": [...]}')
    if not isinstance(value['mappings'], list) or not isinstance(value['unresolved'], list):
        raise ContractError('mappings and unresolved must be lists')
    vocab = STATUS_DOMAINS[domain];known = {nfc(v): v for v in unknown_values}
    accepted = {};rejected = []
    for item in value['mappings']:
        if not isinstance(item, dict) or set(item) != {'value', 'target', 'evidence'}:
            raise ContractError('mapping items need value, target, evidence')
        raw = known.get(nfc(item['value'])) if isinstance(item['value'], str) else None
        if raw is None or raw in accepted:
            rejected.append({'value': item, 'reason': 'value not in reviewed vocabulary or duplicate'});continue
        if item['target'] not in vocab:
            rejected.append({'value': item, 'reason': f'target outside {domain} vocabulary'});continue
        try:
            refs = catalog.check_refs(item['evidence'], minimum=1, sources={'readme', 'rulebook'})
        except ContractError as exc:
            rejected.append({'value': item, 'reason': str(exc)});continue
        accepted[raw] = {'target': item['target'], 'evidence_refs': refs, 'method': 'model_validated'}
    return accepted, rejected


def validate_plan_document(plan):
    """Light structural check of a persisted plan before it is used or published."""
    required = {'schema_version', 'business_id', 'plan_revision', 'status', 'sources', 'tables', 'code'}
    if not isinstance(plan, dict) or not required <= set(plan):
        raise ValueError('Semantic plan missing required fields')
    if plan['schema_version'] != PLAN_SCHEMA_VERSION:
        raise ValueError('Semantic plan schema version mismatch')
    if plan['status'] not in {'draft', 'frozen'}:
        raise ValueError('Semantic plan status must be draft or frozen')
    for name, table in plan['tables'].items():
        for field in ('columns', 'grain', 'group_keys', 'measures', 'joins', 'policies', 'row_decisions', 'revision'):
            if field not in table:
                raise ValueError(f'Semantic plan table {name} missing {field}')
        if table['grain']['kind'] not in GRAIN_KINDS:
            raise ValueError('Invalid grain kind in plan')
        for policy in table['policies']:
            if policy['operation'] not in POLICY_OPERATIONS or policy['decision_status'] not in POLICY_STATUS:
                raise ValueError('Invalid policy in plan')
    return plan
