#!/usr/bin/env python3
"""Create a constrained QA login on an existing PostgreSQL server."""
import argparse
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from psycopg import sql
from datamate_v2.common import ROOT,load_yaml
from datamate_v2.database.connection import connect

def setup_reader(db,role,password=None):
    if password is None and not db.info.host.startswith('/'):
        raise ValueError('Password-free setup is only supported through a local Unix socket')
    exists=db.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,)).fetchone()
    if exists:
        row=db.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=%s',(role,)).fetchone()
        if any(row): raise ValueError('Existing reader role is privileged; choose a clean dedicated database cluster')
    else:
        statement=sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS').format(sql.Identifier(role))
        if password is not None: statement+=sql.SQL(' PASSWORD {}').format(sql.Literal(password))
        db.execute(statement)
    db.execute(sql.SQL('ALTER ROLE {} SET default_transaction_read_only = on').format(sql.Identifier(role)))
    database=db.execute('SELECT current_database()').fetchone()[0]
    db.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(database),sql.Identifier(role)))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--password-env',default='DATAMATE_READER_PASSWORD')
    p.add_argument('--no-password',action='store_true',help='Unix-socket-only login without a password');args=p.parse_args()
    settings=load_yaml(ROOT/'configs/database.yaml');password=None if args.no_password else os.environ[args.password_env]
    with connect(settings,admin=True) as db:
        setup_reader(db,settings['readonly_role'],password)
    print('Read-only QA role ready; no DSNs or credentials printed.')
