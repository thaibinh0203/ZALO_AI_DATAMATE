#!/usr/bin/env python3
"""Prepare source business files and load PostgreSQL; never reads answers/questions."""
import argparse
import json
import os
from pathlib import Path
import sys
from time import perf_counter

os.environ['HF_HUB_OFFLINE']='1'
os.environ['TRANSFORMERS_OFFLINE']='1'
os.environ['HF_HUB_DISABLE_TELEMETRY']='1'

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from datamate_data.pipeline import prepare_business,digest
from datamate_data.parsers import dumps


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--business-dir',type=Path)
    group.add_argument('--partition',choices=['train','development','calibration','holdout'])
    parser.add_argument('--split-root',type=Path,default=ROOT/'artifacts/data_split_v1')
    parser.add_argument('--source-root',type=Path,default=ROOT/'docs/train')
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--csv-encoding',default='utf-8-sig')
    parser.add_argument('--prepare-only',action='store_true',help='Create normalized staging/catalog without a server')
    parser.add_argument('--business-metadata',action='store_true',help='Add source-grounded grain/measure/key/join sidecar after PostgreSQL loading')
    parser.add_argument('--semantic-mode',choices=['local','none'],default='local',
                        help='local: offline Qwen3 proposals + validation; none: deterministic baseline')
    parser.add_argument('--semantic-model-path',type=Path,default=ROOT/'models/qwen3-4b')
    parser.add_argument('--semantic-cache',type=Path,default=ROOT/'.cache/datamate_semantics')
    parser.add_argument('--semantic-device',default='cuda:0')
    parser.add_argument('--semantic-plan-root',type=Path,
                        help='Reuse source-identical local profiles/plans, revalidate with current gates, no new inference')
    args=parser.parse_args();started=perf_counter()
    if args.business_metadata and (args.prepare_only or args.semantic_plan_root or args.semantic_mode!='local'):
        parser.error('Business metadata requires local model and PostgreSQL loading; enrich existing catalogs separately otherwise')
    if args.semantic_plan_root and args.semantic_mode!='local':parser.error('Plan reuse requires semantic-mode local')
    source=args.source_root.resolve();output=args.output_dir.resolve()
    if output.exists():parser.error('Output directory already exists; use a new run path')
    if output==source or source in output.parents:parser.error('Output cannot be within source data')
    split_root=args.split_root.resolve()
    if output==split_root or split_root in output.parents:parser.error('Output cannot be within frozen split')
    if not args.prepare_only and not os.environ.get('DATAMATE_PG_DSN'):
        parser.error('Set DATAMATE_PG_DSN; it is never printed or saved in reports')
    if args.partition:
        index=json.loads((split_root/'artifact_checksums.json').read_text())
        for name in ('source_manifest.json','split_manifest.json'):
            if digest(split_root/name)!=index[name]:parser.error('Frozen manifest checksum mismatch')
        split=json.loads((split_root/'split_manifest.json').read_text(encoding='utf-8'))
        businesses=[source/'data'/b for b in split['partitions'][args.partition]['business_ids']]
        frozen=json.loads((split_root/'source_manifest.json').read_text())
        expected={r['path']:r for r in frozen['files']}
        for business in businesses:
            paths=[business/'README.md',*[p for p in business.iterdir() if p.suffix.lower() in {'.csv','.xlsx','.sqlite'}]]
            for path in paths+[source/'rulebook.md',source/'calendar.csv']:
                relative=path.relative_to(source).as_posix()
                if path.is_symlink() or relative not in expected or digest(path)!=expected[relative]['sha256']:
                    parser.error(f'Selected source changed from frozen split: {relative}')
    else:businesses=[args.business_dir]
    output.mkdir(parents=True,exist_ok=False)
    report={'dialect':'postgres','partition':args.partition,'businesses':[],'status':'running',
            'semantic_mode':'cached_revalidated' if args.semantic_plan_root else args.semantic_mode,
            'labels_read':False,'questions_read':False,'generated_sql_executed':False}
    try:
        semantic_model=None
        if args.semantic_mode=='local' and not args.semantic_plan_root:
            from datamate_data.semantic import LocalSemanticModel,build_semantic_plan
            semantic_model=LocalSemanticModel(args.semantic_model_path,cache_dir=args.semantic_cache,
                                             device=args.semantic_device,max_context_tokens=16384)
        for business in businesses:
            prepared=output/Path(business).name
            plan=None
            if args.semantic_plan_root:
                from datamate_data.semantic import revalidate_semantic_plan
                cache_root=args.semantic_plan_root/Path(business).name
                original=json.loads((cache_root/'semantic_plan.json').read_text(encoding='utf-8'))
                plan=revalidate_semantic_plan(original,business,rulebook=source/'rulebook.md',calendar=source/'calendar.csv')
                archive=output/'_semantic'/Path(business).name;archive.mkdir(parents=True)
                (archive/'semantic_plan.json').write_text(dumps(plan)+'\n',encoding='utf-8')
                if (cache_root/'model_calls.jsonl').exists():
                    with (archive/'model_calls.jsonl').open('w',encoding='utf-8') as stream:
                        for line in (cache_root/'model_calls.jsonl').read_text().splitlines():
                            record=json.loads(line);record['replayed_semantic_plan']=True
                            stream.write(dumps(record)+'\n')
            elif semantic_model is not None:
                plan=build_semantic_plan(business,output/'_semantic'/Path(business).name,
                    rulebook=source/'rulebook.md',calendar=source/'calendar.csv',model=semantic_model,csv_encoding=args.csv_encoding)
            catalog=prepare_business(business,prepared,rulebook=source/'rulebook.md',calendar=source/'calendar.csv',csv_encoding=args.csv_encoding,semantic_plan=plan)
            from datamate_data.review import write_semantic_review
            review=write_semantic_review(catalog,prepared)
            item={'business_id':catalog['business_id'],'schema':catalog['schema'],'fingerprint':catalog['fingerprint'],
                  'rows':catalog['total_source_records'],'tables':len(catalog['tables']),
                  'normalization_errors':sum(sum(t['errors'].values()) for t in catalog['tables'].values()),
                  'derived_errors':sum(sum(t['derived_errors'].values()) for t in catalog['tables'].values()),
                  'base_quantity_unresolved':sum(t['base_quantity_unresolved'] for t in catalog['tables'].values()),
                  'duplicate_rows':sum(t['duplicates'] for t in catalog['tables'].values()),
                  'prepared_dir':str(prepared),'database_loaded':False}
            item.update(semantic_summary=review['summary'],semantic_model_error_tables=review['model_error_tables'])
            if not args.prepare_only:
                from datamate_data.postgres import load_business
                loaded=load_business(os.environ['DATAMATE_PG_DSN'],prepared)
                item.update(database_loaded=True,cache_hit=loaded['cache_hit'])
                if args.business_metadata:
                    from datamate_data.business_metadata import build_business_metadata
                    business_metadata=build_business_metadata(prepared,source,model=semantic_model.model,dsn=os.environ['DATAMATE_PG_DSN'])
                    item['business_metadata']={'version':business_metadata['version'],'tables':len(business_metadata['tables'])}
            report['businesses'].append(item)
            print(dumps(item),flush=True)
        report['status']='complete'
        if semantic_model is not None:
            report['semantic_model']={**semantic_model.model.metadata(),'load_ms':semantic_model.model.load_ms,
                                     'generation_calls':semantic_model.calls,'cache_hits':semantic_model.cache_hits}
        elif args.semantic_plan_root:
            report['semantic_model']={'identity':plan['model'],'generation_calls':0,
                'plan_reuses':len(report['businesses']),'load_ms':None,
                'note':'Model-call logs are historical evidence marked replayed_semantic_plan.'}
    except Exception as error:
        report['status']='failed'
        # Credentials/DSN must not be copied into error reports.
        report['error_type']=type(error).__name__
        print(f'Pipeline failed ({type(error).__name__}); no completed run is claimed.',file=sys.stderr)
        return 1
    finally:
        report['elapsed_ms']=round((perf_counter()-started)*1000,3)
        (output/'run.json').write_text(dumps(report)+'\n',encoding='utf-8')


if __name__=='__main__':sys.exit(main())
