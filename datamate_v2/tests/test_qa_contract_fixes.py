import copy
import pytest
from datamate_v2.common import PipelineError,dumps
from datamate_v2.inference.structured_output import parse_output
from datamate_v2.qa_pipeline.context import build_qa_context
from datamate_v2.qa_pipeline.planning_sql.planner import planning_schema
from datamate_v2.qa_pipeline.sql_validation.guard import validate_sql
from datamate_v2.qa_pipeline.entities import resolve_entities
from datamate_v2.qa_pipeline.execution.executor import run_checks
from datamate_v2.qa_pipeline.pipeline import QAPipeline
from datamate_v2.tests.test_orchestration import FixtureClient,FixtureTool,settings


@pytest.mark.parametrize('predicate',["order_date = DATE '2026-03-03'","order_date = '2026-03-03'","DATE '2026-03-03' = order_date"])
def test_date_equality_proves_single_day(metadata,plan,predicate):
    sql=plan['sql'].replace("order_date >= DATE '2026-03-03' AND order_date < DATE '2026-03-04'",predicate)
    assert validate_sql(sql,metadata,plan)


@pytest.mark.parametrize('failure',['wrong_date','timestamp','several_days','wrong_extra_bound','or_branch'])
def test_date_equality_never_weakens_other_guards(metadata,plan,failure):
    predicate="order_date = DATE '2026-03-03'"
    if failure=='wrong_date': predicate="order_date = DATE '2026-03-02'"
    if failure=='timestamp': metadata['tables'][0]['columns'][1]['type']='TIMESTAMPTZ'
    if failure=='several_days': plan['_validated_windows'][0]['end_date_exclusive']='2026-03-05'
    if failure=='wrong_extra_bound': predicate+=" AND order_date < DATE '2026-03-03'"
    if failure=='or_branch': predicate='('+predicate+' OR 1=1)'
    sql=plan['sql'].replace("order_date >= DATE '2026-03-03' AND order_date < DATE '2026-03-04'",predicate)
    with pytest.raises(PipelineError): validate_sql(sql,metadata,plan)


@pytest.mark.parametrize('failure',['table','column','binding_table','binding_column','wrong_table_column'])
def test_plan_schema_uses_real_table_column_pairs(metadata,plan,window,failure):
    second=copy.deepcopy(metadata['tables'][0]);second['sql_table']='delivery';second['columns']=second['columns'][:2]
    metadata['tables'].append(second)
    schema=planning_schema({'business_context':build_qa_context(metadata),'validated_windows':[window]})
    response={k:v for k,v in copy.deepcopy(plan).items() if not k.startswith('_')}
    assert parse_output(dumps(response),schema)
    if failure=='table': response['required_tables']=['invented_sales']
    if failure=='column': response['column_usage'][0]['column']='status'
    if failure=='binding_table': response['time_bindings'][0]['table']='invented_sales'
    if failure=='binding_column': response['time_bindings'][0]['column']='quantity'
    if failure=='wrong_table_column': response['column_usage'][0].update(table='delivery',column='quantity')
    with pytest.raises(PipelineError): parse_output(dumps(response),schema)


def test_dynamic_catalog_schema_supported_by_actual_json_enforcer(metadata,plan,window):
    from lmformatenforcer import JsonSchemaParser
    second=copy.deepcopy(metadata['tables'][0]);second['sql_table']='delivery';second['columns']=second['columns'][:2]
    metadata['tables'].append(second)
    schema=planning_schema({'business_context':build_qa_context(metadata),'validated_windows':[window]})
    response={k:v for k,v in plan.items() if not k.startswith('_')}
    parser=JsonSchemaParser(schema)
    for char in dumps(response):
        assert char in parser.get_allowed_characters()
        parser=parser.add_character(char)
    assert parser.can_end()
    invalid=copy.deepcopy(response);invalid['column_usage'][0]['table']='invented'
    parser=JsonSchemaParser(schema);blocked=False
    for char in dumps(invalid):
        if char not in parser.get_allowed_characters(): blocked=True;break
        parser=parser.add_character(char)
    assert blocked


def test_repair_identifies_bad_column_and_allowed_columns(metadata,question,plan):
    class Client(FixtureClient):
        def call(self,task,payload,schema,**kwargs):
            if kwargs.get('repair'):
                assert "has no column 'status'" in payload['repair']['error']
                assert 'quantity' in payload['repair']['allowed_columns_for_used_tables']['sales']
            result=super().call(task,payload,schema,**kwargs)
            if task=='planning_sql' and not kwargs.get('repair'): result['column_usage'][0]['column']='status'
            return result
    client=Client(question,plan)
    final,log=QAPipeline(client,{},settings(),FixtureTool).answer(question,metadata)
    assert final['answer']==7 and len(client.calls)==3 and log['repair_attempts']==1


def test_sql_string_is_still_checked_even_when_plan_names_are_valid(metadata,question,plan):
    plan['sql']=plan['sql'].replace('SUM(quantity)','SUM(status)')
    client=FixtureClient(question,plan)
    final,log=QAPipeline(client,{},settings(),FixtureTool).answer(question,metadata)
    assert final['type']=='abstain' and 'Allowed columns by table: sales:' in log['error']


def test_entity_resolution_uses_db_values_not_samples(metadata):
    metadata['tables'][0]['columns'][0]['meaning']='product_name'
    class Tool:
        def entity_values(self,columns): return {'values':['Other','Thuốc lá Vinataba'],'complete':True}
    records=resolve_entities({'products':['Thuoc la Vinataba']},metadata,Tool())
    assert records[0]['canonical_value']=='Thuốc lá Vinataba'


def test_order_metric_phrase_does_not_trigger_full_identifier_domain_scan(metadata):
    class Tool:
        def entity_values(self,columns): pytest.fail('Order metrics are not accent-resolved name entities')
    assert resolve_entities({'orders':['so don hang']},metadata,Tool())==[]


@pytest.mark.parametrize('failure',['ambiguous','partial'])
def test_entity_resolution_never_guesses(metadata,failure):
    metadata['tables'][0]['columns'][0]['meaning']='product_name'
    class Tool:
        def entity_values(self,columns): return {'values':['Hoa','Hòa'],'complete':failure!='partial'}
    with pytest.raises(PipelineError): resolve_entities({'products':['hoa']},metadata,Tool())


def test_empty_population_check_does_not_turn_null_into_zero():
    class Tool:
        def execute_readonly_sql(self,*args,**kwargs): return {'rows':[[0]],'columns':['n'],'row_count':1}
    check={'sql':'SELECT COUNT(*) FROM sales WHERE quantity > 100','expected':'positive'}
    result=run_checks(Tool(),[check],answer_sql='SELECT SUM(quantity) FROM sales WHERE quantity > 100')
    assert result[0]['status']=='empty_population' and not result[0]['assertion_applied']
    with pytest.raises(PipelineError): run_checks(Tool(),[check],answer_sql='SELECT SUM(quantity) FROM sales WHERE quantity > 200')
    with pytest.raises(PipelineError): run_checks(Tool(),[check])
    nullable={**check,'sql':check['sql'].replace('COUNT(*)','COUNT(quantity)')}
    with pytest.raises(PipelineError): run_checks(Tool(),[nullable],answer_sql='SELECT SUM(quantity) FROM sales WHERE quantity > 100')
