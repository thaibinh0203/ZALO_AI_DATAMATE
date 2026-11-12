from copy import deepcopy
from datamate_v2.common import PipelineError,fold

TOPICS=['row_grain','order_count','money_fields','quantity_and_units','join_safety','uncertain_mapping']
FACTS=['row_count','distinct_order_ids','multiple_rows_per_order','date_coverage','observed_domains','mapping_issues','quality','relationships','source_grain_quotes']
EVIDENCE={'oneOf':[
 {'type':'object','additionalProperties':False,'required':['source'],'properties':{'source':{'enum':['readme']}}},
 {'type':'object','additionalProperties':False,'required':['source','field'],'properties':{'source':{'enum':['metadata']},'field':{'enum':FACTS}}}]}
SCHEMA_SCHEMA={'type':'object','additionalProperties':False,
 'required':['description','grain','columns','usage_rules','data_quality_notes','assumptions'],
 'properties':{
 'description':{'type':'string','minLength':10,'maxLength':800},'grain':{'type':'string','minLength':10,'maxLength':500},
 'columns':{'type':'array','items':{'type':'object','additionalProperties':False,'required':['sql_column','description','unit','cautions'],
  'properties':{'sql_column':{'type':'string'},'description':{'type':'string','minLength':5,'maxLength':350},
  'unit':{'type':['string','null'],'maxLength':80},'cautions':{'type':'array','maxItems':4,'items':{'type':'string','maxLength':250}}}}},
 'usage_rules':{'type':'array','maxItems':9,'items':{'type':'object','additionalProperties':False,'required':['topic','status','explanation','evidence'],
  'properties':{'topic':{'enum':TOPICS},'status':{'enum':['confirmed','unresolved']},
    'explanation':{'type':'string','minLength':10,'maxLength':600},'evidence':{'type':'array','maxItems':3,'items':EVIDENCE}}}},
 'data_quality_notes':{'type':'array','minItems':1,'maxItems':10,'items':{'type':'string','minLength':5,'maxLength':350}},
 'assumptions':{'type':'array','maxItems':8,'items':{'type':'string','maxLength':350}}}}


def required_topics(table):
    meanings={c.get('meaning') for c in table['columns']}
    topics={'row_grain','join_safety'}
    if 'order_id' in meanings: topics.add('order_count')
    if meanings & {'unit_price','line_total','amount_paid','revenue','amount','unit_cost'}: topics.add('money_fields')
    if meanings & {'quantity','base_quantity','unit'}: topics.add('quantity_and_units')
    if any(c['semantic_status']!='verified' for c in table['columns']): topics.add('uncertain_mapping')
    return topics


def description_schema(table):
    schema=deepcopy(SCHEMA_SCHEMA)
    names=[c['sql_column'] for c in table['columns']]
    schema['properties']['columns'].update(minItems=len(names),maxItems=len(names))
    schema['properties']['columns']['items']['properties']['sql_column']={'enum':names}
    variants=[]
    for text in (True,False):
        subset=[col['sql_column'] for col in table['columns'] if (col['type']=='TEXT')==text]
        if subset:
            variants.append({'properties':{'sql_column':{'enum':subset},
                'unit':{'type':'null'} if text else {'type':['string','null'],'maxLength':80}}})
    schema['properties']['columns']['items']['oneOf']=variants
    topics=sorted(required_topics(table))
    schema['properties']['usage_rules']['minItems']=len(topics)
    schema['properties']['usage_rules']['items']['properties']['topic']={'enum':topics}
    return schema


def validate_description(description,table,context):
    names=[c['sql_column'] for c in description['columns']]
    if len(set(names))!=len(names) or set(names)!={c['sql_column'] for c in table['columns']}:
        raise PipelineError('Schema description must cover exactly every actual column')
    topics=[r['topic'] for r in description.get('usage_rules',[])]
    if len(set(topics))!=len(topics) or set(topics)!=required_topics(table):
        raise PipelineError('Schema description must explain exactly all required usage-rule topics')
    for rule in description['usage_rules']:
        if rule['status']=='confirmed' and not rule['evidence']: raise PipelineError('Confirmed usage rule needs evidence')
        for evidence in rule['evidence']:
            if evidence['source']=='metadata':
                if evidence['field'] not in table.get('verified_facts',{}): raise PipelineError('Unknown metadata evidence field')
            elif not context.get(evidence['source'],'').strip():
                raise PipelineError('Description references an unavailable source')
    for rendered in description['columns']:
        col=next(c for c in table['columns'] if c['sql_column']==rendered['sql_column'])
        if col['type']=='TEXT' and rendered['unit'] not in (None,''):
            raise PipelineError('Text/code/name/status columns have no physical measurement unit; use null')
        if rendered['unit'] and fold(rendered['unit']) in {'so luong','so tien','ten hang','ma don','trang thai','don vi'}:
            raise PipelineError('Generic labels are not measurement units; use an actual unit or null')
        if col['semantic_status']!='verified' and not rendered['cautions']:
            raise PipelineError('Uncertain mapping must retain cautions')
    grain=fold(description['grain'])
    if table.get('verified_facts',{}).get('multiple_rows_per_order') and any(x in grain for x in ('moi dong la mot don hang','moi dong tuong ung mot don hang')):
        raise PipelineError('Grain contradicts observed multiple rows per order')
    return description
