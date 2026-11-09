#!/usr/bin/env python3
"""Opt-in v5 pipeline: stage -> plan -> check/refine -> materialize -> load -> audit -> publish.

Never reads questions/answers. Every business goes to a NEW output directory; existing
v4 outputs and PostgreSQL schemas are not modified. Without --dsn-env loading, the
result is prepared_not_loaded and is not usable by the calcplan baseline.
"""
import argparse
import json
import os
from pathlib import Path
import sys
from time import perf_counter

os.environ['HF_HUB_OFFLINE'] = '1';os.environ['TRANSFORMERS_OFFLINE'] = '1';os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
ROOT = Path(__file__).resolve().parents[1];sys.path.insert(0, str(ROOT/'src'))
from datamate_data.parsers import dumps  # noqa: E402
from datamate_data.pipeline import digest  # noqa: E402
from datamate_data.semplan.build import build_business  # noqa: E402
from datamate_data.semplan.model import PlanModel  # noqa: E402


def businesses_for(args, parser):
    source = args.source_root.resolve();split_root = args.split_root.resolve()
    if not args.partition:
        return [args.business_dir.resolve()]
    index = json.loads((split_root/'artifact_checksums.json').read_text())
    for name in ('source_manifest.json', 'split_manifest.json'):
        if digest(split_root/name) != index[name]:
            parser.error('Frozen manifest checksum mismatch')
    split = json.loads((split_root/'split_manifest.json').read_text(encoding='utf-8'))
    frozen = {r['path']: r for r in json.loads((split_root/'source_manifest.json').read_text())['files']}
    businesses = [source/'data'/b for b in split['partitions'][args.partition]['business_ids']]
    for business in businesses:
        paths = [business/'README.md', *[p for p in business.iterdir() if p.suffix.lower() in {'.csv', '.xlsx', '.sqlite'}]]
        for path in paths+[source/'rulebook.md', source/'calendar.csv']:
            relative = path.relative_to(source).as_posix()
            if path.is_symlink() or relative not in frozen or digest(path) != frozen[relative]['sha256']:
                parser.error(f'Selected source changed from frozen split: {relative}')
    return businesses


def main():
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--business-dir', type=Path);g.add_argument('--partition', choices=['train', 'development', 'calibration', 'holdout'])
    p.add_argument('--split-root', type=Path, default=ROOT/'artifacts/data_split_v1')
    p.add_argument('--source-root', type=Path, default=ROOT/'docs/train')
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--config', type=Path, default=ROOT/'configs/pipeline_semplan.json')
    p.add_argument('--semantic-mode', choices=['local', 'none'], default='local')
    p.add_argument('--model-path', type=Path, default=ROOT/'models/qwen3-4b')
    p.add_argument('--cache', type=Path, default=ROOT/'.cache/datamate_semplan')
    p.add_argument('--device', default=None)
    p.add_argument('--prepare-only', action='store_true', help='Stop before PostgreSQL; output is not usable by the baseline')
    p.add_argument('--reuse-plan-root', type=Path, help='Previous v5 output root: replay its frozen decisions without inference')
    p.add_argument('--csv-encoding', default='utf-8-sig')
    a = p.parse_args();started = perf_counter()
    config = json.loads(a.config.read_text(encoding='utf-8'))
    output = a.output_dir.resolve();source = a.source_root.resolve()
    if output.exists():
        p.error('Output directory exists; use a new run path (published outputs are immutable)')
    if output == source or source in output.parents or a.split_root.resolve() in output.parents:
        p.error('Output cannot be inside sources or the frozen split')
    dsn = None if a.prepare_only else os.environ.get('DATAMATE_PG_DSN')
    if not a.prepare_only and not dsn:
        p.error('Set DATAMATE_PG_DSN (ingestion role) or use --prepare-only')
    if a.reuse_plan_root and a.semantic_mode != 'local':
        p.error('--reuse-plan-root replays a local-model plan; do not combine with --semantic-mode none')
    businesses = businesses_for(a, p)
    output.mkdir(parents=True)
    report = {'pipeline': config['version'], 'partition': a.partition, 'status': 'running', 'businesses': [],
              'semantic_mode': 'replay' if a.reuse_plan_root else a.semantic_mode, 'questions_read': False,
              'labels_read': False, 'generated_sql_executed': False, 'database_loaded': dsn is not None}
    model = None
    try:
        if a.semantic_mode == 'local' and not a.reuse_plan_root:
            from datamate_b.model import LocalModel, ModelConfig
            m = config['model']
            model = LocalModel(ModelConfig(str(a.model_path.resolve()), a.device or m['device'], m['dtype'],
                                           m['max_new_tokens'], m['max_context_tokens'], m['seed']))
            if model.manifest['resolved_revision'] != m['revision'] or model.manifest['repo_id'] != m['repo_id']:
                raise ValueError('Model manifest differs from pinned pipeline config')
        for business in businesses:
            replay = previous = None
            if a.reuse_plan_root:
                prev_root = a.reuse_plan_root.resolve()/business.name
                replay = json.loads((prev_root/'semantic_plan.json').read_text(encoding='utf-8'))
                previous = {'root': str(prev_root), 'plan_revision': replay['plan_revision'],
                            'manifest_sha256': digest(prev_root/'manifest.json') if (prev_root/'manifest.json').exists() else None}
            plan_model = PlanModel(model, audit_path=output/'_pending_model_calls.jsonl', caps=config['model_caps'],
                                   cache_dir=a.cache, identity={'pipeline': config['version']}) if model is not None else None
            document = build_business(business, output/business.name, rulebook=source/'rulebook.md',
                                      calendar=source/'calendar.csv', config=config, plan_model=plan_model, dsn=dsn,
                                      replay_plan=replay, previous_run=previous, csv_encoding=a.csv_encoding)
            item = {'business_id': business.name, 'status': document['status'], 'semantic_revision': document['semantic_revision'],
                    'database': document['database'], 'model': document['model']}
            report['businesses'].append(item)
        report['status'] = 'complete'
        if model is not None:
            report['model'] = {**model.metadata(), 'load_ms': model.load_ms}
    except Exception as error:
        # Never persist connection strings; the message is printed with the DSN removed.
        message = str(error).replace(dsn, '<dsn>') if dsn else str(error)
        report.update(status='failed', error_type=type(error).__name__)
        print(f'Pipeline failed ({type(error).__name__}: {message[:300]}); no completed run is claimed.', file=sys.stderr)
        return 1
    finally:
        report['elapsed_ms'] = round((perf_counter()-started)*1000, 3)
        (output/'run.json').write_text(dumps(report)+'\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
