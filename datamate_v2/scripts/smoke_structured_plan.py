#!/usr/bin/env python3
"""One synthetic Call2 + SQL compile/guard; never reads train questions or answers."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from datamate_v2.common import ROOT,write_json,dumps
from datamate_v2.cli import model_settings
from datamate_v2.inference.model_loader import LocalEngine
from datamate_v2.inference.model_client import ModelClient
from datamate_v2.qa_pipeline.context import build_qa_context
from datamate_v2.qa_pipeline.planning_sql.planner import generate_plan
from datamate_v2.qa_pipeline.planning_sql.compiler import compile_plan
from datamate_v2.qa_pipeline.temporal_validator import validate_windows
from datamate_v2.qa_pipeline.sql_validation.ast_plan import derive_column_usage
from datamate_v2.qa_pipeline.sql_validation.guard import validate_sql

parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--model',required=True)
args=parser.parse_args();_,settings=model_settings(ROOT/'configs/model_compare_4bit.yaml',args.model)
columns=[{'sql_column':name,'original_column':name,'meaning':name,'type':typ,'semantic_status':'verified'} for name,typ in [('order_id','TEXT'),('order_date','DATE'),('quantity','NUMERIC'),('status','TEXT')]]
metadata={'business_id':'synthetic','fingerprint':'synthetic','schema':'b_synthetic',
 'readme':'Mỗi dòng sales là một dòng hàng. order_date là ngày bán, quantity là số lượng.',
 'rulebook':"Số lượng bán = tổng quantity trong kỳ; bỏ các dòng status = 'Hủy'.",'calendar_csv':'','warnings':[],'relationships':[],
 'tables':[{'sql_table':'sales','columns':columns,'quality':{'uncertain_summary_rows':0},'sample_rows':[],'last_rows':[],
            'observed_domains':{'status':{'values':[{'value':'Đã bán'},{'value':'Hủy'}],'truncated':False}},
            'stats':[{'sql_column':c['sql_column'],'null_count':0,'unresolved_count':0,'min':'2026-03-03' if c['type']=='DATE' else 1,'max':'2026-03-04' if c['type']=='DATE' else 10} for c in columns]}]}
question={'id':'synthetic','business_id':'synthetic','question':'Tổng số lượng bán hôm qua?','as_of_date':'2026-03-04'}
windows=validate_windows([{'name':'yesterday','interpretation':'yesterday','parameters':{},'status':'proposed'}],question['as_of_date'])
payload={'question':question,'business_context':build_qa_context(metadata),'validated_windows':windows,'structured_planning':True,'exploration':[]}
engine=LocalEngine(settings);client=ModelClient(engine,settings)
report={'model':args.model};output=ROOT/'runtime/model_preparation'/f'{args.model}_structured_smoke.json'
try:
    report['attempts']=[]
    for attempt in range(2):
        plan=generate_plan(payload,client,repair=bool(attempt));report['model_plan']=plan
        try:
            assert plan['status']=='answerable','Synthetic question should be answerable'
            override=plan.pop('temporal_override',None)
            if override:
                proposed=validate_windows(override['time_windows'],question['as_of_date'])
                bounds=lambda items:{w['name']:(w['start_date'],w['end_date_exclusive']) for w in items}
                assert bounds(proposed)==bounds(windows),'Synthetic fixture does not authorize a changed calendar'
                report['temporal_override_ignored']='same bounds, as in the production pipeline'
            plan,compilation=compile_plan(plan,metadata,windows,question);report['compilation']=compilation
            plan['_validated_windows']=windows;plan['_original_question']=question['question']
            checked=derive_column_usage(plan['sql'],metadata,plan);report['sql']=validate_sql(checked,metadata,plan=plan)
            report['status']='passed';break
        except Exception as error:
            report['attempts'].append({'error':str(error)})
            if attempt:raise
            payload={**payload,'repair':{'error':str(error),'previous_plan':plan,
                'instruction':'Return a complete corrected plan. Use only declared rule ids/question/facts as basis. For each filters rule encode its condition in business_plan.filters. Keep output_columns equal to declared aggregate/group aliases.'}}
except Exception as error:
    report.update(status='failed',error=str(error));raise
finally:
    report['calls']=client.calls;write_json(output,report)
print(dumps({'status':report['status'],'model':args.model,'mode':report['compilation']['mode'],'report':str(output)}))
