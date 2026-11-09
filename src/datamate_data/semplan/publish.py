"""P9: immutable publication manifest, v2 compatibility sidecar and review page.

Only ``status == "published"`` (audited, loaded) is usable by the baseline. Draft,
prepared-only or audit-failed outputs keep all evidence but are never served.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from ..parsers import dumps
from ..pipeline import digest
from . import MANIFEST_VERSION, PIPELINE_VERSION, PLAN_SCHEMA_VERSION

V2_KINDS = {'order', 'order_line', 'product', 'customer', 'delivery_item', 'delivery', 'payment',
            'purchase_line', 'lot', 'snapshot', 'expense', 'price_change', 'unknown'}
V2_AGGREGATION = {'sum': 'sum', 'sum_once_per_group': 'per_group_once', 'weighted_average': 'weighted_average',
                  'snapshot_latest': 'snapshot', 'none': 'none', 'average': 'none', 'min_max': 'none', 'unknown': 'unknown'}


def artifact_hashes(root, names):
    root = Path(root)
    return {name: digest(root/name) for name in names if (root/name).exists()}


def manifest(root, *, business_id, status, plan, catalog_name, artifacts, sources, config_sha256,
             implementation_sha256, model_summary, database, previous_run=None, audit_status=None):
    return {'manifest_version': MANIFEST_VERSION, 'status': status, 'business_id': business_id,
            'pipeline_version': PIPELINE_VERSION, 'plan_schema_version': PLAN_SCHEMA_VERSION,
            'semantic_revision': plan['plan_revision'], 'sources': sources,
            'code': {'implementation_sha256': implementation_sha256}, 'config_sha256': config_sha256,
            'model': model_summary, 'artifacts': artifacts, 'catalog': catalog_name, 'database': database,
            'audit_status': audit_status, 'previous_run': previous_run,
            'published_at_utc': datetime.now(timezone.utc).isoformat(), 'questions_read': False, 'labels_read': False,
            'usable_by_baseline': status == 'published'}


def compat_v2_sidecar(plan, postgres_catalog, *, postgres_catalog_sha256, readme_sha256, rulebook_sha256, model):
    """Express the unified plan in the v2 business-metadata format (no new inference)."""
    views = {t['name']: t['clean_view'] for t in postgres_catalog['tables'].values()}
    tables = {}
    for name, table in plan['tables'].items():
        grain = table['grain'];kind = grain['kind']
        kind = 'snapshot' if kind == 'stock_snapshot' else kind if kind in V2_KINDS else 'unknown'
        tables[views[name]] = {
            'grain': {'kind': kind, 'description': grain.get('description') or grain['kind'] +
                      (f" ({grain['semantic_kind']})" if grain.get('semantic_kind') else ''),
                      'evidence': {'quote': None, 'status': 'see_semantic_plan_evidence_refs'}, 'status': grain.get('status', 'candidate')},
            'group_keys': [{'name': k['name'], 'columns': k['columns'], 'purpose': k['purpose'],
                            'evidence': {'quote': None, 'status': 'see_semantic_plan'}, 'status': k.get('status')}
                           for k in table['group_keys'] if k.get('status') != 'rejected_by_checks'],
            'measures': [{'column': m['column'], 'scope': m['measure_scope'], 'aggregation': V2_AGGREGATION[m['aggregation']],
                          'unit': m['unit'], 'evidence': {'quote': None, 'status': 'see_semantic_plan'},
                          'status': m.get('status'), 'data_checks': []} for m in table['measures']],
            'joins': [{'target': views.get(j['target'], j['target']), 'source_columns': j['source_columns'],
                       'target_columns': j['target_columns'], 'evidence': {'quote': None, 'status': 'see_semantic_plan'},
                       'status': j.get('status'), 'data_checks': {k: (j.get('data_checks') or {}).get(k) for k in
                                                                 ('status', 'cardinality', 'orphan_source_rows', 'fanout_risk', 'business_verified')}}
                      for j in table['joins'] if j.get('status') != 'rejected_by_checks']}
    return {'version': 'business-metadata-v1', 'business_id': plan['business_id'],
            'catalog_sha256': postgres_catalog_sha256, 'readme_sha256': readme_sha256, 'rulebook_sha256': rulebook_sha256,
            'questions_read': False, 'labels_read': False, 'model': model, 'tables': tables,
            'existing_relationship_candidates': postgres_catalog.get('relationships', []),
            'compatibility_adapter': {'derived_from_plan_revision': plan['plan_revision'], 'new_inference': False}}


def review_markdown(plan, registry, audit):
    lines = [f"# Semantic plan — {plan['business_id']}", '',
             f"Revision `{plan['plan_revision']}`; status `{plan['status']}`. Proposals are candidates checked on data; "
             'checks are evidence, not business proof.', '']
    if audit:
        lines += [f"Audit: **{audit['status']}** ({sum(c['status'] == 'pass' for c in audit['checks'])}/{len(audit['checks'])} checks).", '']
    for name, t in plan['tables'].items():
        g = t['grain']
        lines += [f'## {name}', '', f"- Grain: `{g['kind']}`" + (f" / `{g['semantic_kind']}`" if g.get('semantic_kind') else '') +
                  f" — {g.get('description') or ''} (status {g.get('status')})",
                  f"- Row identity: {t['row_identity']}",
                  f"- Rows: {t['row_decisions']['summary']}",
                  f"- Refinement: {({k: v for k, v in t['refinement'].items() if k in ('used', 'requests', 'accepted')})}", '',
                  '| Cột | Canonical | Semantic | Kiểu | Trạng thái |', '|---|---|---|---|---|']
        for c in t['columns']:
            lines.append(f"| {c['original_name']} | {c.get('canonical_role') or ''} | {c.get('semantic_role') or ''} | "
                         f"{c['sql_type']} | {(c.get('semantic') or {}).get('status', '')} |")
        lines += ['', '| Policy | Cột | Quyết định | Lý do |', '|---|---|---|---|']
        for p in t['policies']:
            lines.append(f"| {p['operation']} | {', '.join(p['columns'])} | {p['decision_status']} | {p['reason']} |")
        if t['measures']:
            lines += ['', '| Measure | Scope | Aggregation | Trạng thái |', '|---|---|---|---|']
            for m in t['measures']:
                lines.append(f"| {m['column']} | {m['measure_scope']} | {m['aggregation']} | {m.get('status')} |")
        for col, m in t['status_mappings'].items():
            lines += ['', f"Status `{col}`: domain `{m['domain']}` ({m['domain_basis']}); mapped {len(m['mappings'])}, "
                      f"unresolved {m['unresolved'][:10]}"]
        lines.append('')
    lines += ['## Quality issues', '', '| Bảng | Loại | Cột | Số dòng | Mức |', '|---|---|---|---|---|']
    for i in registry['issues']:
        lines.append(f"| {i['table']} | {i['kind']} | {', '.join(i['columns'])} | {i['row_count']} | {i['severity']} |")
    return '\n'.join(lines)+'\n'


def make_read_only(root, names):
    for name in names:
        path = Path(root)/name
        if path.is_file():
            os.chmod(path, 0o444)


def load_manifest(business_root):
    path = Path(business_root)/'manifest.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('manifest_version') != MANIFEST_VERSION:
        raise ValueError('Unsupported manifest version')
    for name, expected in value['artifacts'].items():
        if digest(Path(business_root)/name) != expected:
            raise ValueError(f'Published artifact changed after publication: {name}')
    return value


def write_json(path, value):
    Path(path).write_text(dumps(value)+'\n', encoding='utf-8')


__all__ = ['manifest', 'compat_v2_sidecar', 'review_markdown', 'make_read_only', 'load_manifest', 'artifact_hashes', 'write_json']
