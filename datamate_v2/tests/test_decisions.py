from copy import deepcopy
from datetime import date
import ast,json
from pathlib import Path
import pytest
from datamate_v2.common import ROOT,dumps
from datamate_v2.data_pipeline.value_catalog import build_value_catalog,budget_values
from datamate_v2.qa_pipeline.decisions import resolve_decision_times,assemble_sources,DECISION_SCHEMA
from datamate_v2.inference.structured_output import parse_output

CALENDAR={'calendar_rule':'MONDAY_TO_SUNDAY','timezone':'Asia/Ho_Chi_Minh'}


def md():
    return {'business_id':'biz_test','fingerprint':'test','calendar_csv':'','tables':[
        {'sql_table':t,'source_file':'monthly.xlsx','columns':[
            {'sql_column':'date','type':'DATE','meaning':'date'},
            {'sql_column':'status','type':'TEXT','meaning':'status'},
            {'sql_column':'order_id','type':'TEXT','meaning':'order_id'},
            {'sql_column':'amount','type':'NUMERIC','meaning':'line_total'}],
         'stats':[{'sql_column':'date','min':lo,'max':hi}]} for t,lo,hi in
        [('feb','2026-02-01','2026-02-28'),('mar','2026-03-01','2026-03-31'),('aug','2026-08-01','2026-08-31')]]}


def test_unique_values_are_complete_shared_and_exclude_ids():
    metadata=md();rows={t:[{'status':'Đã giao','order_id':'ORD1'},{'status':'Trả hàng','order_id':'ORD2'}] for t in ['feb','mar','aug']}
    catalog=build_value_catalog(metadata,rows)
    assert len(catalog['groups'])==1
    assert catalog['groups'][0]['tables']==['feb','mar','aug']
    assert catalog['groups'][0]['values']==['Trả hàng','Đã giao']
    rows['aug']=[{'status':'Xong','order_id':'ORD3'}]
    assert len(build_value_catalog(metadata,rows)['groups'])==2


def test_threshold_is_exclusive_and_dictionaries_are_not_clipped():
    metadata=md();rows={t:[{'status':f'Label{i}'} for i in range(50)] for t in ['feb','mar','aug']}
    assert build_value_catalog(metadata,rows)['groups']==[]
    rows={t:[{'status':'Very long '+('x'*3000)}] for t in rows}
    catalog=build_value_catalog(metadata,rows)
    selected,usage=budget_values(catalog,'status',len,budget=500)
    assert selected['groups']==[] and usage['tokens']<=500


def test_previous_week_concrete_dates_override_wrong_model_prose():
    q={'question':'sp ban nhieu nhat tuan roi','as_of_date':'2026-03-04'}
    result=resolve_decision_times(q,{'time_requests':[]},md(),CALENDAR)
    assert result['windows'][0]['start_date']=='2026-02-23'
    assert result['windows'][0]['end_date_exclusive']=='2026-03-02'
    assembly=assemble_sources(md(),result)
    assert assembly['sources'][0]['physical_tables']==['feb','mar']
    assert 'UNION ALL' in assembly['sources'][0]['select_sql']
    assert 'SUM(' not in assembly['sources'][0]['select_sql']


def test_multiple_periods_select_both_months_before_aggregation():
    q={'question':'So sánh tháng 2 và tháng 8 năm 2026','as_of_date':'2026-09-10'}
    a={'time_requests':[{'name':n,'interpretation':'absolute_month','parameters':{'year':2026,'month':m},'status':'proposed'} for n,m in [('first',2),('second',8)]]}
    result=resolve_decision_times(q,a,md(),CALENDAR)
    assert len(result['windows'])==2 and not result['unresolved']
    assert assemble_sources(md(),result)['sources'][0]['physical_tables']==['feb','aug']
    assert assemble_sources(md(),result,True)['sources'][0]['physical_tables']==['feb','mar','aug']


def test_unrelated_files_are_not_joined_even_with_identical_columns():
    metadata=md();metadata['tables'][1]['source_file']='payments.xlsx'
    assembly=assemble_sources(metadata,{'windows':[]})
    assert len(assembly['sources'])==2
    assert all('JOIN' not in s['select_sql'] for s in assembly['sources'])


def test_rule_paraphrases_only_need_source_type_not_literal_quote():
    a={'goal':'Số đơn','entity_mentions':[],
        'rules_to_apply':[{'source':'rulebook','action':'Đếm mã đơn duy nhất, bỏ hủy/trả theo định nghĩa nguồn.'}],
        'aggregation':'Đếm đơn duy nhất theo khách; xếp giảm dần.',
        'time_requests':[],'data_requirements':['Các dòng bán hàng'],'requires_full_history':False}
    assert parse_output(dumps(a),DECISION_SCHEMA)==a


def test_invalid_time_request_is_advisory_not_a_business_guard():
    q={'question':'Một kỳ đặc biệt theo tài liệu','as_of_date':'2026-09-10'}
    result=resolve_decision_times(q,{'time_requests':[{'name':'x','interpretation':'invented','parameters':{}}]},md(),CALENDAR)
    assert result['windows']==[] and result['unresolved']


def test_kaggle_notebook_cells_have_valid_python():
    path=ROOT/'notebooks/DataMate_Decisions_Kaggle.ipynb'
    if not path.exists():pytest.skip('Local benchmark notebook is intentionally not published')
    nb=json.loads(path.read_text())
    for cell in nb['cells']:
        if cell['cell_type']=='code':ast.parse(''.join(cell['source']))
