"""Local and offline submission runner. Evaluation is always an explicit command."""
import argparse
from pathlib import Path
from time import perf_counter
import os
import platform
import random
import sys
from .common import ROOT,load_yaml,read_json,write_json,read_jsonl,dumps,digest,file_hash,PipelineError,utc_now,source_fingerprint
from .contracts import validate_question
from .progress import configure as configure_progress,emit

def model_settings(config_path,key=None,device=None):
    config=load_yaml(config_path);key=key or config['active_model']
    if key not in config['models']: raise PipelineError(f'Unknown model: {key}')
    settings=dict(config['models'][key])
    if device:
        settings['device']=device
        if device=='cpu': settings['dtype']='float32'
    return key,settings

def create_client(args):
    from .inference.model_loader import LocalEngine
    from .inference.model_client import ModelClient
    key,settings=model_settings(args.model_config,args.model,args.device)
    emit('model_loading',f'Loading model {key} on {settings["device"]} (including artifact checksum validation)',model=key)
    engine=LocalEngine(settings)
    emit('model_loaded',f'Model ready: {key}; load {engine.load_ms/1000:.1f}s')
    return key,ModelClient(engine,settings,Path(args.cache)/'model_outputs')

def run(args):
    from .data_pipeline.pipeline import DataPipeline
    from .qa_pipeline.pipeline import QAPipeline
    from .evaluation.metrics import aggregate_logs
    start=perf_counter();split=Path(args.split).resolve();out=Path(args.output).resolve()
    if out.is_relative_to(split): raise PipelineError('Output must be outside the read-only source split')
    if out.exists() and (not args.submission_mode or (out/'submission.jsonl').exists() or (out/'run_manifest.json').exists()):
        raise PipelineError('Use a new output directory; refusing to overwrite a run')
    reuse_run=Path(args.reuse_ingestion_from).resolve() if getattr(args,'reuse_ingestion_from',None) else None
    partial_run=Path(args.reuse_successful_ingestion_from).resolve() if getattr(args,'reuse_successful_ingestion_from',None) else None
    if reuse_run and partial_run: raise PipelineError('Choose strict QA-only reuse or partial ingestion reuse, not both')
    source_run=reuse_run or partial_run
    questions=read_jsonl(args.questions or (source_run/'questions.jsonl' if source_run else split/'questions.jsonl'))
    for q in questions: validate_question(q)
    if not questions: raise PipelineError('Question set is empty')
    reused={};reuse_provenance=None
    if source_run:
        from .data_pipeline.reuse import inspect_reusable_ingestion
        reusable_questions=questions
        if partial_run:
            prior=read_json(partial_run/'run_manifest.json');prior_failed=read_json(partial_run/'logs/ingestion.json').get('failed',{})
            successful=(set(prior.get('data_fingerprints',{}))&set(prior.get('ingest_ms',{})))-set(prior_failed)
            reusable_questions=[q for q in questions if q['business_id'] in successful]
        reused,reuse_provenance=inspect_reusable_ingestion(source_run,split,reusable_questions,Path(args.cache))
    out.mkdir(parents=True,exist_ok=args.submission_mode);(out/'logs').mkdir(exist_ok=args.submission_mode)
    (out/'questions.jsonl').write_text(''.join(dumps(q)+'\n' for q in questions),encoding='utf-8')
    configure_progress(out)
    emit('startup',f'Run started: {len(questions)} questions, {len({q["business_id"] for q in questions})} businesses',phase='startup',total_questions=len(questions),completed_questions=0)
    database=load_yaml(args.database_config);pipeline=load_yaml(args.pipeline_config)
    if not 0<=pipeline['qa']['max_repairs']<=1: raise PipelineError('At most one repair inference is allowed')
    key,client=create_client(args)
    startup_ms=(perf_counter()-start)*1000
    warmup_start=perf_counter()
    for index in range(args.warmup_calls):
        emit('warmup',f'Warm-up {index+1}/{args.warmup_calls}')
        client.engine.generate([{'role':'user','content':'Return only {}.'}])
    warmup_ms=(perf_counter()-warmup_start)*1000
    import torch
    hardware={'platform':platform.platform(),'machine':platform.machine(),'cpu':platform.processor(),
              'torch':torch.__version__,'cuda':torch.version.cuda,
              'gpus':[torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())]}
    manifest={'version':'2.0.0','created_at':utc_now(),'model_key':key,'model_settings':client.settings,'model_version':client.version,
              'pipeline_config':pipeline,'database_config':database,'runtime_versions':client.engine.runtime_versions,'questions_sha256':file_hash(out/'questions.jsonl'),
              'source_fingerprint':source_fingerprint(split,[q['business_id'] for q in questions]),'hardware':hardware,'warmup_calls':args.warmup_calls,
              'warmup_ms':warmup_ms,'startup_ms':startup_ms,'model_load_ms':client.engine.load_ms,
              'source_code_fingerprint':digest({str(p.relative_to(ROOT)):file_hash(p) for p in ROOT.rglob('*.py') if 'models' not in p.parts}),
              'ingest_ms':{},'data_fingerprints':{},'status':'running','cold_start_total_ms':None,
              'note':'Three independent model choices for development; one model per submission. No training/fine-tuning performed.'}
    if source_run:
        manifest['reused_ingestion']=reuse_provenance
        manifest['note']+=(' QA-only rerun.' if reuse_run else ' Partial reuse: failed/unprepared businesses are ingested with current code.')+' Reused-data timing excludes original ingestion.'
    write_json(out/'run_manifest.json',manifest)
    data=None if reuse_run else DataPipeline(client,database,pipeline['data'],Path(args.cache)/'businesses')
    prepared={};failed={}
    businesses=list(dict.fromkeys(q['business_id'] for q in questions))
    for index,business in enumerate(businesses,1):
        emit('business_start',f'Data {index}/{len(businesses)}: preparing {business}',phase='data_preparation',business_id=business,table=None,question_id=None)
        try:
            if business in reused:
                from .data_pipeline.reuse import load_reused_business
                reuse_started=perf_counter()
                prepared[business]=load_reused_business(*reused[business],database)
                duration_ms=(perf_counter()-reuse_started)*1000
            else:
                prepared[business]=data.prepare(split/'data'/business,split)
                duration_ms=data.last_ingest_ms
            manifest['ingest_ms'][business]=duration_ms;manifest['data_fingerprints'][business]=prepared[business]['fingerprint']
            emit('business_done',f'Data {index}/{len(businesses)}: {business} ready in {duration_ms/1000:.1f}s'+(' (reused; no ingestion SLM)' if business in reused else ''))
        except Exception as error:
            failed[business]={'error_type':type(error).__name__,'error':str(error)}
            emit('business_error',f'Data {index}/{len(businesses)}: {business} failed ({type(error).__name__}); see logs/ingestion.json')
        write_json(out/'run_manifest.json',manifest)
    write_json(out/'logs/ingestion.json',{'calls':client.calls,'failed':failed,'duration_ms_by_business':manifest['ingest_ms'],
                'reused_businesses':sorted(reused),'ready_businesses':sorted(prepared)})
    emit('ingestion_summary',f'Ingestion ready: {len(prepared)}/{len(businesses)} businesses; {len(reused)} reused; {len(failed)} failed',failed_businesses=sorted(failed))
    if failed and getattr(args,'stop_on_ingestion_error',False):
        manifest.update(status='failed_ingestion',cold_start_total_ms=(perf_counter()-start)*1000)
        write_json(out/'run_manifest.json',manifest)
        emit('run_done','Stopped before QA because ingestion failed; inspect logs/ingestion.json',phase='failed_ingestion')
        return 2
    qa=QAPipeline(client,database,pipeline['qa']);logs=[]
    with (out/'submission.jsonl').open('w',encoding='utf-8') as predictions,(out/'logs/questions.jsonl').open('w',encoding='utf-8') as log_file:
        for index,q in enumerate(questions,1):
            emit('question_start',f'QA {index}/{len(questions)}: {q["id"]} ({q["business_id"]})',phase='qa',question_id=q['id'],business_id=q['business_id'],table=None)
            if q['business_id'] in failed:
                final={'id':q['id'],'type':'abstain'}
                log={'id':q['id'],'business_id':q['business_id'],'question':q,'answer':final,'elapsed_ms':0,
                     'thinking':None,'code':'datamate_v2/cli.py::run','inputs':q,'output':None,'model_calls':[],'queries':[],
                     'root_cause':'unknown','error_type':failed[q['business_id']]['error_type'],'ingestion_failure':failed[q['business_id']]}
            else: final,log=qa.answer(q,prepared[q['business_id']])
            predictions.write(dumps(final)+'\n');predictions.flush();os.fsync(predictions.fileno())
            log_file.write(dumps(log)+'\n');log_file.flush()
            logs.append(log)
            emit('question_done',f'QA {index}/{len(questions)} done: {final["type"]}; {log["elapsed_ms"]/1000:.1f}s',completed_questions=index)
    manifest.update(status='completed_with_ingestion_errors' if failed else 'completed',cold_start_total_ms=(perf_counter()-start)*1000,
                    peak_vram_bytes=client.engine.peak_vram_bytes)
    import resource
    manifest['peak_process_ram_bytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
    write_json(out/'run_manifest.json',manifest);write_json(out/'performance.json',aggregate_logs(logs))
    emit('run_done',f'Run finished: {len(logs)} questions; failed businesses: {len(failed)}',phase=manifest['status'])
    print(dumps({'output':str(out),'questions':len(logs),'failed_businesses':len(failed)}))
    return 2 if failed else 0

def ingest(args):
    from .data_pipeline.pipeline import DataPipeline
    configure_progress(Path(args.output).parent/(Path(args.output).stem+'_progress'))
    emit('startup','Data ingest started',phase='data_preparation')
    key,client=create_client(args)
    split=Path(args.split).resolve()
    config=load_yaml(args.pipeline_config)
    pipeline=DataPipeline(client,load_yaml(args.database_config),config['data'],Path(args.cache)/'businesses')
    businesses=[split/'data'/name for name in args.business] if args.business else sorted(p for p in (split/'data').iterdir() if p.is_dir())
    manifest={'model_key':key,'model_version':client.version,'businesses':{}}
    for path in businesses:
        metadata=pipeline.prepare(path,split,load_database=not args.no_database)
        manifest['businesses'][path.name]={'fingerprint':metadata['fingerprint'],'schema':metadata['schema'],'duration_ms':pipeline.last_ingest_ms}
    write_json(args.output,manifest)
    print(dumps({'prepared_businesses':len(manifest['businesses']),'manifest':args.output}))


def prepare_eval(args):
    questions=read_jsonl(args.questions)
    for q in questions: validate_question(q)
    businesses=sorted(set(args.business or []))
    if businesses:
        available={q['business_id'] for q in questions}
        if set(businesses)-available: raise PipelineError('Unknown businesses: '+', '.join(sorted(set(businesses)-available)))
        questions=[q for q in questions if q['business_id'] in businesses]
    if args.count<=0 or args.count>len(questions): raise PipelineError('Invalid selection count')
    # Selection reads no labels and is repeatable, independent of model outputs.
    rng=random.Random(args.seed)
    if businesses:
        selected=[];base,extra=divmod(args.count,len(businesses))
        if base==0: raise PipelineError('Count must be at least the number of selected businesses')
        for index,business in enumerate(businesses):
            pool=sorted((q for q in questions if q['business_id']==business),key=lambda q:q['id'])
            count=base+(index<extra)
            if len(pool)<count: raise PipelineError(f'{business} has {len(pool)} questions; balanced selection requires {count}. Reduce --count or select another business.')
            selected.extend(rng.sample(pool,count))
    else:
        selected=rng.sample(sorted(questions,key=lambda q:q['id']),args.count)
    selected.sort(key=lambda q:q['id']);out=Path(args.output)
    if out.exists(): raise PipelineError('Evaluation question file already exists')
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text(''.join(dumps(q)+'\n' for q in selected),encoding='utf-8')
    distribution={business:sum(q['business_id']==business for q in selected) for business in sorted({q['business_id'] for q in selected})}
    write_json(out.with_suffix('.manifest.json'),{'count':len(selected),'seed':args.seed,'business_counts':distribution,'selection_policy':'balanced_businesses' if businesses else 'uniform_questions','source_sha256':file_hash(args.questions),'questions_sha256':file_hash(out)})
    print(f'Prepared {len(selected)} questions: {dumps(distribution)}. No inference/evaluation executed.')

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('run');p.add_argument('--split',required=True);p.add_argument('--questions');p.add_argument('--output',required=True)
    p.add_argument('--model',choices=['qwen3_4b','qwen25_7b','gemma4_12b']);p.add_argument('--device')
    p.add_argument('--model-config',default=str(ROOT/'configs/model.yaml'));p.add_argument('--database-config',default=str(ROOT/'configs/database.yaml'))
    p.add_argument('--pipeline-config',default=str(ROOT/'configs/pipeline.yaml'));p.add_argument('--cache',default=str(ROOT/'runtime/cache'));p.add_argument('--warmup-calls',type=int,default=1)
    p.add_argument('--submission-mode',action='store_true',help='Allow an existing empty result directory; predictions are flushed directly to disk')
    p.add_argument('--reuse-ingestion-from',type=Path,help='QA only: reuse verified data fingerprints/cache from this prior run; never re-ingest')
    p.add_argument('--reuse-successful-ingestion-from',type=Path,help='Reuse successful businesses from this prior run; ingest failed/unprepared businesses with current code')
    p.add_argument('--stop-on-ingestion-error',action='store_true',help='Development mode: stop before QA if any business failed preparation')
    p=sub.add_parser('ingest');p.add_argument('--split',required=True);p.add_argument('--business',action='append');p.add_argument('--output',required=True)
    p.add_argument('--model',choices=['qwen3_4b','qwen25_7b','gemma4_12b']);p.add_argument('--device');p.add_argument('--no-database',action='store_true')
    p.add_argument('--model-config',default=str(ROOT/'configs/model.yaml'));p.add_argument('--database-config',default=str(ROOT/'configs/database.yaml'))
    p.add_argument('--pipeline-config',default=str(ROOT/'configs/pipeline.yaml'));p.add_argument('--cache',default=str(ROOT/'runtime/cache'))
    p=sub.add_parser('prepare-eval');p.add_argument('--questions',required=True);p.add_argument('--count',type=int,default=50);p.add_argument('--seed',type=int,default=42);p.add_argument('--output',required=True)
    p.add_argument('--business',action='append',help='Restrict to this business; repeat for multiple businesses. The total count is divided evenly.')
    p=sub.add_parser('evaluate');p.add_argument('--run',required=True);p.add_argument('--references',required=True);p.add_argument('--annotations')
    p=sub.add_parser('compare');p.add_argument('--runs',nargs='+',required=True);p.add_argument('--output',required=True)
    p.add_argument('--allow-mixed-ingestion',action='store_true',help='Compare QA with fresh/reused own-model ingestion; exclude reused runs from cold end-to-end timing')
    p=sub.add_parser('status');p.add_argument('--run',required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='run': return run(args)
        if args.command=='ingest': ingest(args)
        elif args.command=='prepare-eval': prepare_eval(args)
        elif args.command=='evaluate':
            from .evaluation.evaluator import evaluate
            print(dumps(evaluate(args.run,args.references,annotations=args.annotations)['proxy_PA']))
        elif args.command=='compare':
            from .evaluation.compare_variants import compare_runs
            compare_runs(args.runs,args.output,allow_mixed_ingestion=args.allow_mixed_ingestion)
        elif args.command=='status':
            from .progress import print_status
            print_status(args.run)
        return 0
    except (OSError,ValueError,KeyError) as error:
        emit('run_error',f'{type(error).__name__}: operation failed; see terminal error',phase='failed')
        print(f'{type(error).__name__}: {error}',file=sys.stderr);return 2
    except KeyboardInterrupt:
        emit('interrupted','Run interrupted by Ctrl+C',phase='interrupted')
        return 130

if __name__=='__main__': raise SystemExit(main())
