"""One model request and at most one SQL execution per question."""
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from time import perf_counter
import psycopg
from sqlglot.errors import SqlglotError
from datamate_b.contracts import strict_json, validate_request, validate_result
from .context import ContextLoader, PROMPT_VERSION
from .serializer import dumps, format_answer
from .query_tool import checked_sql
from .understanding import interpret


def validate_answer_projection(plan, analysis, qualified_sql):
    """Reject shape mismatch before DB execution; never drop columns to repair it."""
    import sqlglot
    from sqlglot import exp
    kind=analysis['answer']['type']
    if plan['answer_type']!=kind:raise ValueError('SQL answer_type differs from structured requested answer')
    tree=sqlglot.parse_one(qualified_sql,read='postgres')
    select=tree if isinstance(tree,exp.Select) else tree.find(exp.Select)
    if select is None:raise ValueError('Missing final SELECT')
    if kind!='table' and len(select.expressions)!=1:
        raise ValueError('Final '+kind+' projection must contain exactly one column; auxiliary ranking measures belong inside CTE/ORDER BY')


def file_sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def requests_from_file(path):
    rows=[];ids=set()
    for n,line in enumerate(Path(path).read_text(encoding='utf-8-sig').splitlines(),1):
        if not line.strip():continue
        r=validate_request(strict_json(line))
        if not isinstance(r.get('id'),str) or not r['id'].strip():raise ValueError(f'Question line {n}: missing id')
        if not isinstance(r.get('business_id'),str) or not r['business_id'].strip():raise ValueError('Missing business_id')
        if set(r)!={'id','business_id','question','as_of_date'}:raise ValueError('Question input must not contain labels or unexpected fields')
        if r['id'] in ids:raise ValueError('Duplicate question id')
        ids.add(r['id']);rows.append(r)
    if not rows:raise ValueError('Empty question input')
    return rows


def run_batch(requests,*,model,query_tool,loader,output_dir,config,external_startup_ms=0):
    output=Path(output_dir);output.mkdir(parents=True,exist_ok=False)
    started=perf_counter();outcomes=Counter();error_counts=Counter();context_hashes={}
    grounded=config.get('question_understanding',False)
    manifest={'status':'running','started_at_utc':datetime.now(timezone.utc).isoformat(),
        'question_ids':[r['id'] for r in requests],'questions':len(requests),
        'business_ids':sorted({r['business_id'] for r in requests}),
        'config':config,'prompt_version':'baseline-grounded-v2' if grounded else PROMPT_VERSION,'model':model.metadata(),
        'labels_read':False,'repairs':0,'sql_execution_limit_per_question':1,
        'model_requests_limit_per_question':4 if grounded else 1,
        'prepared_root':str(loader.prepared_root),'source_root':str(loader.source_root),
        'code_sha256':{p.name:file_sha(p) for p in Path(__file__).parent.glob('*.py')}}
    (output/'questions.jsonl').write_text(''.join(dumps(r)+'\n' for r in requests),encoding='utf-8')
    processed=0
    try:
        model.load()
        manifest['model_load_ms']=model.load_ms
        manifest['startup_ms']=round(external_startup_ms+(perf_counter()-started)*1000,3)
        with (output/'predictions.jsonl').open('w',encoding='utf-8') as predictions, (output/'timings.jsonl').open('w') as timings, (output/'traces.jsonl').open('w',encoding='utf-8') as traces:
            for request in requests:
                begin=perf_counter();stage='context'
                trace={'id':request['id'],'business_id':request['business_id'],'request':request,
                    'raw_output':None,'parsed_output':None,'json_valid':False,'contract_valid':False,
                    'sql_valid':None,'sql_executed':False,'plan':None,'query_result':None,'error':None,'timing_ms':{}}
                answer={'id':request['id'],'type':'abstain','answer':None}
                try:
                    context=loader.load(request['business_id'])
                    context_hashes[request['business_id']]=context['fingerprint']
                    trace['context_fingerprint']=context['fingerprint'];trace['pg_schema']=context['pg_schema']
                    if grounded:
                        stage='question_understanding';t=perf_counter()
                        analysis,links=interpret(model,context,request,trace,loader)
                        trace['timing_ms']['understanding']=round((perf_counter()-t)*1000,3)
                        if links['status']!='grounded':
                            # Only explicit source evidence can justify missing_data.
                            kind='unanswerable' if links['status']=='missing_data' else 'abstain'
                            trace['grounding_decision']={'status':kind,'reason':links['reasoning']}
                            messages=None
                        else:messages=loader.messages_for_plan(context,request,analysis,links)
                    else:messages=loader.messages(context,request)
                    if messages is None:
                        answer={'id':request['id'],'type':kind,'answer':None,
                                'reason':'; '.join(m['requirement'] for m in links['missing_requirements']) or links['reasoning']}
                        # A normal non-SQL decision still goes through common logging.
                        trace['answer']=answer
                    trace['messages']=messages
                    trace['timing_ms']['context']=round(max(0,(perf_counter()-begin)*1000-trace['timing_ms'].get('understanding',0)),3)
                    if messages is None:raise GroundingDecision(answer)
                    stage='generation';t=perf_counter()
                    generated=model.generate(messages)
                    trace.update(generated)
                    trace['timing_ms']['generation']=round((perf_counter()-t)*1000,3)
                    if generated.get('hit_token_limit'):raise ValueError('Generation reached output token limit')
                    stage='plan_validation';t=perf_counter()
                    parsed=strict_json(generated['raw_output']);trace['parsed_output']=parsed;trace['json_valid']=True
                    plan=validate_result(parsed,context['ddl'],'postgres')
                    trace['contract_valid']=True
                    trace['plan']=plan
                    trace['timing_ms']['plan_validation']=round((perf_counter()-t)*1000,3)
                    if plan['status']=='sql':
                        stage='sql_validation';t=perf_counter()
                        qualified=checked_sql(plan['sql'],context['ddl'],context['allowed']);trace['sql_valid']=True
                        trace['timing_ms']['sql_validation']=round((perf_counter()-t)*1000,3)
                        if grounded:
                            stage='answer_contract'
                            validate_answer_projection(plan,analysis,qualified)
                        stage='sql';t=perf_counter()
                        result=query_tool.execute_sql(context,plan['sql']);trace['query_result']=result;trace['sql_executed']=True
                        trace['timing_ms']['sql']=round((perf_counter()-t)*1000,3)
                        stage='serialization';t=perf_counter()
                        answer=format_answer(request['id'],plan['answer_type'],result)
                        trace['timing_ms']['serialization']=round((perf_counter()-t)*1000,3)
                    else:
                        status='abstain' if grounded and plan['status']=='unanswerable' else plan['status']
                        answer={'id':request['id'],'type':status,'answer':None,
                            'reason':'; '.join(plan['missing_requirements']) or 'Model abstained'}
                except GroundingDecision as decision:
                    answer=decision.answer
                except (ValueError,KeyError,TypeError,SqlglotError,psycopg.Error) as exc:
                    category='context_budget' if 'exceeds context' in str(exc).lower() else ('query_timeout' if getattr(exc,'sqlstate',None)=='57014' else stage)
                    trace['error']={'category':category,'exception':type(exc).__name__,
                        'message':exc.diag.message_primary if isinstance(exc,psycopg.Error) and exc.diag else str(exc),
                        'sqlstate':getattr(exc,'sqlstate',None)}
                    error_counts[category]+=1
                    answer['reason']=category
                trace['answer']=answer
                trace['model_request_attempts']=len(trace.get('understanding_calls',[]))+(trace.get('raw_output') is not None)
                elapsed=round((perf_counter()-begin)*1000,3);trace['elapsed_ms']=elapsed
                predictions.write(dumps(answer)+'\n');predictions.flush()
                timings.write(dumps({'id':request['id'],'elapsed_ms':elapsed})+'\n');timings.flush()
                traces.write(dumps(trace)+'\n');traces.flush()
                processed+=1;outcomes[answer['type']]+=1
                print(dumps({'processed':processed,'total':len(requests),'id':request['id'],
                    'business_id':request['business_id'],'type':answer['type'],
                    'error':trace['error']['category'] if trace['error'] else None,'elapsed_ms':elapsed}),flush=True)
        manifest['status']='complete'
    except KeyboardInterrupt:
        manifest.update(status='interrupted',fatal_error_type='KeyboardInterrupt')
        raise
    except Exception as exc:
        manifest.update(status='failed',fatal_error_type=type(exc).__name__)
        raise
    finally:
        manifest.update(processed=processed,outcomes=dict(outcomes),errors=dict(error_counts),
            context_fingerprints=context_hashes,model=model.metadata(),
            elapsed_ms=round(external_startup_ms+(perf_counter()-started)*1000,3),
            inference_wall_ms=round((perf_counter()-started)*1000,3))
        (output/'run.json').write_text(dumps(manifest)+'\n',encoding='utf-8')
    return manifest


class GroundingDecision(Exception):
    """Control flow for a validated non-SQL grounding result, not a model error."""
    def __init__(self,answer):self.answer=answer
