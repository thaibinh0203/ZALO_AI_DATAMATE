import copy
import pytest
from datamate_v2.common import PipelineError,ROOT,load_yaml,write_json,read_json
from datamate_v2.qa_pipeline.pipeline import QAPipeline
from datamate_v2.data_pipeline.pipeline import DataPipeline
from datamate_v2.tests.factories import schema_fixture

class FixtureClient:
    version='fixture-version'
    def __init__(self,question,plan): self.question=question;self.plan=plan;self.calls=[]
    def call(self,task,payload,schema,**kwargs):
        self.calls.append({'task':task,'cache_hit':False,'input_tokens':3,'output_tokens':4,'duration_ms':1,'thinking':None})
        if task=='query_rewriting':
            response={'id':self.question['id'],'business_id':self.question['business_id'],'original_question':self.question['question'],
              'rewritten_question':self.question['question'],'entities':{},'ambiguities':[],
              'time_windows':[{'name':'period','interpretation':'yesterday','parameters':{},'status':'proposed'}]}
            if payload.get('select_rules_in_call1'):
                response['selected_rules']=[{'id':'r1','source':'rulebook','excerpt':payload['ingestion_context']['rulebook'][:400]}]
            return response
        response={k:copy.deepcopy(v) for k,v in self.plan.items() if not k.startswith('_')}
        if payload.get('rule_selection_from_call1'):
            response.pop('applicable_rules',None)
        if payload.get('structured_planning') and response.get('status')=='answerable':
            if not payload.get('rule_selection_from_call1'):
                response.setdefault('applicable_rules',[{'id':'r1','source':'rulebook',
                    'quote':payload['business_context']['rulebook'][:200],'application':'Synthetic fixture only.','applies_to':'calculation'}])
            response.setdefault('business_plan',{'mode':'raw_sql','fallback_reason':'Synthetic SQL fixture.'})
        return response

class FixtureTool:
    def __init__(self,*args,**kwargs): self.trace=[]
    def execute_readonly_sql(self,sql,**kwargs):
        self.trace.append({'sql':sql,'purpose':kwargs.get('purpose'),'status':'success'})
        return {'columns':['total'],'rows':[[7]],'row_count':1}

def settings(): return load_yaml(ROOT/'configs/pipeline.yaml')['qa']

def test_two_calls_no_final_formatter_model(question,plan,metadata):
    client=FixtureClient(question,plan)
    final,log=QAPipeline(client,{},settings(),FixtureTool).answer(question,metadata)
    assert final=={'id':question['id'],'type':'number','answer':7}
    assert len(client.calls)==2 and log['repair_attempts']==0
    assert log['validated_windows'][0]['status']=='validated'


@pytest.mark.parametrize('description',[None,{'group_ref':'dg_fixture'}])
def test_qa_receives_original_sources_when_description_unavailable(question,plan,metadata,description):
    metadata['tables'][0].update(description=description,description_status='unavailable' if description is None else 'available')
    metadata['description_groups']={'dg_fixture':{'description':{'grain':'Shared row meaning.'},'tables':['sales']}}
    class Client(FixtureClient):
        def call(self,task,payload,schema,**kwargs):
            if task=='planning_sql':
                context=payload['business_context']
                assert context['readme']==metadata['readme']
                if payload.get('rule_selection_from_call1'):
                    assert 'rulebook' not in context and context['rulebook_selected_only']
                    assert payload['selected_rules'][0]['excerpt']==metadata['rulebook']
                else:assert context['rulebook']==metadata['rulebook']
                assert context['tables'][0]['description']==description
                if description:
                    assert context['description_groups']['dg_fixture']['description']==metadata['description_groups']['dg_fixture']['description']
                assert 'stats' not in context['tables'][0]
                group=context['schema_groups'][context['tables'][0]['schema_group']]
                assert {c['sql_column'] for c in group['columns']}=={c['sql_column'] for c in metadata['tables'][0]['columns']}
            return super().call(task,payload,schema,**kwargs)
    client=Client(question,plan)
    final,log=QAPipeline(client,{},settings(),FixtureTool).answer(question,metadata)
    assert final=={'id':question['id'],'type':'number','answer':7}
    assert len(client.calls)==2

def test_unanswerable_no_sql(question,metadata):
    client=FixtureClient(question,{'status':'unanswerable','sql':None,'calculation_plan':[],'reason':'Required field absent'})
    final,log=QAPipeline(client,{},settings(),FixtureTool).answer(question,metadata)
    assert final['type']=='unanswerable' and not log['queries'] and len(client.calls)==2

def test_one_repair_only(question,plan,metadata):
    plan['sql']='DROP TABLE sales';client=FixtureClient(question,plan)
    final,log=QAPipeline(client,{},settings(),FixtureTool).answer(question,metadata)
    assert final['type']=='abstain' and len(client.calls)==3 and log['repair_attempts']==1

def test_no_repair_for_null_result(question,plan,metadata):
    class NullTool(FixtureTool):
        def execute_readonly_sql(self,*args,**kwargs): return {'columns':['total'],'rows':[[None]],'row_count':1}
    client=FixtureClient(question,plan)
    final,log=QAPipeline(client,{},settings(),NullTool).answer(question,metadata)
    assert final['type']=='abstain' and len(client.calls)==2

def test_outside_coverage_is_ua_without_execution(question,plan,metadata):
    metadata['tables'][0]['stats'][1].update(min='2025-01-01',max='2025-12-31')
    client=FixtureClient(question,plan)
    final,log=QAPipeline(client,{},settings(),FixtureTool).answer(question,metadata)
    assert final['type']=='unanswerable' and not log['queries']

def test_business_isolation_before_model(question,plan,metadata):
    metadata['business_id']='another';client=FixtureClient(question,plan)
    final,log=QAPipeline(client,{},settings(),FixtureTool).answer(question,metadata)
    assert final['type']=='abstain' and not client.calls

def test_schema_description_failure_is_explicit_and_nonfatal(tmp_path):
    split=tmp_path/'split';business=split/'data/synthetic';business.mkdir(parents=True)
    (business/'README.md').write_text('Synthetic fixture.')
    (business/'sales.csv').write_text('Mã đơn,Số lượng\na,2\nb,3\n')
    (split/'rulebook.md').write_text('Synthetic fixture.')
    (split/'calendar.csv').write_text('date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n')
    class FailingClient:
        version='mock';calls=[]
        def call(self,*args,**kwargs): raise PipelineError('Schema generation failed')
    result=DataPipeline(FailingClient(),{}, {},tmp_path/'cache').prepare(business,split,load_database=False)
    table=result['tables'][0]
    assert table['description'] is None and table['description_status']=='unavailable'
    assert table['description_error']['error']=='Schema generation failed'
    assert table['verified_facts']['row_count']==2
    assert result['readme']=='Synthetic fixture.' and result['rulebook']=='Synthetic fixture.'
    assert any('description unavailable' in warning for warning in result['warnings'])

def test_ingest_cache_and_provenance(tmp_path):
    split=tmp_path/'split';business=split/'data/synthetic';business.mkdir(parents=True)
    (business/'README.md').write_text('Synthetic fixture.')
    (business/'sales.csv').write_text('Mã đơn,Số lượng\na,2\nb,3\n')
    (split/'rulebook.md').write_text('Synthetic fixture.')
    (split/'calendar.csv').write_text('date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n')
    class Client:
        version='mock';calls=[]
        def call(self,task,payload,*args,**kwargs):
            self.calls.append(task)
            return schema_fixture(payload['table'])
    client=Client();pipeline=DataPipeline(client,{}, {},tmp_path/'cache')
    first=pipeline.prepare(business,split,load_database=False);second=pipeline.prepare(business,split,load_database=False)
    assert len(client.calls)==1 and first['fingerprint']==second['fingerprint']
    assert first['tables'][0]['row_count']==2
    assert list((tmp_path/'cache').rglob('mapping_registry.json'))
    (business/'sales.csv').write_text('Mã đơn,Số lượng\na,2\nb,4\n')
    third=pipeline.prepare(business,split,load_database=False)
    assert third['fingerprint']!=first['fingerprint'] and len(client.calls)==2
