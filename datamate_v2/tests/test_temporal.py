import pytest
from datamate_v2.qa_pipeline.temporal_validator import validate_windows
from datamate_v2.contracts import validate_question
from datamate_v2.common import PipelineError

@pytest.mark.parametrize('kind,params,start,end',[
 ('today',{},'2026-03-04','2026-03-05'),('yesterday',{},'2026-03-03','2026-03-04'),
 ('previous_calendar_week',{},'2026-02-23','2026-03-02'),('current_calendar_week',{},'2026-03-02','2026-03-05'),
 ('previous_calendar_month',{},'2026-02-01','2026-03-01'),('current_calendar_month',{},'2026-03-01','2026-03-05'),
 ('previous_calendar_quarter',{},'2025-10-01','2026-01-01'),('previous_calendar_year',{},'2025-01-01','2026-01-01'),
 ('rolling_days',{'n':7},'2026-02-26','2026-03-05'),('absolute_month',{'year':2024,'month':2},'2024-02-01','2024-03-01'),
 ('month_of_year',{'month':10},'2025-10-01','2025-11-01'),('as_of',{},None,'2026-03-05'),
 ('absolute_range',{'start_date':'2024-02-28','end_date_inclusive':'2024-02-29'},'2024-02-28','2024-03-01'),
 ('previous_month_same_elapsed_days',{},'2026-02-01','2026-02-05')])
def test_calendar(kind,params,start,end):
    result=validate_windows([{'name':'a','interpretation':kind,'parameters':params,'status':'proposed','start_date':'1900-01-01'}],'2026-03-04')[0]
    assert (result['start_date'],result['end_date_exclusive'])==(start,end)
    assert result['corrected'] and result['status']=='validated'

def test_year_boundary():
    w=validate_windows([{'name':'a','interpretation':'previous_calendar_week','status':'proposed'}],'2026-01-01')[0]
    assert (w['start_date'],w['end_date_exclusive'])==('2025-12-22','2025-12-29')

def test_previous_month_clamps_to_leap_day():
    w=validate_windows([{'name':'a','interpretation':'previous_month_same_elapsed_days','status':'proposed'}],'2024-03-31')[0]
    assert w['end_date_exclusive']=='2024-03-01'

def test_multi_window():
    ws=validate_windows([{'name':'oct','interpretation':'absolute_month','parameters':{'year':2025,'month':10},'status':'proposed'},
                         {'name':'sep','interpretation':'absolute_month','parameters':{'year':2025,'month':9},'status':'proposed'}],'2026-03-04')
    assert ws[0]['start_date']=='2025-10-01' and ws[1]['end_date_exclusive']=='2025-10-01'

def test_lunar_calendar_only_from_source():
    data='date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n2026-02-10,2,23,12,2025,false,\n2026-02-26,4,10,1,2026,false,\n'
    result=validate_windows([{'name':'tet','interpretation':'tet','parameters':{'year':2026},'status':'proposed'}],'2026-03-04',calendar_csv=data)[0]
    assert (result['start_date'],result['end_date_exclusive'])==('2026-02-10','2026-02-27')

@pytest.mark.parametrize('w',[{'name':'x','interpretation':'yesterday','status':'ambiguous'},
 {'name':'x','interpretation':'unsupported_calendar','status':'proposed'},
 {'name':'x','interpretation':'rolling_days','parameters':{'n':0},'status':'proposed'},
 {'name':'x','interpretation':'yesterday','status':'proposed','reference_date':'2025-01-01'},
 {'name':'x','interpretation':'absolute_day','parameters':{'date':'2025-02-29'},'status':'proposed'}])
def test_invalid_time(w):
    with pytest.raises((PipelineError,ValueError)): validate_windows([w],'2026-03-04')

@pytest.mark.parametrize('field,value',[('as_of_date',None),('as_of_date','2026-02-30'),('as_of_date','2026-3-4'),('business_id','../other'),('question','')])
def test_input_contract(question,field,value):
    question[field]=value
    with pytest.raises((ValueError,TypeError)): validate_question(question)
