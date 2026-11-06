"""Bounded, fixed-query catalog/preview API. No generated SQL is accepted here."""
import json
from pathlib import Path
import psycopg
from psycopg import sql


class PreparedStore:
    def __init__(self,dsn,prepared_dir):
        self.dsn=dsn
        self.catalog=json.loads((Path(prepared_dir)/'postgres_catalog.json').read_text(encoding='utf-8'))

    def list_tables(self):
        return [{'name':m['clean_view'],'source_table':m['name'],'role':m['role'],
                 'source_rows':m['rows'],'duplicates':m['duplicates']} for m in self.catalog['tables'].values()]

    def describe_table(self,name):
        for meta in self.catalog['tables'].values():
            if name in {meta['name'],meta['clean_view']}:return meta
        raise ValueError('Unknown table in this business catalog')

    def preview_rows(self,name,limit=5):
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('preview limit must be 1..100')
        meta=self.describe_table(name)
        with psycopg.connect(self.dsn,connect_timeout=10) as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            conn.execute("SET LOCAL statement_timeout='5s'")
            columns=[c['name'] for c in meta['columns']]+['__row_id','__source','__quality','__quantity_base','__unit_base']
            cursor=conn.execute(sql.SQL('SELECT {} FROM {}.{} ORDER BY __row_id LIMIT %s').format(
                sql.SQL(',').join(map(sql.Identifier,columns)),sql.Identifier(self.catalog['schema']),sql.Identifier(meta['clean_view'])),(limit+1,))
            rows=cursor.fetchall()
        return {'status':'success','columns':columns,'rows':rows[:limit],
                'truncated':len(rows)>limit,'sample_only':True,'schema':self.catalog['schema'],
                'note':'Preview is not a complete result for SUM/COUNT or a final answer.'}
