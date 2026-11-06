#!/usr/bin/env python3
"""Manage an isolated, Unix-socket-only development cluster. No sudo/system service."""
import argparse
import os
import shlex
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=["init","start","stop","status","dsn"])
    parser.add_argument("--root",type=Path,default=ROOT/".cache/postgresql")
    args=parser.parse_args();base=args.root.resolve()
    binaries=base/"root/usr/lib/postgresql/18/bin"
    data=base/"data";socket=base/"socket"
    env=os.environ.copy();env["LD_LIBRARY_PATH"]=str(base/"root/usr/lib/x86_64-linux-gnu")
    if args.action=="dsn":
        escaped=str(socket).replace('\\','\\\\').replace("'","\\'")
        print(f"host='{escaped}' port=55432 dbname=postgres user=datamate_owner");return
    if not (binaries/"initdb").exists():
        parser.error("PostgreSQL binaries missing; see docs/DATA_PIPELINE.md for preparation")
    if args.action=="init":
        if data.exists(): parser.error("Cluster directory already exists; refusing to overwrite")
        base.mkdir(parents=True,exist_ok=True)
        subprocess.run([str(binaries/"initdb"),"-D",str(data),"-U","datamate_owner",
                        "--encoding=UTF8","--no-locale","--auth-local=trust","--auth-host=reject"],check=True,env=env)
        return
    if args.action=="start":
        if subprocess.run([str(binaries/'pg_ctl'),'-D',str(data),'status'],env=env,capture_output=True).returncode==0:
            print('Local cluster already running');return
        socket.mkdir(mode=0o700,parents=True,exist_ok=True);socket.chmod(0o700)
        options=f"-h '' -p 55432 -k {shlex.quote(str(socket))} -c unix_socket_permissions=0700 -c shared_buffers=128MB -c max_connections=20"
        subprocess.run([str(binaries/"pg_ctl"),"-D",str(data),"-l",str(base/"server.log"),"-o",options,"-w","start"],check=True,env=env)
    else:
        cmd=[str(binaries/"pg_ctl"),"-D",str(data)]
        cmd += ["-m","fast","-w","stop"] if args.action=="stop" else ["status"]
        raise SystemExit(subprocess.run(cmd,env=env).returncode)


if __name__=="__main__":main()
