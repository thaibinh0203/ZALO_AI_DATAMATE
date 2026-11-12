from collections import Counter
from decimal import Decimal
from datamate_v2.common import PipelineError

def percentile(values,q):
    if not values: return None
    xs=sorted(values);point=(len(xs)-1)*q;lo=int(point);hi=min(lo+1,len(xs)-1)
    return xs[lo]+(xs[hi]-xs[lo])*(point-lo)

def distribution(values):
    xs=[float(v) if isinstance(v,Decimal) else v for v in values]
    return {'n':len(xs),'mean':sum(xs)/len(xs) if xs else None,'p50':percentile(xs,.5),'p95':percentile(xs,.95),'max':max(xs) if xs else None}

def rate(numerator,denominator): return {'numerator':numerator,'denominator':denominator,'value':numerator/denominator if denominator else None}

def aggregate_logs(logs,scored=None):
    latency=distribution(x['elapsed_ms'] for x in logs)
    stages={}
    for log in logs:
        for stage in log.get('stages',[]): stages.setdefault(stage['stage'],[]).append(stage['duration_ms'])
    attempted=[q for log in logs for q in log.get('queries',[]) if q.get('purpose')=='answer']
    repairs=sum(log.get('repair_attempts',0)>0 for log in logs)
    calls=[sum(not c.get('cache_hit',False) for c in x.get('model_calls',[])) for x in logs]
    tokens=[];input_tokens=[];output_tokens=[];unknown_tokens=0
    for log in logs:
        cs=[c for c in log.get('model_calls',[]) if not c.get('cache_hit',False)]
        if any(c.get('input_tokens') is None or c.get('output_tokens') is None for c in cs): unknown_tokens+=1;continue
        ins=sum(c['input_tokens'] for c in cs);outs=sum(c['output_tokens'] for c in cs)
        input_tokens.append(ins);output_tokens.append(outs);tokens.append(ins+outs)
    by_stage={}
    for log in logs:
        for c in log.get('model_calls',[]):
            stage=by_stage.setdefault(c['task'],{'input_tokens':0,'output_tokens':0,'calls':0,'cache_hits':0,'unknown_token_calls':0})
            stage['cache_hits']+=bool(c.get('cache_hit',False));stage['calls']+=not c.get('cache_hit',False)
            if not c.get('cache_hit'):
                if c.get('input_tokens') is None or c.get('output_tokens') is None: stage['unknown_token_calls']+=1
                else: stage['input_tokens']+=c['input_tokens'];stage['output_tokens']+=c['output_tokens']
    semantic_kinds={'metric','rulebook','schema','entity','time','grain','join','filter','calculation','question'}
    classified=[x for x in logs if x.get('root_cause') in semantic_kinds|{'parser','header','normalization','summary','json','sql_syntax','execution','format','correct'}]
    semantic=sum(x.get('root_cause') in semantic_kinds for x in classified)
    result={'latency_ms':latency,'latency_seconds':{k:(v/1000 if v is not None and k!='n' else v) for k,v in latency.items()},
            'stage_latency_ms':{k:distribution(v) for k,v in stages.items()},
            'latency_groups_ms':{k:distribution(x['elapsed_ms'] for x in logs if predicate(x)) for k,predicate in {
              'no_repair':lambda x:not x.get('repair_attempts'), 'with_repair':lambda x:bool(x.get('repair_attempts')),
              'answered_value':lambda x:x['answer']['type'] not in ('unanswerable','abstain'),
              'unanswerable':lambda x:x['answer']['type']=='unanswerable','abstain':lambda x:x['answer']['type']=='abstain'}.items()},
            'sql_execution_success':rate(sum(q['status']=='success' for q in attempted),len(attempted)),
            'repair_rate':rate(repairs,len(logs)),'repair_attempts':distribution(x.get('repair_attempts',0) for x in logs),
            'repair_success':rate(sum(bool(x.get('repair_attempts')) and 'error_type' not in x for x in logs),repairs),
            'model_calls_per_question':distribution(calls),
            'tokens':{'input':distribution(input_tokens),'output':distribution(output_tokens),'total':distribution(tokens),
                      'input_sum':sum(input_tokens),'output_sum':sum(output_tokens),'total_sum':sum(tokens),'unknown_questions':unknown_tokens,'by_stage':by_stage},
            'semantic_error_rate':rate(semantic,len(classified)), 'semantic_errors_all':rate(semantic,len(logs)),
            'unknown_root_cause':sum(x.get('root_cause','unknown')=='unknown' for x in logs),
            'errors':dict(Counter(x['error_type'] for x in logs if x.get('error_type'))),
            'errors_by_stage':dict(Counter(s['stage'] for x in logs for s in x.get('stages',[]) if s.get('status')=='error')),
            'data_quality_failures':sum(any(s['stage']=='data_quality_validation' and s.get('status')=='error' for s in x.get('stages',[])) for x in logs),
            'execution_error_types':dict(Counter(q.get('error_type','unknown') for q in attempted if q['status']=='error')),
            'note':'p95 from a small sample (especially n=50) has high uncertainty; no inferred tokenizer counts. Semantic errors require explicit root-cause annotations.'}
    result['compilation_modes']=dict(Counter(x.get('compilation',{}).get('mode','not_reached') for x in logs))
    for stage in ('call_1','call_2','sql_guard'):
        events=[s for x in logs for s in x.get('stages',[]) if s['stage']==stage]
        result[stage+'_validation_pass']=rate(sum(s['status']=='success' for s in events),len(events))
    return result
