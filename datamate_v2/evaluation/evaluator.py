"""Local proxy, not the unavailable official executable scorer."""
from pathlib import Path
import csv
import io
from datamate_v2.common import read_jsonl,read_json,write_json,dumps,PipelineError,file_hash
from .matching import score_answer,PROFILE
from .metrics import aggregate_logs,rate
from .error_analysis import annotate

LIMITATIONS=['Local proxy: official executable scorer not supplied.',
 'Numbers use exact Decimal equality; floating-point tolerance unspecified by official rules.',
 'Percent tie rounding uses HALF_UP; tables use multiset matching (duplicate policy not confirmed).',
 'No synthetic benchmark scores; semantic errors require explicit reviewed annotations.']

def evaluate(run_dir,references,*,annotations=None):
    run_dir=Path(run_dir);manifest=read_json(run_dir/'run_manifest.json')
    predictions=read_jsonl(run_dir/'submission.jsonl');logs=read_jsonl(run_dir/'logs/questions.jsonl')
    questions=read_jsonl(run_dir/'questions.jsonl');ids={q['id'] for q in questions}
    if set(p['id'] for p in predictions)-ids: raise PipelineError('Predictions include unknown ids')
    if set(x['id'] for x in logs)!=ids: raise PipelineError('Question logs must cover selected questions exactly')
    refs={r['id']:r for r in read_jsonl(references)};preds={r['id']:r for r in predictions}
    if ids-set(refs): raise PipelineError('Selected questions are missing references')
    logs=annotate(logs,annotations);scored=[];confusion={}
    log_by_id={x['id']:x for x in logs}
    for q in questions:
        record={'id':q['id'],'business_id':q['business_id'],**score_answer(refs[q['id']],preds.get(q['id']))}
        log=log_by_id[q['id']]
        record.update(question=q,rewritten=log.get('rewritten'),validated_windows=log.get('validated_windows'),
                      calculation_plan=log.get('plan',{}).get('calculation_plan'),sql=log.get('sql'),result=log.get('result'),
                      final_answer=log.get('answer'),root_cause=log.get('root_cause','unknown'),elapsed_ms=log['elapsed_ms'],
                      stages=log.get('stages',[]),model_calls=log.get('model_calls',[]),repair_attempts=log.get('repair_attempts',0),
                      data_fingerprint=log.get('data_fingerprint'),model_version=log.get('model_version'))
        scored.append(record)
        expected='unanswerable' if refs[q['id']]['type']=='unanswerable' else 'answer'
        p=preds.get(q['id'],{}).get('type','abstain');predicted=p if p in ('unanswerable','abstain') else 'answer'
        confusion.setdefault(expected,{}).setdefault(predicted,0);confusion[expected][predicted]+=1
    correct=sum(r['score']==1 for r in scored);wrong=sum(r['score']==-1 for r in scored);n=len(questions)
    report={'profile':PROFILE,'official_score':None,'proxy_PA':rate(correct-wrong,n),'proxy_accuracy':rate(correct,n),
            'correct':correct,'wrong':wrong,'abstain':n-correct-wrong,'n':n,'limitations':LIMITATIONS,
            'references_sha256':file_hash(references),'run_manifest':manifest,'confusion_matrix':confusion,
            'performance':aggregate_logs(logs,scored)}
    classes={}
    for label in ('answer','unanswerable','abstain'):
        tp=confusion.get(label,{}).get(label,0)
        support=sum(confusion.get(label,{}).values());predicted=sum(row.get(label,0) for row in confusion.values())
        classes[label]={'precision':rate(tp,predicted),'recall':rate(tp,support),'support':support}
    report['answerability_metrics']=classes
    out=run_dir/'evaluation';out.mkdir(exist_ok=True)
    write_json(out/'report.json',report)
    (out/'per_question.jsonl').write_text(''.join(dumps(x)+'\n' for x in scored),encoding='utf-8')
    fields=list(scored[0]) if scored else ['id','score']
    buf=io.StringIO();writer=csv.DictWriter(buf,fieldnames=fields);writer.writeheader()
    writer.writerows({k:dumps(v) if isinstance(v,(dict,list)) else v for k,v in record.items()} for record in scored)
    (out/'per_question.csv').write_text(buf.getvalue(),encoding='utf-8')
    lines=['# DataMate V2 evaluation (local proxy)',f"Questions: {n}. Correct: {correct}; wrong: {wrong}; abstain: {n-correct-wrong}.",
      f"Proxy PA = (correct − wrong) / all selected questions = {correct-wrong}/{n} = {report['proxy_PA']['value']}.",
      'Official score: unavailable. No official executable scorer was provided.',
      'SQL success denominator: attempted answer executions only; UA/abstain without SQL are excluded.',
      'Semantic error denominator: explicitly classified questions; unclassified questions remain unknown.',
      'Latency uses monotonic clock; startup/loading and ingestion are separate from warm QA. p95 is uncertain at n=50.',
      'Token counts come from the actual tokenizer/backend; unavailable counts remain unknown.',
      'Performance and metric numerators/denominators:','```json',dumps(report['performance'],indent=2),'```',*LIMITATIONS]
    (out/'report.md').write_text('\n\n'.join(lines)+'\n',encoding='utf-8')
    return report
