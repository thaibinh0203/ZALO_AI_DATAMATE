#!/usr/bin/env python3
"""Offline calculation-plan baseline over published v5 outputs. Never reads labels."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from time import perf_counter

os.environ['HF_HUB_OFFLINE'] = '1';os.environ['TRANSFORMERS_OFFLINE'] = '1';os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
ROOT = Path(__file__).resolve().parents[1];sys.path.insert(0, str(ROOT/'src'))
from datamate_b.contracts import validate_request  # noqa: E402
from datamate_baseline.runner import requests_from_file  # noqa: E402
from datamate_baseline.calcplan.context import PublishedContextLoader  # noqa: E402
from datamate_baseline.calcplan.runner import run_batch  # noqa: E402
from datamate_baseline.calcplan.tools import ReadOnlyExecutor  # noqa: E402


def main():
    start = perf_counter();p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prepared-root', type=Path, required=True, help='v5 output root with published businesses')
    p.add_argument('--source-root', type=Path, default=ROOT/'docs/train')
    p.add_argument('--config', type=Path, default=ROOT/'configs/baseline_calcplan.json')
    p.add_argument('--model-path', type=Path, default=ROOT/'models/qwen3-4b')
    p.add_argument('--questions-file', type=Path);p.add_argument('--business-id');p.add_argument('--question');p.add_argument('--as-of-date')
    p.add_argument('--question-ids', type=Path, help='JSON list of IDs (no labels)');p.add_argument('--limit', type=int)
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--dsn-env', default='DATAMATE_BASELINE_PG_DSN')
    a = p.parse_args()
    if a.output_dir.exists():
        p.error('Output exists; use a new directory')
    config = json.loads(a.config.read_text())
    for key, expected in {'do_sample': False, 'enable_thinking': False, 'requires_published_semantic_plan': True}.items():
        if config.get(key) != expected:
            p.error('Unsupported calcplan setting: '+key)
    executor = None
    try:
        if a.questions_file:
            if any([a.business_id, a.question, a.as_of_date]):
                raise ValueError('Use either questions-file or a single question')
            requests = requests_from_file(a.questions_file)
            config['question_file_sha256'] = hashlib.sha256(a.questions_file.read_bytes()).hexdigest()
        else:
            if not all([a.business_id, a.question, a.as_of_date]):
                raise ValueError('Single question requires business-id, question, as-of-date')
            requests = [validate_request({'id': 'manual_0001', 'business_id': a.business_id, 'question': a.question,
                                          'as_of_date': a.as_of_date})]
        if a.question_ids:
            selected = json.loads(a.question_ids.read_text());known = {r['id'] for r in requests}
            if not isinstance(selected, list) or len(selected) != len(set(selected)) or set(selected)-known:
                raise ValueError('Invalid selected question IDs')
            requests = [r for r in requests if r['id'] in set(selected)]
        if a.limit is not None:
            if a.limit <= 0:
                raise ValueError('limit must be positive')
            requests = requests[:a.limit]
        if not requests:
            raise ValueError('No selected questions')
        dsn = os.environ.get(a.dsn_env)
        if not dsn:
            raise ValueError('Set '+a.dsn_env+' to the SELECT-only role DSN')
        loader = PublishedContextLoader(a.prepared_root, a.source_root)
        for business in sorted({r['business_id'] for r in requests}):
            loader.load(business)  # fail before inference for unpublished or changed businesses
        executor = ReadOnlyExecutor(dsn, timeout_ms=config['sql_timeout_ms'], max_rows=config['max_result_rows'],
                                    metadata_timeout_ms=config['metadata_timeout_ms'])
        from datamate_b.model import LocalModel, ModelConfig
        model = LocalModel(ModelConfig(str(a.model_path.resolve()), config['device'], config['dtype'],
                                       config['max_new_tokens'], config['max_context_tokens'], config['seed']))
        if model.manifest['resolved_revision'] != config['revision'] or model.manifest['repo_id'] != config['repo_id']:
            raise ValueError('Model manifest differs from pinned baseline config')
        run_batch(requests, model=model, executor=executor, loader=loader, output_dir=a.output_dir, config=config,
                  external_startup_ms=(perf_counter()-start)*1000)
    except KeyboardInterrupt:
        print('Run interrupted; partial results retained, not a complete evaluation.', file=sys.stderr)
        return 130
    except Exception as exc:
        print('Calcplan baseline failed: '+type(exc).__name__+'; no complete run is claimed.', file=sys.stderr)
        if isinstance(exc, (ValueError, FileNotFoundError)):
            print(str(exc), file=sys.stderr)
        return 1
    finally:
        if executor is not None:
            executor.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
