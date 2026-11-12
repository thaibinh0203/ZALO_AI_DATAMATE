from datetime import date
import re
from .common import PipelineError
from .qa_pipeline.temporal_contract import WINDOW_SCHEMA

VALUE_TYPES = ["number","percent","string","date","boolean","list","table"]

ENTITY_FIELDS=['metrics','products','categories','customers','customer_groups','employees','branches','locations','suppliers',
 'orders','invoices','statuses','payment_methods','channels','units','operations','filters','grouping','comparison_targets','ranking','sorting','limits','other_entities']
ENTITY_SCHEMA={'type':'object','additionalProperties':False,'properties':{name:{'type':'array','maxItems':16,
    'items':{'type':'string','minLength':1,'maxLength':300}} for name in ENTITY_FIELDS}}


def normalize_entities(entities):
    """Lossless compatibility at boundaries; never iterate an entity string."""
    if not isinstance(entities,dict): raise PipelineError('entities must be an object')
    result={}
    for key,value in entities.items():
        if key not in ENTITY_FIELDS: raise PipelineError(f'Unknown entity field: {key}')
        if isinstance(value,str): value=[value] if value.strip() else []
        if not isinstance(value,list) or any(not isinstance(item,str) for item in value):
            raise PipelineError(f'entities.{key} must be an array of strings')
        result[key]=list(dict.fromkeys(item.strip() for item in value if item.strip()))
    return result

def validate_question(q):
    if not isinstance(q,dict): raise PipelineError("Question must be an object")
    for key in ("id","business_id","question","as_of_date"):
        if not isinstance(q.get(key),str) or not q[key].strip(): raise PipelineError(f"Missing/invalid {key}")
    if not re.fullmatch(r"[A-Za-z0-9_-]+",q["business_id"]): raise PipelineError("Unsafe business_id")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}",q["as_of_date"]): raise PipelineError("as_of_date must be YYYY-MM-DD")
    date.fromisoformat(q["as_of_date"])
    return q

REWRITE_SCHEMA={
 "type":"object","additionalProperties":False,
 "required":["id","business_id","original_question","rewritten_question","entities","time_windows","ambiguities"],
 "properties":{
  "id":{"type":"string"},"business_id":{"type":"string"},"original_question":{"type":"string"},
  "rewritten_question":{"type":"string","minLength":1,"maxLength":1200},"entities":ENTITY_SCHEMA,
  "sub_questions":{"type":"array","maxItems":12,"items":{"type":"string","maxLength":500}},
  "time_windows":{"type":"array","maxItems":12,"items":WINDOW_SCHEMA},
  "ambiguities":{"type":"array","maxItems":12,"items":{"type":"string","maxLength":400}}}}

PLAN_SCHEMA={"type":"object","required":["status","calculation_plan","sql","reason"],"properties":{
 "status":{"enum":["answerable","unanswerable","abstain"]},
 "calculation_plan":{"type":"array","items":{"type":"string"}},
 "sql":{"type":["string","null"]},"reason":{"type":"string"},
 "answer_type":{"enum":VALUE_TYPES},"output_columns":{"type":"array","items":{"type":"string"}},
 "required_tables":{"type":"array","items":{"type":"string"}},
 "column_usage":{"type":"array","items":{"type":"object","required":["table","column","purpose"],"properties":{
  "table":{"type":"string"},"column":{"type":"string"},"purpose":{"type":"string"}}}},
 "time_bindings":{"type":"array","items":{"type":"object","required":["table","column","window"],"properties":{
  "table":{"type":"string"},"column":{"type":"string"},"window":{"type":"string"}}}},
 "quality_checks":{"type":"array","items":{"type":"object","required":["sql","expected","reason"],"properties":{
  "sql":{"type":"string"},"expected":{"enum":["zero","positive","true"]},"reason":{"type":"string"}}}},
 "summary_policy":{"enum":["exclude","include_explicit"]},"summary_evidence":{"type":"string"},
 "null_policy":{"enum":["abstain","zero_with_evidence"]},"null_evidence":{"type":"string"},
 "unit":{"type":"string"},"round_digits":{"type":["integer","null"],"minimum":0,"maximum":8}}}

# Constrain complete plans at generation time, rather than discovering missing
# answerable fields only after an unconstrained model response.
from copy import deepcopy as _deepcopy
_plan_properties=_deepcopy(PLAN_SCHEMA['properties'])
_plan_properties.update({
 'list_mode':{'enum':['rows','row']},
 'temporal_override':{'type':'object','additionalProperties':False,'required':['source_quote','time_windows'],
   'properties':{'source_quote':{'type':'string','minLength':1,'maxLength':800},'time_windows':{'type':'array','maxItems':12,'items':WINDOW_SCHEMA}}}})
_answerable_properties=_deepcopy(_plan_properties)
_answerable_properties['status']={'enum':['answerable']}
_answerable_properties['calculation_plan'].update(minItems=1,maxItems=6)
_answerable_properties['calculation_plan']['items']['maxLength']=200
_answerable_properties['reason']['maxLength']=300
_answerable_properties['quality_checks']['items']['additionalProperties']=False
_answerable_properties['quality_checks']['items']['properties']['reason']['maxLength']=160
_answerable_properties['quality_checks']['items']['properties']['sql']['maxLength']=4000
_answerable_properties['output_columns']['items']={'type':'string',
    'pattern':'^[A-Za-z_][A-Za-z0-9_]{0,119}$'}
for _name,_max in [('column_usage',120),('required_tables',40),('time_bindings',30),('quality_checks',3),('output_columns',40)]:
    _answerable_properties[_name]['maxItems']=_max
_answerable_properties['unit']['maxLength']=80
_answerable_properties['sql']={'type':'string','minLength':1,'maxLength':12000}
_answerable={'type':'object','additionalProperties':False,'properties':_answerable_properties,
 'required':['status','calculation_plan','sql','reason','answer_type','output_columns','required_tables','time_bindings','summary_policy','quality_checks','null_policy']}
_answerable['oneOf']=[{'properties':{'answer_type':{'enum':['number','percent','string','date','boolean']},'output_columns':{'maxItems':1,'minItems':1}}},
                     {'properties':{'answer_type':{'enum':['list','table']},'output_columns':{'minItems':1}}}]
_nonanswerable={'type':'object','additionalProperties':False,
 'required':['status','calculation_plan','sql','reason'],
 'properties':{'status':{'enum':['unanswerable','abstain']},'calculation_plan':{'type':'array','maxItems':6,'items':{'type':'string','maxLength':200}},
               'sql':{'type':'null'},'reason':{'type':'string','minLength':1,'maxLength':300}}}
PLAN_SCHEMA={'oneOf':[_answerable,_nonanswerable]}
