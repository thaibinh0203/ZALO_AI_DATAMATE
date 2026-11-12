"""Bounded relational plan. No business formulas are inferred by this schema."""
from copy import deepcopy

IDENT={'type':'string','pattern':'^[A-Za-z_][A-Za-z0-9_]{0,79}$'}
BASIS={'enum':['r1','r2','r3','question','facts']}
FIELD={'type':'object','additionalProperties':False,'required':['relation','column'],
       'properties':{'relation':{'enum':['source','lookup']},'column':deepcopy(IDENT)}}
VALUE={'type':['string','number','boolean'],'maxLength':300}
FILTER={'type':'object','additionalProperties':False,'required':['relation','column','operator','values','basis'],
        'properties':{**deepcopy(FIELD['properties']),'operator':{'enum':['eq','ne','in','not_in','gt','gte','lt','lte','is_null','is_not_null']},
                      'values':{'type':'array','maxItems':32,'items':VALUE},'basis':BASIS}}
FILTER['oneOf']=[{'properties':{'operator':{'enum':operators},'values':{'type':'array','minItems':low,'maxItems':high,'items':deepcopy(VALUE)}}}
    for operators,low,high in [(['eq','ne','gt','gte','lt','lte'],1,1),(['in','not_in'],1,32),(['is_null','is_not_null'],0,0)]]
RULES={'type':'array','minItems':1,'maxItems':3,'items':{'type':'object','additionalProperties':False,
       'required':['id','source','quote','application','applies_to'],'properties':{'id':{'enum':['r1','r2','r3']},
       'source':{'enum':['rulebook','readme']},'quote':{'type':'string','minLength':1,'maxLength':240},
       'applies_to':{'enum':['filters','calculation','ranking','other']},
       'application':{'type':'string','minLength':1,'maxLength':160}}}}
GROUP={'type':'object','additionalProperties':False,'required':['relation','column','alias','basis'],
       'properties':{**deepcopy(FIELD['properties']),'alias':deepcopy(IDENT),'basis':BASIS}}
AGGREGATE={'type':'object','additionalProperties':False,'required':['function','columns','alias','basis'],
           'properties':{'function':{'enum':['SUM','COUNT','COUNT_DISTINCT','AVG','MIN','MAX','ROWS']},
           'columns':{'type':'array','maxItems':2,'items':FIELD},'alias':deepcopy(IDENT),'basis':BASIS}}
AGGREGATE['oneOf']=[{'properties':{'function':{'enum':functions},'columns':{'type':'array','minItems':low,'maxItems':high,'items':deepcopy(FIELD)}}}
    for functions,low,high in [(['SUM','COUNT','AVG','MIN','MAX'],1,1),(['COUNT_DISTINCT'],1,2),(['ROWS'],0,0)]]
BUSINESS_PLAN={'type':'object','additionalProperties':False,
 'required':['mode','metric','sources','lookups','filters','group_by','aggregates','having','order_by','limit','result_mode'],
 'properties':{'mode':{'enum':['compiled']},'metric':{'type':'string','minLength':1,'maxLength':100},
  'sources':{'type':'array','minItems':1,'maxItems':24,'uniqueItems':True,'items':deepcopy(IDENT)},
  'lookups':{'type':'array','maxItems':1,'items':{'type':'object','additionalProperties':False,
    'required':['table','source_column','lookup_column'],'properties':{'table':deepcopy(IDENT),'source_column':deepcopy(IDENT),'lookup_column':deepcopy(IDENT)}}},
  'filters':{'type':'array','maxItems':12,'items':FILTER},
  'group_by':{'type':'array','maxItems':3,'items':GROUP},
  'aggregates':{'type':'array','minItems':1,'maxItems':4,'items':AGGREGATE},
  'having':{'type':'array','maxItems':3,'items':{'type':'object','additionalProperties':False,
    'required':['aggregate','operator','value','basis'],'properties':{'aggregate':deepcopy(IDENT),
    'operator':{'enum':['eq','ne','gt','gte','lt','lte']},'value':{'type':'number'},'basis':BASIS}}},
  'order_by':{'type':'array','maxItems':3,'items':{'type':'object','additionalProperties':False,
    'required':['column','direction'],'properties':{'column':deepcopy(IDENT),'direction':{'enum':['ASC','DESC']}}}},
  'limit':{'type':['integer','null'],'minimum':1,'maximum':1000},
  'result_mode':{'enum':['rows','count_groups']}}}
RAW_PLAN={'type':'object','additionalProperties':False,'required':['mode','fallback_reason'],
          'properties':{'mode':{'enum':['raw_sql']},'fallback_reason':{'type':'string','minLength':1,'maxLength':160}}}


def structured_schema(legacy,*,selected_by_call1=False):
    schema=deepcopy(legacy);template=schema['oneOf'][0];branches=[]
    definitions={'CompiledBusinessPlan':deepcopy(BUSINESS_PLAN),'RawBusinessPlan':deepcopy(RAW_PLAN),
                 'ApplicableRules':deepcopy(RULES)}
    for key,value in template['properties'].items():
        if key not in ('status','sql'):definitions['Shared_'+key]=deepcopy(value)
    for mode in ('compiled','raw_sql'):
        answerable=deepcopy(template)
        props={'status':deepcopy(template['properties']['status'])}
        if not selected_by_call1:props['applicable_rules']={'$ref':'#/$defs/ApplicableRules'}
        props['business_plan']={'$ref':'#/$defs/'+('CompiledBusinessPlan' if mode=='compiled' else 'RawBusinessPlan')}
        for key in template['properties']:
            if key not in ('status','sql'):props[key]={'$ref':'#/$defs/Shared_'+key}
            elif key=='sql':props[key]={'type':'null'} if mode=='compiled' else {'type':'string','minLength':1,'maxLength':12000}
        if mode=='compiled':props['quality_checks']={'type':'array','maxItems':0,'items':deepcopy(template['properties']['quality_checks']['items'])}
        answerable['properties']=props
        answerable['required']+=['business_plan']+([] if selected_by_call1 else ['applicable_rules'])
        branches.append(answerable)
    schema['oneOf']=branches+[schema['oneOf'][1]]
    schema['$defs']=definitions
    if selected_by_call1:definitions.pop('ApplicableRules',None)
    return schema
