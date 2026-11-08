#!/usr/bin/env python3
"""Local readable runtime/evaluation report, including a standalone HTML viewer."""
import argparse
from collections import Counter
import html
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from datamate_baseline.serializer import dumps


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run-dir',type=Path,required=True);a=p.parse_args();root=a.run_dir
    run=json.loads((root/'run.json').read_text());traces=[json.loads(l) for l in (root/'traces.jsonl').read_text().splitlines()]
    errors=Counter((r.get('error') or {}).get('category') for r in traces if r.get('error'))
    requested=[r for r in traces if isinstance(r.get('parsed_output'),dict) and r['parsed_output'].get('status')=='sql']
    totals={'questions':len(traces),'generated':sum(r['raw_output'] is not None for r in traces),
        'understanding_model_calls':sum(len(r.get('understanding_calls',[])) for r in traces),
        'understanding_generated_outputs':sum(c.get('raw_output') is not None for r in traces for c in r.get('understanding_calls',[])),
        'understanding_refinements':sum(len({c['stage'] for c in r.get('understanding_calls',[])})<len(r.get('understanding_calls',[])) for r in traces),
        'json_valid':sum(r['json_valid'] for r in traces),'plan_valid':sum(r['contract_valid'] for r in traces),
        'sql_requested':len(requested),'sql_gate_valid':sum(bool(r['sql_valid']) for r in traces),
        'sql_execution_success':sum(r['sql_executed'] for r in traces),
        'final_types':dict(Counter(r['answer']['type'] for r in traces)),'errors':dict(errors),
        'startup_ms':run.get('startup_ms'),'wall_ms':run['elapsed_ms']}
    evaluation=None;diagnostics={}
    if (root/'evaluation/report.json').exists():
        evaluation=json.loads((root/'evaluation/report.json').read_text())
        diagnostics={r['id']:r for r in [json.loads(l) for l in (root/'evaluation/diagnostics.jsonl').read_text().splitlines()]}
    (root/'runtime_summary.json').write_text(dumps(totals)+'\n')
    lines=['# Baseline SQL — kết quả','',f"- Run: `{run['status']}`; model load: {run.get('model_load_ms')} ms; startup: {run.get('startup_ms')} ms; toàn run: {run['elapsed_ms']} ms.",
        f"- Questions: {len(traces)}; có output model: {totals['generated']}; JSON hợp lệ: {totals['json_valid']}; plan hợp lệ (gồm SQL sơ bộ): {totals['plan_valid']}.",
        f"- SQL requested: {len(requested)}; qua gate: {totals['sql_gate_valid']}; thực thi thành công: {totals['sql_execution_success']}.",
        f"- Final types: {totals['final_types']}; lỗi: {dict(errors)}.",'',
        'SQL chạy được không chứng minh metric đúng. Xem evaluation/report.json để chấm final answers. Diagnostics có đáp án tham chiếu, chỉ dùng sau inference; không đưa vào prompt.','']
    if evaluation:
        lines += [f"- Evaluation: {evaluation.get('scope','full_partition')}; {evaluation['summary']}.",
            f"- Unanswerable: {evaluation['unanswerable']}.",f"- Latency: {evaluation['timing']}.",'']
    lines+=['## File để đọc','','- `report.html`: lọc theo ID/business/outcome và xem SQL/đáp án/lỗi.','- `traces.jsonl`: toàn bộ prompt, raw output, parsed plan, query result, lỗi và timings.','- `predictions.jsonl`: đáp án cuối; `timings.jsonl`: thời gian từng câu.','- `evaluation/report.json`: điểm và breakdown; `evaluation/diagnostics.jsonl`: expected/prediction sau chấm.']
    (root/'summary.md').write_text('\n'.join(lines)+'\n')
    def esc(v):return html.escape(str(v))
    rows=[]
    for r in traces:
        d=diagnostics.get(r['id'],{});parsed=r.get('plan') or r.get('parsed_output') or {};query=parsed.get('sql') if isinstance(parsed,dict) else None
        detail={'sql':query,'answer':r['answer'],'expected':d.get('expected'),'error':r.get('error'),
            'question_analysis':r.get('question_analysis'),'schema_linking':r.get('schema_linking'),
            'understanding_calls':r.get('understanding_calls'),
            'raw_model_output':r.get('raw_output'),'sql_executed':(r.get('query_result') or {}).get('sql_executed')}
        search=' '.join([r['id'],r['business_id'],d.get('outcome',r['answer']['type']),r['request']['question']])
        rows.append('<tr data-search="'+esc(search)+'"><td>'+esc(r['id'])+'</td><td>'+esc(r['business_id'])+'</td><td>'+esc(d.get('outcome',r['answer']['type']))+'</td><td>'+esc(r['request']['question'])+'<details><summary>SQL / output / lỗi</summary><pre>'+esc(dumps(detail))+'</pre></details></td><td>'+esc(r['elapsed_ms'])+'</td></tr>')
    title=esc(root.name)
    page='''<!doctype html><html lang="vi"><meta charset="utf-8"><title>Baseline report</title><style>body{font:15px system-ui;margin:24px;background:#f8fafc;color:#14263d}table{border-collapse:collapse;width:100%;background:white}td,th{padding:10px;border:1px solid #ddd;vertical-align:top}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-width:900px}input{padding:10px;width:70%;margin:12px 0}summary{cursor:pointer;color:#155e9b}</style><h1>'''+title+'</h1><p>'+esc(dumps(evaluation['summary'] if evaluation else totals))+'''</p><p>Chỉ xem local. Expected answers thuộc evaluator, không phải input model.</p><input id="filter" placeholder="Lọc theo ID, business, correct/wrong/abstain hoặc câu hỏi"><table><thead><tr><th>ID</th><th>Business</th><th>Outcome</th><th>Câu hỏi / chi tiết</th><th>ms</th></tr></thead><tbody>'''+''.join(rows)+'''</tbody></table><script>document.getElementById('filter').addEventListener('input',function(){const q=this.value.toLowerCase();document.querySelectorAll('tbody tr').forEach(r=>r.hidden=!r.dataset.search.toLowerCase().includes(q));});</script></html>'''
    (root/'report.html').write_text(page,encoding='utf-8')
    print(dumps(totals))


if __name__=='__main__':main()
