#!/usr/bin/env python3
"""Create/extend a project-owned local SELECT role; grants only catalog clean views."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
MARKER='datamate-baseline-readonly-v1'


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prepared-root',type=Path,action='append',required=True)
    p.add_argument('--role',default='datamate_baseline');p.add_argument('--print-dsn',action='store_true')
    a=p.parse_args()
    if not re.fullmatch(r'datamate_[a-z0-9_]+',a.role):p.error('Use a datamate_ project role')
    catalogs=[]
    for root in a.prepared_root:
        files=list(root.glob('*/postgres_catalog.json'))
        if not files:p.error('No prepared catalogs in '+str(root))
        catalogs.extend(json.loads(f.read_text()) for f in files)
    dsn=os.environ.get('DATAMATE_PG_DSN')
    if not dsn:p.error('Set DATAMATE_PG_DSN to the ingestion/admin connection')
    with psycopg.connect(dsn) as conn:
        role=sql.Identifier(a.role)
        existing=conn.execute("SELECT r.oid,shobj_description(r.oid,'pg_authid') FROM pg_roles r WHERE rolname=%s",(a.role,)).fetchone()
        if existing and existing[1]!=MARKER:raise ValueError('Existing role is not marked as owned by this baseline setup')
        if not existing:
            conn.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS').format(role))
            conn.execute(sql.SQL('COMMENT ON ROLE {} IS {}').format(role,sql.Literal(MARKER)))
        # Do not reset passwords or mutate memberships of an existing role.
        attrs=conn.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=%s',(a.role,)).fetchone()
        if any(attrs):raise ValueError('Existing baseline role has elevated privileges')
        conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only='on'").format(role))
        conn.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(conn.info.dbname),role))
        for cat in catalogs:
            schema=sql.Identifier(cat['schema'])
            conn.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(schema,role))
            for t in cat['tables'].values():
                conn.execute(sql.SQL('GRANT SELECT ON {}.{} TO {}').format(schema,sql.Identifier(t['clean_view']),role))
        output_dsn=make_conninfo(host=conn.info.host,port=conn.info.port,dbname=conn.info.dbname,user=a.role)
    if a.print_dsn:print(output_dsn)
    else:print(json.dumps({'role':a.role,'businesses':len(catalogs),'clean_views':sum(len(c['tables']) for c in catalogs),'password_modified':False}))


if __name__=='__main__':main()
