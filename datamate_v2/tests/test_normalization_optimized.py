from decimal import Decimal
import pytest
from datamate_v2.data_pipeline.normalization.column_profiler import profile_column,sample_positions
from datamate_v2.data_pipeline.normalization.table_normalizer import normalize_table
from datamate_v2.data_pipeline.normalization.column_normalizer import normalize_columns
from datamate_v2.data_pipeline.normalization.value_normalizer import parse_value
from datamate_v2.common import PipelineError


def raw_rows(values): return [{'index':i+1,'values':row,'flags':[]} for i,row in enumerate(values)]

def column(name='amount',index=0,meaning='line_total',kind='NUMERIC'):
    return {'index':index,'sql_column':name,'original_column':name,'meaning':meaning,'type':kind,'semantic_status':'verified'}

class NoCalls:
    def call(self,*args,**kwargs): raise AssertionError('SLM must not be called for known rules')

class RejectingClient:
    def __init__(self): self.calls=[]
    def call(self,task,payload,*args,**kwargs):
        self.calls.append(payload)
        return {'proposals':[{'item_id':item['item_id'],'clean_value':999,'unit':'VND','source_quote':''} for item in payload['items']]}


def test_sampling_includes_head_middle_tail():
    selected=sample_positions(100,5)
    assert selected[0]==0 and selected[-1]==99 and 50 in selected and len(selected)==5

@pytest.mark.parametrize('values,locale',[
 (['1.000.000','2.000.000','1.250'],'vi'),
 (['1,000,000','2,000,000','1,250'],'en')])
def test_locale_confirmed_then_applied_to_full_column(values,locale):
    data=raw_rows([[v] for v in values]);profile=profile_column(data,column(),{})
    assert profile['status']=='validated_from_patterns' and profile['locale']==locale
    rows,logs,slm,profiles,stats=normalize_table(data,[column()],NoCalls(),{}, {})
    assert rows[-1]['amount']==1250 and stats['slm_items']==0
    assert [r['_raw'][0] for r in rows]==values


def test_one_distinct_anchor_is_insufficient():
    data=raw_rows([['1,000,000']]*100+[['1,250']])
    assert profile_column(data,column(),{})['locale'] is None


def test_conflict_outside_sample_prevents_global_rule():
    data=raw_rows([['1,000,000'],['2,000,000']]+[['1,250']]*100)
    data[37]['values']=['1.000.000']
    assert 37 not in sample_positions(len(data),3)
    p=profile_column(data,column(),{'profile_sample_size':3})
    assert p['locale'] is None and p['status']=='mixed'


def test_decimal_contradiction_prevents_grouping_inference():
    p=profile_column(raw_rows([['1,000,000'],['2,000,000'],['1,5'],['1,250']]),column(),{})
    assert p['status']=='mixed' and p['locale'] is None


def test_duplicate_exceptions_sent_once_and_stay_unresolved_when_rejected():
    client=RejectingClient();data=raw_rows([['1,250']]*1000)
    rows,logs,slm,profiles,stats=normalize_table(data,[column()],client,{}, {})
    assert len(client.calls)==1 and len(client.calls[0]['items'])==1
    assert 'occurrence_count' not in client.calls[0]['items'][0]
    assert 'row' not in client.calls[0]['items'][0] and 'example_source_rows' not in client.calls[0]['items'][0]
    assert slm[0]['occurrence_count']==1000
    assert stats['unique_rule_parses']==1 and stats['rule_cache_hits']==999
    assert stats['slm_items']==1 and stats['deduplicated_slm_cells']==999
    assert all(r['amount'] is None and r['_quality'] for r in rows)
    assert len(logs)==1000 and not slm[0]['accepted']


def test_validated_proposal_applies_to_all_exact_occurrences():
    quote='Dùng dấu phẩy thập phân.'
    class Client:
        def call(self,task,payload,*args,**kwargs):
            item=payload['items'][0]
            return {'proposals':[{'item_id':item['item_id'],'clean_value':Decimal('1.250'),'unit':'VND','source_quote':quote}]}
    rows,logs,slm,profiles,stats=normalize_table(raw_rows([['1,250']]*20),[column()],Client(),{'readme':quote,'rulebook':''}, {})
    assert all(r['amount']==Decimal('1.250') and not r['_quality'] for r in rows)
    assert all(log['status']=='verified' and log['clean']==Decimal('1.250') for log in logs)
    assert stats['slm_applied_cells']==20 and stats['slm_accepted_values']==1
    assert slm[0]['source_rows']==list(range(1,21))


def test_no_merge_across_columns():
    client=RejectingClient()
    columns=[column('price',0),column('qty',1,'quantity')]
    _,_,_,_,stats=normalize_table(raw_rows([['1,250','1,250']]*30),columns,client,{}, {})
    assert stats['slm_items']==2 and len(client.calls)==1
    assert {item['column'] for item in client.calls[0]['items']}=={'price','qty'}


def test_text_id_never_receives_numeric_rule():
    data=raw_rows([['1.250','1.250'],['1.000.000','001'],['2.000.000','002']])
    rows,_,_,_,_=normalize_table(data,[column(),column('id',1,'order_id','TEXT')],NoCalls(),{}, {})
    assert rows[0]['amount']==1250 and rows[0]['id']=='1.250' and rows[1]['id']=='001'


def test_missing_formula_cache_not_sent_to_slm():
    data=raw_rows([[None]]);data[0]['flags']=[{'column':0,'reason':'formula_without_cache'}]
    rows,_,_,_,stats=normalize_table(data,[column()],NoCalls(),{}, {})
    assert rows[0]['amount'] is None and rows[0]['_quality'][0]['status']=='unresolved'
    assert stats['source_error_cells_not_sent_to_slm']==1 and stats['slm_items']==0

@pytest.mark.parametrize('value,expected',[('1 thg 6 2025','2025-06-01'),('29 tháng 2 2024','2024-02-29'),('01.06.2025','2025-06-01')])
def test_date_regex(value,expected):
    assert parse_value(value,column('date',0,'date','DATE'))==(expected,'verified')


def test_bad_vi_calendar_date_stays_unresolved():
    assert parse_value('29 thg 2 2025',column('date',0,'date','DATE'))==(None,'unresolved')


def test_readme_definitions_do_not_need_model(aliases):
    readme='- **Nội dung**: tên hàng\n- **Ngày tháng**: ngày bán\n- **Đối tác**: tên khách (Khách lẻ nếu không lưu)'
    cols=normalize_columns(['Nội dung','Ngày tháng','Đối tác'],raw_rows([['Sản phẩm','1 thg 6 2025','Khách']]),aliases,NoCalls(),{'readme':readme,'rulebook':''})
    assert [c['meaning'] for c in cols]==['product_name','order_date','customer_name']
    assert all(c['semantic_status']=='verified' and c.get('source_quote') for c in cols)


def test_bad_slm_proposal_index_keeps_cells_unresolved():
    class Client:
        def call(self,*args,**kwargs):
            return {'proposals':[{'item_id':'not_requested','clean_value':1,'unit':'VND','source_quote':''}]}
    rows,_,logs,_,stats=normalize_table(raw_rows([['1,250']]),[column()],Client(),{}, {})
    assert rows[0]['amount'] is None and rows[0]['_quality']
    assert stats['slm_batch_errors']==1 and logs[0]['accepted'] is False


def test_numeric_columns_keep_separate_locale_caches():
    data=raw_rows([['1.250','1.250'],['1.000.000','1,000,000'],['2.000.000','2,000,000']])
    cols=[column('vi_amount',0),column('en_amount',1)]
    rows,_,_,_,_=normalize_table(data,cols,NoCalls(),{}, {})
    assert rows[0]['vi_amount']==1250 and rows[0]['en_amount']==Decimal('1.250')


def test_many_shapes_fill_one_batch_instead_of_one_call_per_shape():
    client=RejectingClient()
    data=raw_rows([[f'bad {letter}'] for letter in 'abcdefghijklmnopqrstuvwx']*2)
    _,_,_,_,stats=normalize_table(data,[column()],client,{}, {'value_batch_size':32})
    assert len(client.calls)==1 and stats['slm_items']==24
    assert stats['slm_batches']==1 and stats['deduplicated_slm_cells']==24


def test_source_error_not_overwritten_when_identical_value_is_repaired():
    quote='Dùng dấu phẩy thập phân.'
    class Client:
        def call(self,task,payload,*args,**kwargs):
            item=payload['items'][0]
            return {'proposals':[{'item_id':item['item_id'],'clean_value':Decimal('1.25'),'unit':'VND','source_quote':quote}]}
    data=raw_rows([['1,250'],['1,250']]);data[1]['flags']=[{'column':0,'reason':'excel_error'}]
    rows,_,_,_,stats=normalize_table(data,[column()],Client(),{'readme':quote}, {})
    assert rows[0]['amount']==Decimal('1.25') and rows[1]['amount'] is None
    assert rows[1]['_quality'][0]['status']=='unresolved' and stats['slm_applied_cells']==1

def test_conflicting_readme_definitions_cannot_be_promoted_by_slm(aliases):
    readme='- **Tiền TH**: thành tiền\n- **Tiền TH**: đơn giá'
    class Client:
        def call(self,*args,**kwargs):
            return {'mappings':[{'index':0,'meaning':'line_total','candidate_meanings':['thành tiền','đơn giá'],'evidence':['conflicting definitions'],'source_quote':'Tiền TH**: thành tiền'}]}
    cols=normalize_columns(['Tiền TH'],raw_rows([[100]]),aliases,Client(),{'readme':readme,'rulebook':''})
    assert cols[0]['semantic_status']=='uncertain'
