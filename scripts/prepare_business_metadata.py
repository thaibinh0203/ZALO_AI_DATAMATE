#!/usr/bin/env python3
"""Enrich existing pipeline output without re-ingestion, questions or labels."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from time import perf_counter
os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from datamate_b.model import LocalModel,ModelConfig
from datamate_data.business_metadata import build_business_metadata

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prepared-root',type=Path,required=True)
    p.add_argument('--source-root',type=Path,default=ROOT/'docs/train')
    p.add_argument('--output-root',type=Path,required=True)
    p.add_argument('--model-path',type=Path,default=ROOT/'models/qwen3-4b')
    a=p.parse_args()
    if a.output_root.exists():p.error('Use a new output root')
    if not os.environ.get('DATAMATE_PG_DSN'):p.error('Set DATAMATE_PG_DSN for read-only data checks')
    files=sorted(a.prepared_root.glob('*/postgres_catalog.json'))
    if not files:p.error('No prepared catalogs')
    model=LocalModel(ModelConfig(str(a.model_path.resolve()),'cuda:0','bfloat16',2048,16384,42))
    a.output_root.mkdir(parents=True)
    started=perf_counter()
    report={'status':'running','questions_read':False,'labels_read':False,'businesses':[],
            'prepared_root':str(a.prepared_root),'source_root':str(a.source_root),
            'implementation_sha256':hashlib.sha256((ROOT/'src/datamate_data/business_metadata.py').read_bytes()).hexdigest()}
    try:
        for path in files:
            metadata=build_business_metadata(path.parent,a.source_root,model=model,dsn=os.environ['DATAMATE_PG_DSN'],output_dir=a.output_root/path.parent.name)
            report['businesses'].append({'id':path.parent.name,'tables':len(metadata['tables']),'elapsed_ms':metadata['elapsed_ms']})
        report['status']='complete'
    except KeyboardInterrupt:
        report['status']='interrupted'
        raise
    except Exception as exc:
        report.update(status='failed',error_type=type(exc).__name__)
        raise
    finally:
        report.update(model=model.metadata(),elapsed_ms=round((perf_counter()-started)*1000,3))
        (a.output_root/'run.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__':main()
