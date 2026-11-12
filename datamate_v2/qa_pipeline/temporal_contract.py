"""Single machine-readable contract shared by Call 1 and Python time validation."""
from copy import deepcopy
import jsonschema
from datamate_v2.common import PipelineError

DAY={'type':'string','format':'date','pattern':r'^\d{4}-\d{2}-\d{2}$'}
YEAR={'type':'integer','minimum':1900,'maximum':2200}
MONTH={'type':'integer','minimum':1,'maximum':12}
QUARTER={'type':'integer','minimum':1,'maximum':4}
EMPTY={'type':'object','properties':{},'additionalProperties':False}
PARAMETER_SCHEMAS={kind:deepcopy(EMPTY) for kind in (
 'today','yesterday','current_calendar_week','previous_calendar_week','current_calendar_month','previous_calendar_month',
 'previous_month_same_elapsed_days','current_calendar_quarter','previous_calendar_quarter','current_calendar_year','previous_calendar_year')}

def params(properties,required=()):
    return {'type':'object','properties':properties,'required':list(required),'additionalProperties':False}

PARAMETER_SCHEMAS.update({
 'rolling_days':params({'n':{'type':'integer','minimum':1,'maximum':36600}},['n']),
 'rolling_weeks':params({'n':{'type':'integer','minimum':1,'maximum':5200}},['n']),
 'rolling_months':params({'n':{'type':'integer','minimum':1,'maximum':1200}},['n']),
 'absolute_day':params({'date':DAY},['date']),
 'absolute_month':params({'year':YEAR,'month':MONTH},['year','month']),
 'month_of_year':params({'year':YEAR,'month':MONTH},['month']),
 'absolute_quarter':params({'year':YEAR,'quarter':QUARTER},['year','quarter']),
 'quarter_of_year':params({'year':YEAR,'quarter':QUARTER},['quarter']),
 'absolute_year':params({'year':YEAR},['year']),
 'absolute_range':params({'start_date':DAY,'end_date_inclusive':DAY},['start_date','end_date_inclusive']),
 'as_of':params({'date':DAY}),
 'same_period_previous_year':params({'base_window':{'type':'string','minLength':1,'maxLength':80}},['base_window']),
 'tet':params({'year':YEAR},['year']), 'before_tet':params({'year':YEAR},['year']),
 'ambiguous':deepcopy(EMPTY)})

def window_schema(kind):
    return {'type':'object','additionalProperties':False,'required':['name','interpretation','parameters','status'],
      'properties':{'name':{'type':'string','minLength':1,'maxLength':80},
       'interpretation':{'enum':[kind]},'parameters':deepcopy(PARAMETER_SCHEMAS[kind]),
       'status':{'enum':['ambiguous' if kind=='ambiguous' else 'proposed']},
       'original_expression':{'type':'string','maxLength':400},'reference_date':DAY,
       'calendar_rule':{'enum':['MONDAY_TO_SUNDAY']},
       'start_date':{'type':['string','null'],'maxLength':10},
       'end_date_exclusive':{'type':['string','null'],'maxLength':10}}}

WINDOW_SCHEMA=window_schema('today')
WINDOW_SCHEMA['properties']['interpretation']={'enum':list(PARAMETER_SCHEMAS)}
WINDOW_SCHEMA['properties']['parameters']={'type':'object'}
WINDOW_SCHEMA['properties']['status']={'enum':['proposed','ambiguous']}
WINDOW_SCHEMA['oneOf']=[{'properties':{key:window_schema(kind)['properties'][key]
                       for key in ('interpretation','parameters','status')}} for kind in PARAMETER_SCHEMAS]

def validate_window_parameters(window):
    kind=window.get('interpretation')
    if kind not in PARAMETER_SCHEMAS: raise PipelineError(f'Unsupported temporal interpretation code: {kind}')
    parameters=window.get('parameters',{})
    try: jsonschema.validate(parameters,PARAMETER_SCHEMAS[kind],format_checker=jsonschema.FormatChecker())
    except jsonschema.ValidationError as error:
        raise PipelineError(f'Invalid parameters for {kind}: {error.message}') from error
