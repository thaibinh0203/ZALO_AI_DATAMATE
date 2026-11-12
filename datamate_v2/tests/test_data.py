from decimal import Decimal
import sqlite3
import pytest
from datamate_v2.common import PipelineError
from datamate_v2.data_pipeline.parsers.csv_parser import parse_csv
from datamate_v2.data_pipeline.parsers.sqlite_parser import parse_sqlite
from datamate_v2.data_pipeline.parsers.excel_parser import parse_excel
from datamate_v2.data_pipeline.adapters.header_detector import detect_header
from datamate_v2.data_pipeline.adapters.table_extractor import extract_tables
from datamate_v2.data_pipeline.normalization.value_normalizer import parse_value,numeric
from datamate_v2.data_pipeline.normalization.column_normalizer import normalize_columns
from datamate_v2.data_pipeline.anomaly_detection.summary_detector import detect_summary
from datamate_v2.data_pipeline.schema_generation.schema_validator import validate_description
from datamate_v2.data_pipeline.normalization.group_rules import apply_group_rules

def rows(values): return [{'index':i+1,'values':v,'flags':[]} for i,v in enumerate(values)]

@pytest.mark.parametrize('raw,expected',[('1tr500k',1500000),('1,5 triệu',1500000),('1.250.000',1250000),('1.250.000,50',Decimal('1250000.50'))])
def test_money(raw,expected): assert numeric(raw,money=True,locale='vi')==expected

@pytest.mark.parametrize('value',['1.250','1,250','abc','3.2.1'])
def test_ambiguous_number_not_guessed(value):
    with pytest.raises(ValueError): numeric(value)

def test_id_not_numeric(): assert parse_value('001250',{'type':'TEXT','meaning':'order_id'})==('001250','verified')
def test_missing_not_zero(): assert parse_value('',{'type':'NUMERIC','meaning':'line_total'})==(None,'missing')
def test_money_rule_not_applied_to_quantity(): assert parse_value('1tr500k',{'type':'NUMERIC','meaning':'quantity'})==(None,'unresolved')
def test_date_invalid(): assert parse_value('31/02/2026',{'type':'DATE','meaning':'date'})==(None,'unresolved')

def test_csv_keeps_raw(tmp_path):
    p=tmp_path/'a.csv';p.write_text('Mã đơn;Số lượng\n001;2\n',encoding='utf-8')
    source=parse_csv(p)[0]
    assert source['rows'][1]['values']==['001','2']

def test_bad_csv_quote(tmp_path):
    p=tmp_path/'bad.csv';p.write_text('id,name\n1,"not closed\n')
    with pytest.raises(Exception): parse_csv(p)

def test_header_below_title(aliases):
    source=rows([['BÁO CÁO BÁN HÀNG',None,None],['Mã đơn','Số lượng','Thành tiền'],['a',1,10],['b',2,20]])
    assert detect_header(source,aliases)['position']==1

def test_ambiguous_header_not_selected(aliases):
    assert detect_header(rows([['ghi chú'],['chưa rõ']]),aliases)['status']=='unresolved'

def test_two_tables(aliases):
    source={'name':'sheet','rows':rows([['Mã đơn','Số lượng'],['a',1],[None,None],['Mã hàng','Đơn giá'],['b',20]])}
    blocks=extract_tables(source,aliases)
    assert len(blocks)==2
    assert all(b['header'] is not None for b in blocks)

def test_sqlite_readonly_and_declared_header(tmp_path):
    path=tmp_path/'data.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE lines (id TEXT, amount NUMERIC)');db.execute("INSERT INTO lines VALUES ('001',12)")
    source=parse_sqlite(path)[0]
    assert source['declared_header'] and source['rows'][1]['values'][0]=='001'

def test_xlsx_formula_without_cache_preserved(tmp_path):
    from openpyxl import Workbook
    p=tmp_path/'data.xlsx';w=Workbook();s=w.active;s.append(['Mã đơn','Số lượng']);s.append(['001','=1+1']);w.save(p)
    row=parse_excel(p)[0]['rows'][1]
    assert row['values']==['001',None] and row['cells'][1]['formula']=='=1+1'
    assert row['flags'][0]['reason']=='formula_without_cache'

def test_summary_scan_entire_table():
    cols=[{'sql_column':'name','type':'TEXT'},{'sql_column':'amount','type':'NUMERIC'},{'sql_column':'extra','type':'TEXT'}]
    data=[{'name':'a','amount':Decimal(2),'extra':None,'_source_row':1},
          {'name':'b','amount':Decimal(3),'extra':None,'_source_row':2},
          {'name':'Tổng cộng','amount':Decimal(5),'extra':None,'_source_row':3}]
    data += [{'name':'Tổng công ty ABC','amount':Decimal(1),'extra':'customer','_source_row':i+4} for i in range(80)]
    detect_summary(data,cols)
    assert data[2]['_is_summary_row']
    assert not any(r['_is_summary_row'] for r in data[3:])

def test_unknown_mapping_preserved_and_no_money_conflation(aliases):
    class Client:
        def call(self,*args,**kwargs): return {'mappings':[{'index':3,'meaning':None,'candidate_meanings':['thành tiền','tiền thực thu'],'evidence':['ambiguous abbreviation'],'source_quote':''}]}
    result=normalize_columns(['Đơn giá','Thành tiền','Đã trả','Tiền TH'],rows([[1,2,3,4]]),aliases,Client(),{'readme':'','rulebook':''})
    assert [c['meaning'] for c in result[:3]]==['unit_price','line_total','amount_paid']
    assert result[3]['semantic_status']=='uncertain'

def test_schema_cannot_invent_missing_columns(metadata):
    d={'columns':[],'business_rules':[]}
    with pytest.raises(PipelineError): validate_description(d,metadata['tables'][0],metadata)

def test_group_rules_only_from_source():
    cols=[{'sql_column':'id','meaning':'order_id','semantic_status':'verified'},{'sql_column':'status','meaning':'status'}]
    data=[{'id':'a','status':'đang giao','_source_row':1,'_raw':['a','đang giao','x'],'_quality':[]},
          {'id':None,'status':None,'_source_row':2,'_raw':[None,None,'y'],'_quality':[{'column':'id','status':'missing'},{'column':'status','status':'missing'}]}]
    logs=apply_group_rules(data,cols,'Ô để trống ở các dòng sau của cùng một nhóm dòng')
    assert data[1]['id']=='a' and data[1]['status']=='đang giao' and logs

def test_adjacent_tables_with_different_headers(aliases):
    source={'name':'sheet','rows':rows([['Mã đơn','Số lượng'],['a',1],['b',2],['Mã hàng','Đơn giá'],['x',10],['y',20]])}
    blocks=extract_tables(source,aliases)
    assert len(blocks)==2
    assert [r['values'][0] for r in blocks[0]['data_rows']]==['a','b']
    assert [r['values'][0] for r in blocks[1]['data_rows']]==['x','y']

def test_single_column_known_header(aliases):
    assert detect_header(rows([['Mã đơn'],['001'],['002']]),aliases)['position']==0

def test_timezone_free_timestamp_requires_explicit_zone():
    column={'type':'TIMESTAMPTZ','meaning':'created_at'}
    assert parse_value('2026-03-03 07:30:00',column)[1]=='unresolved'
    assert parse_value('2026-03-03 07:30:00',column,timezone='Asia/Ho_Chi_Minh')==('2026-03-03T07:30:00+07:00','verified')

def test_slm_quote_must_define_proposed_meaning(aliases):
    class Client:
        def call(self,*args,**kwargs): return {'mappings':[{'index':0,'meaning':'unit_price','candidate_meanings':['đơn giá'],'evidence':['claimed'],'source_quote':'Tiền TH: thành tiền.'}]}
    result=normalize_columns(['Tiền TH'],rows([[100]]),aliases,Client(),{'readme':'Theo tài liệu: Tiền TH: thành tiền.','rulebook':''})
    assert result[0]['semantic_status']=='uncertain'

def test_slm_mapping_can_be_verified_from_literal_definition(aliases):
    class Client:
        def call(self,*args,**kwargs): return {'mappings':[{'index':0,'meaning':'line_total','candidate_meanings':['thành tiền'],'evidence':['literal definition'],'source_quote':'Tiền TH: thành tiền.'}]}
    result=normalize_columns(['Tiền TH'],rows([[100]]),aliases,Client(),{'readme':'Tiền TH: thành tiền.','rulebook':''})
    assert result[0]['semantic_status']=='verified' and result[0]['meaning']=='line_total'

def test_cross_column_formula_only_when_verified():
    from datamate_v2.data_pipeline.normalization.validators import cross_column_checks
    columns=[{'sql_column':x,'meaning':x} for x in ['quantity','unit_price','line_total']]
    row={'quantity':Decimal(2),'unit_price':Decimal(10),'line_total':Decimal(19)}
    assert cross_column_checks(row,columns,formula_verified=False)==[]
    assert cross_column_checks(row,columns,formula_verified=True)==['quantity_price_total_mismatch']

def test_grand_total_after_subtotals():
    columns=[{'sql_column':'label','type':'TEXT'},{'sql_column':'amount','type':'NUMERIC'}]
    data=[{'label':label,'amount':Decimal(amount),'_source_row':i+1} for i,(label,amount) in enumerate([('a',2),('b',3),('Subtotal',5),('c',4),('Subtotal',4),('Grand total',9)])]
    detect_summary(data,columns)
    assert [r['_is_summary_row'] for r in data]==[False,False,True,False,True,True]
