"""Actual PostgreSQL integration. Set DATAMATE_TEST_ADMIN_DSN to enable."""
import copy
import os
import pytest
from datamate_v2.common import ROOT,load_yaml,PipelineError
from datamate_v2.database.loader import load_business
from datamate_v2.database.tools import QueryTool
from datamate_v2.qa_pipeline.sql_validation.guard import validate_sql
from datamate_v2.qa_pipeline.pipeline import QAPipeline
from datamate_v2.tests.factories import schema_fixture
from datamate_v2.tests.test_orchestration import FixtureClient,settings

@pytest.fixture
def pg_settings(monkeypatch):
    dsn=os.environ.get('DATAMATE_TEST_ADMIN_DSN')
    if not dsn: pytest.skip('Set DATAMATE_TEST_ADMIN_DSN for isolated PostgreSQL integration')
    import psycopg
    from psycopg import sql
    with psycopg.connect(dsn) as db:
        role='datamate_v2_test_reader'
        if not db.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,)).fetchone():
            db.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS').format(sql.Identifier(role)))
        db.execute(sql.SQL('ALTER ROLE {} SET default_transaction_read_only = on').format(sql.Identifier(role)))
    values=psycopg.conninfo.conninfo_to_dict(dsn);values['user']=role
    monkeypatch.setenv('DATAMATE_ADMIN_DSN',dsn)
    monkeypatch.setenv('DATAMATE_READONLY_DSN',psycopg.conninfo.make_conninfo(**values))
    return {**load_yaml(ROOT/'configs/database.yaml'),'readonly_role':role}

@pytest.fixture
def loaded(metadata,pg_settings):
    rows=[{'order_id':'a','order_date':'2026-03-03','quantity':2,'line_total':20,'_source_row':1,'_raw':['a','2026-03-03','2','20'],'_quality':[],'_is_summary_row':False,'_summary_status':'detail'},
          {'order_id':'b','order_date':'2026-03-03','quantity':5,'line_total':50,'_source_row':2,'_raw':['b','2026-03-03','5','50'],'_quality':[],'_is_summary_row':False,'_summary_status':'detail'},
          {'order_id':'total','order_date':'2026-03-03','quantity':7,'line_total':70,'_source_row':3,'_raw':['Tổng','2026-03-03','7','70'],'_quality':[],'_is_summary_row':True,'_summary_status':'verified'}]
    load_business(metadata,{'sales':rows},pg_settings)
    return metadata

def test_actual_readonly_execution(loaded,pg_settings,plan):
    tool=QueryTool(loaded,pg_settings,validate_sql)
    result=tool.execute_readonly_sql(plan['sql'],plan=plan)
    assert result['rows'][0][0]==7 and len(tool.trace)==1


def test_direct_entity_domains_are_actual_column_values(pg_settings):
    from datamate_v2.qa_pipeline.direct_sql import execution_guard
    from datamate_v2.qa_pipeline.direct_entities import validate_literals
    from datamate_v2.qa_pipeline.result_validation.validator import serialize_result
    md={'schema':'b_direct_entity_fixture','tables':[{'sql_table':'entity_sales','columns':[
        {'sql_column':c,'type':'TEXT'} for c in ['product_name','category','status']]}]}
    rows=[{'product_name':'Nếp cái hoa vàng','category':'điện','status':'Đã giao',
        '_source_row':1,'_is_summary_row':False,'_summary_status':'detail','_raw':[],'_quality':[]},
        {'product_name':'Tổng','category':'not a category','status':'not a status',
        '_source_row':2,'_is_summary_row':True,'_summary_status':'verified','_raw':[],'_quality':[]}]
    load_business(md,{'entity_sales':rows},pg_settings)
    tool=QueryTool(md,pg_settings,execution_guard,max_queries=8)
    domains=tool.text_domains([('entity_sales',c) for c in ['product_name','status','category']])
    assert all(d['complete'] and len(d['values'])==1 for d in domains)
    sql,changes=validate_literals("SELECT product_name FROM entity_sales WHERE product_name='nep cai hoa vang' AND status='Đã giao' AND category='điện'",md,domains,tool)
    assert changes[0]['canonical_value']=='Nếp cái hoa vàng'
    assert tool.execute_readonly_sql(sql)['rows']==[['Nếp cái hoa vàng']]
    for column,value in [('status','đã xong'),('category','Electrical')]:
        with pytest.raises(PipelineError,match='actual value'):
            validate_literals(f"SELECT product_name FROM entity_sales WHERE {column}='{value}'",md,domains,tool)
    result=tool.execute_readonly_sql("SELECT TIMESTAMPTZ '2026-07-29 20:00:00+00' AS day")
    answer=serialize_result({'id':'q'},{'answer_type':'date','output_columns':['day'],
        '_timezone':'Asia/Ho_Chi_Minh'},result)
    assert answer['answer']=='2026-07-30'


@pytest.mark.parametrize('scoped',[False,True])
def test_decisions_two_month_cte_executes_with_source_grounded_actions(pg_settings,scoped):
    from datamate_v2.data_pipeline.value_catalog import build_value_catalog
    from datamate_v2.inference.structured_output import parse_output
    from datamate_v2.common import dumps
    md={'business_id':'decisions_test','fingerprint':'decisions_fixture','schema':'b_decisions_fixture',
        'readme':'Mỗi dòng là dòng hàng.','rulebook':'Đếm mã đơn duy nhất; bỏ đơn trả hàng.',
        'calendar_csv':'','tables':[]}
    rows={}
    for name,day in [('feb','2026-02-27'),('mar','2026-03-01')]:
        md['tables'].append({'sql_table':name,'source_file':'monthly.xlsx','columns':[
            {'sql_column':'date','type':'DATE','meaning':'date'},
            {'sql_column':'order_id','type':'TEXT','meaning':'order_id'},
            {'sql_column':'status','type':'TEXT','meaning':'status'}],
            'stats':[{'sql_column':'date','min':day,'max':day}]})
        rows[name]=[{'date':day,'order_id':name,'status':'Đã giao','_source_row':i,
            '_is_summary_row':False,'_summary_status':'detail','_raw':[],'_quality':[]} for i in [1,2]]
    md['value_catalog']=build_value_catalog(md,rows)
    load_business(md,rows,pg_settings)
    class Client:
        version='fixture'
        def __init__(self):self.calls=[]
        def call(self,task,payload,schema):
            self.calls.append({'task':task,'cache_hit':False})
            if task=='qa_decisions':
                result={'goal':'Số đơn tuần rồi','entity_mentions':[],
                    'rules_to_apply':[{'source':'rulebook','action':'Đếm mã đơn duy nhất; bỏ trả hàng theo nguồn.'}],
                    'aggregation':'Đếm mã đơn không trùng sau khi hợp nhất các tháng.',
                    'time_requests':[],'data_requirements':['Bán hàng'],'requires_full_history':False}
            else:
                source=payload['data_assembly']['sources'][0];w=payload['resolved_time']['windows'][0]
                assert source['physical_tables']==['feb','mar']
                query=f"WITH records AS ({source['select_sql']}) SELECT COUNT(DISTINCT order_id) AS answer FROM records WHERE date >= DATE '{w['start_date']}' AND date < DATE '{w['end_date_exclusive']}' AND status='Đã giao'"
                result={'status':'answerable','calculation_plan':['Đếm đơn duy nhất trong kỳ.'],
                    'sql':query,'reason':'Theo quyết định và dữ liệu.','answer_type':'number',
                    'list_mode':'row','unit':'','round_digits':None}
            return parse_output(dumps(result),schema)
    client=Client();q={'id':'q','business_id':'decisions_test','question':'Số đơn tuần rồi?', 'as_of_date':'2026-03-04'}
    final,log=QAPipeline(client,pg_settings,{'mode':'direct_sql','decision_planning':True,'scoped_context':scoped,
        'max_repairs':0,'calendar':{'calendar_rule':'MONDAY_TO_SUNDAY','timezone':'Asia/Ho_Chi_Minh'}}).answer(q,md)
    assert final=={'id':'q','type':'number','answer':2},log
    assert len(client.calls)==2 and log['value_dictionary_budget']['tokens']<=2500

def test_qa_end_to_end_with_real_database(loaded,pg_settings,question,plan):
    client=FixtureClient(question,plan)
    final,log=QAPipeline(client,pg_settings,settings()).answer(question,loaded)
    assert final=={'id':question['id'],'type':'number','answer':7},log
    assert len(client.calls)==2 and log['queries'][0]['status']=='success'


def test_redundant_temporal_override_executes_without_extra_inference(loaded,pg_settings,question,plan):
    plan['temporal_override']={'source_quote':question['question'],'time_windows':[
        {'name':'period','interpretation':'yesterday','parameters':{},'status':'proposed'}]}
    client=FixtureClient(question,plan)
    legacy=settings();legacy['rule_selection_in_call1']=False
    final,log=QAPipeline(client,pg_settings,legacy).answer(question,loaded)
    assert final['answer']==7 and len(client.calls)==2
    assert log['temporal_override_ignored'] and log['queries'][-1]['status']=='success'


def test_reuse_validates_existing_database_and_restores_missing_rows(loaded,pg_settings,tmp_path):
    from datamate_v2.data_pipeline.reuse import database_ready,load_reused_business
    from datamate_v2.database.connection import connect
    from datamate_v2.common import write_json
    loaded['tables'][0]['row_count']=3
    assert database_ready(loaded,pg_settings)
    assert load_reused_business(loaded,tmp_path/'not-read.json',pg_settings) is loaded
    with connect(pg_settings,admin=True) as db:
        rows=db.execute('SELECT order_id,order_date,quantity,line_total,_source_row,_raw,_quality,_is_summary_row,_summary_status FROM b_synthetic.sales').fetchall()
        keys=['order_id','order_date','quantity','line_total','_source_row','_raw','_quality','_is_summary_row','_summary_status']
        clean={'sales':[dict(zip(keys,row)) for row in rows]}
        db.execute('DELETE FROM b_synthetic.sales')
    assert not database_ready(loaded,pg_settings)
    path=tmp_path/'clean_rows.json';write_json(path,clean)
    assert load_reused_business(loaded,path,pg_settings) is loaded
    assert database_ready(loaded,pg_settings)

def test_readonly_role_rejects_mutation_even_without_guard(loaded,pg_settings):
    from datamate_v2.database.connection import connect
    import psycopg
    with pytest.raises(psycopg.Error):
        with connect(pg_settings) as db: db.execute('DELETE FROM b_synthetic.sales')

def test_budget_enforced(loaded,pg_settings):
    tool=QueryTool(loaded,pg_settings,validate_sql,max_queries=1)
    tool.execute_readonly_sql('SELECT COUNT(*) AS n FROM sales')
    with pytest.raises(PipelineError,match='budget'): tool.execute_readonly_sql('SELECT COUNT(*) AS n FROM sales')

def test_incomplete_values_trigger_abstain(loaded,pg_settings,question,plan):
    from datamate_v2.database.connection import connect
    with connect(pg_settings,admin=True) as db: db.execute('UPDATE b_synthetic.sales SET quantity=NULL WHERE order_id=\'a\'')
    loaded['tables'][0]['stats'][2]['null_count']=1
    final,log=QAPipeline(FixtureClient(question,plan),pg_settings,settings()).answer(question,loaded)
    assert final['type']=='abstain' and 'quality' in log['error'].lower()

def test_optimized_ingest_publishes_rules_normalized_rows(tmp_path,pg_settings):
    from datamate_v2.data_pipeline.pipeline import DataPipeline
    split=tmp_path/'split';biz=split/'data/synthetic';biz.mkdir(parents=True)
    (biz/'README.md').write_text('Synthetic fixture, one line per order.')
    (split/'rulebook.md').write_text('Synthetic fixture.')
    (split/'calendar.csv').write_text('date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n')
    (biz/'sales.csv').write_text('Mã đơn,Ngày đặt,Thành tiền\na,1 thg 6 2025,"1,000,000"\nb,2 thg 6 2025,"2,000,000"\nc,3 thg 6 2025,"1,250"\n')
    class Client:
        version='optimized-fixture';calls=[]
        def call(self,task,payload,*args,**kwargs):
            assert task=='schema_description','Rules should handle all fixture values'
            self.calls.append(task)
            return schema_fixture(payload['table'])
    client=Client();metadata=DataPipeline(client,pg_settings,{},tmp_path/'cache').prepare(biz,split)
    table=metadata['tables'][0]
    assert table['normalization_stats']['slm_items']==0 and client.calls==['schema_description']
    result=QueryTool(metadata,pg_settings,validate_sql).execute_readonly_sql(f'SELECT SUM(line_total) AS total FROM "{table["sql_table"]}" WHERE _is_summary_row = FALSE')
    assert result['rows'][0][0]==3001250

def test_summary_filter_completion_executes_without_extra_inference(loaded,pg_settings,question,plan):
    plan['sql']=plan['sql'].replace(' AND _is_summary_row = FALSE','')
    client=FixtureClient(question,plan)
    final,log=QAPipeline(client,pg_settings,settings()).answer(question,loaded)
    assert final['answer']==7 and len(client.calls)==2
    assert log['sql_adjustments'] and '_is_summary_row' in log['queries'][-1]['sql']


@pytest.mark.parametrize('description_fails',[False,True])
def test_value_batch_failure_does_not_block_unaffected_columns(tmp_path,pg_settings,description_fails):
    from datamate_v2.data_pipeline.pipeline import DataPipeline
    from datamate_v2.common import PipelineError
    split=tmp_path/'split';biz=split/'data/synthetic';biz.mkdir(parents=True)
    (biz/'README.md').write_text('Synthetic fixture, one record per line.')
    (split/'rulebook.md').write_text('Synthetic fixture.')
    (split/'calendar.csv').write_text('date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n')
    (biz/'sales.csv').write_text('Mã đơn,Số lượng,Thành tiền\na,1,unparseable\nb,2,unparseable\n')
    class Client:
        version='partial-errors-fixture';calls=[]
        def call(self,task,payload,*args,**kwargs):
            self.calls.append(task)
            if task=='value_normalization': raise PipelineError('Model output hit max_new_tokens')
            assert task=='schema_description'
            if description_fails: raise PipelineError('Model output hit max_new_tokens')
            return schema_fixture(payload['table'])
    metadata=DataPipeline(Client(),pg_settings,{},tmp_path/'cache').prepare(biz,split)
    table=metadata['tables'][0]
    assert table['quality']['unresolved_values']==2
    assert table['normalization_stats']['slm_batch_errors']==1
    assert table['description_status']==('unavailable' if description_fails else 'available')
    assert metadata['readme']=='Synthetic fixture, one record per line.'
    assert table['verified_facts']['row_count']==2
    result=QueryTool(metadata,pg_settings,validate_sql).execute_readonly_sql(f'SELECT COUNT(DISTINCT order_id) AS n FROM "{table["sql_table"]}" WHERE _is_summary_row = FALSE')
    assert result['rows'][0][0]==2


def test_monthly_group_ingest_keeps_physical_tables_queryable(tmp_path,pg_settings):
    from openpyxl import Workbook
    from datamate_v2.data_pipeline.pipeline import DataPipeline
    split=tmp_path/'split';biz=split/'data/synthetic';biz.mkdir(parents=True)
    (biz/'README.md').write_text('Sổ bán hàng: mỗi dòng là dòng hàng.\n## Cột trong `sales.xlsx`\n- **Mã đơn**: mã đơn\n- **Số lượng**: số lượng\n')
    (split/'rulebook.md').write_text('Shared business rules, retained for QA.')
    (split/'calendar.csv').write_text('date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n')
    workbook=Workbook();workbook.remove(workbook.active)
    for month,count in [('T02-2025',2),('T03-2025',3)]:
        sheet=workbook.create_sheet(month);sheet.append(['Mã đơn','Số lượng'])
        for i in range(count): sheet.append([month,i+1])
    workbook.save(biz/'sales.xlsx')
    class Client:
        version='grouped-fixture'
        def __init__(self): self.calls=[]
        def call(self,task,payload,*args,**kwargs):
            self.calls.append(task);assert task=='schema_description'
            return schema_fixture(payload['table'])
    client=Client();pipeline=DataPipeline(client,pg_settings,{},tmp_path/'cache')
    metadata=pipeline.prepare(biz,split)
    assert client.calls==['schema_description']
    assert metadata['description_generation']['reused_tables']==1
    assert sorted(t['row_count'] for t in metadata['tables'])==[2,3]
    totals=[]
    for table in metadata['tables']:
        assert table['description']['group_ref'] in metadata['description_groups']
        result=QueryTool(metadata,pg_settings,validate_sql).execute_readonly_sql(f'SELECT SUM(quantity) AS n FROM "{table["sql_table"]}" WHERE _is_summary_row = FALSE')
        totals.append(result['rows'][0][0])
    assert sorted(totals)==[3,6]
    cached=pipeline.prepare(biz,split)
    assert len(client.calls)==1 and cached['description_groups']==metadata['description_groups']


@pytest.fixture
def entity_loaded(metadata,pg_settings):
    metadata['schema']='b_entity_resolution_fixture'
    table=metadata['tables'][0]
    table['columns'].append({'sql_column':'product_name','original_column':'Tên hàng','meaning':'product_name','type':'TEXT','semantic_status':'verified'})
    table['stats'].append({'sql_column':'product_name','null_count':0,'unresolved_count':0,'min':'Hoa','max':'Thuốc lá Vinataba'})
    table['row_count']=4
    rows=[]
    for i,name in enumerate(['Other','Thuốc lá Vinataba','Hoa','Hòa']):
        rows.append({'order_id':str(i),'order_date':'2026-03-03','quantity':7 if i==1 else 1,'line_total':10,
            'product_name':name,'_source_row':i+1,'_raw':[name],'_quality':[],'_is_summary_row':False,'_summary_status':'detail'})
    load_business(metadata,{'sales':rows},pg_settings)
    return metadata


@pytest.mark.parametrize('mode',['resolved','count_empty','sum_empty','ambiguous','repair_literal'])
def test_entity_resolution_and_empty_population_in_real_db(entity_loaded,pg_settings,question,plan,mode):
    raw='Thuoc la Vinataba' if mode in ('resolved','repair_literal') else 'hoa' if mode=='ambiguous' else 'Absent'
    canonical='Thuốc lá Vinataba' if mode in ('resolved','repair_literal') else raw
    question={**question,'question':f'Tổng số lượng {raw} hôm qua?'}
    plan['sql']=plan['sql'].replace("order_date >= DATE '2026-03-03' AND order_date < DATE '2026-03-04'","order_date = DATE '2026-03-03'")
    plan['sql']+=f" AND product_name = '{canonical}'"
    if mode=='count_empty':
        plan['sql']=plan['sql'].replace('SUM(quantity)','COUNT(DISTINCT order_id)')
        plan['column_usage'].append({'table':'sales','column':'order_id','purpose':'count distinct orders'})
    plan['column_usage'].append({'table':'sales','column':'product_name','purpose':'filter'})
    plan['quality_checks']=[{'sql':plan['sql'].replace('SUM(quantity)','COUNT(*)').replace('COUNT(DISTINCT order_id)','COUNT(*)'),
                            'expected':'positive','reason':'Model incorrectly assumes transactions must exist'}]
    class Client(FixtureClient):
        def call(self,task,payload,schema,**kwargs):
            response=super().call(task,payload,schema,**kwargs)
            if task=='query_rewriting': response['entities']={'products':[raw]}
            if task=='planning_sql' and mode=='repair_literal' and not kwargs.get('repair'):
                response['sql']=response['sql'].replace(canonical,raw)
            if task=='planning_sql':
                assert payload['exploration'][0]['input']==raw
            return response
    client=Client(question,plan)
    final,log=QAPipeline(client,pg_settings,settings()).answer(question,entity_loaded)
    if mode in ('resolved','repair_literal'):
        assert final['answer']==7,log
        assert log['exploration'][0]['canonical_value']=='Thuốc lá Vinataba'
        assert len(client.calls)==(3 if mode=='repair_literal' else 2)
    elif mode=='count_empty':
        assert final.get('answer')==0,(log.get('error'),log.get('checks'),log.get('queries'))
        assert log['checks'][0]['status']=='empty_population'
    elif mode=='sum_empty':
        assert final['type']=='abstain' and 'NULL result' in log['error']
    else:
        assert final['type']=='abstain' and 'Ambiguous entity' in log['error']
        assert len(client.calls)==1
    assert log['queries'][0]['purpose']=='entity_lookup'


def test_entity_domain_limit_is_explicit(entity_loaded,pg_settings):
    tool=QueryTool(entity_loaded,pg_settings,validate_sql)
    result=tool.entity_values([('sales','product_name')],limit=1)
    assert result['complete'] is False and len(result['values'])==2


def test_scalar_projection_preserves_ranking_in_postgres(loaded,pg_settings,question,plan):
    question={**question,'question':'Ngày nào bán nhiều tiền nhất hôm qua?'}
    plan.update(answer_type='date',output_columns=['order_date'],column_usage=[])
    plan['sql']=plan['sql'].replace('SUM(quantity) AS total','order_date, SUM(quantity) AS total')+' GROUP BY order_date ORDER BY total DESC LIMIT 1'
    client=FixtureClient(question,plan)
    final,log=QAPipeline(client,pg_settings,settings()).answer(question,loaded)
    assert final=={'id':question['id'],'type':'date','answer':'2026-03-03'},log
    assert log['projection_adjustments'] and log['result']['columns']==['order_date']
    assert len(client.calls)==2


def test_new_ingest_catalog_name_relationship_and_month_reuse(tmp_path,pg_settings):
    from openpyxl import Workbook
    from datamate_v2.data_pipeline.pipeline import DataPipeline
    split=tmp_path/'split';biz=split/'data/synthetic';biz.mkdir(parents=True)
    (biz/'README.md').write_text('Tên hàng là tên hàng. Mã hàng là mã hàng.\n## Cột trong `sales.xlsx`\n- **Tên hàng**: tên hàng\n')
    (split/'rulebook.md').write_text('Category comes from catalog; exact product names identify catalog rows.')
    (split/'calendar.csv').write_text('date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n')
    workbook=Workbook();workbook.remove(workbook.active)
    for name in ['T02-2026','T03-2026']:
        sheet=workbook.create_sheet(name);sheet.append(['Mã đơn','Tên hàng','Số lượng'])
        sheet.append(['a','Alpha',2]);sheet.append(['a','Beta',3])
    workbook.save(biz/'sales.xlsx')
    catalog=Workbook();sheet=catalog.active
    sheet.append(['Mã hàng','Tên hàng','Nhóm hàng']);sheet.append(['P1','Alpha','điện']);sheet.append(['P2','Beta','khác'])
    catalog.save(biz/'catalog.xlsx')
    class Client:
        version='pipeline-v3-catalog-fixture'
        def __init__(self): self.calls=[]
        def call(self,task,payload,*args,**kwargs):
            self.calls.append(task);assert task=='schema_description'
            return schema_fixture(payload['table'])
    client=Client();metadata=DataPipeline(client,pg_settings,{},tmp_path/'cache').prepare(biz,split)
    assert len(metadata['tables'])==3 and len(metadata['relationships'])==2
    assert metadata['description_generation']['groups']==2 and len(client.calls)==2
    relation=metadata['relationships'][0]
    assert relation['from_column']=='product_name' and relation['to_column']=='product_name'
    query=f'''SELECT SUM(s.quantity) AS total FROM "{relation['from_table']}" s
        JOIN "{relation['to_table']}" c ON s.product_name=c.product_name
        WHERE s._is_summary_row=FALSE AND c._is_summary_row=FALSE AND c.category='điện' '''
    result=QueryTool(metadata,pg_settings,validate_sql).execute_readonly_sql(query)
    assert result['rows']==[[2]]


def test_real_catalog_with_all_column_slm_response_still_loads(tmp_path,pg_settings):
    import shutil
    from datamate_v2.data_pipeline.pipeline import DataPipeline
    split=tmp_path/'split';biz=split/'data/synthetic';biz.mkdir(parents=True)
    source=ROOT.parent/'docs/train/data/biz_118'
    shutil.copy(source/'Danh sách hàng hóa.xlsx',biz/'Danh sách hàng hóa.xlsx')
    (biz/'README.md').write_text((source/'README.md').read_text())
    (split/'rulebook.md').write_text('Fixture business rules.')
    (split/'calendar.csv').write_text('date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n')
    known={4:'product_id',7:'product_name',9:'category',10:'unit',13:'note'}
    class Client:
        version='all-column-response-regression'
        def __init__(self): self.calls=[]
        def call(self,task,payload,schema,**kwargs):
            self.calls.append(task)
            if task=='column_normalization':
                assert not set(known)&set(payload['allowed_indices'])
                return {'mappings':[{'index':i,'meaning':known.get(i),'candidate_meanings':[],
                                      'evidence':[],'source_quote':''} for i in range(16)]}
            assert task=='schema_description'
            return schema_fixture(payload['table'])
    client=Client();metadata=DataPipeline(client,pg_settings,{},tmp_path/'cache').prepare(biz,split)
    assert len(metadata['tables'])==1 and metadata['tables'][0]['row_count']==55
    assert client.calls.count('column_normalization')==1
    table=metadata['tables'][0]
    assert table['quality']['mapping_proposal_warnings']==5
    result=QueryTool(metadata,pg_settings,validate_sql).execute_readonly_sql(f'SELECT COUNT(*) AS n FROM "{table["sql_table"]}" WHERE _is_summary_row = FALSE')
    assert result['rows']==[[55]]


@pytest.mark.parametrize('bad_span',[False,True])
def test_entity_string_and_bad_span_run_with_real_database(entity_loaded,pg_settings,question,plan,bad_span):
    raw='mấy bao Thuoc la Vinataba' if bad_span else 'Thuoc la Vinataba'
    question={**question,'question':'Bán được bao nhiêu Thuoc la Vinataba hôm qua?'}
    plan['sql']+=" AND product_name = 'Thuốc lá Vinataba'"
    plan['column_usage'].append({'table':'sales','column':'product_name','purpose':'filter'})
    class Client(FixtureClient):
        def call(self,task,payload,schema,**kwargs):
            response=super().call(task,payload,schema,**kwargs)
            if task=='query_rewriting': response['entities']={'products':raw}
            if task=='planning_sql':
                assert payload['exploration'][0]['canonical_value']=='Thuốc lá Vinataba'
                assert len(payload['exploration'])==1
            return response
    client=Client(question,plan)
    final,log=QAPipeline(client,pg_settings,settings()).answer(question,entity_loaded)
    assert final.get('answer')==7,log.get('error')
    assert len(client.calls)==2 and len(log['exploration'])==1


@pytest.mark.parametrize('grouped',[False,True])
def test_unnamed_scalar_alias_does_not_bypass_cardinality(loaded,pg_settings,question,plan,grouped):
    plan['sql']=plan['sql'].replace('SUM(quantity) AS total','SUM(quantity)')
    if grouped:plan['sql']+=' GROUP BY order_id'
    client=FixtureClient(question,plan)
    final,log=QAPipeline(client,pg_settings,settings()).answer(question,loaded)
    assert log['projection_adjustments'][0]['action']=='normalize_scalar_alias'
    assert 'SUM(' in log['sql'] and len(client.calls)==2
    if grouped:
        assert final['type']=='abstain'
        assert len(log['result']['rows'])==2
    else:
        assert final['answer']==7 and log['result']['columns']==['_dm_answer_value']


@pytest.mark.parametrize('sql,expected',[
    ('WITH x AS (SELECT order_id,quantity FROM sales) SELECT SUM(quantity) AS total FROM x',7),
    ('SELECT SUM(quantity) AS total FROM (SELECT quantity FROM sales UNION ALL SELECT quantity FROM sales) x',14),
    ('SELECT SUM(s.quantity) AS total FROM sales s INNER JOIN sales x ON s.order_id=x.order_id',7),
])
def test_multi_scope_summary_filters_execute_on_real_postgres(loaded,pg_settings,plan,sql,expected):
    from datamate_v2.qa_pipeline.sql_validation.summary_policy import enforce_summary_policy
    checked,adjustments=enforce_summary_policy(sql,loaded,plan,'Tổng số lượng')
    assert adjustments
    tool=QueryTool(loaded,pg_settings,validate_sql)
    assert tool.execute_readonly_sql(checked)['rows']==[[expected]]


@pytest.mark.parametrize('mode,expected',[
    ('total',7),('exclude_a',5),('tuple_count',2),('ranking','b'),('count_groups',1),('union',14),
])
def test_structured_compiler_end_to_end_postgres(loaded,pg_settings,question,plan,mode,expected):
    from datamate_v2.tests.test_business_compiler import structured
    response=structured(plan,loaded);business=response['business_plan']
    if mode=='exclude_a':
        business['filters']=[{'relation':'source','column':'order_id','operator':'not_in','values':['a'],'basis':'r1'}]
    elif mode=='tuple_count':
        business['aggregates']=[{'function':'COUNT_DISTINCT','columns':[{'relation':'source','column':'order_id'},{'relation':'source','column':'order_date'}],'alias':'total','basis':'r1'}]
    elif mode in ('ranking','count_groups'):
        business['group_by']=[{'relation':'source','column':'order_id','alias':'order_id','basis':'question'}]
        if mode=='ranking':
            response['answer_type']='string';response['output_columns']=['order_id'];business['order_by']=[{'column':'total','direction':'DESC'}];business['limit']=1
        else:
            business['result_mode']='count_groups';business['having']=[{'aggregate':'total','operator':'gte','value':3,'basis':'question'}]
    elif mode=='union':
        from datamate_v2.database.connection import connect
        from psycopg import sql
        other=copy.deepcopy(loaded['tables'][0]);other['sql_table']='sales_extra';loaded['tables'].append(other)
        with connect(pg_settings,admin=True) as db:
            db.execute(sql.SQL('CREATE TABLE IF NOT EXISTS {}.sales_extra (LIKE {}.sales INCLUDING ALL)').format(sql.Identifier(loaded['schema']),sql.Identifier(loaded['schema'])))
            db.execute(sql.SQL('TRUNCATE {}.sales_extra').format(sql.Identifier(loaded['schema'])))
            db.execute(sql.SQL('INSERT INTO {}.sales_extra SELECT * FROM {}.sales').format(sql.Identifier(loaded['schema']),sql.Identifier(loaded['schema'])))
            db.execute(sql.SQL('GRANT SELECT ON {}.sales_extra TO {}').format(sql.Identifier(loaded['schema']),sql.Identifier(pg_settings['readonly_role'])))
        business['sources'].append('sales_extra');response['time_bindings'].append({**response['time_bindings'][0],'table':'sales_extra'})
    client=FixtureClient(question,response)
    final,log=QAPipeline(client,pg_settings,settings()).answer(question,loaded)
    assert final.get('answer')==expected,(log.get('error'),log.get('sql'),log.get('checks'))
    assert log['compilation']['mode']=='compiled' and log['model_sql'] is None and len(client.calls)==2
    assert len(log['plan']['quality_checks'])==0


def test_scalar_cte_cross_join_is_proven_one_row(loaded,pg_settings,plan):
    from datamate_v2.qa_pipeline.sql_validation.summary_policy import enforce_summary_policy
    from datamate_v2.qa_pipeline.sql_validation.ast_plan import derive_column_usage
    stmt="""WITH a AS (SELECT SUM(quantity) AS n FROM sales WHERE order_date >= DATE '2026-03-03' AND order_date < DATE '2026-03-04'),
    b AS (SELECT COUNT(*) AS n FROM sales WHERE order_date >= DATE '2026-03-03' AND order_date < DATE '2026-03-04')
    SELECT a.n / b.n AS total FROM a CROSS JOIN b"""
    patched,_=enforce_summary_policy(stmt,loaded,plan,'Tổng số lượng')
    patched=derive_column_usage(patched,loaded,plan)
    checked=validate_sql(patched,loaded,plan=plan)
    assert QueryTool(loaded,pg_settings,validate_sql).execute_readonly_sql(checked)['rows']==[[3.5]]


def test_structured_lookup_filters_compile_and_execute(loaded,pg_settings,question,plan):
    from datamate_v2.tests.test_business_compiler import structured
    from datamate_v2.database.connection import connect
    from psycopg import sql
    lookup={'sql_table':'catalog','columns':[{'sql_column':name,'original_column':name,'type':'TEXT','meaning':name,'semantic_status':'verified'} for name in ['order_id','category']],
            'quality':{'uncertain_summary_rows':0},'sample_rows':[],'last_rows':[],
            'stats':[{'sql_column':name,'null_count':0,'unresolved_count':0,'min':'a','max':'z'} for name in ['order_id','category']]}
    loaded['tables'].append(lookup);loaded['relationships']=[{'from_table':'sales','from_column':'order_id','to_table':'catalog','to_column':'order_id','status':'verified'}]
    with connect(pg_settings,admin=True) as db:
        db.execute(sql.SQL('CREATE TABLE IF NOT EXISTS {}.catalog (order_id TEXT, category TEXT, _source_row BIGINT, _is_summary_row BOOLEAN, _summary_status TEXT)').format(sql.Identifier(loaded['schema'])))
        db.execute(sql.SQL('TRUNCATE {}.catalog').format(sql.Identifier(loaded['schema'])))
        db.execute(sql.SQL("INSERT INTO {}.catalog VALUES ('a','điện',1,FALSE,'detail'),('b','khác',2,FALSE,'detail')").format(sql.Identifier(loaded['schema'])))
        db.execute(sql.SQL('GRANT SELECT ON {}.catalog TO {}').format(sql.Identifier(loaded['schema']),sql.Identifier(pg_settings['readonly_role'])))
    response=structured(plan,loaded);business=response['business_plan']
    business['lookups']=[{'table':'catalog','source_column':'order_id','lookup_column':'order_id'}]
    business['filters']=[{'relation':'lookup','column':'category','operator':'eq','values':['điện'],'basis':'question'}]
    client=FixtureClient(question,response)
    final,log=QAPipeline(client,pg_settings,settings()).answer(question,loaded)
    assert final.get('answer')==2,(log.get('error'),log.get('sql'))
    assert log['compilation']['lookup']=='catalog'
