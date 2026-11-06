"""Transactional PostgreSQL loading. Trusted ingestion SQL, not an LLM SQL sandbox."""
from pathlib import Path
import json

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from .parsers import dumps
from .pipeline import digest


INTERNAL = [('__row_id','TEXT PRIMARY KEY'),('__raw','JSONB NOT NULL'),('__source','JSONB NOT NULL'),
            ('__quality','JSONB NOT NULL'),('__row_kind','TEXT NOT NULL'),('__duplicate_of','TEXT'),
            ('__quantity_base','NUMERIC'),('__unit_base','TEXT')]


def load_business(dsn,prepared_dir):
    root=Path(prepared_dir);catalog=json.loads((root/'catalog.json').read_text(encoding='utf-8'))
    schema=catalog['schema']
    for meta in catalog['tables'].values():
        path=(root/meta['data_path']).resolve()
        if not path.is_relative_to(root.resolve()):raise ValueError('Staging file escapes preparation directory')
        if digest(path)!=meta['data_sha256']:raise ValueError('Staging checksum mismatch; refusing database load')
    with psycopg.connect(dsn,connect_timeout=10) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)",(int(catalog['fingerprint'][:15],16),))
        exists=conn.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s",(schema,)).fetchone()
        if exists:
            stored=conn.execute(sql.SQL('SELECT catalog FROM {}.{}').format(sql.Identifier(schema),sql.Identifier('_catalog'))).fetchone()[0]
            if stored['fingerprint']!=catalog['fingerprint']:raise ValueError('Schema fingerprint conflict')
            for meta in stored['tables'].values():
                count=conn.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(sql.Identifier(schema),sql.Identifier(meta['name']))).fetchone()[0]
                if count!=meta['rows']:raise ValueError('Cached database row counts differ; refusing reuse')
            (root/'postgres_catalog.json').write_text(dumps(stored)+'\n',encoding='utf-8')
            return {'schema':schema,'cache_hit':True,'rows':stored['total_source_records'],'catalog':stored}
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        for name,meta in catalog['tables'].items():
            definitions=[sql.SQL('{} {}').format(sql.Identifier(c['name']),sql.SQL(c['sql_type'])) for c in meta['columns']]
            definitions += [sql.SQL('{} {}').format(sql.Identifier(n),sql.SQL(t)) for n,t in INTERNAL]
            conn.execute(sql.SQL('CREATE TABLE {}.{} ({})').format(sql.Identifier(schema),sql.Identifier(name),sql.SQL(',').join(definitions)))
            columns=[c['name'] for c in meta['columns']]+[n for n,_ in INTERNAL]
            statement=sql.SQL('COPY {}.{} ({}) FROM STDIN').format(sql.Identifier(schema),sql.Identifier(name),sql.SQL(',').join(map(sql.Identifier,columns)))
            with conn.cursor().copy(statement) as copy,(root/meta['data_path']).open(encoding='utf-8') as stream:
                for line in stream:
                    row=json.loads(line)
                    values=row['typed']+[row['row_id'],Jsonb(row['raw']),Jsonb(row['source']),Jsonb(row['quality']),
                                          row['kind'],row['duplicate_of'],row['quantity_base'],row['unit_base']]
                    copy.write_row(values)
            if digest(root/meta['data_path'])!=meta['data_sha256']:raise ValueError('Staging changed during database load')
            raw_count=conn.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(sql.Identifier(schema),sql.Identifier(name))).fetchone()[0]
            if raw_count!=meta['rows']:raise ValueError('PostgreSQL COPY row count mismatch')
            view=name+'_clean'
            conn.execute(sql.SQL("CREATE VIEW {}.{} AS SELECT * FROM {}.{} WHERE __row_kind='data' AND __duplicate_of IS NULL").format(
                sql.Identifier(schema),sql.Identifier(view),sql.Identifier(schema),sql.Identifier(name)))
            meta['clean_view']=view
            meta['profiles']={}
            for col in meta['columns']:
                # Exact full-data counts, no sampling. No customer/product values emitted.
                c=sql.Identifier(col['name'])
                result=conn.execute(sql.SQL('SELECT count(*), count({}), count(DISTINCT {}) FROM {}.{}').format(
                    c,c,sql.Identifier(schema),sql.Identifier(view))).fetchone()
                profile={'clean_rows':result[0],'non_null':result[1],'distinct':result[2],
                         'unique_non_null_candidate':result[1]>0 and result[1]==result[2]}
                if col['sql_type'] in {'DATE','TIMESTAMP'}:
                    bounds=conn.execute(sql.SQL('SELECT min({}),max({}) FROM {}.{}').format(c,c,sql.Identifier(schema),sql.Identifier(view))).fetchone()
                    profile['min']=str(bounds[0]) if bounds[0] is not None else None
                    profile['max']=str(bounds[1]) if bounds[1] is not None else None
                meta['profiles'][col['name']]=profile
            for col in meta['columns']:
                if col['role'].endswith('_id') or col['sql_type'] in {'DATE','TIMESTAMP'}:
                    conn.execute(sql.SQL('CREATE INDEX ON {}.{} ({})').format(sql.Identifier(schema),sql.Identifier(name),sql.Identifier(col['name'])))
            conn.execute(sql.SQL('ANALYZE {}.{}').format(sql.Identifier(schema),sql.Identifier(name)))
        catalog['relationships']=relationships(conn,catalog)
        conn.execute(sql.SQL('CREATE TABLE {}.{} (catalog JSONB NOT NULL)').format(sql.Identifier(schema),sql.Identifier('_catalog')))
        conn.execute(sql.SQL('INSERT INTO {}.{} VALUES (%s)').format(sql.Identifier(schema),sql.Identifier('_catalog')),(Jsonb(catalog),))
    # Publish metadata only after the database transaction commits.
    (root/'postgres_catalog.json').write_text(dumps(catalog)+'\n',encoding='utf-8')
    return {'schema':schema,'cache_hit':False,'rows':catalog['total_source_records'],'catalog':catalog}


def relationships(conn,catalog):
    result=[];schema=catalog['schema'];tables=list(catalog['tables'].values())
    for source in tables:
        for target in tables:
            if source is target:continue
            for role in ('order_id','product_id','customer_id','purchase_id','lot_id'):
                sc=[c for c in source['columns'] if c['role']==role]
                tc=[c for c in target['columns'] if c['role']==role]
                if len(sc)!=1 or len(tc)!=1:continue
                s,t=sc[0]['name'],tc[0]['name']
                # Composite store+ID keys are required when both tables have store scope.
                sstore=[c['name'] for c in source['columns'] if c['role']=='store_id']
                tstore=[c['name'] for c in target['columns'] if c['role']=='store_id']
                if bool(sstore)!=bool(tstore):continue
                pairs=[(s,t)]+([(sstore[0],tstore[0])] if len(sstore)==len(tstore)==1 else [])
                group=sql.SQL(',').join(sql.Identifier(b) for _,b in pairs)
                not_null=sql.SQL(' AND ').join(sql.SQL('{} IS NOT NULL').format(sql.Identifier(b)) for _,b in pairs)
                duplicate=conn.execute(sql.SQL('SELECT 1 FROM {}.{} WHERE {} GROUP BY {} HAVING count(*)>1 LIMIT 1').format(
                    sql.Identifier(schema),sql.Identifier(target['clean_view']),not_null,group)).fetchone()
                if duplicate:continue
                predicate=sql.SQL(' AND ').join(sql.SQL('s.{}=t.{}').format(sql.Identifier(a),sql.Identifier(b)) for a,b in pairs)
                source_not_null=sql.SQL(' AND ').join(sql.SQL('s.{} IS NOT NULL').format(sql.Identifier(a)) for a,_ in pairs)
                count,orphans=conn.execute(sql.SQL('SELECT count(*),count(*) FILTER (WHERE t.{} IS NULL) FROM {}.{} s LEFT JOIN {}.{} t ON {} WHERE {}').format(
                    sql.Identifier(t),sql.Identifier(schema),sql.Identifier(source['clean_view']),sql.Identifier(schema),sql.Identifier(target['clean_view']),predicate,source_not_null)).fetchone()
                if count:
                    result.append({'source':source['name'],'target':target['name'],'column_pairs':pairs,
                        'source_non_null_rows':count,'orphan_rows':orphans,'target_unique_non_null':True,
                        'status':'data_supported_candidate' if orphans==0 else 'candidate_with_orphans',
                        'note':'Role/uniqueness/overlap evidence; semantic join still requires README confirmation.'})
    return result
