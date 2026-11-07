#!/usr/bin/env python3
"""Compare a bounded-refinement experiment to immutable baseline output; no labels."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def read(path):return json.loads(path.read_text(encoding='utf-8'))
def sha(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def role(c):return {'canonical':c.get('canonical_role'),'semantic':c.get('semantic_role')}
def usable(c):return any(role(c).values())


def primary_calls(path):
    if not path.exists():return [],[]
    primary=[];refinement=[]
    for line in path.read_text().splitlines():
        r=json.loads(line);payload=json.loads(r['messages'][1]['content'])
        if payload.get('semantic_refinement'):refinement.append(r)
        else:primary.append((r['cache_key'],r['raw_output'],r.get('parse_error')))
    return primary,refinement


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline',type=Path,default=ROOT/'outputs/data_pipeline/train')
    p.add_argument('--candidate',type=Path,default=ROOT/'outputs/data_pipeline/train_candidate')
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--baseline-snapshot',type=Path,help='Optional JSON of relative baseline paths and SHA256; refuse changes when supplied')
    a=p.parse_args();old=read(a.baseline/'run.json');new=read(a.candidate/'run.json')
    if old['status']!='complete' or new['status']!='complete':p.error('Both runs must be complete')
    if {b['business_id'] for b in old['businesses']}!={b['business_id'] for b in new['businesses']}:p.error('Business membership changed')
    if a.output_dir.exists():p.error('Output already exists; choose a new directory')
    baseline_unchanged=None
    if a.baseline_snapshot:
        snapshot=read(a.baseline_snapshot)
        current_files={x.relative_to(a.baseline).as_posix() for x in a.baseline.rglob('*') if x.is_file()}
        baseline_unchanged=current_files==snapshot.keys() and all(sha(a.baseline/name)==expected for name,expected in snapshot.items())
        if not baseline_unchanged:raise ValueError('Baseline outputs changed since snapshot')
    changes=[];counts=Counter();attempts=Counter();initial_identical=True;data_changed=[]
    per_business=[];baseline_stats=Counter();candidate_stats=Counter()
    for b in old['businesses']:
        biz=b['business_id'];before=read(a.baseline/biz/'catalog.json');after=read(a.candidate/biz/'catalog.json')
        if before['sources']!=after['sources']:raise ValueError('Source mismatch: '+biz)
        def columns(cat):return {(t['name'],c['name']):c for t in cat['tables'].values() for c in t['columns']}
        x=columns(before);y=columns(after)
        if x.keys()!=y.keys():raise ValueError('Column layout changed: '+biz)
        for name,t in before['tables'].items():
            if t['data_sha256']!=after['tables'][name]['data_sha256']:data_changed.append({'business':biz,'table':name})
        local=Counter()
        for k,c in x.items():
            d=y[k];baseline_stats[c['semantic']['status']]+=1;candidate_stats[d['semantic']['status']]+=1
            audit=d['semantic'].get('refinement')
            if audit:
                attempts['eligible_columns']+=1
                attempts['accepted_final']+=bool(audit.get('accepted') and usable(d))
                attempts['kept_original']+=not audit.get('accepted',False)
            if not usable(c) and usable(d):counts['newly_usable']+=1;local['newly_usable']+=1
            if usable(c) and not usable(d):counts['lost_usable']+=1;local['lost_usable']+=1
            if usable(c) and role(c)!=role(d):counts['changed_previously_usable_role']+=1
            if role(c)!=role(d) or c['sql_type']!=d['sql_type'] or c['semantic']['status']!=d['semantic']['status']:
                changes.append({'business':biz,'table':k[0],'column':c['original_name'],
                    'before_role':role(c),'after_role':role(d),'before_type':c['sql_type'],'after_type':d['sql_type'],
                    'before_status':c['semantic']['status'],'after_status':d['semantic']['status'],
                    'before_meaning':(c['semantic'].get('proposed') or {}).get('meaning'),
                    'after_meaning':(d['semantic'].get('proposed') or {}).get('meaning'),
                    'refinement':audit,'checks':d['semantic'].get('checks')})
        pri0,_=primary_calls(a.baseline/'_semantic'/biz/'model_calls.jsonl')
        pri1,extra=primary_calls(a.candidate/'_semantic'/biz/'model_calls.jsonl')
        same=pri0==pri1;initial_identical &= same
        attempts['extra_requests']+=len(extra)
        attempts['new_generations']+=sum(not r.get('cache_hit',False) for r in extra)
        attempts['cache_hits']+=sum(r.get('cache_hit',False) for r in extra)
        attempts['parse_errors']+=sum(r.get('parse_error') is not None for r in extra)
        attempts['generation_ms']+=sum(r.get('generation_ms',0) for r in extra if not r.get('cache_hit',False))
        per_business.append({'business':biz,'initial_outputs_identical':same,**local})
    for metric in ['newly_usable','lost_usable','changed_previously_usable_role']:
        counts[metric] += 0
    report={'baseline':str(a.baseline),'candidate':str(a.candidate),'baseline_unchanged':baseline_unchanged,
        'primary_outputs_identical':initial_identical,'counts':dict(counts),'before_status':dict(baseline_stats),
        'after_status':dict(candidate_stats),'refinement_attempts':dict(attempts),'businesses':per_business,
        'changed_staging_tables':data_changed,'changes':changes,
        'baseline_database_validation':read(a.baseline/'database_validation.json')['status'],
        'candidate_database_validation':read(a.candidate/'database_validation.json')['status'],
        'note':'Metadata coverage comparison, not semantic correctness or QA accuracy. Candidate can reuse initial inference cache; full wall times are not a fair speed benchmark. No alias learning.'}
    a.output_dir.mkdir(parents=True,exist_ok=False)
    (a.output_dir/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    def cell(v):return ('null' if v is None else str(v)).replace('|','\\|').replace('\n',' ')
    lines=['# So sánh refinement có giới hạn','',report['note'],'',
        f"- Baseline nguyên vẹn: {baseline_unchanged}; output phân tích ban đầu giống nhau: {initial_identical}.",
        f"- Role mới dùng được: {counts['newly_usable']}; mất role trước đó: {counts['lost_usable']}; thay đổi role vốn dùng được: {counts['changed_previously_usable_role']}.",
        f"- Refinement: {dict(attempts)}.",
        f"- Trạng thái trước: {dict(baseline_stats)}.",f"- Trạng thái sau: {dict(candidate_stats)}.",'',
        '| Business | Bảng | Cột | Role trước | Role sau | Trạng thái trước → sau | Ý nghĩa mới |',
        '|---|---|---|---|---|---|---|']
    for c in changes:
        lines.append('| '+' | '.join(cell(v) for v in [c['business'],c['table'],c['column'],c['before_role'],c['after_role'],
            c['before_status']+' → '+c['after_status'],c['after_meaning']])+' |')
    (a.output_dir/'comparison.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in {'changes','businesses'}},ensure_ascii=False))


if __name__=='__main__':main()
