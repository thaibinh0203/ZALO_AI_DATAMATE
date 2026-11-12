from copy import deepcopy
from decimal import Decimal
import pytest
from datamate_v2.common import PipelineError
from datamate_v2.qa_pipeline.pipeline import QAPipeline
from datamate_v2.qa_pipeline.direct_sql import execution_guard


def metadata():
    return {'business_id':'biz_test','schema':'biz_test_schema','fingerprint':'test',
        'readme':'Readme definition','rulebook':'Rulebook definition',
        'tables':[{'sql_table':'sales','columns':[{'sql_column':'amount','type':'NUMERIC',
            'semantic_status':'uncertain'}],'sample_rows':[{'amount':10}]}]}


class Client:
    version='test'
    def __init__(self,sql='SELECT SUM(amount) AS revenue FROM sales',kind='number'):
        self.calls=[];self.payloads=[];self.sql=sql;self.kind=kind
    def call(self,task,payload,schema):
        self.calls.append({'task':task,'cache_hit':False})
        self.payloads.append(deepcopy(payload))
        if task=='qa_analysis':
            return {'question_analysis':'count','entity_mentions':[],
                'entity_definitions':[],'time_analysis':''}
        return {'status':'answerable','calculation_plan':['model decision'],
            'sql':self.sql,'reason':'test','answer_type':self.kind,'unit':'VND',
            'list_mode':'rows','round_digits':None}


def run(client,rows,columns=None,error=None):
    class Tool:
        def __init__(self,md,db,guard,max_queries):
            self.trace=[];self.md=md;self.guard=guard
        def execute_readonly_sql(self,sql,**kwargs):
            checked=self.guard(sql,self.md)
            self.trace.append({'sql':checked})
            if error:raise error
            return {'columns':columns or ['revenue'],'rows':rows,'row_count':len(rows)}
    q={'id':'q1','business_id':'biz_test','question':'Số đơn hôm nay?', 'as_of_date':'2026-11-12'}
    return QAPipeline(client,{}, {'mode':'direct_sql','max_repairs':0,
        'calendar':{'calendar_rule':'MONDAY_TO_SUNDAY','timezone':'Asia/Ho_Chi_Minh'}},Tool).answer(q,metadata())


def test_direct_executes_without_business_guards_and_passes_full_sources_to_both_calls():
    client=Client()
    answer,log=run(client,[[Decimal('928200')]])
    assert answer=={'id':'q1','type':'number','answer':928200}
    assert log['checks']==[] and log['model_call_count']==2
    assert [c['task'] for c in client.calls]==['qa_analysis','qa_sql']
    for payload in client.payloads:
        assert payload['business_context']['rulebook']=='Rulebook definition'
        assert payload['business_context']['readme']=='Readme definition'
        assert len(payload['business_context']['queryable_tables'])==1
    assert client.payloads[1]['analysis']['question_analysis']=='count'
    assert '_is_summary_row' not in log['queries'][0]['sql']


def test_postgres_error_abstains_without_repair():
    client=Client('SELECT missing_column FROM sales')
    answer,log=run(client,[],error=RuntimeError('column missing_column does not exist'))
    assert answer['type']=='abstain'
    assert log['error']=='column missing_column does not exist'
    assert len(client.calls)==2


@pytest.mark.parametrize('rows', [[],[[None]],[[1],[2]]])
def test_invalid_scalar_output_abstains(rows):
    answer,log=run(Client(),rows)
    assert answer['type']=='abstain' and log['error']


def test_ranking_helper_inside_cte_is_allowed_without_alias_contract():
    sql='WITH r AS (SELECT amount AS total FROM sales) SELECT total FROM r LIMIT 1'
    answer,_=run(Client(sql),[[12]],['total'])
    assert answer['answer']==12


@pytest.mark.parametrize('sql',[
    'DELETE FROM sales', 'SELECT 1; SELECT 2',
    'SELECT amount FROM other_business.sales',
    'SELECT * FROM pg_catalog.pg_authid',
    'WITH sales AS (SELECT amount FROM forbidden) SELECT amount FROM sales',
    'SELECT pg_read_file(\'/etc/passwd\') FROM sales',
])
def test_safety_and_business_isolation_remain(sql):
    with pytest.raises(PipelineError):execution_guard(sql,metadata())


def test_unknown_column_is_delegated_to_postgres_not_semantic_guard():
    assert 'unknown_column' in execution_guard('SELECT unknown_column FROM sales',metadata())


def test_select_star_and_model_dates_are_not_rejected_as_business_policy():
    assert 'CURRENT_DATE' in execution_guard('SELECT *, CURRENT_DATE FROM sales',metadata())
