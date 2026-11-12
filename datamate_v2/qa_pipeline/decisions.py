"""Source-grounded decisions, resolved calendars and compatible fact sources."""
from copy import deepcopy
from datamate_v2.common import PipelineError
from .temporal_contract import window_schema,PARAMETER_SCHEMAS
from .temporal_validator import validate_windows
from .query_rewriting.temporal_facts import temporal_facts


def text(n):return {'type':'string','maxLength':n}


def request_schema():
    variants=[]
    for kind in PARAMETER_SCHEMAS:
        variant=window_schema(kind)
        variant['properties']={k:v for k,v in variant['properties'].items() if k in ('name','interpretation','parameters','status')}
        variants.append(variant)
    return {'oneOf':variants}


DECISION_SCHEMA={'type':'object','additionalProperties':False,
    'required':['goal','entity_mentions','rules_to_apply','aggregation','time_requests','data_requirements','requires_full_history'],
    'properties':{
        'goal':text(180),'entity_mentions':{'type':'array','maxItems':6,'items':text(100)},
        'rules_to_apply':{'type':'array','maxItems':6,'items':{'type':'object','additionalProperties':False,
            'required':['source','action'],'properties':{'source':{'enum':['rulebook','readme']},'action':text(220)}}},
        'aggregation':text(500),'time_requests':{'type':'array','maxItems':4,'items':request_schema()},
        'data_requirements':{'type':'array','maxItems':4,'items':text(160)},
        'requires_full_history':{'type':'boolean'}}}


def resolve_decision_times(question,analysis,metadata,calendar):
    facts=temporal_facts(question,calendar)
    requests=facts['windows'] if facts else analysis.get('time_requests',[])
    result=[];unresolved=[]
    for request in requests:
        try:
            resolved=validate_windows([*result,request],question['as_of_date'],calendar_csv=metadata.get('calendar_csv',''),**calendar)
            result=resolved
        except (PipelineError,ValueError,KeyError,TypeError) as error:
            unresolved.append({'request':request,'error':str(error)})
    return {'windows':[{k:w[k] for k in ('name','interpretation','start_date','end_date_exclusive','timezone')} for w in result],
        'authority':'original_question_python_facts' if facts else 'call1_interpretation_python_arithmetic',
        'unresolved':unresolved,'instruction':'Use these concrete half-open bounds; do not recalculate dates. Unresolved interpretations remain for source-based reasoning, not an automatic abstain.'}


def overlaps(table,windows):
    # Different event-date columns can represent different business periods.
    # Keep candidates if ANY date column overlaps ANY requested window.
    stats=[s for s in table.get('stats',[]) if s.get('min') is not None and s.get('max') is not None and
        any(c['sql_column']==s['sql_column'] and c['type'] in ('DATE','TIMESTAMPTZ') for c in table['columns'])]
    return not windows or not stats or any(str(s['min'])[:10]<w['end_date_exclusive'] and
        (w['start_date'] is None or str(s['max'])[:10]>=w['start_date']) for s in stats for w in windows)


def assemble_sources(metadata,resolved,full_history=False):
    """CTE templates UNION compatible monthly facts; never join unrelated facts."""
    from sqlglot import exp
    groups={}
    for table in metadata['tables']:
        # Different files/grain may be unrelated even with identical column names.
        signature=(table.get('source_file',table['sql_table']),tuple(sorted(
            (c['sql_column'],c['type'],c.get('meaning','')) for c in table['columns'])))
        groups.setdefault(signature,[]).append(table)
    sources=[]
    for index,tables in enumerate(groups.values(),1):
        selected=[t for t in tables if full_history or overlaps(t,resolved['windows'])]
        if not selected:continue
        columns=sorted(c['sql_column'] for c in selected[0]['columns'])+['_is_summary_row','_source_row','_summary_status']
        branches=[exp.select(*[exp.column(c,quoted=True) for c in columns]).from_(
            exp.Table(this=exp.to_identifier(t['sql_table'],quoted=True))) for t in selected]
        query=branches[0]
        for branch in branches[1:]:query=exp.Union(this=query,expression=branch,distinct=False)
        sources.append({'cte_name':f'records_{index}','physical_tables':[t['sql_table'] for t in selected],
            'select_sql':query.sql(dialect='postgres')})
    return {'sources':sources,'instruction':'Paste needed SELECTs as WITH CTEs. They combine records, not aggregates, and apply no business/date predicates. Use compatible monthly fact UNION ALL sources before calculating. Join dimensions on documented keys; for many-to-many fact joins aggregate each side to the required key/grain FIRST. Do not join everything indiscriminately. Different periods keep separate predicates over the combined facts. These are CTE templates, not permanent database tables; full physical catalog remains available.'}
