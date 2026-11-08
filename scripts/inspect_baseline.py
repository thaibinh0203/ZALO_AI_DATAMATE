#!/usr/bin/env python3
"""Print one stored baseline trace; does not rerun inference or SQL."""
import argparse,json
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run-dir',type=Path,required=True);p.add_argument('--id',required=True);a=p.parse_args()
for line in (a.run_dir/'traces.jsonl').open(encoding='utf-8'):
 r=json.loads(line)
 if r['id']==a.id:print(json.dumps(r,ensure_ascii=False,indent=2));break
else:p.error('Question ID not found')
