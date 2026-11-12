from decimal import Decimal
import pytest
from datamate_v2.common import PipelineError,loads,dumps
from datamate_v2.qa_pipeline.result_validation.validator import serialize_result,verify_check
from datamate_v2.evaluation.matching import score_answer
from datamate_v2.evaluation.metrics import aggregate_logs

def test_zero_is_valid_answer(question,plan):
    assert serialize_result(question,plan,{'columns':['total'],'rows':[[0]]})['answer']==0

@pytest.mark.parametrize('rows',[[[None]],[],[[1],[2]]])
def test_scalar_missing_not_zero(rows,question,plan):
    with pytest.raises(PipelineError): serialize_result(question,plan,{'columns':['total'],'rows':rows})

def test_vnd_rounding_half_up(question,plan):
    plan['unit']='VND'
    assert serialize_result(question,plan,{'columns':['total'],'rows':[[Decimal('2.5')]]})['answer']==3

def test_percent_is_not_fraction(question,plan):
    plan['answer_type']='percent'
    assert serialize_result(question,plan,{'columns':['total'],'rows':[[Decimal('12.345')]]})['answer']==Decimal('12.35')

def test_multi_number_list(question,plan):
    plan.update(answer_type='list',list_mode='row',output_columns=['large','small'])
    assert serialize_result(question,plan,{'columns':['large','small'],'rows':[[2,3]]})['answer']==[2,3]

def test_invalid_quality_result():
    with pytest.raises(PipelineError): verify_check({'rows':[[1]]},'zero')

@pytest.mark.parametrize('reference,prediction,score',[
 ({'type':'number','answer':10},{'type':'number','answer':10},1),
 ({'type':'number','answer':10},{'type':'number','answer':'10'},-1),
 ({'type':'string','answer':'Cà phê'},{'type':'string','answer':' CÀ  PHÊ! '},1),
 ({'type':'string','answer':'Cà phê'},{'type':'string','answer':'Ca phe'},-1),
 ({'type':'unanswerable'},{'type':'abstain'},0),
 ({'type':'unanswerable'},{'type':'unanswerable'},1),
 ({'type':'list','answer':[1,2]},{'type':'list','answer':[2,1]},-1)])
def test_local_proxy(reference,prediction,score): assert score_answer(reference,prediction)['score']==score

def test_exact_json_no_float_roundtrip_loss():
    assert loads(dumps({'a':Decimal('9007199254740993.25')}))['a']==Decimal('9007199254740993.25')

def test_no_fake_latency_or_tokens():
    m=aggregate_logs([])
    assert m['latency_ms']['p95'] is None and m['sql_execution_success']['value'] is None
    assert m['semantic_error_rate']['value'] is None


def test_table_alias_row_matching():
    reference={'type':'table','answer':{'columns':['name','value'],'rows':[['Cà phê',2],['Trà',3]]},'alias':[['Coffee'],['Tea']]}
    prediction={'type':'table','answer':{'columns':['value','name'],'rows':[[3,'Tea'],[2,'Coffee']]}}
    assert score_answer(reference,prediction)['score']==1
