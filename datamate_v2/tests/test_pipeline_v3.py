import copy
import pytest
from datamate_v2.common import PipelineError,ROOT,read_json
from datamate_v2.data_pipeline.adapters.header_detector import score_header,detect_header
from datamate_v2.qa_pipeline.query_rewriting.intent_guard import validate_rewrite_intent
from datamate_v2.qa_pipeline.routing import route_metadata
from datamate_v2.qa_pipeline.sql_validation.ast_plan import derive_column_usage,project_answer
from datamate_v2.qa_pipeline.sql_validation.guard import validate_sql
from datamate_v2.qa_pipeline.pipeline import QAPipeline
from datamate_v2.tests.test_orchestration import FixtureClient,FixtureTool,settings


def rows(values): return [{'values':r,'index':i+1,'flags':[]} for i,r in enumerate(values)]


def test_sparse_optional_cells_do_not_penalize_header(aliases):
    header=['A name','B name','C name','D name']
    full=rows([header,[1,2,3,4],[5,6,7,8]])
    sparse=rows([header,[1,None,None,None],[2,None,None,None]])
    assert score_header(full,0,aliases)==score_header(sparse,0,aliases)
    assert detect_header(sparse,aliases)['position']==0
    assert score_header(rows([['A',None],[1,2]]),0,aliases)==0
    assert score_header(rows([['A',42],[1,2]]),0,aliases)==0


def test_actual_product_catalog_header_is_detected(aliases):
    from datamate_v2.data_pipeline.parsers.excel_parser import parse_excel
    from datamate_v2.data_pipeline.adapters.table_extractor import extract_tables
    p=ROOT.parent/'docs/train/data/biz_118/Danh sách hàng hóa.xlsx'
    if not p.exists(): pytest.skip('Actual training package unavailable')
    blocks=[b for s in parse_excel(p) for b in extract_tables(s,aliases)]
    assert len(blocks)==1 and blocks[0]['header'] is not None
    assert 'Nhóm hàng hóa' in blocks[0]['header']['values'] and len(blocks[0]['data_rows'])==55


def rewrite(text,kind,parameters=None):
    return {'rewritten_question':text,'time_windows':[{'name':'p','interpretation':kind,'parameters':parameters or {},'status':'proposed'}]}


@pytest.mark.parametrize('original,changed,kind',[
 ('hqua cua hang ban duoc may don','Hàng bán được nhiều nhất là hàng nào','current_calendar_month'),
 ('doanh thu hqua cua cua hang la bao nhiu','Doanh thu quý vừa qua','previous_calendar_quarter'),
 ('Số đơn hôm qua','Doanh thu hôm qua','yesterday'),
 ('tháng 2/2026 doanh thu bao nhiêu','Doanh thu tháng 3/2026','absolute_month')])
def test_rewrite_semantic_drift_is_rejected(original,changed,kind):
    q={'question':original,'as_of_date':'2026-03-04'}
    with pytest.raises(PipelineError): validate_rewrite_intent(q,rewrite(changed,kind,{'year':2026,'month':3} if kind=='absolute_month' else {}))


def test_same_elapsed_month_comparison_is_required():
    q={'question':'doanh thu tháng này hơn hay kém cùng kỳ tháng trước bao nhiêu','as_of_date':'2026-03-04'}
    response=rewrite('So sánh doanh thu tháng này với tháng trước','current_calendar_month')
    response['time_windows']+=rewrite('','previous_calendar_month')['time_windows']
    with pytest.raises(PipelineError): validate_rewrite_intent(q,response)
    response['time_windows'][1]['interpretation']='previous_month_same_elapsed_days'
    assert validate_rewrite_intent(q,response)


def test_absolute_window_can_preserve_yesterday_meaning():
    q={'question':'doanh thu hqua','as_of_date':'2026-03-04'}
    assert validate_rewrite_intent(q,rewrite('Doanh thu hôm qua','absolute_day',{'date':'2026-03-03'}))


def test_routing_retains_boundary_months_and_catalog(metadata):
    base=metadata['tables'][0]
    metadata['tables']=[]
    for month in [2,3,4]:
        table=copy.deepcopy(base);table['sql_table']=f'sales_{month}'
        table['stats'][1].update(min=f'2026-{month:02d}-01',max=f'2026-{month:02d}-28')
        metadata['tables'].append(table)
    catalog=copy.deepcopy(base);catalog['sql_table']='catalog';catalog['columns']=[catalog['columns'][0]];catalog['stats']=[catalog['stats'][0]]
    metadata['tables'].append(catalog)
    before=copy.deepcopy(metadata)
    routed,diagnostics=route_metadata(metadata,[{'start_date':'2026-02-23','end_date_exclusive':'2026-03-02'}])
    assert {t['sql_table'] for t in routed['tables']}=={'sales_2','sales_3','catalog'}
    assert diagnostics['excluded'][0]['table']=='sales_4' and metadata==before


def test_ast_fills_missing_verified_dependencies_without_repair(metadata,question,plan):
    plan['column_usage']=[]
    client=FixtureClient(question,plan)
    final,log=QAPipeline(client,{},settings(),FixtureTool).answer(question,metadata)
    assert final['answer']==7 and len(client.calls)==2
    assert {c['column'] for c in log['plan']['column_usage']}=={'quantity','order_date'}


def test_ast_never_confirms_uncertain_semantics(metadata,plan):
    metadata['tables'][0]['columns'][2]['semantic_status']='uncertain'
    plan['column_usage']=[]
    checked=derive_column_usage(plan['sql'],metadata,plan)
    with pytest.raises(PipelineError,match='source-backed'): validate_sql(checked,metadata,plan)


def test_final_projection_preserves_inner_ranking(metadata,plan):
    plan['answer_type']='date';plan['output_columns']=['order_date'];plan['column_usage']=[]
    sql=plan['sql'].replace('SUM(quantity) AS total','order_date, SUM(quantity) AS total')+' GROUP BY order_date ORDER BY total DESC LIMIT 1'
    checked=derive_column_usage(sql,metadata,plan)
    projected,changes=project_answer(checked,plan)
    assert changes and 'ORDER BY' in projected and 'LIMIT 1' in projected
    assert validate_sql(projected,metadata,plan)
    assert 'quantity' in {c['column'] for c in plan['column_usage']}


def test_stale_proposed_dates_are_not_sent_to_planner(question,plan,metadata):
    class Client(FixtureClient):
        def call(self,task,payload,schema,**kwargs):
            if task=='planning_sql':
                assert payload['rewrite']['time_windows'][0]['start_date']=='2026-03-03'
                assert 'proposed_start_date' not in payload['validated_windows'][0]
                assert '1900-01-01' not in str(payload)
            result=super().call(task,payload,schema,**kwargs)
            if task=='query_rewriting': result['time_windows'][0].update(start_date='1900-01-01',end_date_exclusive='1900-02-01')
            return result
    final,log=QAPipeline(Client(question,plan),{},settings(),FixtureTool).answer(question,metadata)
    assert final['answer']==7


def test_two_windows_on_same_table_are_proved_per_scope(metadata,plan):
    second={**plan['_validated_windows'][0],'name':'other','start_date':'2026-03-02','end_date_exclusive':'2026-03-03'}
    plan['_validated_windows'].append(second)
    plan['time_bindings'].append({'table':'sales','column':'order_date','window':'other'})
    first=plan['sql'];other=first.replace('2026-03-03','PLACEHOLDER').replace('2026-03-04','2026-03-03').replace('PLACEHOLDER','2026-03-02')
    statement=first+' UNION ALL '+other
    derive_column_usage(statement,metadata,plan)
    assert validate_sql(statement,metadata,plan)
    with pytest.raises(PipelineError): validate_sql(first,metadata,plan)
    with pytest.raises(PipelineError): validate_sql(first+' UNION ALL '+first,metadata,plan)
