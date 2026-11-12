"""A model-facing view of metadata. SQL validation always uses the full original."""
from copy import deepcopy
from datamate_v2.common import digest,dumps


def clean_columns(columns):
    rendered=[]
    for column in columns:
        item={key:deepcopy(column[key]) for key in
              ('sql_column','original_column','type','meaning','semantic_status') if key in column}
        if column.get('semantic_status')!='verified':
            for key in ('candidate_meanings','source_quote'):
                if key in column: item[key]=deepcopy(column[key])
        rendered.append(item)
    return sorted(rendered,key=lambda c:c['sql_column'])


def build_qa_context(metadata):
    context={key:deepcopy(metadata[key]) for key in
             ('business_id','schema','readme','rulebook','data_coverage') if key in metadata}
    context.update(format='compact_qa_v2',table_catalog={},schema_groups={},description_groups={},domains={},tables=[],
                   system_columns={'_is_summary_row':'BOOLEAN','_summary_status':'TEXT','_source_row':'BIGINT'},
                   relationships=[{key:deepcopy(r[key]) for key in
                       ('from_table','from_column','to_table','to_column','cardinality','status') if key in r}
                       for r in metadata.get('relationships',[]) if r.get('status')=='verified'])
    context['calendar_source']={'sha256':digest(metadata.get('calendar_csv','')),
                               'resolver':'Python validates the complete calendar; use validated_windows'}
    for table in metadata['tables']:
        columns=clean_columns(table['columns'])
        group_id='sg_'+digest(columns)[:16]
        context['table_catalog'][table['sql_table']]=group_id
        group=context['schema_groups'].setdefault(group_id,{'columns':columns,'sample_rows':[]})
        # Samples are illustrative. Never include the original/audit copies, and
        # omit long values rather than making a clipped entity look exact.
        for row in table.get('sample_rows',[])+table.get('last_rows',[]):
            if len(group['sample_rows'])>=2: break
            sample={c['sql_column']:deepcopy(row[c['sql_column']]) for c in columns
                    if c['sql_column'] in row and
                    (not isinstance(row[c['sql_column']],str) or len(row[c['sql_column']])<=120)}
            if sample and sample not in group['sample_rows']: group['sample_rows'].append(sample)
        facts=table.get('verified_facts',{})
        item={'sql_table':table['sql_table'],'schema_group':group_id,'row_count':table.get('row_count'),
              'coverage':[{key:deepcopy(stat[key]) for key in ('sql_column','min','max') if key in stat}
                          for stat in table.get('stats',[]) if any(c['sql_column']==stat['sql_column'] and c['type'] in ('DATE','TIMESTAMPTZ') for c in columns)],
              'column_quality':{stat['sql_column']:{key:stat[key] for key in
                   ('null_count','unresolved_count','missing_count') if stat.get(key)}
                   for stat in table.get('stats',[]) if any(stat.get(key) for key in ('null_count','unresolved_count','missing_count'))},
              'quality':deepcopy(table.get('quality',{})),
              'grain_facts':{key:facts[key] for key in ('distinct_order_ids','multiple_rows_per_order') if key in facts},
              'description_status':table.get('description_status','unavailable'),'domains':{}}
        for key in ('source_file','source_table'):
            if key in table: item[key]=table[key]
        for column,domain in table.get('observed_domains',{}).items():
            values=sorted((deepcopy(v['value']) for v in domain.get('values',[])),key=dumps)
            rendered={'values':values,'truncated':domain.get('truncated',False),'distinct_count':domain.get('distinct_count')}
            domain_id='dom_'+digest(rendered)[:16]
            context['domains'][domain_id]=rendered
            item['domains'][column]=domain_id
        description=table.get('description')
        reference=description.get('group_ref') if isinstance(description,dict) else None
        if reference:
            source=metadata.get('description_groups',{}).get(reference)
            if source:
                context['description_groups'][reference]={key:deepcopy(source[key]) for key in ('status','scope','description') if key in source}
                item['description']={'group_ref':reference}
        elif description:
            reference='desc_'+digest(description)[:16]
            context['description_groups'][reference]={'status':'available','description':deepcopy(description)}
            item['description']={'group_ref':reference}
        else:
            item['description']=None
        context['tables'].append(item)
    # Report omitted tables, but exclude header-detection row dumps and traces.
    context['unavailable_tables']=[{key:w[key] for key in ('table','reason') if key in w}
                                  for w in metadata.get('warnings',[]) if isinstance(w,dict) and w.get('reason')=='unresolved_header']
    return context
