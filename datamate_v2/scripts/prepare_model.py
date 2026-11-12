#!/usr/bin/env python3
"""Explicit ONLINE preparation only; inference never imports this script."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from datamate_v2.cli import model_settings
from datamate_v2.common import ROOT,write_json,read_json,file_hash,utc_now,PipelineError

def prepare(key,*,download=False,model_config=None):
    key,settings=model_settings(model_config or ROOT/'configs/model.yaml',key)
    path=ROOT/settings['path'];registry=read_json(ROOT/'models/registry.json')[key]
    if download:
        from huggingface_hub import snapshot_download
        snapshot_download(settings['repo_id'],revision=settings['revision'],local_dir=path,
            ignore_patterns=['*.bin','*.h5','*.msgpack','*.onnx','*.gguf','original/*'])
    from datamate_v2.inference.model_loader import validate_artifacts
    validate_artifacts({**settings,'verify_checksums':False})
    files={str(p.relative_to(path)):file_hash(p) for p in sorted(path.rglob('*')) if p.is_file() and '.cache' not in p.parts and p.name!='artifact_manifest.json'}
    manifest={**registry,'prepared_at':utc_now(),'sha256':files}
    write_json(path/'artifact_manifest.json',manifest)
    print(f'{key}: {len(files)} artifacts hashed at {path}')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--model',required=True,choices=['qwen3_4b','qwen25_7b','gemma4_12b']);p.add_argument('--download',action='store_true')
    p.add_argument('--model-config',type=Path,default=ROOT/'configs/model.yaml')
    a=p.parse_args();prepare(a.model,download=a.download,model_config=a.model_config)
