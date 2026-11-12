from datetime import datetime,timezone
import pytest
from datamate_v2.common import PipelineError,dumps
from datamate_v2.qa_pipeline.direct_entities import entity_hints,validate_literals
from datamate_v2.qa_pipeline.direct_sql import build_direct_context,ANALYSIS_SCHEMA,execution_guard
from datamate_v2.qa_pipeline.result_validation.validator import serialize_result
from datamate_v2.tests.test_direct_sql import Client
from datamate_v2.qa_pipeline.pipeline import QAPipeline


@pytest.fixture
def md():
    return {'business_id':'biz_test','fingerprint':'x','schema':'b_test','readme':'README','rulebook':'Rules',
        'tables':[{'sql_table':t,'columns':[{'sql_column':c,'type':'TEXT'} for c in
            ['product_name','category','status']],'sample_rows':[]} for t in ['sales','catalog']]}


@pytest.fixture
def domains():
    return [{'table':t,'column':c,'values':v,'complete':True} for t,c,v in [
        ('sales','status',['Đã giao','Trả hàng','Không giao được']),
        ('sales','product_name',['Nếp cái hoa vàng']),
        ('catalog','category',['điện','sơn'])]]


def test_context_labels_are_not_sql_identifiers(md):
    c=build_direct_context(md)
    assert 'schema_groups' not in c and 'table_catalog' not in c
    assert [t['sql_table'] for t in c['queryable_tables']]==['sales','catalog']
    assert all(t['schema_reference'] in c['schema_reference'] for t in c['queryable_tables'])
    assert 'sg_' not in dumps(c)
    assert 'candidate_tables' not in ANALYSIS_SCHEMA['properties']
    assert ANALYSIS_SCHEMA['properties']['entity_definitions']['maxItems']==4


def test_hints_recover_original_product_even_when_analysis_translates_it(domains):
    hint=entity_hints(domains,'thang 2 ban may kg nep cai hoa vang',{'entity_mentions':['gold paint cans']})
    p=next(x for x in hint['columns'] if x['column']=='product_name')
    assert p['matched_question_values']==['Nếp cái hoa vàng']
    assert next(x for x in hint['columns'] if x['column']=='status')['actual_values']==['Đã giao','Trả hàng','Không giao được']


@pytest.mark.parametrize('sql',[
    "SELECT product_name FROM sales WHERE product_name = 'nep cai hoa vang'",
    "WITH r AS (SELECT product_name AS name FROM sales) SELECT name FROM r WHERE name='nep cai hoa vang'",
    "SELECT product_name FROM sales s WHERE 'nep cai hoa vang' = s.product_name",
    "SELECT product_name FROM sales WHERE product_name IN ('nep cai hoa vang')",
])
def test_unique_accent_spelling_is_replaced_with_real_value(md,domains,sql):
    checked,changes=validate_literals(sql,md,domains,None)
    assert 'Nếp cái hoa vàng' in checked and len(changes)==1


@pytest.mark.parametrize('sql',[
    "SELECT status FROM sales WHERE status='đã xong'",
    "SELECT category FROM catalog WHERE category='Electrical'",
    "WITH c AS (SELECT category AS kind FROM catalog) SELECT kind FROM c WHERE kind='Electrical'",
    "WITH c(kind) AS (SELECT category FROM catalog) SELECT kind FROM c WHERE kind='Electrical'",
    "SELECT status FROM sales WHERE status NOT IN ('đã xong')",
    "SELECT category FROM catalog WHERE category ILIKE '%Electrical%'",
    "SELECT category FROM catalog WHERE CASE WHEN category='Electrical' THEN 1 ELSE 0 END=1",
])
def test_semantic_synonyms_and_translations_are_not_guessed(md,domains,sql):
    with pytest.raises(PipelineError,match='actual value'):
        validate_literals(sql,md,domains,None)


def test_case_function_is_checked_against_transformed_domain(md,domains):
    sql="SELECT status FROM sales WHERE LOWER(status)='đã giao'"
    checked,changes=validate_literals(sql,md,domains,None)
    assert changes==[] and "'đã giao'" in checked


def test_union_sources_cannot_borrow_each_others_status(md,domains):
    domains.append({'table':'catalog','column':'status','values':['Xong'],'complete':True})
    sql="WITH u AS (SELECT status FROM sales UNION ALL SELECT status FROM catalog) SELECT status FROM u WHERE status='Xong'"
    with pytest.raises(PipelineError,match='sales.status'):
        validate_literals(sql,md,domains,None)


def test_ambiguous_accent_match_is_not_chosen(md,domains):
    domains[1]['values']=['Ma','Má']
    with pytest.raises(PipelineError):validate_literals("SELECT product_name FROM sales WHERE product_name='ma'",md,domains,None)


def test_partial_domain_uses_exact_database_lookup(md,domains):
    domains[1].update(values=['Other'],complete=False)
    class Tool:
        def lookup_entity(self,t,c,v):return {'rows':[[v]]}
    sql="SELECT product_name FROM sales WHERE product_name='Real but outside bounded list'"
    assert validate_literals(sql,md,domains,Tool())[1]==[]


def test_partial_pattern_domain_is_verified_in_database(md,domains):
    domains[1].update(values=['Other'],complete=False)
    class Tool:
        def execute_readonly_sql(self,statement,**kwargs):
            assert 'ILIKE' in statement and kwargs['purpose']=='entity_lookup'
            return {'rows':[['Nếp cái hoa vàng']]}
    sql="SELECT product_name FROM sales WHERE product_name ILIKE '%hoa vàng%'"
    assert validate_literals(sql,md,domains,Tool())[1]==[]


def test_domain_lookup_array_sql_is_allowed(md):
    sql="SELECT 'sales' AS table_name, 'status' AS column_name, ARRAY(SELECT DISTINCT status FROM sales WHERE status IS NOT NULL AND _is_summary_row=FALSE ORDER BY status LIMIT 2049) AS domain_values"
    assert 'ARRAY' in execution_guard(sql,md)


@pytest.mark.parametrize('value,expected',[
    (datetime(2026,7,30,0,0,tzinfo=timezone.utc),'2026-07-30'),
    (datetime(2026,7,29,20,0,tzinfo=timezone.utc),'2026-07-30'),
    (datetime(2026,7,30,0,0),'2026-07-30'),
])
def test_date_output_converts_datetime_in_business_timezone(value,expected):
    p={'answer_type':'date','output_columns':['day'],'_timezone':'Asia/Ho_Chi_Minh'}
    assert serialize_result({'id':'q'},p,{'columns':['day'],'rows':[[value]]})['answer']==expected


def test_non_date_output_keeps_timestamp():
    p={'answer_type':'string','output_columns':['day']}
    value=datetime(2026,7,30)
    assert serialize_result({'id':'q'},p,{'columns':['day'],'rows':[[value]]})['answer']==value.isoformat()


def test_direct_pipeline_supplies_db_values_and_caches_domains(md,domains):
    reads=[]
    class Tool:
        def __init__(self,md,db,guard,max_queries):self.trace=[]
        def text_domains(self,columns):reads.append(columns);return domains
        def execute_readonly_sql(self,sql,**kw):
            self.trace.append({'sql':sql});return {'columns':['product_name'],'rows':[['Nếp cái hoa vàng']]}
    client=Client("SELECT product_name FROM sales WHERE product_name='nep cai hoa vang'",'string')
    pipeline=QAPipeline(client,{}, {'mode':'direct_sql','max_repairs':0,
        'calendar':{'calendar_rule':'MONDAY_TO_SUNDAY','timezone':'Asia/Ho_Chi_Minh'}},Tool)
    q={'id':'q','business_id':'biz_test','question':'nep cai hoa vang','as_of_date':'2026-07-30'}
    for _ in range(2):
        final,log=pipeline.answer(q,md)
        assert final['answer']=='Nếp cái hoa vàng',log
        assert log['entity_literal_adjustments'][0]['canonical_value']=='Nếp cái hoa vàng'
        assert 'Đã giao' in dumps(client.payloads[-1]['entity_catalog'])
    assert len(reads)==1 and len(client.calls)==4
