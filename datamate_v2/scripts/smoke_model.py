#!/usr/bin/env python3
"""Explicit real-model smoke: one load, all five tasks, no train evaluation."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from datamate_v2.cli import model_settings
from datamate_v2.inference.model_loader import LocalEngine
from datamate_v2.inference.model_client import ModelClient
from datamate_v2.data_pipeline.normalization.column_normalizer import COLUMN_SCHEMA
from datamate_v2.data_pipeline.normalization.validators import VALUE_SCHEMA
from datamate_v2.data_pipeline.schema_generation.schema_validator import SCHEMA_SCHEMA
from datamate_v2.contracts import REWRITE_SCHEMA,PLAN_SCHEMA
from datamate_v2.common import ROOT,dumps

# LMFE 0.11.3 treats bool enum values as Python integers and emits `True`,
# which is not JSON. A plain boolean uses its correct true/false parser.
QUICK_SCHEMA={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--model',default='qwen3_4b');p.add_argument('--device')
    p.add_argument('--model-config',type=Path,default=ROOT/'configs/model.yaml')
    p.add_argument('--quick',action='store_true',help='One short synthetic constrained JSON generation only')
    a=p.parse_args()
    _,settings=model_settings(a.model_config,a.model,a.device)
    engine=LocalEngine(settings);client=ModelClient(engine,settings)
    if a.quick:
        result=engine.generate([{'role':'user','content':'Return JSON with ok set to true.'}],
            schema=QUICK_SCHEMA,max_new_tokens=32)
        print(dumps({'generation':result}),flush=True)
        from datamate_v2.inference.structured_output import parse_output
        assert parse_output(result['text'],{'type':'object','properties':{'ok':{'const':True}},'required':['ok']})=={'ok':True}
        print(dumps({'model':a.model,'model_load_count':engine.load_count,'text':result['text'],'runtime_versions':engine.runtime_versions,'peak_vram_bytes':engine.peak_vram_bytes}))
        sys.exit(0)
    question={'id':'synthetic','business_id':'synthetic','question':'Tổng số lượng hôm qua?','as_of_date':'2026-03-04'}
    payloads=[('column_normalization',{'columns':[{'index':0,'original_column':'SL','samples':[1,2]}],'readme':'SL là số lượng.','rulebook':''},COLUMN_SCHEMA),
              ('value_normalization',{'items':[],'readme':'','rulebook':''},VALUE_SCHEMA),
              ('schema_description',{'table':{'sql_table':'sales','columns':[{'sql_column':'quantity','original_column':'SL','type':'NUMERIC','semantic_status':'verified'}]},'readme':'SL là số lượng.','rulebook':''},SCHEMA_SCHEMA),
              ('query_rewriting',question,REWRITE_SCHEMA),
              ('planning_sql',{'question':question,'business_context':{'tables':[],'readme':'','rulebook':''}},PLAN_SCHEMA)]
    for task,payload,schema in payloads:
        client.call(task,payload,schema)
        print(task,'JSON valid')
    assert engine.load_count==1
    print(dumps({'model_load_count':engine.load_count,'calls':len(client.calls),'offline':True}))
