#!/usr/bin/env python3
"""Manage this project's password-free, Unix-socket-only PostgreSQL cluster."""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import psycopg
from datamate_v2.common import ROOT,load_yaml,PipelineError
from datamate_v2.scripts.setup_database import setup_reader

class LocalPostgres:
    def __init__(self,root=None,bin_dir=None):
        self.root=Path(root or ROOT/'runtime/postgresql').resolve()
        self.data=self.root/'data';self.socket=self.root/'socket';self.port=55432
        self.owner='datamate_owner'
        self.reader=load_yaml(ROOT/'configs/database.yaml')['readonly_role']
        self.env=os.environ.copy()
        self.env.pop('PGPASSWORD',None)
        candidates=[]
        if bin_dir: candidates.append(Path(bin_dir))
        if os.environ.get('DATAMATE_PG_BIN'): candidates.append(Path(os.environ['DATAMATE_PG_BIN']))
        candidates.extend(sorted((self.root/'root/usr/lib/postgresql').glob('*/bin'),reverse=True))
        candidates.extend(sorted((ROOT/'runtime/postgresql/root/usr/lib/postgresql').glob('*/bin'),reverse=True))
        pg_config=shutil.which('pg_config')
        if pg_config:
            result=subprocess.run([pg_config,'--bindir'],capture_output=True,text=True)
            if result.returncode==0: candidates.append(Path(result.stdout.strip()))
        candidates.extend(sorted(Path('/usr/lib/postgresql').glob('*/bin'),reverse=True))
        self.bin=next((p.resolve() for p in candidates if all((p/name).is_file() for name in ('initdb','pg_ctl','postgres'))),None)
        libs=self.root/'root/usr/lib/x86_64-linux-gnu'
        if not libs.is_dir(): libs=ROOT/'runtime/postgresql/root/usr/lib/x86_64-linux-gnu'
        if libs.is_dir(): self.env['LD_LIBRARY_PATH']=str(libs)

    def dsn(self,reader=False):
        return psycopg.conninfo.make_conninfo(host=str(self.socket),port=self.port,dbname='postgres',user=self.reader if reader else self.owner)

    def command(self,*args,capture=False):
        if not self.bin: raise PipelineError('PostgreSQL binaries missing. Install PostgreSQL or pass --bin-dir /path/to/postgresql/bin.')
        return subprocess.run([str(self.bin/args[0]),*args[1:]],env=self.env,capture_output=capture,text=True)

    def running(self): return self.command('pg_ctl','-D',str(self.data),'status',capture=True).returncode==0

    def check_owned(self):
        if not (self.root/'datamate-local-cluster').is_file():
            raise PipelineError('This directory was not initialized by local_postgres.py; refusing to manage it')

    def start(self):
        if not self.bin: self.command('pg_ctl','--version')
        self.root.mkdir(parents=True,exist_ok=True);self.root.chmod(0o700)
        self.socket.mkdir(exist_ok=True);self.socket.chmod(0o700)
        if len(str(self.socket).encode())>85: raise PipelineError('Unix socket path too long; use --root /tmp/datamate-pg')
        if not self.data.exists():
            result=self.command('initdb','-D',str(self.data),'-U',self.owner,'--encoding=UTF8','--no-locale','--auth-local=trust','--auth-host=reject',capture=True)
            if result.returncode: raise PipelineError(result.stderr.strip())
            (self.root/'datamate-local-cluster').write_text('DataMate local Unix-socket-only cluster\n')
        self.check_owned()
        if not self.running():
            options=f"-h '' -p {self.port} -k {shlex.quote(str(self.socket))} -c unix_socket_permissions=0700"
            result=self.command('pg_ctl','-D',str(self.data),'-l',str(self.root/'server.log'),'-o',options,'-w','start',capture=True)
            if result.returncode:
                raise PipelineError(result.stderr.strip()+f'; inspect {self.root / "server.log"}')
        with psycopg.connect(self.dsn(),connect_timeout=10) as db: setup_reader(db,self.reader)
        print('PostgreSQL local ready: Unix socket only, no password, QA role read-only.')

    def stop(self):
        self.check_owned()
        if self.running():
            result=self.command('pg_ctl','-D',str(self.data),'-m','fast','-w','stop',capture=True)
            if result.returncode: raise PipelineError(result.stderr.strip())
        print('PostgreSQL local stopped.')

    def print_env(self):
        for name,value in [('DATAMATE_ADMIN_DSN',self.dsn()),('DATAMATE_READONLY_DSN',self.dsn(True))]:
            print(f'export {name}={shlex.quote(value)}')


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['start','stop','status','env'])
    p.add_argument('--root',type=Path);p.add_argument('--bin-dir',type=Path);args=p.parse_args(argv)
    try:
        cluster=LocalPostgres(args.root,args.bin_dir)
        if args.action=='start': cluster.start()
        elif args.action=='stop': cluster.stop()
        elif args.action=='status': print('running' if cluster.running() else 'stopped')
        else: cluster.print_env()
        return 0
    except (OSError,ValueError,psycopg.Error) as error:
        print(f'{type(error).__name__}: {error}',file=sys.stderr);return 2

if __name__=='__main__': raise SystemExit(main())
