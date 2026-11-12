from datamate_v2.contracts import PLAN_SCHEMA,VALUE_TYPES
from datamate_v2.common import PipelineError,dumps
from datamate_v2.inference.structured_output import parse_output
from copy import deepcopy
from .feedback import repair_feedback
from datamate_v2.qa_pipeline.sql_validation.guard import parse_readonly
from .business_schema import structured_schema


def deduplicate_quality_checks(checks):
    unique=[];seen=set();removed=[]
    for index,check in enumerate(checks):
        try:
            canonical=parse_readonly(check['sql']).sql(dialect='postgres',comments=False)
        except PipelineError:
            canonical=check['sql']  # Keep invalid SQL for the ordinary guard.
        key=(canonical,check['expected'])
        if key in seen:
            removed.append(index)
        else:
            seen.add(key);unique.append(check)
    return unique,removed


def planning_schema(payload):
    schema=deepcopy(PLAN_SCHEMA)
    for branch in schema['oneOf']:
        if branch['properties']['status']['enum']!=['answerable']:continue
        properties=branch['properties']
        context=payload.get('business_context',{})
        if context.get('tables') and context.get('schema_groups'):
            groups={}
            for table in context['tables']:
                groups.setdefault(table['schema_group'],[]).append(table['sql_table'])
            tables=[t['sql_table'] for t in context['tables']]
            properties['required_tables']['items']={'enum':tables}
            properties['required_tables']['uniqueItems']=True
            for field,dates_only in [('column_usage',False),('time_bindings',True)]:
                variants=[]
                for group_id,names in groups.items():
                    columns=[c['sql_column'] for c in context['schema_groups'][group_id]['columns']
                             if not dates_only or c['type'] in ('DATE','TIMESTAMPTZ')]
                    if columns: variants.append({'properties':{'table':{'enum':names},'column':{'enum':columns}}})
                item=properties[field]['items']
                item['additionalProperties']=False
                if field=='column_usage': item['properties']['source_quote']={'type':'string','maxLength':800}
                if variants:
                    item['oneOf']=variants
                else:
                    properties[field]['maxItems']=0
        if 'validated_windows' in payload:
            names=[w['name'] for w in payload['validated_windows']]
            if names:
                properties['time_bindings']['items']['properties']['window']={'enum':names}
                if properties['time_bindings'].get('maxItems')!=0: properties['time_bindings']['minItems']=len(names)
                properties['temporal_override']['properties']['time_windows']['items']['properties']['name']={'enum':names}
                properties['temporal_override']['properties']['time_windows'].update(minItems=len(names),maxItems=len(names))
            else:
                properties['time_bindings']['maxItems']=0
                properties.pop('temporal_override',None)
    if payload.get('rule_selection_from_call1'):
        for branch in schema['oneOf']:
            if branch['properties']['status']['enum']==['answerable']:
                branch['properties'].pop('temporal_override',None)
    if payload.get('structured_planning'):
        schema=structured_schema(schema,selected_by_call1=payload.get('rule_selection_from_call1',False))
        context=payload.get('business_context',{})
        if context.get('tables'):
            tables=[t['sql_table'] for t in context['tables']]
            compiled=schema['$defs']['CompiledBusinessPlan']['properties']
            compiled['sources']['items']={'enum':tables}
            compiled['lookups']['items']['properties']['table']={'enum':tables}
            if payload.get('rule_selection_from_call1'):
                # Date range authority is time_bindings, not another DSL filter.
                ordinary=sorted({c['sql_column'] for group in context.get('schema_groups',{}).values()
                    for c in group['columns'] if c['type'] not in ('DATE','TIMESTAMPTZ')})
                if ordinary:compiled['filters']['items']['properties']['column']={'enum':ordinary}
                else:compiled['filters']['maxItems']=0
        if payload.get('rule_selection_from_call1'):
            bases=[r['id'] for r in payload.get('selected_rules',[])]+['question','facts']
            def restrict(node):
                if isinstance(node,dict):
                    if 'basis' in node.get('properties',{}):node['properties']['basis']={'enum':bases}
                    for item in node.values():restrict(item)
                elif isinstance(node,list):
                    for item in node:restrict(item)
            restrict(schema['$defs']['CompiledBusinessPlan'])
    return schema

def generate_plan(payload,client,*,repair=False):
    schema=planning_schema(payload)
    response=client.call('planning_sql',payload,schema,repair=repair)
    try:
        plan=parse_output(dumps(response),schema)
    except PipelineError as error:
        details=repair_feedback(response if isinstance(response,dict) else {},payload.get('business_context',{}),payload.get('validated_windows',[]))
        failure=PipelineError('Invalid planning contract. '+ '; '.join(details['catalog_errors'])+' '+str(error))
        failure.plan=response if isinstance(response,dict) else None
        raise failure from error
    if plan['status']!='answerable':
        if plan['sql'] is not None: raise PipelineError('Unanswerable/abstain plan must have sql=null')
        if not plan['reason'].strip(): raise PipelineError('Non-answerable requires evidence/reason')
    else:
        plan.setdefault('column_usage',[])
        for field in ('sql','answer_type','output_columns','required_tables','summary_policy','quality_checks','null_policy'):
            if field not in plan: raise PipelineError(f'Answerable plan missing {field}')
        compiled=plan.get('business_plan',{}).get('mode')=='compiled'
        if (not compiled and not plan['sql']) or plan['answer_type'] not in VALUE_TYPES: raise PipelineError('Invalid answerable plan')
        plan['quality_checks'],removed=deduplicate_quality_checks(plan['quality_checks'])
        if removed: plan['_quality_check_adjustments']={'action':'deduplicate','removed_indices':removed}
    if payload.get('rule_selection_from_call1'):
        plan['selected_rules']=deepcopy(payload.get('selected_rules',[]))
        plan['_rule_selection_from_call1']=True
    return plan
