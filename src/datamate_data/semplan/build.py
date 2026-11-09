"""Orchestrate P0-P9 for one business into a new output directory.

Layout of ``<output>/<business_id>/``:
  raw/<table>.jsonl            P0-P2 provisional staging (raw, provenance, attempts)
  row_decisions/<table>.jsonl  non-default row decisions (rule/model/needs_review)
  checks.jsonl                 scoped checks (stage, selection, revision)
  model_calls.jsonl            every model attempt (prompt/raw/errors/cache)
  semantic_plan.json           frozen unified plan
  final/<table>.jsonl          P7 materialized rows
  catalog.json                 table/column catalog (v4-compatible fields)
  quality_registry.json        located quality issues
  postgres_catalog.json        after load (views, profiles, relationships)
  audit.json                   P8 audit
  review.md                    human-readable plan/quality summary
  business_metadata/metadata.json  v2-format sidecar derived from the plan (no new inference)
  manifest.json                P9 publication record (written last)
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import sys

from ..normalization import identifier
from ..parsers import dumps
from ..pipeline import Conversions, digest
from . import CHECKS_VERSION, PIPELINE_VERSION, PLAN_SCHEMA_VERSION, ROW_DECISION_VERSION
from .contracts import EvidenceCatalog, detect_rules, object_sha, validate_plan_document
from .materialize import build_conversions, declared_coverage_end, materialize_table
from .planner import Planner, _strip_check as strip_check
from .publish import (artifact_hashes, compat_v2_sidecar, make_read_only, manifest, review_markdown, write_json)
from .quality import registry_document, table_issues
from .staging import stage_business

PACKAGE = Path(__file__).resolve().parent


def implementation_hash():
    files = sorted(PACKAGE.glob('*.py'))+sorted(PACKAGE.parent.glob('*.py'))
    return hashlib.sha256(''.join(digest(p) for p in files).encode()).hexdigest()


LOADED_IMPLEMENTATION_HASH = implementation_hash()


def source_snapshot(business, rulebook, calendar):
    business = Path(business)
    paths = [business/'README.md', *sorted(p for p in business.iterdir() if p.suffix.lower() in {'.xlsx', '.csv', '.sqlite'}),
             Path(rulebook), Path(calendar)]
    if any(p.is_symlink() for p in paths):
        raise ValueError('Source symlinks are not allowed')
    return [{'name': p.name, 'sha256': digest(p), 'bytes': p.stat().st_size} for p in paths]


def header_resolver_for(plan_model, readme, headers, replay=None):
    """Resolve Excel headers the rule vocabulary cannot find (v4 header contract)."""
    from .. import semantic as v4
    from .contracts import object_sha as sha_of

    def resolve(table, preview, _readme):
        key = table.path.name+'::'+table.name
        if replay is not None:
            record = replay.get('headers', {}).get(key)
            if record is None:
                raise ValueError('Replay plan lacks header decision: '+key)
            candidate = next((r for r in preview if r.number == record['row']), None)
            if candidate is None or sha_of(candidate.values) != record['values_sha256']:
                raise ValueError('Replay header/source mismatch')
            headers[key] = record
            return preview.index(candidate), {'method': 'replayed_header_decision', **record}
        if plan_model is None:
            raise ValueError(f'Excel header needs model or explicit mapping: {key}')
        payload = {'task': 'header', 'source_file': table.path.name, 'source_table': table.name,
                   'rows': [{'row': r.number, 'values': r.values} for r in preview], 'readme': readme}

        def validator(value):
            if set(value) != {'row', 'reason'} or type(value['row']) is not int:
                raise ValueError('Header contract: {"row": <int>, "reason": <text>}')
            index = next((i for i, r in enumerate(preview) if r.number == value['row']), None)
            if index is None or sum(isinstance(v, str) and bool(v.strip()) for v in preview[index].values) < 2:
                raise ValueError('Header must be an existing row with at least two text cells')
            return value
        value, _ = plan_model.ask('header', key, v4.SYSTEM, payload, validator=validator)
        if value is None:
            raise ValueError('Model could not identify a source-backed Excel header: '+key)
        index = next(i for i, r in enumerate(preview) if r.number == value['row'])
        record = {'row': value['row'], 'reason': value['reason'], 'values_sha256': sha_of(preview[index].values)}
        headers[key] = record
        return index, {'method': 'slm_existing_row_candidate', **record}
    return resolve


def build_business(business_dir, output_dir, *, rulebook, calendar, config, plan_model=None, dsn=None,
                   replay_plan=None, previous_run=None, csv_encoding='utf-8-sig', log=print):
    business = Path(business_dir).resolve();out = Path(output_dir).resolve()
    if out.exists():
        raise FileExistsError('Output exists; publication directories are immutable')
    if out == business or business in out.parents:
        raise ValueError('Output must be outside the source business directory')
    if implementation_hash() != LOADED_IMPLEMENTATION_HASH:
        raise ValueError('Pipeline code changed after import; restart')
    readme = (business/'README.md').read_text(encoding='utf-8')
    rules_text = Path(rulebook).read_text(encoding='utf-8')
    catalog_units = EvidenceCatalog.from_sources(readme, rules_text)
    rules = detect_rules([e for e in catalog_units.entries.values() if e['source'] == 'rulebook'])
    sources = source_snapshot(business, rulebook, calendar)
    if replay_plan is not None:
        validate_plan_document(replay_plan)
        if replay_plan['business_id'] != business.name or replay_plan['sources'] != sources:
            raise ValueError('Replay plan does not match this business/source snapshot')
        if replay_plan['code']['pipeline_version'] != PIPELINE_VERSION:
            raise ValueError('Replay plan was built by another pipeline version')
    out.mkdir(parents=True)
    if plan_model is not None:
        plan_model.audit_path = out/'model_calls.jsonl'
    headers = {}
    resolver = header_resolver_for(plan_model, readme, headers, replay=replay_plan)
    staged = stage_business(business, out, readme=readme, rules=rules, header_resolver=resolver, csv_encoding=csv_encoding)
    planner = Planner(business.name, out, staged, readme=readme, rulebook=rules_text, rules=rules,
                      model=plan_model, config=config, replay=replay_plan)
    tables = planner.run(readme_conversions=_readme_conversions(readme))
    plan = {'schema_version': PLAN_SCHEMA_VERSION, 'business_id': business.name, 'status': 'frozen',
            'sources': sources, 'headers': headers, 'rules_detected': rules,
            'code': {'pipeline_version': PIPELINE_VERSION, 'implementation_sha256': LOADED_IMPLEMENTATION_HASH,
                     'row_decision_version': ROW_DECISION_VERSION, 'checks_version': CHECKS_VERSION,
                     'python': '.'.join(map(str, sys.version_info[:3])), 'sqlite': sqlite3.sqlite_version},
            'config': config, 'model': plan_model.summary() if plan_model else {'mode': 'none'},
            'tables': tables, 'events': planner.events, 'ignored_files': staged['ignored_files'],
            'previous_run': previous_run, 'questions_read': False, 'labels_read': False,
            'limitations': ['Model proposals are candidates; acceptance is by contract and data checks, not business proof.',
                            'Unknown rows are kept raw, excluded from analytic views and listed in the quality registry.',
                            'Join/grain checks show structure (uniqueness/overlap/fanout), not semantic correctness.']}
    plan['plan_revision'] = 'plan-'+object_sha([plan['tables'], sources, LOADED_IMPLEMENTATION_HASH])[:20]
    validate_plan_document(plan)
    (out/'row_decisions').mkdir()
    for name in tables:
        with (out/'row_decisions'/f'{name}.jsonl').open('w', encoding='utf-8') as stream:
            for record in planner.decisions_file_records(name):
                stream.write(dumps(record)+'\n')
        tables[name]['row_decisions']['file'] = f'row_decisions/{name}.jsonl'
        tables[name]['row_decisions']['sha256'] = digest(out/'row_decisions'/f'{name}.jsonl')
    with (out/'checks.jsonl').open('w', encoding='utf-8') as stream:
        for check in planner.checks.values():
            stream.write(dumps(strip_check(check))+'\n')
    write_json(out/'semantic_plan.json', plan)
    # P7 materialization
    (out/'final').mkdir()
    staged['decisions_final'] = {n: planner.decisions_file_records(n) for n in tables}
    conversions, _ = build_conversions(out, tables, staged, readme)
    fingerprint = hashlib.sha256(dumps([sources, object_sha(config), LOADED_IMPLEMENTATION_HASH, plan['plan_revision']]).encode()).hexdigest()
    catalog = {'version': PIPELINE_VERSION, 'dialect': 'postgres', 'business_id': business.name, 'fingerprint': fingerprint,
               'schema': 'dm5_'+identifier(business.name)[:24]+'_'+fingerprint[:12], 'config': {'csv_encoding': csv_encoding},
               'sources': sources, 'ignored_files': staged['ignored_files'], 'readme_ref': 'README.md',
               'rulebook_sha256': digest(rulebook), 'calendar_sha256': digest(calendar),
               'declared_coverage_end': declared_coverage_end(readme), 'semantic_revision': plan['plan_revision'],
               'tables': {}, 'conversion_conflicts': conversions.conflicts,
               'created_at_utc': datetime.now(timezone.utc).isoformat(),
               'limitations': plan['limitations']}
    issues = [];stats_by_table = {}
    for name, table_plan in tables.items():
        entry, stats = materialize_table(out, name, table_plan, staged['tables'][name], staged['decisions_final'][name],
                                         conversions, rules=rules, business_id=business.name)
        catalog['tables'][name] = entry;stats_by_table[name] = stats
        issues += table_issues(business.name, table_plan, entry, stats, name+'_clean', conversion_conflicts=conversions.conflicts)
    catalog['total_source_records'] = sum(t['rows'] for t in catalog['tables'].values())
    write_json(out/'catalog.json', catalog)
    registry = registry_document(business.name, issues, plan_revision=plan['plan_revision'])
    write_json(out/'quality_registry.json', registry)
    audit = None;database = {'loaded': False, 'schema': catalog['schema']};status = 'prepared_not_loaded'
    if dsn:
        from .postgres import audit as run_audit, load_published
        loaded = load_published(dsn, out, catalog)
        postgres_catalog = loaded['catalog']
        write_json(out/'postgres_catalog.json', postgres_catalog)
        audit = run_audit(dsn, out, postgres_catalog)
        write_json(out/'audit.json', audit)
        database = {'loaded': True, 'schema': catalog['schema'], 'cache_hit': loaded['cache_hit'],
                    'clean_views': sorted(t['clean_view'] for t in postgres_catalog['tables'].values())}
        status = 'published' if audit['status'] == 'passed' else 'audit_failed'
        sidecar = compat_v2_sidecar(plan, postgres_catalog, postgres_catalog_sha256=digest(out/'postgres_catalog.json'),
                                    readme_sha256=digest(business/'README.md'), rulebook_sha256=digest(rulebook),
                                    model=plan['model'])
        (out/'business_metadata').mkdir()
        write_json(out/'business_metadata'/'metadata.json', sidecar)
    (out/'review.md').write_text(review_markdown(plan, registry, audit), encoding='utf-8')
    if implementation_hash() != LOADED_IMPLEMENTATION_HASH:
        raise ValueError('Pipeline code changed during preparation')
    if source_snapshot(business, rulebook, calendar) != sources:
        raise ValueError('Sources changed during preparation')
    names = ['semantic_plan.json', 'catalog.json', 'quality_registry.json', 'checks.jsonl', 'review.md',
             'postgres_catalog.json', 'audit.json', 'business_metadata/metadata.json', 'model_calls.jsonl']
    names += [f'final/{n}.jsonl' for n in tables]+[f'row_decisions/{n}.jsonl' for n in tables]
    artifacts = artifact_hashes(out, names)
    document = manifest(out, business_id=business.name, status=status, plan=plan,
                        catalog_name='postgres_catalog.json' if dsn else 'catalog.json', artifacts=artifacts,
                        sources=sources, config_sha256=object_sha(config), implementation_sha256=LOADED_IMPLEMENTATION_HASH,
                        model_summary=plan['model'], database=database, previous_run=previous_run,
                        audit_status=audit['status'] if audit else None)
    write_json(out/'manifest.json', document)
    make_read_only(out, names+['manifest.json'])
    log(dumps({'business_id': business.name, 'status': status, 'tables': len(tables), 'rows': catalog['total_source_records'],
               'row_kinds': dict(sum((Counter(t['row_kinds']) for t in catalog['tables'].values()), Counter())),
               'quality_issues': len(issues), 'plan_revision': plan['plan_revision']}))
    return document


def _readme_conversions(readme):
    registry = Conversions();registry.from_readme(readme)
    return bool(registry.factors)
