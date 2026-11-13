"""Scope Call 2 to candidate tables and retrieve source policies, without an LLM."""
import re
from copy import deepcopy
from .entities import entity_key
from .routing import route_metadata


TOPICS={
    'revenue':(['doanh thu','revenue','thu ve','tien ban'],['doanh thu mot don','don tinh vao doanh thu','ban duoc bao nhieu tien','don thuoc ky nao']),
    'orders':(['so don','may don','bao nhieu don','dem don','order count'],['so don','don tinh vao doanh thu','don thuoc ky nao']),
    'selling':(['ban chay','ban nhieu nhat','ban dc nhieu nhat','ban duoc nhieu nhat','hot nhat'],['ban chay','mat hang','don tinh vao doanh thu','don vi co ban']),
    'delivery':(['dot giao','dot hang','lan giao','cho giao'],['giao nhieu dot']),
    'customers':(['khach','customer'],['xac dinh khach','so don','don tinh vao doanh thu']),
    'categories':(['nhom','category','ti trong','ty trong'],['nhom hang','ti trong','ty trong']),
    'debt':(['cong no','con no','tra no','thu no','debt'],['don duoc tinh vao no','no mot don','no theo khach','no them']),
    'inventory':(['ton kho','con ton','con hang','inventory'],['ton','nhap','don vi co ban','don tinh vao doanh thu']),
    'units':(['don vi','unit','thung','kg','tan','bao','thien','quy doi'],['don vi co ban','don vi lon','ban chay','quy doi']),
}


def source_blocks(source,text):
    heading='';paragraph=[];blocks=[]
    def flush():
        if paragraph:
            blocks.append({'source':source,'section':heading,'text':'\n'.join(paragraph)})
            paragraph.clear()
    for line in text.splitlines():
        if line.lstrip().startswith('#'):
            flush();heading=line.strip().lstrip('#').strip()
        elif not line.strip():flush()
        elif line.lstrip().startswith('|') or re.match(r'^\s*[-*]\s+',line):
            flush()
            if not re.fullmatch(r'[\s|:\-]+',line):blocks.append({'source':source,'section':heading,'text':line})
        else:paragraph.append(line)
    flush();return blocks


def retrieve_sources(metadata,question,analysis,count_tokens,budget=3500):
    q=entity_key(question['question']);metric=entity_key(analysis.get('metric',''))
    topics={name for name,(triggers,_) in TOPICS.items() if any(t in q or t in metric for t in triggers)}
    if re.search(r'ban.*tien|thu ve.*tien',q):topics.add('revenue')
    if 'delivery' in topics and not re.search(r'khach.*(?:nao|nhieu|nhat)|theo khach|cua khach',q):
        topics.discard('customers')  # "giao ... cho khách" is not customer ranking.
    if 'customers' in topics and not topics.intersection({'orders','revenue','debt'}):topics.add('revenue')
    labels=[label for topic in topics for label in TOPICS[topic][1]]
    labels+=['o de trong o cac dong sau','ngay x','thang nay','tuan','thang m/y','hom nay']
    blocks=source_blocks('rulebook',metadata.get('rulebook',''))+source_blocks('readme',metadata.get('readme',''))
    # Search original entities and the requested unit, not invented translations.
    units=analysis.get('requested_units',[])
    terms={w for w in re.findall(r'\w+',q+' '+metric+' '+entity_key(' '.join(units))) if len(w)>2}
    ranked=[]
    for index,block in enumerate(blocks):
        s=entity_key(block['text']);h=entity_key(block['section'])
        label=s.split('|')[1].strip() if s.startswith('|') and '|' in s[1:] else h
        label=re.sub(r'[*`#]','',label).strip()
        score=sum(80 for wanted in labels if label.startswith(wanted))
        # Always keep the global as_of_date convention and row-fill/grain notes.
        if 'as_of_date' in s:score+=100
        score+=min(15,len(terms&set(re.findall(r'\w+',s))))
        if block['source']=='readme':
            score+=12  # README is short and contains column/grain definitions.
            if 'units' in topics or 'selling' in topics:
                if 'quy doi' in h or 'don vi' in h or re.search(r'\b1\b.*=',s):score+=90
        if not topics:score+=20  # Broad source fallback for unsupported metrics.
        # One generic shared word (e.g. "đơn") must not retrieve every policy.
        if block['source']=='rulebook' and topics and score<4:continue
        if score>0:ranked.append((score,index,block))
    selected=[];used=0
    for score,index,block in sorted(ranked,key=lambda x:(-x[0],x[1])):
        rendered=f"[{block['source']} / {block['section']}]\n{block['text']}\n"
        cost=count_tokens(rendered)
        if used+cost>budget:continue  # Never cut a policy/exception mid-sentence.
        selected.append((index,block));used+=cost
    selected=[b for _,b in sorted(selected)]
    return selected,{'topics':sorted(topics),'tokens':used,'budget':budget,
        'selected_blocks':len(selected),'omitted_blocks':len(blocks)-len(selected),
        'fallback':not bool(topics),'method':'source text/section retrieval; no verbatim-quote guard or extra model call'}


def scope_metadata(metadata,resolved,full_history):
    if full_history or resolved.get('unresolved'):
        scoped=metadata;routing={'reason':'full history requested or unresolved time; preserve sources','excluded':[]}
    else:scoped,routing=route_metadata(metadata,resolved['windows'])
    scoped=deepcopy(scoped);names={t['sql_table'] for t in scoped['tables']}
    catalog=deepcopy(metadata.get('value_catalog',{'groups':[]}))
    catalog['groups']=[{**g,'tables':[t for t in g['tables'] if t in names]}
        for g in catalog.get('groups',[]) if any(t in names for t in g['tables'])]
    catalog['omitted']=[g for g in catalog.get('omitted',[]) if g.get('table') in names]
    scoped['value_catalog']=catalog
    return scoped,{**routing,'selected_tables':sorted(names)}


def call2_context(metadata,question,analysis,resolved,calendar,count_tokens,values_budget=2500,rules_budget=3500):
    from .direct_sql import build_direct_context
    from datamate_v2.data_pipeline.value_catalog import budget_values
    scoped,routing=scope_metadata(metadata,resolved,analysis.get('requires_full_history',False))
    context=build_direct_context(scoped)
    context.pop('domains',None);context.pop('unavailable_tables',None)
    for table in context['queryable_tables']:table.pop('domains',None)
    fragments,retrieval=retrieve_sources(scoped,question,analysis,count_tokens,rules_budget)
    for source in ('readme','rulebook'):
        context[source]='\n\n'.join(f"[{b['section']}]\n{b['text']}" for b in fragments if b['source']==source)
    values,usage=budget_values(scoped['value_catalog'],question['question'],count_tokens,values_budget)
    context['value_dictionaries']=values;context['calendar_defaults']=calendar
    for table in context['queryable_tables']:
        table['value_dictionary_refs']={g['column']:g['id'] for g in values['groups'] if table['sql_table'] in g['tables']}
    context['scope_instruction']='This is the complete allowed table catalog for this SQL call. Dictionaries and samples refer only to these tables. Other months are not SQL candidates. Source labels/IDs are not SQL tables.'
    # Model-selected raw table names/time requests can contradict Python. Keep
    # the original analysis in the log, but do not reintroduce them into Call 2.
    decision={k:deepcopy(v) for k,v in analysis.items() if k not in ('data_requirements','time_requests')}
    return scoped,context,decision,{'routing':routing,'retrieval':retrieval,'fragments':fragments,'values':usage}
