#!/usr/bin/env python3
"""Summarize local business metadata candidates; no database/model/QA access."""
import argparse
from collections import Counter
import json
from pathlib import Path


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True);a=p.parse_args()
    files=sorted(a.root.glob('*/metadata.json'))
    if not files:p.error('No business metadata files')
    counts=Counter();rows=[];details=[]
    def cell(value):return str(value).replace('|','\\|').replace('\n',' ')
    for path in files:
        business=json.loads(path.read_text())
        for name,t in business['tables'].items():
            counts[t['grain']['kind']]+=1
            checks={'grain_evidence_status':t['grain'].get('evidence',{}).get('status','unresolved'),
                'groups':len(t['group_keys']),'measures':len(t['measures']),'joins':len(t['joins']),
                'fanout_risks':sum(bool(j.get('data_checks',{}).get('fanout_risk')) for j in t['joins']),
                'contract_error':t.get('error')}
            keys='; '.join(k['name']+': '+', '.join(k['columns']) for k in t['group_keys']) or '—'
            rows.append('| '+' | '.join(map(cell,[business['business_id'],name,t['grain']['kind'],keys,
                checks['measures'],checks['joins'],checks['fanout_risks'],checks['grain_evidence_status']]))+' |')
            details.append({'business_id':business['business_id'],'table':name,'grain':t['grain'],**checks})
    report={'businesses':len(files),'tables':len(details),'grain_kinds':dict(counts),'tables_detail':details,
        'note':'All semantics remain candidates; exact quote and structural checks are not business proof.'}
    (a.root/'overview.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    lines=['# Metadata nghiệp vụ — tổng quan','',
        f"{len(files)} doanh nghiệp, {len(details)} bảng. Grain unknown: {counts['unknown']}.",
        '', 'Mọi grain/measure/JOIN vẫn là candidates. Mở <biz>/review.md để xem evidence và data checks; model_calls.jsonl giữ raw output/lỗi. Không coi khóa nhóm là khóa duy nhất của dòng.', '',
        '| Biz | Bảng | Grain candidate | Khóa nhóm đề xuất | Measures | JOIN | Fanout risks | Grain evidence |',
        '|---|---|---|---|---:|---:|---:|---|',*rows]
    (a.root/'overview.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'businesses':len(files),'tables':len(details),'grain_kinds':dict(counts)},ensure_ascii=False))


if __name__=='__main__':main()
