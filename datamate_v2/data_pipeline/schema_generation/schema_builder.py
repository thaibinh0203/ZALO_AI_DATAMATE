from datamate_v2.common import dumps,fold,PipelineError
from datamate_v2.inference.structured_output import parse_output
from .schema_validator import description_schema,validate_description,required_topics


def verified_facts(table,context):
    ids=[c['sql_column'] for c in table['columns'] if c.get('meaning')=='order_id' and c['semantic_status']=='verified']
    distinct=next((s['distinct_count'] for s in table['stats'] if len(ids)==1 and s['sql_column']==ids[0]),None)
    return {'row_count':table['row_count'],'distinct_order_ids':distinct,
        'multiple_rows_per_order':bool(distinct is not None and distinct<table['row_count']),
        'date_coverage':[{k:s.get(k) for k in ('sql_column','min','max','null_count','unresolved_count')} for s in table['stats'] if any(c['sql_column']==s['sql_column'] and c['type'] in ('DATE','TIMESTAMPTZ') for c in table['columns'])],
        'observed_domains':table.get('observed_domains',{}),
        'mapping_issues':[{k:c.get(k) for k in ('original_column','sql_column','semantic_status','candidate_meanings','evidence')} for c in table['columns'] if c['semantic_status']!='verified'],
        'quality':table['quality'],'relationships':table.get('relationships',[]),
        'source_grain_quotes':[{'source':'readme','quote':line.strip()} for line in context['readme'].splitlines() if 'moi dong' in fold(line)][:8]}


def build_schema(table,client,context,max_repairs=1):
    table['verified_facts']=verified_facts(table,context)
    from .description_groups import compact_input
    compact,source=compact_input(table,context)
    schema=description_schema(table)
    fields=sorted(compact['verified_facts'])
    schema['properties']['usage_rules']['items']['properties']['evidence']['items']['oneOf'][1]['properties']['field']={'enum':fields}
    payload={'table':compact,'readme':source,'scope':'shared_structure_only',
             'business_rules_location':'business_context.readme and business_context.rulebook at QA; not supplied for table descriptions',
             'required_usage_topics':sorted(required_topics(compact))}
    for attempt in range(max_repairs+1):
        try:
            result=client.call('schema_description',payload,schema,cache=True,repair=bool(attempt))
            description=parse_output(dumps(result),schema)
            validate_description(description,compact,{'readme':source})
            table['description']=description
            table['description_status']='available'
            table.pop('description_error',None)
            return table
        except PipelineError as error:
            if attempt>=max_repairs:
                # Keep verified metadata and original sources available to QA.
                # Do not invent a semantic description when model generation fails.
                table['description']=None
                table['description_status']='unavailable'
                table['description_error']={'error_type':type(error).__name__,'error':str(error)[:600]}
                from datamate_v2.progress import emit
                emit('schema_warning',f'{table["sql_table"]}: optional description unavailable; continuing with verified metadata')
                return table
            payload={**payload,'repair':{'error':str(error)[:600],'instruction':'Regenerate compact semantic insights with every column and required usage topic; refer to source names without quoting; unknown physical units are null.'}}


def render_metadata(metadata):
    return dumps(metadata)
