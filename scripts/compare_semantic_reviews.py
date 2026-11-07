#!/usr/bin/env python3
"""Compare source-identical business semantic reviews; not an accuracy evaluator."""
import argparse
from collections import Counter
import json
from pathlib import Path


def compare(before, after):
    old_catalog=json.loads((before/'catalog.json').read_text())
    new_catalog=json.loads((after/'catalog.json').read_text())
    if old_catalog['business_id']!=new_catalog['business_id'] or old_catalog['sources']!=new_catalog['sources']:
        raise ValueError('Comparison requires the same business and source hashes')
    old=json.loads((before/'semantic_review.json').read_text())
    new=json.loads((after/'semantic_review.json').read_text())
    def index(review):return {(c['table'],c['column']):c for c in review['columns']}
    def unknown(c):return (c.get('proposed') or {}).get('meaning') in {'unknown','',None}
    old_idx=index(old);new_idx=index(new)
    if old_idx.keys()!=new_idx.keys():raise ValueError('Column layout changed')
    changes=[]
    for identity,c in old_idx.items():
        if c.get('status')=='described_other' and unknown(c):
            n=new_idx[identity]
            changes.append({'table':identity[0],'column':identity[1],
                'before_meaning':(c.get('proposed') or {}).get('meaning'),
                'after_meaning':(n.get('proposed') or {}).get('meaning'),
                'canonical_role':n.get('canonical_role'),'semantic_role':n.get('semantic_role'),
                'has_data':n.get('has_data'),'sql_type':n['sql_type'],'status':n['status'],
                'checks':n.get('checks',[])})
    return {'business_id':old['business_id'],'before':str(before),'after':str(after),
        'before_status':dict(Counter(c['status'] for c in old_idx.values())),
        'after_status':dict(Counter(c['status'] for c in new_idx.values())),
        'previous_unknown_columns':len(changes),'columns':changes,
        'note':'Metadata comparison only; source-grounded descriptions and role proposals still require business review. No QA accuracy measured.'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--before',type=Path,required=True);p.add_argument('--after',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True);a=p.parse_args()
    report=compare(a.before,a.after)
    a.output_dir.mkdir(parents=True,exist_ok=False)
    (a.output_dir/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    def cell(v):return ('null' if v is None else str(v)).replace('|','\\|').replace('\n',' ')
    lines=[f"# Semantic comparison — {report['business_id']}",'',report['note'],'',
        '| Bảng | Cột nguồn | Ý nghĩa cũ | Ý nghĩa mới | Canonical role | Semantic role | Kiểu | Có dữ liệu | Trạng thái |',
        '|---|---|---|---|---|---|---|---|---|']
    for c in report['columns']:
        lines.append('| '+' | '.join(cell(c[k]) for k in ['table','column','before_meaning','after_meaning',
            'canonical_role','semantic_role','sql_type','has_data','status'])+' |')
    (a.output_dir/'comparison.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='columns'},ensure_ascii=False))


if __name__=='__main__':main()
