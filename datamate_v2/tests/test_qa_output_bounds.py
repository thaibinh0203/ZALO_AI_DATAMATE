import copy
import jsonschema
import pytest
from sqlglot import exp
from datamate_v2.common import PipelineError
from datamate_v2.qa_pipeline.sql_validation.ast_plan import normalize_scalar_alias,project_answer
from datamate_v2.qa_pipeline.sql_validation.guard import parse_readonly,validate_sql
from datamate_v2.qa_pipeline.planning_sql.planner import planning_schema,deduplicate_quality_checks


@pytest.mark.parametrize('target',['order_count','COUNT(DISTINCT order_id)','COUNT(DISTINCT order_id) AS order_count'])
def test_sole_aggregate_alias_preserves_expression_and_grouping(target):
    sql='SELECT COUNT(DISTINCT order_id) FROM sales GROUP BY order_id ORDER BY order_id LIMIT 2'
    plan={'answer_type':'number','output_columns':[target]}
    changed,adjustments=normalize_scalar_alias(sql,plan)
    before=parse_readonly(sql);after=parse_readonly(changed)
    assert after.expressions[0].this==before.expressions[0]
    for key in ('group','order','limit','from'):assert after.args[key]==before.args[key]
    assert plan['output_columns']==['_dm_answer_value'] and adjustments
    assert normalize_scalar_alias(changed,plan)==(changed,[])


def test_alias_does_not_change_metric_or_distinct():
    for expression in ['COUNT(order_id)','COUNT(DISTINCT order_id)','SUM(line_total)']:
        sql='SELECT '+expression+' FROM sales'
        plan={'answer_type':'number','output_columns':['answer']}
        changed,_=normalize_scalar_alias(sql,plan)
        assert parse_readonly(changed).expressions[0].this==parse_readonly(sql).expressions[0]


def test_scalar_alias_survives_qualify(metadata):
    plan={'answer_type':'number','output_columns':['order_count']}
    changed,_=normalize_scalar_alias('SELECT COUNT(DISTINCT order_id) FROM sales',plan)
    qualified=validate_sql(changed,metadata)
    projected,changes=project_answer(qualified,plan)
    assert parse_readonly(projected).expressions[0].alias_or_name==plan['output_columns'][0]
    assert not changes


def test_alias_does_not_guess_another_selected_column():
    for sql,kind,targets in [
        ('SELECT SUM(quantity) AS actual FROM sales','number',['wrong']),
        ('SELECT order_id FROM sales','string',['line_total']),
        ('SELECT order_id, SUM(quantity) AS total FROM sales GROUP BY order_id','number',['missing']),
        ('SELECT SUM(quantity) FROM sales','list',['SUM(quantity)']),
    ]:
        plan={'answer_type':kind,'output_columns':targets.copy()}
        assert normalize_scalar_alias(sql,plan)==(sql,[])
        assert plan['output_columns']==targets


def test_alias_name_does_not_shadow_order_by_column():
    plan={'answer_type':'number','output_columns':['answer']}
    sql='SELECT SUM(quantity) FROM sales GROUP BY _dm_answer_value ORDER BY _dm_answer_value'
    changed,_=normalize_scalar_alias(sql,plan)
    assert plan['output_columns']==['_dm_answer_value_1']
    assert parse_readonly(changed).args['order']==parse_readonly(sql).args['order']


@pytest.mark.parametrize('field,value',[
    ('reason','x'*301),('calculation_plan',['x']*7),('calculation_plan',['x'*201]),
    ('quality_checks',[{'sql':'SELECT 1','expected':'positive','reason':'x'}]*4),
    ('quality_checks',[{'sql':'SELECT 1','expected':'positive','reason':'x'*161}]),
    ('output_columns',['COUNT(order_id) AS order_count']),
])
def test_planning_contract_rejects_unbounded_explanation(plan,field,value):
    plan=copy.deepcopy(plan);plan.pop('_validated_windows');plan[field]=value
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(plan,planning_schema({}))


def test_compact_plan_validates(plan):
    plan=copy.deepcopy(plan);plan.pop('_validated_windows')
    jsonschema.validate(plan,planning_schema({}))


def test_quality_check_dedup_keeps_distinct_assertions():
    checks=[{'sql':'select count(*) from sales','expected':'positive','reason':'a'},
            {'sql':'SELECT COUNT(*) FROM sales','expected':'positive','reason':'duplicate'},
            {'sql':'SELECT COUNT(*) FROM sales','expected':'zero','reason':'different assertion'}]
    result,removed=deduplicate_quality_checks(checks)
    assert result==[checks[0],checks[2]] and removed==[1]
