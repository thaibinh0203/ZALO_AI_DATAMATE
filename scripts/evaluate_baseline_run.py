#!/usr/bin/env python3
"""Post-inference evaluation; labels never enter the baseline runner or prompts."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from datamate_eval.runner import load_partition,evaluate,read_jsonl,sha256_file
from datamate_baseline.serializer import dumps


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',type=Path,required=True)
    p.add_argument('--partition',choices=['train','development','calibration','holdout'],required=True)
    p.add_argument('--split-root',type=Path,default=ROOT/'artifacts/data_split_v1')
    p.add_argument('--subset',action='store_true',help='Explicit diagnostic subset; denominator is selected run IDs, not full partition')
    p.add_argument('--output-dir',type=Path)
    a=p.parse_args();out=a.output_dir or a.run_dir/'evaluation'
    if out.exists():p.error('Evaluation output exists')
    run=json.loads((a.run_dir/'run.json').read_text())
    if run['status']!='complete':p.error('Refusing incomplete run')
    questions,refs,manifest=load_partition(a.split_root,a.partition)
    ids=set(run['question_ids']);all_ids={q['id'] for q in questions}
    if ids-all_ids:p.error('Run contains questions outside partition')
    if not a.subset and ids!=all_ids:p.error('Run is a subset; use --subset explicitly for diagnostic evaluation')
    selected=[q for q in questions if q['id'] in ids];selected_refs=[r for r in refs if r['id'] in ids]
    saved={r['id']:r for r in read_jsonl(a.run_dir/'questions.jsonl')}
    if saved!={r['id']:r for r in selected}:p.error('Run questions differ from frozen inputs')
    predictions=read_jsonl(a.run_dir/'predictions.jsonl');timings=read_jsonl(a.run_dir/'timings.jsonl')
    if {r['id'] for r in predictions}!=ids:p.error('Run predictions do not cover selected IDs')
    report,records=evaluate(selected,selected_refs,predictions,business_metadata=manifest['businesses'],timings=timings)
    report.update(partition=a.partition,scope='diagnostic_subset' if a.subset else 'full_partition',
        selected_questions=len(ids),partition_questions=len(all_ids),
        selected_question_ids=run['question_ids'],startup_ms=run.get('startup_ms'),
        total_run_wall_ms=run['elapsed_ms'],prediction_sha256=sha256_file(a.run_dir/'predictions.jsonl'))
    out.mkdir(parents=True,exist_ok=False)
    (out/'report.json').write_text(dumps(report)+'\n')
    (out/'per_question.jsonl').write_text(''.join(dumps(r)+'\n' for r in records))
    # Explicit evaluator-only diagnostics: never mounted/read by the generator.
    qmap={q['id']:q for q in selected};rmap={r['id']:r for r in selected_refs};pmap={r['id']:r for r in predictions}
    (out/'diagnostics.jsonl').write_text(''.join(dumps({'id':d['id'],'question':qmap[d['id']]['question'],
        'business_id':qmap[d['id']]['business_id'],'outcome':d['outcome'],
        'expected':rmap[d['id']],'prediction':pmap[d['id']]})+'\n' for d in records))
    print(dumps({'scope':report['scope'],'partition':a.partition,**report['summary'],'report':str(out/'report.json')}))


if __name__=='__main__':main()
