#!/usr/bin/env python3
"""Compare two evaluated runs on identical questions; never used by inference."""
import argparse
from collections import Counter
import html
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from datamate_baseline.serializer import dumps


def load(root):
    def records(name):return [json.loads(l) for l in (root/name).open()]
    run=json.loads((root/'run.json').read_text())
    if run['status']!='complete':raise ValueError('Compare complete runs only')
    questions=records('questions.jsonl');traces=records('traces.jsonl')
    report=json.loads((root/'evaluation/report.json').read_text())
    diagnostics=records('evaluation/diagnostics.jsonl')
    return {'run':run,'questions':{r['id']:r for r in questions},
            'traces':{r['id']:r for r in traces},'report':report,
            'diagnostics':{r['id']:r for r in diagnostics}}


def metrics(data):
    traces=list(data['traces'].values())
    return {**data['report']['summary'],
        'errors':dict(Counter(r['error']['category'] for r in traces if r.get('error'))),
        'sql_execution_success':sum(r['sql_executed'] for r in traces),
        'json_valid':sum(r['json_valid'] for r in traces),
        'understanding_calls':sum(len(r.get('understanding_calls',[])) for r in traces),
        'sql_generation_calls':sum(r.get('raw_output') is not None for r in traces),
        'latency':data['report']['timing'],'startup_ms':data['run'].get('startup_ms'),
        'wall_ms':data['run']['elapsed_ms'],
        'wall_time_note':data['run'].get('wall_time_note','Measured from CLI main; Python imports excluded.')}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--before',required=True,type=Path);p.add_argument('--after',required=True,type=Path)
    p.add_argument('--output-dir',required=True,type=Path);a=p.parse_args()
    if a.output_dir.exists():p.error('Output exists')
    before=load(a.before);after=load(a.after)
    if before['questions']!=after['questions']:p.error('Questions differ; cannot make paired comparison')
    ids=list(before['questions']);transitions=Counter();details=[]
    for id in ids:
        b=before['diagnostics'][id];n=after['diagnostics'][id]
        transitions[b['outcome']+' -> '+n['outcome']]+=1
        q=before['questions'][id]
        def detail(data):
            t=data['traces'][id];plan=t.get('plan') or t.get('parsed_output') or {}
            return {'outcome':data['diagnostics'][id]['outcome'],'prediction':t['answer'],
                'sql':plan.get('sql') if isinstance(plan,dict) else None,'error':t.get('error'),
                'elapsed_ms':t['elapsed_ms'],'analysis':t.get('question_analysis'),
                'bindings':t.get('schema_linking'),'understanding_errors':[{'stage':c['stage'],'error':c.get('error')}
                    for c in t.get('understanding_calls',[]) if c.get('error')]}
        details.append({'id':id,**q,'expected':b['expected'],'before':detail(before),'after':detail(after)})
    report={'scope':'paired_development_diagnostic_subset','questions':len(ids),
        'before_run':str(a.before),'after_run':str(a.after),'before':metrics(before),'after':metrics(after),
        'transitions':dict(transitions),
        'limitations':['Previously inspected development questions, not blind generalization.',
            'Old predictions reused without rerunning model; old wall time reconstructed.',
            'Business metadata preparation time is separate from inference wall time.',
            'All error stages count, including new answer_contract/question_understanding stages.']}
    a.output_dir.mkdir(parents=True)
    (a.output_dir/'comparison.json').write_text(dumps(report)+'\n')
    (a.output_dir/'per_question.jsonl').write_text(''.join(dumps(r)+'\n' for r in details))
    lines=['# So sánh baseline trên cùng '+str(len(ids))+' câu development','',
           'Đây là diagnostic subset đã dùng để phát triển, không phải đo generalization độc lập.','',
           '| Chỉ số | V1 đã lưu | V2 chạy mới |','|---|---:|---:|']
    for k in ['correct','wrong','abstain','pa','coverage','answered_accuracy','format_errors','sql_execution_success','understanding_calls','sql_generation_calls']:
        lines.append('| '+k+' | '+str(report['before'][k])+' | '+str(report['after'][k])+' |')
    lines += ['', '## Lỗi theo bước', '', '```json',json.dumps({'before':report['before']['errors'],'after':report['after']['errors']},ensure_ascii=False,indent=2),'```',
              '', '## Chuyển kết quả từng câu','','```json',json.dumps(dict(transitions),indent=2),'```','',
              'Mở report.html để lọc câu và xem analysis, binding, SQL và lỗi. Thời gian metadata prep tách riêng; thời gian v1 subset dựng từ startup cũ + 50 elapsed, không phải cold-run mới.']
    (a.output_dir/'summary.md').write_text('\n'.join(lines)+'\n')
    esc=lambda v:html.escape(str(v))
    rows=[]
    for r in details:
        rows.append('<tr><td>'+esc(r['id'])+'<br>'+esc(r['business_id'])+'</td><td>'+esc(r['question'])+
            '</td><td>'+esc(r['before']['outcome'])+' → '+esc(r['after']['outcome'])+
            '<details><summary>SQL / hiểu câu hỏi / binding / đáp án</summary><pre>'+esc(json.dumps(r,ensure_ascii=False,indent=2))+'</pre></details></td></tr>')
    page='''<!doctype html><html lang="vi"><meta charset="utf-8"><title>Baseline comparison</title><style>body{font:15px system-ui;margin:24px;color:#19314b}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:12px;vertical-align:top}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-width:900px}input{padding:12px;width:80%;margin:12px 0}summary{cursor:pointer}</style><h1>Baseline v1 → v2: '''+str(len(ids))+''' câu</h1><p>Development diagnostic subset, không phải generalization độc lập. Expected chỉ do evaluator đọc sau inference.</p><pre>'''+esc(json.dumps({'before':report['before'],'after':report['after']},ensure_ascii=False,indent=2))+'''</pre><input id="filter" placeholder="Lọc ID, biz, câu hỏi, lỗi, outcome"><table><thead><tr><th>ID / biz</th><th>Câu hỏi</th><th>Trước → sau</th></tr></thead><tbody>'''+''.join(rows)+'''</tbody></table><script>document.getElementById('filter').addEventListener('input',function(){const q=this.value.toLowerCase();document.querySelectorAll('tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q));});</script></html>'''
    (a.output_dir/'report.html').write_text(page)
    print(dumps({'before':report['before']['pa'],'after':report['after']['pa'],'questions':len(ids),'transitions':dict(transitions)}))


if __name__=='__main__':main()
