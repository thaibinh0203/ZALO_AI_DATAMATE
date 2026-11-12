import copy
import pytest
from datamate_v2.common import ROOT,PipelineError,dumps,read_json,read_jsonl
from datamate_v2.data_pipeline.normalization.column_normalizer import normalize_columns,column_schema
from datamate_v2.inference.structured_output import parse_output
from datamate_v2.contracts import ENTITY_SCHEMA,normalize_entities
from datamate_v2.qa_pipeline.entities import resolve_entities


def proposal(index,meaning=None,candidates=None,quote=''):
    return {'index':index,'meaning':meaning,'candidate_meanings':candidates or [],'source_quote':quote,'evidence':[]}


def rows(values): return [{'values':v,'index':i+1,'flags':[]} for i,v in enumerate(values)]


def test_column_schema_only_accepts_active_indices():
    schema=column_schema([0,2])
    assert parse_output(dumps({'mappings':[proposal(2)]}),schema)
    with pytest.raises(PipelineError): parse_output(dumps({'mappings':[proposal(1)]}),schema)


def test_historical_all_column_response_cannot_overwrite_verified_columns(aliases):
    class Client:
        def __init__(self): self.calls=0
        def call(self,task,payload,schema,**kwargs):
            self.calls+=1
            assert payload['allowed_indices']==[2]
            assert schema['properties']['mappings']['maxItems']==1
            return {'mappings':[proposal(0,'product_id'),proposal(1,'line_total'),proposal(2)]}
    client=Client()
    columns=normalize_columns(['Mã hàng','Tên hàng','Auxiliary'],rows([['P1','Alpha','x']]),aliases,client,{'readme':'','rulebook':''})
    assert columns[0]['meaning']=='product_id' and columns[1]['meaning']=='product_name'
    assert columns[0]['semantic_status']==columns[1]['semantic_status']=='verified'
    assert columns[2]['semantic_status']=='unresolved' and client.calls==1
    assert 'redundant' in columns[0]['mapping_warnings'][0]
    assert 'conflicting' in columns[1]['mapping_warnings'][0]


def test_duplicate_unknown_mapping_repaired_before_any_application(aliases):
    class Client:
        def __init__(self): self.calls=[]
        def call(self,task,payload,schema,**kwargs):
            self.calls.append(kwargs)
            if len(self.calls)==1:
                return {'mappings':[proposal(1,'quantity',['quantity'],'Mystery: số lượng'),proposal(1,'line_total',['line_total'],'Mystery: số lượng')]}
            assert kwargs['repair'] is True
            return {'mappings':[proposal(1)]}
    client=Client()
    columns=normalize_columns(['Mã hàng','Mystery'],rows([['P1','1']]),aliases,client,{'readme':'','rulebook':''})
    assert len(client.calls)==2 and columns[1]['meaning'] is None
    assert columns[1]['type']=='TEXT' and columns[1]['semantic_status']=='unresolved'


def test_failed_auxiliary_mapping_calls_do_not_abort_table(aliases):
    class Client:
        calls=0
        def call(self,*args,**kwargs):
            self.calls+=1;raise PipelineError('Model output hit max_new_tokens')
    client=Client()
    columns=normalize_columns(['Mã hàng','Tên hàng','Mystery'],rows([['P1','Alpha','?']]),aliases,client,{'readme':'','rulebook':''})
    assert client.calls==2
    assert [c['semantic_status'] for c in columns]==['verified','verified','unresolved']
    assert columns[2]['mapping_warnings']


def test_exact_failed_catalog_response_is_tolerated_at_boundary(aliases):
    p=ROOT/'runtime/two_biz50/qwen3_pipeline_v3_run1/logs/ingestion.json'
    if not p.exists(): pytest.skip('Historical log unavailable')
    from datamate_v2.data_pipeline.parsers.excel_parser import parse_excel
    from datamate_v2.data_pipeline.adapters.table_extractor import extract_tables
    from datamate_v2.common import loads
    calls=[c for c in read_json(p)['calls'] if c['task']=='column_normalization' and c.get('text')]
    response=loads(calls[-1]['text'])
    if len(response.get('mappings',[]))!=16: pytest.skip('Historical fixture changed')
    source=parse_excel(ROOT.parent/'docs/train/data/biz_118/Danh sách hàng hóa.xlsx')[0]
    block=extract_tables(source,aliases)[0]
    class Client:
        def call(self,*args,**kwargs): return copy.deepcopy(response)
    columns=normalize_columns(block['header']['values'],block['data_rows'],aliases,Client(),{
        'readme':(ROOT.parent/'docs/train/data/biz_118/README.md').read_text(),'rulebook':''})
    assert [columns[i]['meaning'] for i in [4,7,9,10,13]]==['product_id','product_name','category','unit','note']
    assert all(columns[i]['semantic_status']=='verified' for i in [4,7,9,10,13])


def test_entity_contract_has_optional_array_fields():
    assert parse_output('{}',ENTITY_SCHEMA)=={}
    with pytest.raises(PipelineError): parse_output('{"products":"Thuoc la Vinataba"}',ENTITY_SCHEMA)
    assert normalize_entities({'products':'Thuoc la Vinataba'})=={'products':['Thuoc la Vinataba']}
    for wrong in [None,{'products':None},{'products':[1]},{'products':{'name':'x'}}]:
        with pytest.raises(PipelineError): normalize_entities(wrong)


def named_metadata(metadata):
    metadata['tables'][0]['columns'][0]['meaning']='product_name'
    return metadata


def test_string_entity_is_one_lookup_not_character_lookups(metadata):
    class Tool:
        def entity_values(self,*args):return {'values':['Thuốc lá Vinataba'],'complete':True}
    result=resolve_entities({'products':'Thuoc la Vinataba'},named_metadata(metadata),Tool())
    assert len(result)==1 and result[0]['input']=='Thuoc la Vinataba'
    assert result[0]['canonical_value']=='Thuốc lá Vinataba'


def test_bad_entity_phrase_recovers_unique_whole_name_from_original(metadata):
    class Tool:
        def entity_values(self,*args):return {'values':['Nếp cái hoa vàng','Coca Cola 330ml'],'complete':True}
    result=resolve_entities({'products':'máy kg nep cai hoa vang'},named_metadata(metadata),Tool(),
        original_question='check gium thang 2 cua hang ban dc may kg nep cai hoa vang')
    assert result[0]['canonical_value']=='Nếp cái hoa vàng' and result[0]['recovered_from']


def test_recovery_rejects_ambiguous_names_and_partial_words(metadata):
    class Tool:
        def entity_values(self,*args):return {'values':['Cola','Pepsi'],'complete':True}
    md=named_metadata(metadata)
    with pytest.raises(PipelineError,match='Ambiguous original-question'):
        resolve_entities({'products':'wrong entity'},md,Tool(),original_question='Cola và Pepsi')
    assert resolve_entities({'products':'wrong entity'},md,Tool(),original_question='Colabook')[0]['status']=='not_found'
