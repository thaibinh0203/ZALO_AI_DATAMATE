#!/usr/bin/env python3
"""Build a readable index of semantic reviews after a completed pipeline run."""
import argparse
from collections import Counter
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',required=True,type=Path)
    args=parser.parse_args();root=args.run_dir
    run=json.loads((root/'run.json').read_text())
    if run['status']!='complete':parser.error('Run is not complete')
    counts=Counter();calls=Counter();businesses=[];attention=[];changes=[]
    for item in run['businesses']:
        biz=item['business_id'];review=json.loads((root/biz/'semantic_review.json').read_text())
        counts.update(review['summary'])
        businesses.append({**item,'review':f'{biz}/semantic_review.md'})
        for col in review['columns']:
            if col.get('status')=='applied_candidate':changes.append({'business_id':biz,**col})
            if col.get('status') in {'unresolved','conflict_existing','role_conflict'}:attention.append({'business_id':biz,**col})
        # Keep the short matrix and add evidence details without exposing raw samples.
        review_path=root/biz/'semantic_review.md'
        original=review_path.read_text().split('<!-- semantic-evidence -->')[0].rstrip()
        details=['','<!-- semantic-evidence -->','','## Bằng chứng và đề xuất chi tiết','']
        for col in review['columns']:
            proposed=col.get('proposed')
            if not proposed:continue
            details.extend([f"### {col['table']} / {col['column']}",'',
                f"- Trạng thái: `{col['status']}`; áp dụng: `{col['role']} / {col['sql_type']}`.",
                f"- SLM đề xuất: `{proposed['role']} / {proposed['sql_type']}`; {proposed['meaning']}",
                f"- Hướng xử lý đề xuất (không thực thi): {proposed['handling']}"])
            ref=col.get('evidence_reference') or {}
            if ref:details.append(f"- Tham chiếu nguồn: `{ref.get('id')}` → `{ref.get('location')}`.")
            for evidence in proposed.get('evidence',[]):
                details.append(f"- Nguồn {evidence['source']}: {evidence['quote']}")
            details.append('')
        review_path.write_text(original+'\n'+'\n'.join(details)+'\n',encoding='utf-8')
        audit=root/'_semantic'/biz/'model_calls.jsonl'
        if audit.exists():
            for line in audit.read_text().splitlines():
                record=json.loads(line);calls['logged_attempts']+=1
                if record.get('replayed_semantic_plan'):calls['replayed_plan_attempts']+=1
                else:calls['cache_hits' if record['cache_hit'] else 'new_generation_calls']+=1
                calls['parse_errors_including_retried']+=record['parse_error'] is not None
                calls['max_prompt_tokens']=max(calls['max_prompt_tokens'],record.get('prompt_tokens',0))
                calls['max_cuda_allocated_bytes']=max(calls['max_cuda_allocated_bytes'],record.get('peak_cuda_allocated_bytes',0))
                if not record['cache_hit'] and not record.get('replayed_semantic_plan'):calls['generation_ms']+=record.get('generation_ms',0)
    totals={k:sum(b[k] for b in businesses) for k in ('rows','tables','normalization_errors','derived_errors','base_quantity_unresolved','duplicate_rows')}
    report={'run_dir':str(root),'status':'complete','semantic_summary':dict(counts),
            'totals':totals,'model_attempts':dict(calls),'businesses':businesses,
            'applied_candidates':changes,'needs_review':attention,
            'note':'Applied mappings passed structural checks, not proof of business correctness. No questions or answers used.'}
    (root/'semantic_overview.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    def cell(value):return str(value or '').replace('|','\\|').replace('\n',' ')
    lines=['# Semantic pipeline — kết quả để review','',report['note'],'',
           '## Tổng quan','',f"- Business: {len(businesses)}; bảng: {totals['tables']}; dòng nguồn: {totals['rows']:,}.",
           f"- Semantic status: `{dict(counts)}`.",f"- Model attempts: `{dict(calls)}`.",
           f"- Quality: normalization errors={totals['normalization_errors']}; derived errors={totals['derived_errors']}; unresolved base quantity={totals['base_quantity_unresolved']}.",
           '', '## Theo doanh nghiệp','',
           '| Business / review | Applied candidate | Described other | Conflict | Unresolved | Model-error tables |',
           '|---|---:|---:|---:|---:|---:|']
    for b in businesses:
        c=b['semantic_summary']
        lines.append(f"| [{b['business_id']}]({b['review']}) | {c.get('applied_candidate',0)} | {c.get('described_other',0)} | {c.get('conflict_existing',0)+c.get('role_conflict',0)} | {c.get('unresolved',0)} | {b['semantic_model_error_tables']} |")
    for title,rows in [('Mapping mới được áp dụng — vẫn là candidate',changes),('Cần xem lại — giữ dữ liệu/mapping hiện có',attention)]:
        lines.extend(['',f'## {title}','','| Business | Bảng / cột | Role / kiểu hiện tại | Ý nghĩa SLM | Trạng thái / checks |','|---|---|---|---|---|'])
        for c in rows:
            p=c.get('proposed') or {}
            lines.append('| '+' | '.join(map(cell,[c['business_id'],c['table']+' / '+c['column'],
                c['role']+' / '+c['sql_type'],p.get('meaning',''),c['status']+'; '+'; '.join(c.get('checks',[]))]))+' |')
    (root/'semantic_overview.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({'totals':totals,'semantic_summary':dict(counts),'model_attempts':dict(calls)},ensure_ascii=False))


if __name__=='__main__':main()
