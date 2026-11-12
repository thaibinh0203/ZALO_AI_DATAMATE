"""Call 1 excerpts are model-selected context, not exact-match attestations."""
from copy import deepcopy
from datamate_v2.common import PipelineError

SELECTED_RULES={'type':'array','maxItems':3,'items':{'type':'object','additionalProperties':False,
    'required':['id','source','excerpt'],'properties':{'id':{'enum':['r1','r2','r3']},
    'source':{'enum':['rulebook']},'excerpt':{'type':'string','minLength':1,'maxLength':600}}}}


def normalize_selected_rules(rules):
    if not isinstance(rules,list):raise PipelineError('selected_rules must be an array')
    result=[];seen=set()
    for rule in rules:
        text=rule['excerpt'].strip()
        if not text:continue
        key=' '.join(text.split())
        if key in seen:continue
        seen.add(key)
        # Binding IDs are assigned before Call 2, so duplicate model IDs cannot
        # create ambiguity there. Do not invent or repair excerpt contents.
        result.append({'id':f'r{len(result)+1}','source':rule['source'],'excerpt':text})
    return result


def build_selection_context(metadata):
    """Read ingestion for rule selection without raw audit/domain repetitions."""
    from ..context import build_qa_context
    context=build_qa_context(metadata)
    keep=('sql_table','schema_group','source_file','source_table','coverage','grain_facts','description','description_status')
    context['tables']=[{k:t[k] for k in keep if k in t} for t in context['tables']]
    context.pop('domains',None)
    groups={}
    for key,group in context.get('description_groups',{}).items():
        description=group.get('description')
        if isinstance(description,dict):
            groups[key]={'description':{k:description[k] for k in ('description','grain') if k in description}}
    context['description_groups']=groups
    return context
