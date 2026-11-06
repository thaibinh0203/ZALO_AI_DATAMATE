#!/usr/bin/env python3
"""ONLINE Ubuntu 26.04 setup: apt download + verified extraction, never apt install."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
PACKAGES={'postgresql-18':'18.6-0ubuntu0.26.04.1','postgresql-client-18':'18.6-0ubuntu0.26.04.1',
          'libpq5':'18.6-0ubuntu0.26.04.1','libicu78':'78.2-2ubuntu1',
          'libnuma1':'2.0.19-1build1','liburing2':'2.14-1','libxslt1.1':'1.1.45-0.1'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--cache',type=Path,default=ROOT/'.cache/postgresql');a=p.parse_args()
    os_release=Path('/etc/os-release').read_text()
    if 'ID=ubuntu' not in os_release or 'VERSION_ID="26.04"' not in os_release:
        p.error('Pinned packages target Ubuntu 26.04 amd64; use an existing PostgreSQL server on other systems')
    cache=a.cache.resolve();debs=cache/'debs';target=cache/'root';debs.mkdir(parents=True,exist_ok=True)
    if (target/'usr/lib/postgresql/18/bin/postgres').exists():
        print('Existing local PostgreSQL binaries left unchanged; use a new --cache directory to replay installation.');return
    specs=[f'{name}={version}' for name,version in PACKAGES.items()]
    subprocess.run(['apt-get','download',*specs],cwd=debs,check=True)
    records=[]
    for name,version in PACKAGES.items():
        meta=subprocess.run(['apt-cache','show',f'{name}={version}'],capture_output=True,text=True,check=True).stdout
        entries=[dict(line.split(': ',1) for line in block.splitlines() if ': ' in line and not line.startswith(' ')) for block in meta.split('\n\n') if block.strip()]
        entry=next(e for e in entries if e.get('Package')==name and e.get('Version')==version)
        path=debs/Path(entry['Filename']).name
        with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        if digest!=entry['SHA256']:raise ValueError(f'Package SHA-256 mismatch: {name}')
        subprocess.run(['dpkg-deb','-x',str(path),str(target)],check=True)
        records.append({'package':name,'version':version,'sha256':digest,'source':entry['Filename']})
    (cache/'packages.json').write_text(json.dumps(records,indent=2)+'\n')
    print('Verified PostgreSQL binaries extracted locally. No system packages/services changed.')


if __name__=='__main__':main()
