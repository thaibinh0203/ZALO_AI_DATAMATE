import copy
import jsonschema
import pytest
from lmformatenforcer import JsonSchemaParser
from datamate_v2.common import PipelineError,dumps
from datamate_v2.qa_pipeline.planning_sql.compiler import compile_plan
from datamate_v2.qa_pipeline.planning_sql.planner import planning_schema
from datamate_v2.qa_pipeline.sql_validation.guard import validate_sql
from datamate_v2.qa_pipeline.sql_validation.summary_policy import enforce_summary_policy


def structured(plan,metadata):
    result=copy.deepcopy(plan);result.pop('_validated_windows',None);result['sql']=None
    result['applicable_rules']=[{'id':'r1','source':'rulebook','quote':metadata['rulebook'],'application':'Use the selected fixture metric.','applies_to':'calculation'}]
    result['business_plan']={'mode':'compiled','metric':'fixture quantity','sources':['sales'],'lookups':[],
        'filters':[],'group_by':[],'aggregates':[{'function':'SUM','columns':[{'relation':'source','column':'quantity'}],'alias':'total','basis':'r1'}],
        'having':[],'order_by':[],'limit':None,'result_mode':'rows'}
    return result


def test_structured_contract_is_supported_by_actual_enforcer(plan,metadata):
    response=structured(plan,metadata);schema=planning_schema({'structured_planning':True})
    jsonschema.validate(response,schema)
    parser=JsonSchemaParser(schema)
    for char in dumps(response):
        assert char in parser.get_allowed_characters(),repr(char)
        parser=parser.add_character(char)
    assert parser.can_end()


def test_compile_total_preserves_each_filter(plan,metadata,window):
    response=structured(plan,metadata)
    response['business_plan']['filters']=[{'relation':'source','column':'order_id','operator':'not_in','values':['cancelled','returned'],'basis':'r1'}]
    compiled,log=compile_plan(response,metadata,[window],{'question':'fixture'})
    assert log['mode']=='compiled' and log['filters_compiled']==1
    assert "'cancelled'" in compiled['sql'] and "'returned'" in compiled['sql']
    assert '2026-03-03' in compiled['sql'] and '_is_summary_row' in compiled['sql']
    validate_sql(compiled['sql'],metadata)
    assert response['sql'] is None  # No mutation of the original model object.


@pytest.mark.parametrize('change', ['bad_column','bad_basis','bad_output','missing_window','raw_injection'])
def test_compiler_rejects_invalid_sources_and_contracts(change,plan,metadata,window):
    response=structured(plan,metadata)
    if change=='bad_column':response['business_plan']['aggregates'][0]['columns'][0]['column']='missing'
    if change=='bad_basis':response['business_plan']['aggregates'][0]['basis']='invented_rule'
    if change=='bad_output':
        response['output_columns']=['not_an_aggregate']
        response['business_plan']['group_by']=[{'relation':'source','column':'order_id','alias':'order_id','basis':'question'}]
    if change=='missing_window':response['time_bindings']=[]
    if change=='raw_injection':response['business_plan']['sources']=['sales; DROP TABLE sales']
    with pytest.raises(PipelineError):compile_plan(response,metadata,[window],{'question':'fixture'})


def test_literal_cannot_inject_sql(plan,metadata,window):
    response=structured(plan,metadata)
    response['business_plan']['filters']=[{'relation':'source','column':'order_id','operator':'eq','values':["a'; DROP TABLE sales; --"],'basis':'question'}]
    compiled,_=compile_plan(response,metadata,[window],{'question':'fixture'})
    validate_sql(compiled['sql'],metadata)


def test_raw_fallback_verifies_rule_without_rewriting_model_sql(plan,metadata,window):
    response=structured(plan,metadata);response['business_plan']={'mode':'raw_sql','fallback_reason':'Unsupported formula fixture'};response['sql']=plan['sql']
    result,log=compile_plan(response,metadata,[window],{'question':'fixture'})
    assert result['sql']==plan['sql'] and log['mode']=='raw_sql'


def test_summary_nested_subquery_reusing_alias_is_not_a_conflict(metadata,plan):
    sql='SELECT SUM(quantity) AS total FROM sales WHERE order_id IN (SELECT order_id FROM sales)'
    compiled,changes=enforce_summary_policy(sql,metadata,plan,'Tổng số lượng')
    assert len(changes)==2 and compiled.count('FALSE')==2
    again,changes=enforce_summary_policy(compiled,metadata,plan,'Tổng số lượng')
    assert not changes


def test_summary_exists_alias_shadow_is_not_a_conflict(metadata,plan):
    sql='SELECT SUM(s.quantity) AS total FROM sales s WHERE EXISTS (SELECT x.order_id FROM sales x WHERE x.order_id=s.order_id)'
    compiled,changes=enforce_summary_policy(sql,metadata,plan,'Tổng số lượng')
    assert len(changes)==2 and compiled.count('FALSE')==2


def test_structured_raw_contract_and_boolean_filter_enforcer(plan,metadata):
    response=structured(plan,metadata);response['sql']=plan['sql'];response['business_plan']={'mode':'raw_sql','fallback_reason':'Unsupported fixture formula'}
    schema=planning_schema({'structured_planning':True})
    jsonschema.validate(response,schema)
    parser=JsonSchemaParser(schema)
    for char in dumps(response):
        assert char in parser.get_allowed_characters()
        parser=parser.add_character(char)
    assert parser.can_end()
    response=structured(plan,metadata)
    response['business_plan']['filters']=[{'relation':'source','column':'order_id','operator':'eq','values':['a'],'basis':'question'}]
    parser=JsonSchemaParser(schema)
    for char in dumps(response):
        assert char in parser.get_allowed_characters()
        parser=parser.add_character(char)
    assert parser.can_end()


def test_empty_population_check_does_not_ignore_different_sources():
    from datamate_v2.qa_pipeline.execution.executor import row_existence_check
    assert not row_existence_check('SELECT COUNT(*) FROM missing_data','SELECT SUM(quantity) FROM sales')
    assert row_existence_check('SELECT COUNT(*) FROM sales','SELECT SUM(quantity) FROM sales')


def test_failed_check_keeps_diagnostic_result():
    from datamate_v2.qa_pipeline.execution.executor import run_checks
    class Tool:
        def execute_readonly_sql(self,*args,**kwargs):return {'columns':['x'],'rows':[[None]],'row_count':1}
    with pytest.raises(PipelineError) as failure:
        run_checks(Tool(),[{'sql':'SELECT SUM(quantity) FROM sales','expected':'positive','reason':'fixture'}])
    assert failure.value.check_results[0]['status']=='failed'
    assert failure.value.check_results[0]['result']['rows']==[[None]]


def test_rule_text_no_longer_creates_a_basis_linkage_gate(plan,metadata,window):
    response=structured(plan,metadata);response['applicable_rules'][0]['applies_to']='filters'
    compiled,_=compile_plan(response,metadata,[window],{'question':'fixture'})
    assert compiled['sql']
    response['business_plan']['filters']=[{'relation':'source','column':'order_id','operator':'ne','values':['cancelled'],'basis':'r1'}]
    compiled,_=compile_plan(response,metadata,[window],{'question':'fixture'})
    assert "'cancelled'" in compiled['sql']


def test_paraphrased_rule_is_advisory_not_an_exact_match_gate(plan,metadata,window):
    response=structured(plan,metadata);response['applicable_rules'][0]['applies_to']='ranking'
    response['applicable_rules'][0]['quote']='Rule text with Markdown formatting removed.'
    compiled,_=compile_plan(response,metadata,[window],{'question':'fixture'})
    assert compiled['sql']


def test_derived_scope_feedback_lists_exports_and_unbound_alias(metadata):
    from datamate_v2.qa_pipeline.context import build_qa_context
    from datamate_v2.qa_pipeline.planning_sql.feedback import repair_feedback
    feedback=repair_feedback({'sql':'WITH x AS (SELECT SUM(quantity) AS total FROM sales) SELECT x.total','required_tables':['sales']},build_qa_context(metadata),[])
    outer=feedback['scope_catalog'][-1]
    assert 'x' in outer['unbound_aliases'] and 'x' in outer['available_ctes']
    feedback=repair_feedback({'sql':'WITH x AS (SELECT SUM(quantity) AS total FROM sales) SELECT quantity FROM x'},build_qa_context(metadata),[])
    assert feedback['scope_catalog'][-1]['visible_sources']['x']['columns']==['total']


def test_unique_scalar_compiled_output_alias_can_be_synchronized(plan,metadata,window):
    response=structured(plan,metadata);response['output_columns']=['quantity_sum']
    compiled,log=compile_plan(response,metadata,[window],{'question':'fixture'})
    assert compiled['output_columns']==['total'] and log['output_alias_adjusted']
    assert 'SUM(' in compiled['sql']
