"""Context adapter over a published semantic-plan business (manifest-pinned).

Refuses drafts, prepared-only or audit-failed outputs, mismatched sources and any
artifact changed after publication. The context is pinned at run start; each question
re-checks the manifest hash so metadata never changes between questions.
"""
import hashlib
import json
from pathlib import Path
import re

from datamate_data.semplan.contracts import STATUS_DOMAINS, EvidenceCatalog, detect_rules
from datamate_data.semplan.publish import load_manifest

PROMPT_VERSION = 'calcplan-context-v1'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _meaning(column):
    proposal = (column.get('semantic') or {}).get('normalized_proposal') or (column.get('semantic') or {}).get('proposed') or {}
    return column.get('description') or proposal.get('meaning')


class PublishedContextLoader:
    def __init__(self, prepared_root, source_root):
        self.prepared_root = Path(prepared_root).resolve();self.source_root = Path(source_root).resolve()
        self.rules_text = (self.source_root/'rulebook.md').read_text(encoding='utf-8')
        self.cache = {};self.pins = {}

    def _source_hashes(self, business_id):
        paths = {'README.md': self.source_root/'data'/business_id/'README.md',
                 'rulebook.md': self.source_root/'rulebook.md', 'calendar.csv': self.source_root/'calendar.csv'}
        return {name: sha(p) for name, p in paths.items() if p.exists()}

    def load(self, business_id):
        if not isinstance(business_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', business_id):
            raise ValueError('Invalid business_id')
        if business_id in self.cache:
            return self.cache[business_id]
        root = (self.prepared_root/business_id).resolve()
        if root.parent != self.prepared_root:
            raise ValueError('Business path escapes prepared root')
        manifest = load_manifest(root)
        if manifest['status'] != 'published' or not manifest.get('usable_by_baseline'):
            raise ValueError(f'Business output is not published (status={manifest["status"]})')
        if manifest['business_id'] != business_id:
            raise ValueError('Manifest business mismatch')
        sources = {s['name']: s['sha256'] for s in manifest['sources']}
        for name, digest in self._source_hashes(business_id).items():
            if sources.get(name) != digest:
                raise ValueError(f'Source {name} differs from the published snapshot')
        plan = json.loads((root/'semantic_plan.json').read_text(encoding='utf-8'))
        pg = json.loads((root/'postgres_catalog.json').read_text(encoding='utf-8'))
        registry = json.loads((root/'quality_registry.json').read_text(encoding='utf-8'))
        if plan['plan_revision'] != manifest['semantic_revision'] or registry['plan_revision'] != plan['plan_revision']:
            raise ValueError('Plan/quality/manifest revision mismatch')
        readme = (self.source_root/'data'/business_id/'README.md').read_text(encoding='utf-8')
        evidence = EvidenceCatalog.from_sources(readme, self.rules_text)
        tables = {};allowed = {};measures = {};views = {}
        for name, meta in pg['tables'].items():
            view = meta['clean_view'];views[name] = view;tp = plan['tables'][name]
            allowed[view] = {n: t for n, t in meta['exposed_columns']}
            by_name = {c['name']: c for c in tp['columns']}
            columns = []
            for col_name, typ in meta['exposed_columns']:
                c = by_name.get(col_name)
                if c is not None:
                    note = None
                    if c['name'] in meta['temporal_columns']:
                        note = 'full dates only; partial source dates in __y/__m/__d, precision __p, resolution __r'
                    if c['name'] in meta['status_columns']:
                        mapping = tp['status_mappings'].get(c['name'], {})
                        vocab = list(STATUS_DOMAINS.get(mapping.get('domain'), ()))
                        observed = sorted({m['target'] for m in mapping.get('mappings', {}).values()})
                        note = (f"status domain={mapping.get('domain')}: filter with canonical labels {vocab} "
                                f"(observed {observed}); {len(mapping.get('unresolved', []))} unmapped raw values kept as-is; "
                                f"raw text in {c['name']}__raw")
                    columns.append({'name': col_name, 'source': c['original_name'], 'type': typ,
                                    'canonical_role': c.get('canonical_role'), 'semantic_role': c.get('semantic_role'),
                                    'meaning': _meaning(c), 'has_data': c.get('has_data'), 'note': note,
                                    'profile': meta['profiles'].get(col_name)})
                else:
                    columns.append({'name': col_name, 'source': None, 'type': typ, 'canonical_role': None,
                                    'semantic_role': 'derived_companion', 'meaning': _companion_meaning(col_name),
                                    'has_data': None, 'note': None, 'profile': meta['profiles'].get(col_name)})
            keys = {k['name']: k['columns'] for k in tp['group_keys']}
            for m in tp['measures']:
                measures[(view, m['column'])] = {**m, 'group_columns': keys.get(m['measure_scope'], [])}
            tables[view] = {'view': view, 'table': name, 'grain': tp['grain'], 'row_identity': tp['row_identity'],
                            'group_keys': tp['group_keys'], 'measures': tp['measures'],
                            'joins': [{**j, 'target_view': None} for j in tp['joins']],
                            'status_mappings': tp['status_mappings'], 'columns': columns,
                            'clean_rows': next(iter(meta['profiles'].values()), {}).get('clean_rows'),
                            'date_bounds': meta.get('date_bounds', {})}
        for view, info in tables.items():
            for j in info['joins']:
                j['target_view'] = views.get(j['target'])
        relationships = []
        for r in pg.get('relationships', []):
            relationships.append({'left': views.get(r['source']), 'right': views.get(r['target']),
                                  'left_columns': [p[0] for p in r['column_pairs']], 'right_columns': [p[1] for p in r['column_pairs']],
                                  'target_unique': r.get('target_unique_non_null', False), 'status': r['status'], 'source': 'data_relationship_check'})
        for view, info in tables.items():
            for j in info['joins']:
                if j.get('status') in {'candidate_checked', 'needs_review_fanout'} and j['target_view']:
                    relationships.append({'left': view, 'right': j['target_view'], 'left_columns': j['source_columns'],
                                          'right_columns': j['target_columns'], 'source': 'semantic_plan',
                                          'target_unique': bool((j.get('data_checks') or {}).get('target_unique')),
                                          'status': j['status']})

        def relation(left, right, lcols, rcols):
            for r in relationships:
                if r['left'] == left and r['right'] == right and r['left_columns'] == lcols and r['right_columns'] == rcols:
                    return r
                if r['left'] == right and r['right'] == left and r['left_columns'] == rcols and r['right_columns'] == lcols:
                    return {**r, 'target_unique': False, 'reversed': True}
            return None
        ddl = '\n'.join('CREATE TABLE "'+v+'" ('+', '.join(f'"{c}" {t}' for c, t in cols.items())+');' for v, cols in allowed.items())
        manifest_sha = sha(root/'manifest.json')
        context = {'business_id': business_id, 'pg_schema': pg['schema'], 'manifest_sha256': manifest_sha,
                   'semantic_revision': manifest['semantic_revision'], 'root': str(root),
                   'fingerprint': hashlib.sha256(f'{manifest_sha}:{PROMPT_VERSION}'.encode()).hexdigest(),
                   'readme': readme, 'rulebook': self.rules_text, 'evidence': evidence,
                   'rules_detected': detect_rules([e for e in evidence.entries.values() if e['source'] == 'rulebook']),
                   'calendar_path': str(self.source_root/'calendar.csv'), 'declared_coverage_end': pg.get('declared_coverage_end'),
                   'tables': tables, 'allowed': allowed, 'ddl': ddl, 'quality': registry, 'relationships': relationships,
                   'relation': relation, 'measure': lambda view, col: measures.get((view, col))}
        self.cache[business_id] = context;self.pins[business_id] = manifest_sha
        return context

    def verify_pinned(self, business_id):
        root = self.prepared_root/business_id
        if sha(root/'manifest.json') != self.pins[business_id]:
            raise ValueError('Published manifest changed during the run')


def _companion_meaning(name):
    suffix = name.rsplit('__', 1)[-1]
    return {'y': 'năm của ngày nguồn (kể cả ngày thiếu thông tin)', 'm': 'tháng của ngày nguồn', 'd': 'ngày trong tháng của ngày nguồn',
            'p': 'độ chính xác ngày: datetime/day/month_day/month/year', 'r': 'cách xác định ngày (explicit, year_from_sheet, missing_year...)',
            'raw': 'giá trị trạng thái nguyên văn trong nguồn', 'quantity_base': 'số lượng quy về đơn vị cơ bản (dẫn xuất)',
            'unit_base': 'đơn vị cơ bản của __quantity_base'}.get(suffix, 'cột dẫn xuất')


def schema_for_prompt(context, views=None):
    """Compact per-table description for prompts (no rows, no labels)."""
    out = []
    for view, t in context['tables'].items():
        if views is not None and view not in views:
            continue
        out.append({'table': view, 'grain': {k: t['grain'].get(k) for k in ('kind', 'semantic_kind', 'description', 'status')},
                    'row_identity': t['row_identity'].get('status'), 'clean_rows': t['clean_rows'],
                    'columns': [[c['name'], c['type'], c['canonical_role'] or c['semantic_role'], c['meaning'],
                                 c['has_data'], c['note']] for c in t['columns']],
                    'group_keys': [[k['name'], k['columns'], k.get('status')] for k in t['group_keys']],
                    'measures': [[m['column'], m['measure_scope'], m['aggregation'], m.get('status')] for m in t['measures']],
                    'date_bounds': t['date_bounds']})
    return {'columns_format': ['name', 'type', 'role', 'meaning', 'has_data', 'note'], 'tables': out,
            'relationships': [{k: r[k] for k in ('left', 'right', 'left_columns', 'right_columns', 'target_unique', 'status')}
                              for r in context['relationships']]}
