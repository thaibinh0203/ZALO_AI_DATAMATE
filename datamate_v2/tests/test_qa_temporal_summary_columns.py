import copy
import jsonschema
import pytest
from sqlglot import exp
from lmformatenforcer import JsonSchemaParser
from datamate_v2.common import PipelineError,dumps
from datamate_v2.contracts import REWRITE_SCHEMA
from datamate_v2.qa_pipeline.query_rewriting.temporal_facts import temporal_facts,constrain_time_schema
from datamate_v2.qa_pipeline.query_rewriting.rewriter import rewrite_question
from datamate_v2.qa_pipeline.temporal_validator import validate_windows
from datamate_v2.qa_pipeline.sql_validation.summary_policy import enforce_summary_policy
from datamate_v2.qa_pipeline.sql_validation.guard import parse_readonly,validate_sql,_base_scopes,conjuncts
from datamate_v2.qa_pipeline.planning_sql.feedback import repair_feedback
from datamate_v2.qa_pipeline.context import build_qa_context
from datamate_v2.qa_pipeline.pipeline import QAPipeline
from datamate_v2.tests.test_orchestration import FixtureClient,FixtureTool,settings


@pytest.mark.parametrize('text,kind,params',[
    ('thang 2/2026 mat hang Alpha thu ve bao nhieu tien','absolute_month',{'year':2026,'month':2}),
    ('Doanh thu tháng 8 năm 2026','absolute_month',{'year':2026,'month':8}),
    ('Đếm số đơn quý 4/2025','absolute_quarter',{'year':2025,'quarter':4}),
    ('Doanh thu 7 ngày gần nhất','rolling_days',{'n':7}),
    ('Số đơn hôm qua','yesterday',{}),
])
def test_time_facts_from_original_only(text,kind,params,question):
    question['question']=text
    facts=temporal_facts(question,settings()['calendar'])
    assert facts['windows'][0]['interpretation']==kind
    assert facts['windows'][0]['parameters']==params
    assert facts['source_question']==text


@pytest.mark.parametrize('text',[
    'Doanh thu quý tài chính vừa rồi', 'Doanh thu dịp Tết',
    '15 ngày đầu tháng 2/2026', 'Doanh thu từ tháng 2/2026 đến tháng 4/2026',
    'Doanh thu ngày 15 tháng 2 năm 2026', 'Thứ ba tuần trước bán được bao nhiêu',
    'Tháng này so với toàn bộ tháng trước', 'Doanh thu trước hôm nay',
    'Doanh thu tháng 13/2026', 'Doanh thu tháng 5',
])
def test_ambiguous_or_partial_expressions_are_not_guessed(text,question):
    question['question']=text
    assert temporal_facts(question,settings()['calendar']) is None


def test_month_comparison_keeps_both_windows(question):
    question.update(question='dthu tu dau thang nay den het hnay hon kem cung ky thang trc bao nhieu',as_of_date='2026-03-04')
    facts=temporal_facts(question,settings()['calendar'])
    windows=validate_windows(facts['windows'],question['as_of_date'])
    assert [(w['start_date'],w['end_date_exclusive']) for w in windows]==[('2026-03-01','2026-03-05'),('2026-02-01','2026-02-05')]
    schema=constrain_time_schema(copy.deepcopy(REWRITE_SCHEMA),facts)
    response={'id':'x','business_id':'x','original_question':question['question'],'rewritten_question':question['question'],
              'entities':{},'time_windows':facts['windows'],'ambiguities':[]}
    parser=JsonSchemaParser(schema)
    for char in dumps(response):
        assert char in parser.get_allowed_characters()
        parser=parser.add_character(char)
    assert parser.can_end()


def test_clear_month_locks_schema_and_recovers_empty_window(question,plan):
    question['question']='Tháng 2/2026 bán được bao nhiêu tiền?'
    class Client(FixtureClient):
        def call(self,task,payload,schema,**kwargs):
            assert payload['temporal_facts']['windows'][0]['parameters']=={'month':2,'year':2026}
            response=super().call(task,payload,schema,**kwargs)
            bad=copy.deepcopy(response)
            bad['time_windows']=[{'name':'x','interpretation':'current_calendar_month','parameters':{},'status':'proposed'}]
            with pytest.raises(jsonschema.ValidationError):jsonschema.validate(bad,schema)
            response['time_windows']=[]
            return response
    client=Client(question,plan)
    rewrite=rewrite_question(question,client,settings()['calendar'])
    assert rewrite['time_windows'][0]['interpretation']=='absolute_month'
    assert rewrite['_temporal_facts']['model_windows']==[] and len(client.calls)==1


def test_temporal_recovery_preserves_repair_budget(question,plan,metadata):
    class Client(FixtureClient):
        def call(self,task,payload,schema,**kwargs):
            response=super().call(task,payload,schema,**kwargs)
            if task=='query_rewriting':response['time_windows'][0]['interpretation']='current_calendar_month'
            return response
    client=Client(question,plan)
    final,log=QAPipeline(client,{},settings(),FixtureTool).answer(question,metadata)
    assert final['answer']==7 and len(client.calls)==2 and log['repair_attempts']==0
    assert log['validated_windows'][0]['name']=='period'
    assert log['temporal_facts']['model_windows'][0]['interpretation']=='current_calendar_month'


@pytest.mark.parametrize('sql',[
    'WITH x AS (SELECT order_id,quantity FROM sales) SELECT SUM(quantity) AS total FROM x',
    'SELECT SUM(quantity) AS total FROM (SELECT quantity FROM sales UNION ALL SELECT quantity FROM sales) AS x',
    'SELECT SUM(s.quantity) AS total FROM sales s JOIN sales x ON s.order_id=x.order_id',
    'SELECT SUM(s.quantity) AS total FROM sales s JOIN sales x ON s.order_id=x.order_id WHERE NOT x._is_summary_row',
])
def test_summary_exclusion_in_every_physical_scope(sql,metadata,plan):
    patched,actions=enforce_summary_policy(sql,metadata,plan,'Tổng số lượng')
    assert actions
    for scope,bases in _base_scopes(parse_readonly(patched),{'sales':metadata['tables'][0]}):
        where=scope.expression.args.get('where')
        predicates=conjuncts(where.this) if where else []
        for alias in bases:
            assert any(isinstance(p,exp.EQ) and isinstance(p.this,exp.Column) and p.this.table==alias
                       and p.this.name=='_is_summary_row' and p.expression==exp.Boolean(this=False) for p in predicates)
    second,actions=enforce_summary_policy(patched,metadata,plan,'Tổng số lượng')
    assert not actions


@pytest.mark.parametrize('side',['LEFT','RIGHT','FULL'])
def test_summary_exclusion_never_changes_outer_join(side,metadata,plan):
    sql=f'SELECT SUM(s.quantity) AS total FROM sales s {side} JOIN sales x ON s.order_id=x.order_id'
    patched,actions=enforce_summary_policy(sql,metadata,plan,'Tổng số lượng')
    assert not actions and '_is_summary_row' not in patched


def test_contradictory_summary_policy_is_not_silently_overridden(metadata,plan):
    with pytest.raises(PipelineError,match='Conflicting'):
        enforce_summary_policy('SELECT SUM(quantity) AS total FROM sales WHERE _is_summary_row=TRUE',metadata,plan,'Tổng số lượng')


def test_missing_column_feedback_is_scope_specific(metadata):
    sales=metadata['tables'][0]
    sales['columns'].append({'sql_column':'status','type':'TEXT','semantic_status':'verified'})
    delivery={'sql_table':'delivery','columns':[{'sql_column':'order_id','type':'TEXT','semantic_status':'verified'}],
              'quality':{'uncertain_summary_rows':0},'sample_rows':[],'last_rows':[],'stats':[]}
    metadata['tables'].append(delivery)
    sql='SELECT SUM(s.quantity) AS total FROM sales s JOIN delivery d ON s.order_id=d.order_id WHERE d.status=\'Xong\''
    with pytest.raises(PipelineError,match='Alias d refers to delivery') as failure:validate_sql(sql,metadata)
    d=failure.value.column_diagnostics[0]
    assert d['column']=='status' and d['visible_candidates']==[{'alias':'s','table':'sales'}]
    context=build_qa_context(metadata)
    feedback=repair_feedback({'sql':sql,'required_tables':[]},context,[])
    assert feedback['column_errors'][0]['alias']=='d'
    assert 'delivery' in feedback['allowed_columns_for_used_tables']
    assert 'd.status' in sql  # Diagnostics never edit the model query.
