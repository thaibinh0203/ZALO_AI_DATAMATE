import pytest
from datamate_v2.common import PipelineError
from datamate_v2.qa_pipeline.sql_validation.guard import validate_sql,scoped_quality_queries

def test_safe_plan(metadata,plan):
    checked=validate_sql(plan['sql'],metadata,plan)
    assert 'SUM(' in checked and 'CURRENT_DATE' not in checked

@pytest.mark.parametrize('sql',[
 'DROP TABLE sales','DELETE FROM sales RETURNING quantity','SELECT 1; SELECT 2',
 'WITH changed AS (DELETE FROM sales RETURNING quantity) SELECT quantity FROM changed',
 'SELECT pg_read_file(\'/etc/passwd\')','SELECT pg_sleep(10)','SELECT * FROM sales',
 'SELECT quantity INTO stolen FROM sales','SELECT quantity FROM sales FOR UPDATE',
 'SELECT quantity FROM other.sales','SELECT quantity FROM pg_catalog.pg_authid',
 'SELECT missing_column FROM sales','SELECT now()','SELECT CURRENT_DATE',
 'SELECT quantity FROM sales WHERE EXISTS (SELECT 1 FROM other_business.sales)',
 'SELECT random()','SELECT "_raw" FROM sales','SELECT generate_series(1,10000000)'])
def test_sql_rejections(sql,metadata):
    with pytest.raises(PipelineError): validate_sql(sql,metadata)

@pytest.mark.parametrize('replacement',[
 "order_date >= DATE '2026-03-02'", "NOT (order_date >= DATE '2026-03-03')",
 "(order_date >= DATE '2026-03-03' OR 1=1)"])
def test_time_scope_and_polarity(metadata,plan,replacement):
    sql=plan['sql'].replace("order_date >= DATE '2026-03-03'",replacement)
    with pytest.raises(PipelineError): validate_sql(sql,metadata,plan)

@pytest.mark.parametrize('replacement',['TRUE','NOT (_is_summary_row = FALSE)','(_is_summary_row = FALSE OR TRUE)'])
def test_summary_scope_and_polarity(metadata,plan,replacement):
    sql=plan['sql'].replace('_is_summary_row = FALSE',replacement)
    with pytest.raises(PipelineError): validate_sql(sql,metadata,plan)

def test_missing_declared_column_use(metadata,plan):
    plan['column_usage']=[]
    with pytest.raises(PipelineError): validate_sql(plan['sql'],metadata,plan)

def test_uncertain_mapping_cannot_be_used_as_fact(metadata,plan):
    metadata['tables'][0]['columns'][2]['semantic_status']='uncertain'
    with pytest.raises(PipelineError): validate_sql(plan['sql'],metadata,plan)

def test_quality_guards_not_just_model_assertions(metadata,plan):
    metadata['tables'][0]['stats'][2]['null_count']=2
    qs=scoped_quality_queries(plan['sql'],metadata,plan)
    assert len(qs)==1 and 'IS NULL' in qs[0]['sql']
    assert qs[0]['expected']=='zero'

def test_cross_business_in_subquery(metadata):
    with pytest.raises(PipelineError): validate_sql('SELECT quantity FROM sales WHERE quantity IN (SELECT quantity FROM other.sales)',metadata)

def test_count_star_allowed(metadata):
    assert 'COUNT(*)' in validate_sql('SELECT COUNT(*) AS n FROM sales',metadata)

def test_join_without_verified_relation(metadata,plan):
    sql=plan['sql'].replace('FROM sales','FROM sales JOIN sales AS other ON sales.order_id = other.order_id')
    with pytest.raises(PipelineError): validate_sql(sql,metadata,plan)
