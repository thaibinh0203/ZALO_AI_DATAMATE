import copy
import pytest
from datamate_v2.common import PipelineError
from datamate_v2.data_pipeline.schema_generation.schema_builder import verified_facts,build_schema
from datamate_v2.data_pipeline.schema_generation.schema_validator import validate_description,required_topics
from datamate_v2.tests.factories import schema_fixture


def table_fixture(metadata):
    table=copy.deepcopy(metadata['tables'][0])
    table.update(row_count=10,relationships=[],observed_domains={'status':{'values':[{'value':'Xong','count':7},{'value':'Đã hủy','count':1},{'value':'Đổi trả','count':1},{'value':'Đang giao','count':1}],'distinct_count':4,'truncated':False}})
    table['stats'][0]['distinct_count']=3
    return table


def test_facts_expose_grain_statuses_and_coverage(metadata):
    table=table_fixture(metadata)
    facts=verified_facts(table,metadata)
    assert facts['multiple_rows_per_order'] and facts['distinct_order_ids']==3
    assert len(facts['observed_domains']['status']['values'])==4
    assert facts['date_coverage'][0]['min']=='2026-01-01'


def test_all_usage_topics_and_real_units_required(metadata):
    table=table_fixture(metadata);table['verified_facts']=verified_facts(table,metadata)
    desc=schema_fixture(table)
    assert validate_description(desc,table,metadata)
    desc['usage_rules']=desc['usage_rules'][:-1]
    with pytest.raises(PipelineError,match='usage-rule topics'): validate_description(desc,table,metadata)


def test_text_column_unit_is_null(metadata):
    table=table_fixture(metadata);table['verified_facts']=verified_facts(table,metadata)
    desc=schema_fixture(table);desc['columns'][0]['unit']='Mã đơn'
    with pytest.raises(PipelineError,match='physical measurement unit'): validate_description(desc,table,metadata)


def test_grain_cannot_claim_one_order_per_row(metadata):
    table=table_fixture(metadata);table['verified_facts']=verified_facts(table,metadata)
    desc=schema_fixture(table);desc['grain']='Mỗi dòng tương ứng một đơn hàng.'
    with pytest.raises(PipelineError,match='Grain contradicts'): validate_description(desc,table,metadata)


def test_source_reference_allows_paraphrased_insights(metadata):
    table=table_fixture(metadata);table['verified_facts']=verified_facts(table,metadata)
    desc=schema_fixture(table)
    desc['usage_rules'][0].update(status='confirmed',explanation='Diễn giải ngữ nghĩa mà không chép lại Markdown của tài liệu.',evidence=[{'source':'readme'}])
    assert validate_description(desc,table,metadata)


def test_missing_source_cannot_support_confirmed_rule(metadata):
    table=table_fixture(metadata);table['verified_facts']=verified_facts(table,metadata)
    desc=schema_fixture(table)
    desc['usage_rules'][0].update(status='confirmed',evidence=[{'source':'readme'}])
    with pytest.raises(PipelineError,match='unavailable source'): validate_description(desc,table,{**metadata,'readme':''})


def test_usage_metadata_evidence_must_exist(metadata):
    table=table_fixture(metadata);table['verified_facts']=verified_facts(table,metadata)
    desc=schema_fixture(table);desc['usage_rules'][0].update(status='confirmed',evidence=[{'source':'metadata','field':'invented_foreign_key'}])
    with pytest.raises(PipelineError,match='Unknown metadata'): validate_description(desc,table,metadata)


def test_schema_generation_has_one_bounded_repair(metadata):
    table=table_fixture(metadata)
    class Client:
        def __init__(self): self.calls=[]
        def call(self,task,payload,schema,**kwargs):
            self.calls.append(kwargs)
            response=schema_fixture(payload['table'])
            if len(self.calls)==1: response['columns']=[]
            return response
    client=Client();result=build_schema(table,client,metadata)
    assert len(client.calls)==2 and client.calls[1]['repair'] is True
    assert result['description']['usage_rules'] and result['verified_facts']['row_count']==10
    assert result['description_status']=='available'


@pytest.mark.parametrize('failure',['length','semantic'])
def test_description_failure_preserves_verified_metadata(metadata,failure):
    table=table_fixture(metadata)
    class Client:
        calls=0
        def call(self,task,payload,schema,**kwargs):
            self.calls+=1
            if failure=='length': raise PipelineError('Model output hit max_new_tokens')
            response=schema_fixture(payload['table']);response['grain']='Mỗi dòng tương ứng một đơn hàng.'
            return response
    client=Client();result=build_schema(table,client,metadata)
    assert client.calls==2
    assert result['description'] is None and result['description_status']=='unavailable'
    assert result['description_error']['error_type']=='PipelineError'
    assert result['verified_facts']['row_count']==10 and result['columns']==table['columns']
