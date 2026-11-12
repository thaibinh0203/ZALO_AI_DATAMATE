#!/usr/bin/env python3
"""Build context with exactly one model; no three-model weights in Docker layers."""
import argparse
from pathlib import Path
import shutil
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from datamate_v2.common import ROOT,read_json,write_json,file_hash,PipelineError
from datamate_v2.cli import model_settings
from datamate_v2.inference.model_loader import validate_artifacts

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--model',required=True);p.add_argument('--output',default=str(ROOT/'runtime/docker-context'))
    p.add_argument('--training-split',default=str(ROOT.parent/'docs/train'),help='Provided train package for reproducibility evidence, kept separate from inference /data');a=p.parse_args()
    key,settings=model_settings(ROOT/'configs/model.yaml',a.model)
    registry=read_json(ROOT/'models/registry.json')[key]
    if registry['parameter_count']>15_000_000_000: raise PipelineError('Selected model exceeds competition parameter budget')
    weights=validate_artifacts(settings);target=Path(a.output).resolve()
    if target.exists(): raise PipelineError('Use a new packaging directory')
    if target==ROOT or ROOT.is_relative_to(target) or target.is_relative_to(weights): raise PipelineError('Unsafe build context output')
    target.mkdir(parents=True)
    for source in ROOT.iterdir():
        if source.name in {'models','runtime','tests','__pycache__','.pytest_cache'}: continue
        dest=target/source.name
        if source.is_dir(): shutil.copytree(source,dest,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        else: shutil.copy2(source,dest)
    (target/'models').mkdir()
    for name in ('README.md','registry.json'): shutil.copy2(ROOT/'models'/name,target/'models'/name)
    # Hardlink when possible to avoid duplicating multi-GB weights on the same disk.
    def copy(src,dst):
        import os
        try: os.link(src,dst)
        except OSError: shutil.copy2(src,dst)
    shutil.copytree(weights,target/'models'/weights.name,copy_function=copy,ignore=shutil.ignore_patterns('.cache'))
    training=Path(a.training_split).resolve()
    if not training.is_dir() or not (training/'rulebook.md').exists(): raise PipelineError('Provided training split missing')
    if training.name in ('public_test','private_test'): raise PipelineError('Do not package test data as training evidence')
    evidence=target/'training_code';evidence.mkdir()
    shutil.copytree(training,evidence/'data/train',copy_function=copy,ignore=shutil.ignore_patterns('*:Zone.Identifier'))
    (evidence/'README.md').write_text('No training or fine-tuning was performed. This baseline uses declared pretrained weights.\n'
      'Preprocessing source: /code/datamate_v2/data_pipeline and prompts.\n'
      'Reproduce preprocessing: python -m datamate_v2.cli ingest --split /code/training_code/data/train --output /tmp/train-ingest.json\n'
      'All settings: /code/datamate_v2/configs. No claims of evaluation results are made.\n',encoding='utf-8')
    write_json(evidence/'data_manifest.json',{'training_performed':False,'model':registry,'files':{
       str(p.relative_to(evidence)): {'sha256':file_hash(p),'bytes':p.stat().st_size} for p in (evidence/'data').rglob('*') if p.is_file()}})
    print(f'Context ready: {target}. MODEL_KEY={key}, MODEL_DIR={weights.name}')
