"""Column rules + per-column memoization + unique, shape-grouped SLM exceptions."""
from collections import OrderedDict
from time import perf_counter
from datamate_v2.common import PipelineError,digest,dumps
from datamate_v2.progress import emit
from .column_profiler import profile_column,raw_key,shape
from .value_normalizer import parse_value,normalize_unit
from .validators import value_schema,validate_value_proposal
from datamate_v2.inference.structured_output import parse_output


def normalize_table(raw_rows,columns,client,context,settings,*,year=None):
    start=perf_counter()
    size=int(settings.get('value_batch_size',8))
    if size<=0: raise PipelineError('value_batch_size must be positive')
    profiles={c['sql_column']:profile_column(raw_rows,c,settings) for c in columns}
    memo={c['sql_column']:{} for c in columns}
    pending=OrderedDict();clean_rows=[];logs=[];slm_logs=[]
    stats={'cells':0,'unique_rule_parses':0,'rule_cache_hits':0,'unresolved_cells_before_slm':0,
           'unique_unresolved_values':0,'slm_items':0,'slm_batches':0,'slm_accepted_values':0,
           'slm_rejected_values':0,'slm_applied_cells':0,'source_error_cells_not_sent_to_slm':0}
    unit_columns=[c for c in columns if c.get('meaning')=='unit' and c.get('semantic_status')=='verified']
    stats['slm_batch_errors']=0;stats['slm_repair_batches']=0
    for raw in raw_rows:
        row={'_source_row':raw['index'],'_raw':raw['values'],'_quality':[]}
        for col in columns:
            stats['cells']+=1;name=col['sql_column'];index=col['index']
            value=raw['values'][index] if index<len(raw['values']) else None
            allowed_units=[]
            if col.get('meaning')=='quantity' and len(unit_columns)==1:
                unit_index=unit_columns[0]['index']
                if unit_index<len(raw['values']) and raw['values'][unit_index] not in (None,''):
                    allowed_units=[str(raw['values'][unit_index])]
            key=(raw_key(value),tuple(normalize_unit(u) for u in allowed_units))
            source_error=any(f.get('column')==index for f in raw.get('flags',[]))
            cache=memo[name]
            if key not in cache:
                cache[key]=parse_value(value,col,locale=profiles[name]['locale'],year=year,timezone=settings.get('timestamp_timezone'),allowed_units=allowed_units)
                stats['unique_rule_parses']+=1
            else: stats['rule_cache_hits']+=1
            clean,status=cache[key]
            if source_error:
                clean,status=None,'unresolved';stats['source_error_cells_not_sent_to_slm']+=1
            row[name]=clean
            if status in ('missing','unresolved'): row['_quality'].append({'column':name,'status':status})
            log={'row':raw['index'],'column':name,'raw':value,'clean':clean,'status':status,
                 'normalizer':'column_rules','column_locale':profiles[name]['locale']}
            logs.append(log)
            if status=='unresolved':
                stats['unresolved_cells_before_slm']+=1
                if not source_error:
                    group=pending.setdefault((name,key),{'row':raw['index'],'column':name,'raw':value,'type':col['type'],'shape':shape(value),'allowed_units':allowed_units,'occurrences':[]})
                    group['occurrences'].append((row,log))
        clean_rows.append(row)
    stats['unique_unresolved_values']=len(pending)
    grouped=OrderedDict()
    for group in pending.values(): grouped.setdefault((group['column'],group['shape']),[]).append(group)
    by_name={c['sql_column']:c for c in columns}
    emit('normalization_rules',f'Rules: {stats["cells"]} cells, {stats["unique_rule_parses"]} unique parses, {stats["rule_cache_hits"]} reused; {len(pending)} distinct SLM exceptions')
    # IDs and source-row lists remain local. The model sees only one opaque ID
    # per distinct value/context and cannot expand example source rows into output.
    planned=[group for groups in grouped.values() for group in groups]
    for group in planned:
        group['item_id']='v_'+digest((group['column'],raw_key(group['raw']),group['allowed_units']))[:12]
    def failure(batch,reason):
        stats['slm_rejected_values']+=len(batch)
        for group in batch:
            slm_logs.append({'item_id':group['item_id'],'column':group['column'],'accepted':False,
                'reason':reason[:600],'occurrence_count':len(group['occurrences']),
                'source_rows':[r['_source_row'] for r,_ in group['occurrences']]})
    def process_batch(batch,depth=0):
        items=[{k:g[k] for k in ('item_id','column','raw','type','shape','allowed_units')} for g in batch]
        names=list(dict.fromkeys(g['column'] for g in batch))
        expected={g['item_id']:g for g in batch}
        schema=value_schema(expected)
        # Only compact column facts: no source_row, occurrence list, raw sample
        # rows or related whole records that a model could mistake for more items.
        profile_payload=[{k:profiles[name][k] for k in ('sql_column','type','locale','status','locale_evidence_counts')} for name in names]
        stats['slm_batches']+=1;stats['slm_items']+=len(items)
        if depth: stats['slm_repair_batches']+=1
        emit('normalization_fallback',f'SLM normalization batch {stats["slm_batches"]}: {len(items)} representative IDs')
        try:
            response=client.call('value_normalization',{'items':items,
                'columns':[{k:v for k,v in by_name[name].items() if k!='index'} for name in names],
                'column_profiles':profile_payload,**context},schema,cache=True,repair=bool(depth))
            result=parse_output(dumps(response),schema)
            ids=[p['item_id'] for p in result['proposals']]
            if len(ids)!=len(set(ids)): raise PipelineError('Duplicate value-normalization item_id')
        except PipelineError as error:
            stats['slm_batch_errors']+=1
            emit('normalization_batch_error',f'Normalization batch failed; preserve unresolved cells ({type(error).__name__})')
            retry_depth=int(settings.get('value_retry_depth',1))
            if len(batch)>1 and depth<retry_depth:
                middle=len(batch)//2
                process_batch(batch[:middle],depth+1);process_batch(batch[middle:],depth+1)
            else: failure(batch,str(error))
            return
        seen=set()
        for proposal in result['proposals']:
            group=expected[proposal['item_id']];name=group['column'];seen.add(proposal['item_id'])
            cleaned=validate_value_proposal(proposal,group['raw'],by_name[name],context,
                profile=profiles[name],year=year,timezone=settings.get('timestamp_timezone'),allowed_units=group['allowed_units'])
            accepted=cleaned is not None
            slm_logs.append({**proposal,'column':name,'accepted':accepted,'occurrence_count':len(group['occurrences']),
                             'source_rows':[r['_source_row'] for r,_ in group['occurrences']]})
            stats['slm_accepted_values' if accepted else 'slm_rejected_values']+=1
            if accepted:
                for row,log in group['occurrences']:
                    row[name]=cleaned;row['_quality']=[q for q in row['_quality'] if q['column']!=name]
                    log.update(clean=cleaned,status='verified',normalizer='slm_proposal_python_validated',proposal_id=group['item_id'])
                    stats['slm_applied_cells']+=1
        omitted=[group for item_id,group in expected.items() if item_id not in seen]
        failure(omitted,'no_proposal')
    retry_depth=int(settings.get('value_retry_depth',1))
    if not 0<=retry_depth<=2: raise PipelineError('value_retry_depth must be between 0 and 2')
    for offset in range(0,len(planned),size): process_batch(planned[offset:offset+size])
    stats['duration_ms']=(perf_counter()-start)*1000
    stats['deduplicated_slm_cells']=stats['unresolved_cells_before_slm']-stats['source_error_cells_not_sent_to_slm']-stats['unique_unresolved_values']
    return clean_rows,logs,slm_logs,list(profiles.values()),stats
