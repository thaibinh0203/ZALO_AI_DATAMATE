from copy import deepcopy
from datamate_v2.contracts import REWRITE_SCHEMA,normalize_entities
from datamate_v2.common import dumps,PipelineError
from datamate_v2.inference.structured_output import parse_output
from .intent_guard import normalize_question,validate_rewrite_intent
from .temporal_facts import temporal_facts,constrain_time_schema,apply_temporal_facts
from .rule_selection import SELECTED_RULES,normalize_selected_rules


def rewrite_question(question,client,calendar_settings,*,repair=False,error=None,ingestion_context=None):
    schema=deepcopy(REWRITE_SCHEMA)
    for field,value in [('id',question['id']),('business_id',question['business_id']),('original_question',question['question'])]:
        schema['properties'][field]={'enum':[value]}
    facts=temporal_facts(question,calendar_settings)
    schema=constrain_time_schema(schema,facts)
    if ingestion_context is not None:
        schema['properties']['selected_rules']=deepcopy(SELECTED_RULES)
        schema['required'].append('selected_rules')
    payload={**question,'normalized_question_hint':normalize_question(question['question']),'calendar_defaults':calendar_settings}
    if facts:payload['temporal_facts']=facts
    if ingestion_context is not None:
        payload['ingestion_context']=ingestion_context
        payload['select_rules_in_call1']=True
    if repair: payload['repair']={'error':str(error)[:600],'instruction':'Regenerate the complete JSON. Use only interpretation enum codes and their required parameters; never substitute a prose explanation.'}
    result=client.call('query_rewriting',payload,schema,repair=repair)
    if not isinstance(result,dict): raise PipelineError('Rewrite output must be an object')
    result=deepcopy(result)
    result['entities']=normalize_entities(result.get('entities'))
    recovery=apply_temporal_facts(result,facts)
    # Enforce the same contract for any backend/client, including adapters and test fixtures.
    result=validate_rewrite_intent(question,parse_output(dumps(result),schema))
    if ingestion_context is not None:result['selected_rules']=normalize_selected_rules(result['selected_rules'])
    if recovery:result['_temporal_facts']=recovery
    return result
