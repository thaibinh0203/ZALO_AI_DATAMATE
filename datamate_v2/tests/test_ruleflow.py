import copy
import jsonschema
import pytest
from lmformatenforcer import JsonSchemaParser
from datamate_v2.common import dumps
from datamate_v2.contracts import REWRITE_SCHEMA
from datamate_v2.qa_pipeline.query_rewriting.rule_selection import SELECTED_RULES,normalize_selected_rules
from datamate_v2.qa_pipeline.planning_sql.planner import planning_schema
from datamate_v2.qa_pipeline.planning_sql.compiler import compile_plan
from datamate_v2.qa_pipeline.context import build_qa_context
from datamate_v2.qa_pipeline.pipeline import QAPipeline
from datamate_v2.tests.test_orchestration import FixtureClient,FixtureTool,settings
from datamate_v2.tests.test_business_compiler import structured


def test_call1_gets_ingestion_and_call2_gets_excerpts_without_literal_gate(question,plan,metadata):
    class Client(FixtureClient):
        def call(self,task,payload,schema,**kwargs):
            result=super().call(task,payload,schema,**kwargs)
            if task=='query_rewriting':
                assert payload['ingestion_context']['rulebook']==metadata['rulebook']
                assert payload['ingestion_context']['tables']
                result['selected_rules']=[{'id':'r3','source':'rulebook','excerpt':'A paraphrased relevant rule, without original Markdown.'}]
            else:
                assert payload['selected_rules']==[{'id':'r1','source':'rulebook','excerpt':'A paraphrased relevant rule, without original Markdown.'}]
                assert 'rulebook' not in payload['business_context']
                assert payload['business_context']['readme']==metadata['readme']
                assert 'selected_rules' not in payload['rewrite']
                assert 'applicable_rules' not in schema['oneOf'][0]['properties']
                assert 'temporal_override' not in schema['oneOf'][0]['properties']
            return result
    client=Client(question,plan)
    answer,log=QAPipeline(client,{},settings(),FixtureTool).answer(question,metadata)
    assert answer['answer']==7 and len(client.calls)==2 and log['repair_attempts']==0
    assert log['selected_rules'][0]['id']=='r1'


def test_duplicate_excerpts_are_deduplicated_and_ids_assigned_before_call2():
    snippets=[{'id':'r3','source':'rulebook','excerpt':'First  rule'},
              {'id':'r3','source':'rulebook','excerpt':'First rule'},
              {'id':'r3','source':'rulebook','excerpt':'Second rule'}]
    assert normalize_selected_rules(snippets)==[{'id':'r1','source':'rulebook','excerpt':'First  rule'},
                                               {'id':'r2','source':'rulebook','excerpt':'Second rule'}]


def ruleflow(plan,metadata,window):
    response=structured(plan,metadata);response.pop('applicable_rules')
    payload={'structured_planning':True,'rule_selection_from_call1':True,
             'selected_rules':[{'id':'r1','source':'rulebook','excerpt':'Paraphrased rule'}],
             'validated_windows':[window],'business_context':build_qa_context(metadata)}
    return response,payload


def test_new_call2_contract_supported_by_real_decoder(plan,metadata,window):
    response,payload=ruleflow(plan,metadata,window);schema=planning_schema(payload)
    jsonschema.validate(response,schema)
    parser=JsonSchemaParser(schema)
    for char in dumps(response):
        assert char in parser.get_allowed_characters()
        parser=parser.add_character(char)
    assert parser.can_end()


def test_call2_cannot_emit_a_second_date_filter(plan,metadata,window):
    response,payload=ruleflow(plan,metadata,window);schema=planning_schema(payload)
    response['business_plan']['filters']=[{'relation':'source','column':'order_date','operator':'eq','values':['2026-03-03'],'basis':'facts'}]
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(response,schema)
    response['business_plan']['filters'][0]['column']='order_id'
    jsonschema.validate(response,schema)


def test_compiler_keeps_only_time_binding_bounds_for_adapter_date_filters(plan,metadata,window):
    response,payload=ruleflow(plan,metadata,window)
    response['selected_rules']=payload['selected_rules'];response['_rule_selection_from_call1']=True
    response['business_plan']['filters']=[{'relation':'source','column':'order_date','operator':'in','values':['2026-02-01','2026-02-29'],'basis':'facts'},
        {'relation':'source','column':'order_id','operator':'ne','values':['cancelled'],'basis':'question'}]
    compiled,log=compile_plan(response,metadata,[window],{'question':'fixture'})
    assert '2026-02-29' not in compiled['sql'] and '2026-03-03' in compiled['sql']
    assert "'cancelled'" in compiled['sql'] and len(log['ignored_model_date_filters'])==1
    assert len(compiled['business_plan']['filters'])==1


@pytest.mark.parametrize('kind',['sum_two_columns','comparison_two_values'])
def test_bad_operation_arity_is_blocked_by_generation_schema(plan,metadata,window,kind):
    response,payload=ruleflow(plan,metadata,window)
    if kind=='sum_two_columns':response['business_plan']['aggregates'][0]['columns']*=2
    else:response['business_plan']['filters']=[{'relation':'source','column':'order_id','operator':'eq','values':['a','b'],'basis':'question'}]
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(response,planning_schema(payload))


def test_call1_excerpt_contract_has_no_exact_source_requirement():
    snippets=[{'id':'r1','source':'rulebook','excerpt':'Có thể bỏ dấu Markdown hoặc diễn đạt lại ngắn.'}]
    jsonschema.validate(snippets,SELECTED_RULES)
