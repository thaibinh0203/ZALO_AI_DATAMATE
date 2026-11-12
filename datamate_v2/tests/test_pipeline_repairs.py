import copy
import pytest
from datamate_v2.common import PipelineError
from datamate_v2.qa_pipeline.pipeline import QAPipeline
from datamate_v2.qa_pipeline.sql_validation.summary_policy import enforce_summary_policy
from datamate_v2.qa_pipeline.sql_validation.guard import validate_sql
from datamate_v2.tests.test_orchestration import FixtureClient,FixtureTool,settings
from datamate_v2.qa_pipeline.planning_sql.planner import planning_schema
from datamate_v2.inference.structured_output import parse_output
from datamate_v2.common import dumps,ContextBudgetError


def test_binding_schema_uses_names_not_dates(plan,window):
    schema=planning_schema({'validated_windows':[window]})
    plan={k:v for k,v in plan.items() if not k.startswith('_')}
    assert parse_output(dumps(plan),schema)
    plan['time_bindings'][0]['window']='2026-03-03'
    with pytest.raises(PipelineError): parse_output(dumps(plan),schema)


def test_no_time_windows_forbid_override_and_bindings(plan):
    schema=planning_schema({'validated_windows':[]})
    plan={k:v for k,v in plan.items() if not k.startswith('_')}
    with pytest.raises(PipelineError): parse_output(dumps(plan),schema)
    plan['time_bindings']=[]
    assert parse_output(dumps(plan),schema)


def test_redundant_override_does_not_require_source_quote(question,plan,metadata):
    plan['temporal_override']={'source_quote':question['question'],'time_windows':[
        {'name':'period','interpretation':'yesterday','parameters':{},'status':'proposed'}]}
    client=FixtureClient(question,plan)
    legacy=settings();legacy['rule_selection_in_call1']=False
    final,log=QAPipeline(client,{},legacy,FixtureTool).answer(question,metadata)
    assert final['answer']==7 and len(client.calls)==2 and log['repair_attempts']==0
    assert log['temporal_override_ignored'] and log['validated_windows'][0]['name']=='period'


def test_changed_override_still_needs_source_evidence(question,plan,metadata):
    plan['temporal_override']={'source_quote':question['question'],'time_windows':[
        {'name':'period','interpretation':'today','parameters':{},'status':'proposed'}]}
    client=FixtureClient(question,plan)
    legacy=settings();legacy['rule_selection_in_call1']=False
    final,log=QAPipeline(client,{},legacy,FixtureTool).answer(question,metadata)
    assert final['type']=='abstain' and 'literal source evidence' in log['error'] and not log['queries']


def test_wrong_binding_consumes_only_one_repair(question,plan,metadata):
    class Client(FixtureClient):
        def call(self,task,payload,schema,**kwargs):
            result=super().call(task,payload,schema,**kwargs)
            if task=='planning_sql' and not kwargs.get('repair'): result['time_bindings'][0]['window']='2026-03-03'
            return result
    client=Client(question,plan)
    final,log=QAPipeline(client,{},settings(),FixtureTool).answer(question,metadata)
    assert final['answer']==7 and len(client.calls)==3 and log['repair_attempts']==1


def test_context_overflow_not_retried_with_same_payload(question,plan,metadata):
    class Client(FixtureClient):
        def call(self,task,payload,schema,**kwargs):
            if task=='planning_sql':
                self.calls.append({'task':task})
                raise ContextBudgetError('Full context requires too many tokens')
            return super().call(task,payload,schema,**kwargs)
    client=Client(question,plan)
    final,log=QAPipeline(client,{},settings(),FixtureTool).answer(question,metadata)
    assert final['type']=='abstain' and len(client.calls)==2 and log['repair_attempts']==0


def test_rewrite_repair_and_plan_share_one_budget(question,plan,metadata):
    plan['sql']='DROP TABLE sales'
    class Client(FixtureClient):
        def call(self,task,payload,schema,**kwargs):
            response=super().call(task,payload,schema,**kwargs)
            if len(self.calls)==1:
                response['rewritten_question']=''  # A non-temporal error still needs repair.
            return response
    client=Client(question,plan)
    final,log=QAPipeline(client,{},settings(),FixtureTool).answer(question,metadata)
    assert final['type']=='abstain' and log['repair_attempts']==1
    assert [c['task'] for c in client.calls]==['query_rewriting','query_rewriting','planning_sql']


def test_rewrite_repair_recovers_and_still_reaches_execution(question,plan,metadata):
    class Client(FixtureClient):
        def call(self,task,payload,schema,**kwargs):
            response=super().call(task,payload,schema,**kwargs)
            if len(self.calls)==1:
                response['rewritten_question']=''
            return response
    client=Client(question,plan)
    final,log=QAPipeline(client,{},settings(),FixtureTool).answer(question,metadata)
    assert final['answer']==7 and len(client.calls)==3 and log['repair_attempts']==1


def test_missing_summary_filter_completed_without_model_call(question,plan,metadata):
    plan['sql']=plan['sql'].replace(' AND _is_summary_row = FALSE','')
    client=FixtureClient(question,plan)
    final,log=QAPipeline(client,{},settings(),FixtureTool).answer(question,metadata)
    assert final['answer']==7 and len(client.calls)==2 and log['repair_attempts']==0
    assert log['sql_adjustments'] and '_is_summary_row' in log['sql']
    assert '_is_summary_row' not in log['model_sql']


def test_summary_guard_still_rejects_original_sql(metadata,plan):
    sql=plan['sql'].replace(' AND _is_summary_row = FALSE','')
    with pytest.raises(PipelineError): validate_sql(sql,metadata,plan)
    patched,actions=enforce_summary_policy(sql,metadata,plan,'Tổng số lượng hôm qua?')
    assert actions and validate_sql(patched,metadata,plan)


def test_never_override_explicit_summary_query(metadata,plan):
    sql=plan['sql'].replace('_is_summary_row = FALSE','_is_summary_row = TRUE')
    patched,actions=enforce_summary_policy(sql,metadata,plan,'Cho tôi giá trị ở dòng tổng')
    assert not actions and 'TRUE' in patched


def test_never_rewrite_outer_join_scope(metadata,plan):
    sql='SELECT SUM(s.quantity) AS total FROM sales s LEFT JOIN sales x ON s.order_id = x.order_id'
    patched,actions=enforce_summary_policy(sql,metadata,plan,'Tổng số lượng')
    assert not actions and '_is_summary_row' not in patched
