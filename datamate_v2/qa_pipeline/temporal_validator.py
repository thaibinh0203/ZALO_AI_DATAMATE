"""Pure calendar arithmetic over Call 1 interpretations; no language model or clock."""
from datetime import date,datetime,timedelta
from calendar import monthrange
import csv
import io
from zoneinfo import ZoneInfo
from datamate_v2.common import PipelineError
from .temporal_contract import validate_window_parameters

def shift_month(day,months):
    year,month=divmod(day.year*12+day.month-1+months,12);month+=1
    return date(year,month,min(day.day,monthrange(year,month)[1]))

def _range(window,reference,calendar_csv,validated):
    validate_window_parameters(window)
    kind=window['interpretation'];p=window.get('parameters',{})
    tomorrow=reference+timedelta(days=1)
    monday=reference-timedelta(days=reference.weekday())
    month=reference.replace(day=1);quarter=reference.replace(month=((reference.month-1)//3)*3+1,day=1)
    year=reference.replace(month=1,day=1)
    if kind=='today': return reference,tomorrow
    if kind=='yesterday': return reference-timedelta(days=1),reference
    if kind=='current_calendar_week': return monday,tomorrow
    if kind=='previous_calendar_week': return monday-timedelta(days=7),monday
    if kind=='current_calendar_month': return month,tomorrow
    if kind=='previous_calendar_month': return shift_month(month,-1),month
    if kind=='previous_month_same_elapsed_days':
        start=shift_month(month,-1);end=shift_month(reference,-1)+timedelta(days=1)
        return start,end
    if kind=='current_calendar_quarter': return quarter,tomorrow
    if kind=='previous_calendar_quarter': return shift_month(quarter,-3),quarter
    if kind=='current_calendar_year': return year,tomorrow
    if kind=='previous_calendar_year': return year.replace(year=year.year-1),year
    if kind=='rolling_days':
        n=int(p['n'])
        if n<=0 or n>36600: raise PipelineError('Invalid rolling day count')
        return reference-timedelta(days=n-1),tomorrow
    if kind=='rolling_weeks':
        n=int(p['n'])
        if n<=0 or n>5200: raise PipelineError('Invalid rolling week count')
        return reference-timedelta(days=7*n-1),tomorrow
    if kind=='rolling_months':
        n=int(p['n'])
        if n<=0 or n>1200: raise PipelineError('Invalid rolling month count')
        return shift_month(tomorrow,-n),tomorrow
    if kind=='absolute_day':
        start=date.fromisoformat(p['date']);return start,start+timedelta(days=1)
    if kind in ('absolute_month','month_of_year'):
        m=int(p['month']);y=int(p.get('year',reference.year))
        start=date(y,m,1)
        if kind=='month_of_year' and 'year' not in p and start>reference: start=start.replace(year=y-1)
        return start,shift_month(start,1)
    if kind in ('absolute_quarter','quarter_of_year'):
        q=int(p['quarter']);y=int(p.get('year',reference.year))
        if q not in (1,2,3,4): raise PipelineError('Invalid quarter')
        start=date(y,3*q-2,1)
        if kind=='quarter_of_year' and 'year' not in p and start>reference: start=start.replace(year=y-1)
        return start,shift_month(start,3)
    if kind=='absolute_year':
        y=int(p['year']);return date(y,1,1),date(y+1,1,1)
    if kind=='absolute_range':
        start=date.fromisoformat(p['start_date']);end=date.fromisoformat(p['end_date_inclusive'])+timedelta(days=1)
        return start,end
    if kind=='as_of':
        # No invented data origin: NULL lower bound explicitly means all available history.
        end=date.fromisoformat(p['date'])+timedelta(days=1) if 'date' in p else tomorrow
        return None,end
    if kind=='same_period_previous_year':
        base=validated.get(p['base_window'])
        if not base: raise PipelineError('Comparison base must be validated first')
        start=date.fromisoformat(base['start_date']) if base['start_date'] else None
        end=date.fromisoformat(base['end_date_exclusive'])
        return shift_month(start,-12) if start else None,shift_month(end,-12)
    if kind in ('tet','before_tet'):
        y=int(p['year']);records=list(csv.DictReader(io.StringIO(calendar_csv)))
        first=[r['date'] for r in records if int(r['lunar_year'])==y-1 and int(r['lunar_month'])==12 and int(r['lunar_day'])==23 and r['lunar_leap'].lower()=='false']
        last=[r['date'] for r in records if int(r['lunar_year'])==y and int(r['lunar_month'])==1 and int(r['lunar_day'])==10 and r['lunar_leap'].lower()=='false']
        if len(first)!=1 or len(last)!=1: raise PipelineError('Calendar does not uniquely support Tet window')
        start=date.fromisoformat(first[0]);end=date.fromisoformat(last[0])+timedelta(days=1)
        return (start-(end-start),start) if kind=='before_tet' else (start,end)
    raise PipelineError(f'Unverifiable temporal interpretation: {kind}')

def validate_windows(windows,as_of_date,*,calendar_csv='',calendar_rule='MONDAY_TO_SUNDAY',timezone='Asia/Ho_Chi_Minh'):
    reference=date.fromisoformat(as_of_date)
    ZoneInfo(timezone)
    validated={};out=[]
    for window in windows:
        name=window['name']
        if name in validated: raise PipelineError('Duplicate temporal window name')
        if window['status']=='ambiguous': raise PipelineError('Ambiguous time interpretation')
        if window.get('calendar_rule',calendar_rule)!=calendar_rule or calendar_rule!='MONDAY_TO_SUNDAY':
            raise PipelineError('Special business calendar requires verified implementation')
        if window.get('reference_date',as_of_date)!=as_of_date: raise PipelineError('Model changed as_of_date')
        start,end=_range(window,reference,calendar_csv,validated)
        if start is not None and start>=end: raise PipelineError('Invalid temporal range')
        entry={**window,'reference_date':as_of_date,'status':'validated','calendar_rule':calendar_rule,'timezone':timezone,
               'proposed_start_date':window.get('start_date'),'proposed_end_date_exclusive':window.get('end_date_exclusive'),
               'start_date':start.isoformat() if start else None,'end_date_exclusive':end.isoformat()}
        entry['corrected']=entry['proposed_start_date']!=entry['start_date'] or entry['proposed_end_date_exclusive']!=entry['end_date_exclusive']
        validated[name]=entry;out.append(entry)
    return out
