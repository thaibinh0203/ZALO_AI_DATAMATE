#!/usr/bin/env python3
"""Inspect prepared PostgreSQL tables without arbitrary SQL or model access."""
import argparse
import os
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from datamate_data.store import PreparedStore
from datamate_data.parsers import dumps


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prepared-dir',required=True,type=Path)
    p.add_argument('--table');p.add_argument('--preview',action='store_true')
    p.add_argument('--semantic',action='store_true',help='Read local semantic review, no database connection required')
    p.add_argument('--business-metadata',action='store_true',help='Read grain/measure/group/JOIN sidecar, no model or database required')
    p.add_argument('--metadata-root',type=Path,help='External sidecar root, otherwise use prepared-dir/business_metadata')
    p.add_argument('--limit',default=5,type=int);args=p.parse_args()
    if args.business_metadata:
        import json
        directory=args.metadata_root/args.prepared_dir.name if args.metadata_root else args.prepared_dir/'business_metadata'
        result=json.loads((directory/'metadata.json').read_text(encoding='utf-8'))
        if args.table:
            if args.table not in result['tables']:p.error('Unknown business metadata table; use exact clean view name')
            result['tables']={args.table:result['tables'][args.table]}
        print(dumps(result));return
    if args.semantic:
        import json
        result=json.loads((args.prepared_dir/'semantic_review.json').read_text(encoding='utf-8'))
        if args.table:result['columns']=[c for c in result['columns'] if c['table']==args.table]
        print(dumps(result));return
    if not os.environ.get('DATAMATE_PG_DSN'):p.error('Set DATAMATE_PG_DSN')
    store=PreparedStore(os.environ['DATAMATE_PG_DSN'],args.prepared_dir)
    result=store.list_tables() if not args.table else store.preview_rows(args.table,args.limit) if args.preview else store.describe_table(args.table)
    print(dumps(result))


if __name__=='__main__':main()
