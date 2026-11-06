#!/usr/bin/env python3
"""Compare prepared staging to PostgreSQL counts and exact numeric sums; no labels."""
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from time import perf_counter

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import psycopg
from psycopg import sql
from datamate_data.pipeline import digest
from datamate_data.parsers import dumps


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not os.environ.get('DATAMATE_PG_DSN'):parser.error('Set DATAMATE_PG_DSN')
    run=json.loads((args.run_dir/'run.json').read_text())
    if run['status']!='complete':parser.error('Refusing an incomplete run')
    started=perf_counter();results=[]
    with psycopg.connect(os.environ['DATAMATE_PG_DSN']) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        conn.execute("SET LOCAL statement_timeout='60s'")
        server=conn.execute('SHOW server_version').fetchone()[0]
        for item in run['businesses']:
            prepared=args.run_dir/item['business_id']
            cat=json.loads((prepared/'postgres_catalog.json').read_text())
            for meta in cat['tables'].values():
                path=prepared/meta['data_path']
                if digest(path)!=meta['data_sha256']:raise ValueError('Staging checksum mismatch')
                numeric=[(i,c['name']) for i,c in enumerate(meta['columns']) if c['sql_type']=='NUMERIC']
                sums=[Decimal(0) for _ in numeric];non_null=[0]*len(numeric)
                raw_count=clean_count=0
                base_sum=Decimal(0);base_non_null=0
                with path.open(encoding='utf-8') as stream:
                    for line in stream:
                        record=json.loads(line);raw_count+=1
                        if record['kind']!='data' or record['duplicate_of'] is not None:continue
                        clean_count+=1
                        if record['quantity_base'] is not None:
                            base_sum+=Decimal(record['quantity_base']);base_non_null+=1
                        for j,(i,_) in enumerate(numeric):
                            value=record['typed'][i]
                            if value is not None:sums[j]+=Decimal(str(value));non_null[j]+=1
                expected=[s if n else None for s,n in zip(sums,non_null)]
                actual_raw=conn.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(
                    sql.Identifier(cat['schema']),sql.Identifier(meta['name']))).fetchone()[0]
                aggregates=[sql.SQL('count(*)')]+[sql.SQL('sum({})').format(sql.Identifier(n)) for _,n in numeric]
                aggregates.extend([sql.SQL('sum(__quantity_base)'),sql.SQL('count(__quantity_base)')])
                actual=conn.execute(sql.SQL('SELECT {} FROM {}.{}').format(
                    sql.SQL(',').join(aggregates),sql.Identifier(cat['schema']),sql.Identifier(meta['clean_view']))).fetchone()
                if raw_count!=meta['rows'] or actual_raw!=raw_count or actual[0]!=clean_count or list(actual[1:-2])!=expected or (
                        actual[-2]!=(base_sum if base_non_null else None) or actual[-1]!=base_non_null):
                    raise ValueError(f'Staging/database mismatch: {item["business_id"]}/{meta["name"]}')
                results.append({'business_id':item['business_id'],'table':meta['name'],
                    'raw_rows':raw_count,'clean_rows':clean_count,'numeric_sums_verified':len(numeric),
                    'derived_base_quantity_sum_verified':True,'derived_base_quantity_non_null':base_non_null})
    result={'status':'passed','server_version':server,'run_dir':str(args.run_dir),
            'businesses':len(run['businesses']),'tables':results,'labels_read':False,
            'questions_read':False,'elapsed_ms':round((perf_counter()-started)*1000,3),
            'limitation':'Verifies faithful loading, not business metric correctness or completeness.'}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(dumps(result)+'\n',encoding='utf-8')
    print(dumps({k:v for k,v in result.items() if k!='tables'}))


if __name__=='__main__':main()
