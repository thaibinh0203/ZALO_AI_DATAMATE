import copy
import pytest
from datamate_v2.common import ROOT,read_json

@pytest.fixture
def aliases(): return read_json(ROOT/'metadata/aliases/columns.json')

@pytest.fixture
def metadata():
    columns=[{'sql_column':'order_id','original_column':'Mã đơn','type':'TEXT','meaning':'order_id','semantic_status':'verified'},
             {'sql_column':'order_date','original_column':'Ngày đặt','type':'DATE','meaning':'order_date','semantic_status':'verified'},
             {'sql_column':'quantity','original_column':'Số lượng','type':'NUMERIC','meaning':'quantity','semantic_status':'verified'},
             {'sql_column':'line_total','original_column':'Thành tiền','type':'NUMERIC','meaning':'line_total','semantic_status':'verified'}]
    return {'business_id':'synthetic','fingerprint':'abc','schema':'b_synthetic','readme':'Synthetic fixture.','rulebook':'Synthetic rules.','calendar_csv':'',
            'relationships':[],'warnings':[],
            'tables':[{'sql_table':'sales','columns':columns,'quality':{'uncertain_summary_rows':0},
             'sample_rows':[],'last_rows':[],
             'stats':[{'sql_column':c['sql_column'],'null_count':0,'unresolved_count':0,'min':'2026-01-01' if c['type']=='DATE' else 1,'max':'2026-12-31' if c['type']=='DATE' else 100} for c in columns]}]}

@pytest.fixture
def question(): return {'id':'q_fixture','business_id':'synthetic','question':'Tổng số lượng hôm qua?','as_of_date':'2026-03-04'}

@pytest.fixture
def window(): return {'name':'period','interpretation':'yesterday','status':'validated','start_date':'2026-03-03','end_date_exclusive':'2026-03-04'}

@pytest.fixture
def plan(window):
    return {'status':'answerable','calculation_plan':['Sum verified quantity yesterday, excluding summary rows.'],
            'sql':"SELECT SUM(quantity) AS total FROM sales WHERE order_date >= DATE '2026-03-03' AND order_date < DATE '2026-03-04' AND _is_summary_row = FALSE",
            'reason':'Fixture','answer_type':'number','unit':'units','round_digits':None,'output_columns':['total'],
            'required_tables':['sales'],'column_usage':[{'table':'sales','column':'quantity','purpose':'sum'},{'table':'sales','column':'order_date','purpose':'filter'}],
            'time_bindings':[{'table':'sales','column':'order_date','window':'period'}],
            'summary_policy':'exclude','quality_checks':[],'null_policy':'abstain','_validated_windows':[window]}
