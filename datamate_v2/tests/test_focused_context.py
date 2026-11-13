from copy import deepcopy
import pytest
from datamate_v2.common import dumps,PipelineError
from datamate_v2.qa_pipeline.focused_context import call2_context,scope_metadata,retrieve_sources
from datamate_v2.qa_pipeline.pipeline import QAPipeline
from datamate_v2.tests.test_direct_sql import Client

CALENDAR={'calendar_rule':'MONDAY_TO_SUNDAY','timezone':'Asia/Ho_Chi_Minh'}


@pytest.fixture
def metadata():
    result={'business_id':'biz_test','fingerprint':'test','schema':'b_test','calendar_csv':'',
        'readme':'## Dữ liệu\nMỗi dòng là một dòng hàng của một đơn.\n## Quy đổi\n- Nước A: 1 thùng = 24 chai.\n',
        'rulebook':'| Quy tắc | Cách tính |\n| Đơn tính vào doanh thu | Bỏ hủy/trả; đang giao vẫn tính. |\n| Số đơn | Chỉ đơn hợp lệ, đếm theo đơn. |\n| Xác định khách | Khách lẻ không xếp hạng. |\n| Bán chạy | Số lượng đơn vị cơ bản. |\n',
        'tables':[],'relationships':[],'value_catalog':{'groups':[]}}
    for name,year,values in [('sales_2025',2025,['đã xong','hủy']),('sales_2026',2026,['Đã giao','Trả hàng'])]:
        result['tables'].append({'sql_table':name,'source_file':'sales.xlsx','columns':[
            {'sql_column':'date','type':'DATE','meaning':'date'},
            {'sql_column':'amount','type':'NUMERIC','meaning':'line_total'},
            {'sql_column':'status','type':'TEXT','meaning':'status'}],
            'sample_rows':[{'status':values[0]}],
            'stats':[{'sql_column':'date','min':f'{year}-02-01','max':f'{year}-02-28','null_count':0}]})
        result['value_catalog']['groups'].append({'id':name+'_values','column':'status','values':values,
            'tables':[name],'nunique':2,'complete':True})
    result['tables'].append({'sql_table':'products','columns':[{'sql_column':'category','type':'TEXT'}]})
    return result


def period():
    return {'windows':[{'name':'feb','start_date':'2026-02-01','end_date_exclusive':'2026-03-01'}],'unresolved':[]}


def test_scope_removes_other_month_values_samples_and_raw_model_table_names(metadata):
    q={'question':'Doanh thu tháng 2/2026','as_of_date':'2026-09-10'}
    a={'metric':'doanh thu','requested_units':[], 'data_requirements':['sales_2025'],
        'time_requests':[{'name':'wrong'}],'requires_full_history':False}
    scoped,c,decision,log=call2_context(metadata,q,a,period(),CALENDAR,len,2500,3500)
    assert {t['sql_table'] for t in c['queryable_tables']}=={'sales_2026','products'}
    assert 'sales_2025' not in dumps(c) and 'đã xong' not in dumps(c)
    assert 'Đã giao' in dumps(c)
    assert 'data_requirements' not in decision and 'time_requests' not in decision
    assert log['routing']['excluded'][0]['table']=='sales_2025'
    assert c['queryable_tables'][0]['value_dictionary_refs']['status']=='sales_2026_values'


def test_history_and_unresolved_periods_preserve_sources(metadata):
    scoped,_=scope_metadata(metadata,period(),True)
    assert len(scoped['tables'])==3
    unresolved={**period(),'unresolved':[{'name':'special'}]}
    assert len(scope_metadata(metadata,unresolved,False)[0]['tables'])==3


def test_multi_period_comparison_keeps_both_fact_months(metadata):
    windows=period();windows['windows'].append({'name':'last_year','start_date':'2025-02-01','end_date_exclusive':'2025-03-01'})
    assert len(scope_metadata(metadata,windows,False)[0]['tables'])==3


def test_retrieval_keeps_business_exceptions_and_product_conversion(metadata):
    fragments,log=retrieve_sources(metadata,{'question':'Thùng nước A bán bao nhiêu tiền?'},
        {'metric':'doanh thu','requested_units':['thùng']},len,3500)
    text=dumps(fragments)
    assert 'Bỏ hủy/trả; đang giao vẫn tính' in text
    assert '1 thùng = 24 chai' in text
    assert 'revenue' in log['topics'] and 'units' in log['topics']
    assert {f['source'] for f in fragments}=={'rulebook','readme'}


def test_delivery_request_does_not_promote_customer_order_eligibility(metadata):
    metadata['rulebook']+='| Giao nhiều đợt | Mỗi cặp mã đơn/lần giao tính một đợt, kể cả đơn sau đó trả. |\n'
    fragments,log=retrieve_sources(metadata,{'question':'tháng này chở giao bao nhiêu đợt hàng cho khách'},
        {'metric':'số đợt giao'},len,3500)
    assert 'delivery' in log['topics'] and 'customers' not in log['topics']
    assert 'Mỗi cặp mã đơn/lần giao' in dumps(fragments)


@pytest.mark.parametrize('outside',[False,True])
def test_pipeline_call1_full_catalog_call2_scoped_and_scoped_sql_allowlist(metadata,outside):
    class Model(Client):
        def call(self,task,payload,schema):
            self.calls.append({'task':task,'cache_hit':False});self.payloads.append(deepcopy(payload))
            if task=='qa_decisions':
                return {'goal':'Doanh thu','metric':'doanh thu','entity_mentions':[],
                    'rules_to_apply':[{'source':'rulebook','action':'Bỏ hủy và trả.'}],
                    'aggregation':'Tổng tiền hợp lệ.','time_requests':[],
                    'data_requirements':['sales_2025'],'requires_full_history':False}
            return {'status':'answerable','calculation_plan':['Tổng tiền hợp lệ.'],
                'sql':f"SELECT SUM(amount) FROM {'sales_2025' if outside else 'sales_2026'} WHERE status='Đã giao'",
                'reason':'Tính theo nguồn.','answer_type':'number','unit':'VND','round_digits':None,'list_mode':'row'}
    class Tool:
        def __init__(self,md,db,guard,max_queries):self.trace=[]
        def text_domains(self,columns):
            return [{'table':g['tables'][0],'column':g['column'],'values':g['values'],'complete':True} for g in metadata['value_catalog']['groups']]
        def execute_readonly_sql(self,sql,**kwargs):
            self.trace.append({'sql':sql});return {'columns':['sum'],'rows':[[100]]}
    client=Model()
    q={'id':'q','business_id':'biz_test','question':'Doanh thu tháng 2/2026','as_of_date':'2026-09-10'}
    final,log=QAPipeline(client,{}, {'mode':'direct_sql','decision_planning':True,'scoped_context':True,
        'max_repairs':0,'calendar':CALENDAR},Tool).answer(q,metadata)
    assert len(client.payloads[0]['business_context']['queryable_tables'])==3
    call2=client.payloads[1]
    assert len(call2['business_context']['queryable_tables'])==2
    assert call2['calendar_hints'] is None
    assert 'sales_2025' not in dumps(call2)
    assert client.calls[-1]['task']=='qa_scoped_sql'
    assert call2['resolved_time']['windows'][0]['start_date']=='2026-02-01'
    if outside:
        assert final['type']=='abstain' and 'outside current business' in log['error']
    else:
        assert final['answer']==100,log
