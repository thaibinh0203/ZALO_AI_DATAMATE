from decimal import Decimal
import pytest
from datamate_v2.common import PipelineError,dumps
from datamate_v2.data_pipeline.normalization.table_normalizer import normalize_table
from datamate_v2.data_pipeline.normalization.validators import value_schema
from datamate_v2.inference.structured_output import parse_output
from datamate_v2.tests.test_normalization_optimized import raw_rows,column,NoCalls


def test_no_row_indices_or_occurrence_lists_in_model_payload():
    class Client:
        def call(self,task,payload,schema,**kwargs):
            assert 'row' not in payload['items'][0]
            assert 'occurrence_count' not in payload['items'][0]
            assert 'example_source_rows' not in dumps(payload)
            assert 'neighbors' not in payload
            return {'proposals':[]}
    rows,_,logs,_,_=normalize_table(raw_rows([['1,250']]*1000),[column()],Client(),{}, {})
    assert len(rows)==1000 and logs[0]['occurrence_count']==1000


def test_batch_truncation_splits_once_and_recovers():
    quote='Dùng dấu phẩy thập phân.'
    class Client:
        def __init__(self): self.sizes=[]
        def call(self,task,payload,schema,**kwargs):
            self.sizes.append(len(payload['items']))
            if len(payload['items'])>2: raise PipelineError('Model output hit max_new_tokens')
            return {'proposals':[{'item_id':i['item_id'],'clean_value':Decimal(i['raw'].replace(',','.')),'unit':'VND','source_quote':quote} for i in payload['items']]}
    client=Client()
    rows,_,_,_,stats=normalize_table(raw_rows([[f'{i},250'] for i in range(1,5)]),[column()],client,{'readme':quote},{'value_batch_size':4,'value_retry_depth':1})
    assert client.sizes==[4,2,2] and stats['slm_batch_errors']==1 and stats['slm_repair_batches']==2
    assert [r['amount'] for r in rows]==[Decimal('1.25'),Decimal('2.25'),Decimal('3.25'),Decimal('4.25')]


def test_persistent_truncation_keeps_unresolved_without_aborting_table():
    class Client:
        def __init__(self): self.n=0
        def call(self,*args,**kwargs): self.n+=1;raise PipelineError('Model output hit max_new_tokens')
    client=Client()
    rows,_,logs,_,stats=normalize_table(raw_rows([['1,250'],['2,250']]),[column()],client,{}, {'value_batch_size':2,'value_retry_depth':1})
    assert len(rows)==2 and all(r['amount'] is None and r['_quality'] for r in rows)
    assert client.n==3 and stats['slm_batch_errors']==3 and len(logs)==2


def test_extra_output_items_rejected_by_dynamic_schema():
    schema=value_schema(['v_a'])
    result={'proposals':[{'item_id':'v_a','clean_value':1,'unit':'VND','source_quote':''}]*2}
    with pytest.raises(PipelineError): parse_output(dumps(result),schema)


def test_unknown_id_rejects_whole_response_before_any_application():
    quote='Dùng dấu phẩy thập phân.'
    class Client:
        def call(self,task,payload,schema,**kwargs):
            return {'proposals':[{'item_id':payload['items'][0]['item_id'],'clean_value':Decimal('1.25'),'unit':'VND','source_quote':quote},
                                 {'item_id':'not_requested','clean_value':2,'unit':'VND','source_quote':quote}]}
    rows,_,_,_,stats=normalize_table(raw_rows([['1,250'],['2,250']]),[column()],Client(),{'readme':quote},{'value_retry_depth':0})
    assert all(r['amount'] is None for r in rows) and stats['slm_applied_cells']==0


def test_quantity_unit_verified_from_source_column():
    cols=[column('quantity',0,'quantity'),column('unit',1,'unit','TEXT')]
    rows,_,_,_,stats=normalize_table(raw_rows([['1 cuộn','cuộn'],['2 cây','cây'],['10 m','m'],['2 m²','m2']]),cols,NoCalls(),{}, {})
    assert [r['quantity'] for r in rows]==[1,2,10,2] and stats['slm_items']==0
    assert rows[0]['_raw']==['1 cuộn','cuộn']


def test_quantity_mismatched_unit_not_repaired_by_memo():
    class Client:
        def call(self,*args,**kwargs): return {'proposals':[]}
    cols=[column('quantity',0,'quantity'),column('unit',1,'unit','TEXT')]
    rows,_,_,_,_=normalize_table(raw_rows([['1 cuộn','cuộn'],['1 cuộn','cây']]),cols,Client(),{}, {})
    assert rows[0]['quantity']==1 and rows[1]['quantity'] is None


def test_no_implicit_base_quantity_conversion():
    class Client:
        def call(self,*args,**kwargs): return {'proposals':[]}
    cols=[column('base_quantity',0,'base_quantity'),column('unit',1,'unit','TEXT')]
    rows,_,_,_,_=normalize_table(raw_rows([['2 thùng','thùng']]),cols,Client(),{}, {})
    assert rows[0]['base_quantity'] is None
