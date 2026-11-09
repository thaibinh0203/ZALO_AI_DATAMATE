#!/usr/bin/env python3
"""Read a v5 business output without model or database: manifest, plan, quality, rows."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1];sys.path.insert(0, str(ROOT/'src'))
from datamate_data.parsers import dumps  # noqa: E402
from datamate_data.semplan.publish import load_manifest  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--business-root', type=Path, required=True, help='<v5 output>/<business_id>')
    p.add_argument('--view', choices=['manifest', 'plan', 'table', 'policies', 'quality', 'row-decisions', 'checks'], default='manifest')
    p.add_argument('--table', help='Logical table name (see plan view)')
    p.add_argument('--limit', type=int, default=50)
    a = p.parse_args();root = a.business_root
    if a.view == 'manifest':
        print(dumps(load_manifest(root)) if (root/'manifest.json').exists() else '{"status": "no manifest (draft or failed run)"}');return
    plan = json.loads((root/'semantic_plan.json').read_text(encoding='utf-8'))
    if a.view == 'plan':
        print(dumps({'plan_revision': plan['plan_revision'], 'tables': {n: {'grain': t['grain'], 'rows': t['row_decisions']['summary'],
                     'refinement': {k: t['refinement'].get(k) for k in ('used', 'requests', 'accepted')}} for n, t in plan['tables'].items()},
                     'model': plan['model'], 'rules_detected': plan['rules_detected']}));return
    if a.view == 'quality':
        registry = json.loads((root/'quality_registry.json').read_text(encoding='utf-8'))
        issues = [i for i in registry['issues'] if not a.table or i['table'] == a.table]
        print(dumps({**registry, 'issues': issues[:a.limit]}));return
    if a.view == 'checks':
        rows = [json.loads(l) for l in (root/'checks.jsonl').read_text(encoding='utf-8').splitlines()]
        print(dumps([r for r in rows if not a.table or r['table'] == a.table][:a.limit]));return
    if not a.table or a.table not in plan['tables']:
        p.error('Use --table with one of: '+', '.join(plan['tables']))
    table = plan['tables'][a.table]
    if a.view == 'table':
        print(dumps({k: v for k, v in table.items() if k not in {'table_proposal'}}))
    elif a.view == 'policies':
        print(dumps(table['policies']))
    elif a.view == 'row-decisions':
        lines = (root/table['row_decisions']['file']).read_text(encoding='utf-8').splitlines()
        print(dumps([json.loads(l) for l in lines[:a.limit]]))


if __name__ == '__main__':
    main()
