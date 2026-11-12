from datamate_v2.common import identifier,fold,PipelineError,dumps
from .column_profiler import sample_positions
from datamate_v2.inference.structured_output import parse_output
from datamate_v2.progress import emit
from copy import deepcopy
import re

COLUMN_SCHEMA={"type":"object","required":["mappings"],"properties":{"mappings":{"type":"array","items":{
 "type":"object","required":["index","meaning","candidate_meanings","evidence","source_quote"],"properties":{
 "index":{"type":"integer"},"meaning":{"type":["string","null"]},
 "candidate_meanings":{"type":"array","items":{"type":"string"}},
 "evidence":{"type":"array","items":{"type":"string"}},"source_quote":{"type":"string"}}}}}}

MAPPING_ITEM=COLUMN_SCHEMA['properties']['mappings']['items']
MAPPING_ITEM['additionalProperties']=False
MAPPING_ITEM['properties']['index']['minimum']=0
MAPPING_ITEM['properties']['meaning']['maxLength']=100
MAPPING_ITEM['properties']['source_quote']['maxLength']=1000
for field in ('candidate_meanings','evidence'):
    MAPPING_ITEM['properties'][field].update(maxItems=8)
    MAPPING_ITEM['properties'][field]['items']['maxLength']=300
COLUMN_SCHEMA['additionalProperties']=False


def column_schema(indices):
    schema=deepcopy(COLUMN_SCHEMA)
    schema['properties']['mappings'].update(maxItems=len(indices),uniqueItems=True)
    schema['properties']['mappings']['items']['properties']['index']={'enum':sorted(indices)}
    return schema


def collect_proposals(unknown,columns,aliases,client,context):
    """Stage a whole response before applying any mapping; one bounded repair.

    Verified columns are immutable. A malformed auxiliary proposal must not
    discard the verified product/category columns or every table in a business.
    """
    active={item['index']:item for item in unknown};accepted={};warnings={}
    def warn(index,reason):
        warnings.setdefault(index,[]).append(reason)
        emit('column_mapping_warning',f'Column mapping {index}: {reason}',table=context.get('table'))
    for attempt in range(2):
        payload={'columns':list(active.values()),'allowed_indices':sorted(active),
                 'verified_columns':[{key:c.get(key) for key in ('index','original_column','meaning')} for c in columns if c['semantic_status']=='verified'],
                 'alias_dictionary':aliases,**context}
        if attempt: payload['repair']={'instruction':'Return mappings only for allowed_indices. Do not repeat verified or previously accepted columns. Invalid/duplicate auxiliary proposals remain unresolved.',
                                     'issues':{str(i):messages[-2:] for i,messages in warnings.items()}}
        invalid=False;groups={}
        try:
            result=client.call('column_normalization',payload,column_schema(active),cache=True,repair=bool(attempt))
            if not isinstance(result,dict) or not isinstance(result.get('mappings'),list):
                raise PipelineError('Column mapping response must contain a mappings array')
            if len(result['mappings'])>max(64,len(columns)*2):
                raise PipelineError('Column mapping response is unexpectedly large')
            for proposal in result['mappings']:
                try: proposal=parse_output(dumps(proposal),MAPPING_ITEM)
                except PipelineError as error:
                    warn(-1,'Rejected malformed proposal: '+str(error)[:300]);invalid=True;continue
                index=proposal['index']
                if index in active:
                    groups.setdefault(index,[]).append(proposal)
                elif 0<=index<len(columns) and columns[index]['semantic_status']=='verified':
                    reason='Ignored redundant verified mapping' if proposal['meaning']==columns[index]['meaning'] else 'Rejected conflicting proposal for immutable verified column'
                    warn(index,reason)
                elif index in accepted:
                    warn(index,'Ignored repeated previously accepted mapping')
                else:
                    warn(-1,f'Rejected out-of-domain index {index}');invalid=True
            # No mutation of columns until every item has been classified.
            for index,proposals in groups.items():
                if len(proposals)!=1:
                    warn(index,'Rejected duplicate index; column remains unresolved pending repair');invalid=True
                else: accepted[index]=proposals[0]
        except PipelineError as error:
            warn(-1,'Mapping call failed: '+str(error)[:400]);invalid=True
        active={i:item for i,item in active.items() if i not in accepted}
        if not active or not invalid: break
    for index,messages in warnings.items():
        targets=[columns[index]] if 0<=index<len(columns) else [columns[item['index']] for item in unknown]
        for column in targets: column.setdefault('mapping_warnings',[]).extend(messages)
    return accepted.values()

NUMERIC={"quantity","base_quantity","unit_price","line_total","amount_paid","revenue","discount","unit_cost","stock","amount","percent","conversion_factor"}
DATES={"order_date","payment_date","date","due_date","expiry_date","created_at"}

def readme_mappings(readme,aliases):
    """Literal column definitions only; no fuzzy semantic guesses or model calls."""
    out={}
    for line in readme.splitlines():
        match=re.match(r'^\s*(?:[-*]\s+)?(.+?)\s*:\s*(.+?)\s*$',line)
        if not match: continue
        original=fold(match[1].strip(' *`'));meaning=fold(match[2])
        matches=[(len(fold(alias)),key) for key,values in aliases.items() for alias in values
                 if meaning==fold(alias) or meaning.startswith(fold(alias)+' ')]
        if not matches: continue
        longest=max(length for length,_ in matches)
        candidates={key for length,key in matches if length==longest}
        if len(candidates)==1: out.setdefault(original,[]).append((next(iter(candidates)),line.strip()))
    return out

def normalize_columns(headers,rows,aliases,client,context):
    columns=[];used=set();unknown=[]
    literal_definitions=readme_mappings(context.get('readme',''),aliases)
    alias_index={}
    for key,values in aliases.items():
        for value in values: alias_index.setdefault(fold(value),set()).add(key)
    for i,original in enumerate(headers):
        original=str(original) if original is not None and str(original).strip() else f"unnamed_{i+1}"
        candidates=sorted(alias_index.get(fold(original),set()))
        meaning=candidates[0] if len(candidates)==1 else None
        source_quote=None
        if meaning is None:
            literal=literal_definitions.get(fold(original),[])
            meanings={entry[0] for entry in literal}
            if len(meanings)==1: meaning=next(iter(meanings));source_quote=literal[0][1]
        base=identifier(meaning or original)
        if base.startswith('_'): base='c'+base
        name=base;n=2
        while name in used or name in {"_source_row","_is_summary_row","_summary_status","_raw","_quality"}: name=f"{base}_{n}";n+=1
        used.add(name)
        col={"index":i,"original_column":original,"sql_column":name,"meaning":meaning,
             "semantic_status":"verified" if meaning else "unresolved","candidate_meanings":candidates,
             "evidence":["literal README definition",source_quote] if source_quote else ["exact alias dictionary"] if meaning else [],"type":"NUMERIC" if meaning in NUMERIC else "DATE" if meaning in DATES and meaning!="created_at" else "TIMESTAMPTZ" if meaning=="created_at" else "TEXT"}
        if source_quote: col['source_quote']=source_quote
        columns.append(col)
        if not meaning: unknown.append({"index":i,"original_column":original,"samples":[rows[pos]["values"][i] for pos in sample_positions(len(rows),5) if i<len(rows[pos]["values"])]})
    if unknown:
        for proposal in collect_proposals(unknown,columns,aliases,client,context):
            i=proposal["index"]
            col=columns[i];quote=proposal["source_quote"]
            source=context.get("readme","")+"\n"+context.get("rulebook","")
            # SLM evidence alone does not verify semantics: demand a literal source quote containing the original name.
            meaning=proposal['meaning']
            definitions=[]
            if meaning in aliases:
                original=fold(col['original_column'])
                definitions=[original+' '+fold(alias) for alias in aliases[meaning]]+[original+' la '+fold(alias) for alias in aliases[meaning]]
            explicit_definition=any(fold(quote)==definition or fold(quote).startswith(definition+' ') for definition in definitions)
            conflicting_definitions=len({entry[0] for entry in literal_definitions.get(fold(col['original_column']),[])})>1
            backed=bool(quote and quote in source and col["original_column"] in quote and explicit_definition and not conflicting_definitions)
            col.update(candidate_meanings=proposal["candidate_meanings"],evidence=proposal["evidence"],source_quote=quote,
                       semantic_status="verified" if backed else "uncertain" if proposal["candidate_meanings"] else "unresolved")
            if backed:
                col["meaning"]=proposal["meaning"]
                col["type"]="NUMERIC" if col["meaning"] in NUMERIC else "TIMESTAMPTZ" if col["meaning"]=='created_at' else "DATE" if col["meaning"] in DATES else "TEXT"
    # Preserve intraday precision in date-like source columns when timestamps are present.
    import re
    for col in columns:
        if col['type']=='DATE' and any(col['index']<len(r['values']) and re.search(r'[T ]\d{2}:\d{2}',str(r['values'][col['index']])) for r in rows):
            col['type']='TIMESTAMPTZ'
    return columns
