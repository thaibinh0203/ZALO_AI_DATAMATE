"""Two calls: source-grounded entity analysis, then direct PostgreSQL SQL.

Read-only/business isolation, actual entity literals and output format are checked.
Business correctness is evaluated afterwards against reference answers.
"""
from time import perf_counter
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope
from datamate_v2.common import PipelineError, dumps
from datamate_v2.contracts import VALUE_TYPES, validate_question
from datamate_v2.evaluation.latency_profiler import Profiler
from .context import build_qa_context
from .query_rewriting.temporal_facts import temporal_facts
from .result_validation.validator import serialize_result
from .sql_validation.guard import FUNCTIONS


def text(limit=1200):
    return {'type':'string','maxLength':limit}


ANALYSIS_SCHEMA={'type':'object','additionalProperties':False,
    'required':['question_analysis','entity_mentions','entity_definitions','time_analysis'],
    'properties':{
        'question_analysis':text(240),
        'entity_mentions':{'type':'array','maxItems':6,'items':text(100)},
        'entity_definitions':{'type':'array','maxItems':4,'items':{
            'type':'object','additionalProperties':False,
            'required':['entity','interpretation','source','relevant_rule'],
            'properties':{'entity':text(80),'interpretation':text(160),
                'source':{'enum':['rulebook','readme','data','inference']},'relevant_rule':text(200)}}},
        'time_analysis':text(200)}}

SQL_SCHEMA={'type':'object','additionalProperties':False,
    'required':['status','calculation_plan','sql','reason','answer_type','list_mode','unit','round_digits'],
    'properties':{
        'status':{'enum':['answerable','abstain','unanswerable']},
        'calculation_plan':{'type':'array','maxItems':8,'items':text(400)},
        'sql':{'type':['string','null'],'maxLength':16000},'reason':text(800),
        'answer_type':{'enum':VALUE_TYPES},'list_mode':{'enum':['rows','row']},
        'unit':text(80),'round_digits':{'type':['integer','null'],'minimum':0,'maximum':8}}}


def execution_guard(statement,metadata,plan=None):
    # Preserve SQL safety only. No column/metric/time/summary/entity validation.
    try:
        trees=sqlglot.parse(statement,read='postgres')
    except Exception as error:
        raise PipelineError(f'Invalid PostgreSQL SQL: {error}') from error
    if len(trees)!=1 or not isinstance(trees[0],(exp.Select,exp.Union,exp.Intersect,exp.Except)):
        raise PipelineError('Only a single SELECT query is allowed')
    tree=trees[0]
    for name in ('Insert','Update','Delete','Create','Drop','Alter','Command','Copy','Into',
                 'Lock','Transaction','Set','Use','Merge','Grant','Revoke','Execute'):
        cls=getattr(exp,name,None)
        if cls and tree.find(cls):raise PipelineError('Non-read-only SQL: '+name)
    functions=FUNCTIONS|{'CURRENT_DATE','CURRENT_TIMESTAMP','CURRENT_TIME','ARRAY'}
    for node in tree.find_all(exp.Func):
        if isinstance(node,(exp.And,exp.Or,exp.Not)):continue
        name=node.name.upper() if isinstance(node,exp.Anonymous) else node.sql_name()
        if name not in functions or isinstance(node.parent,exp.Dot):
            raise PipelineError('Function outside read-only safety allowlist: '+name)
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this,exp.Identifier):
            raise PipelineError('Table functions are outside read-only safety allowlist')
    allowed={t['sql_table'] for t in metadata['tables']}
    for scope in traverse_scope(tree):
        for source in scope.sources.values():
            if not isinstance(source,exp.Table):
                continue
            if source.catalog or (source.db and source.db!=metadata['schema']) or source.name not in allowed:
                raise PipelineError('SQL table outside current business: '+source.sql(dialect='postgres'))
    return tree.sql(dialect='postgres')


def build_direct_context(metadata):
    context=build_qa_context(metadata)
    context.pop('calendar_source',None)
    for table in context['tables']:
        for key in ('quality','column_quality','description_status'):
            table.pop(key,None)
    # Preserve descriptions/grain/column meanings; source policies are already
    # supplied in full via README/Rulebook, not repeated as generated usage rules.
    for group in context['description_groups'].values():
        description=group.get('description')
        if isinstance(description,dict):
            group['description']={k:v for k,v in description.items()
                if k in ('description','grain','columns')}
    # References deliberately do not resemble SQL identifiers. Physical names
    # occur only in queryable_tables; no opaque sg_* keys reach the model.
    labels={key:f'Schema group {i+1} (reference only)' for i,key in enumerate(context['schema_groups'])}
    context['schema_reference']={labels[k]:v for k,v in context.pop('schema_groups').items()}
    for table in context['tables']:
        table['schema_reference']=labels[table.pop('schema_group')]
    context['queryable_tables']=context.pop('tables')
    context.pop('table_catalog',None)
    context['catalog_instruction']='Only queryable_tables[].sql_table are physical SQL tables. schema_reference keys are documentation labels, never SQL tables.'
    return context


def answer_direct(pipeline,question,metadata):
    started=perf_counter();call_start=len(pipeline.client.calls)
    profiler=Profiler(question);tool=None
    final={'id':question.get('id'),'type':'abstain'}
    log={'id':question.get('id'),'business_id':question.get('business_id'),'question':question,
         'model_version':pipeline.client.version,'data_fingerprint':metadata.get('fingerprint'),
         'qa_mode':'direct_sql','repair_attempts':0,'checks':[]}
    try:
        validate_question(question)
        if metadata['business_id']!=question['business_id']:
            raise PipelineError('Business isolation mismatch')
        with profiler.stage('context_loading'):
            context=build_direct_context(metadata)
            decision_mode=pipeline.settings.get('decision_planning',False)
            if decision_mode:
                from datamate_v2.data_pipeline.value_catalog import budget_values
                # Drop the old observed-domain copy: one budgeted dictionary only.
                context.pop('domains',None)
                for table in context['queryable_tables']:table.pop('domains',None)
                tokenizer=getattr(getattr(pipeline.client,'engine',None),'tokenizer',None)
                count_tokens=(lambda s:len(tokenizer.encode(s,add_special_tokens=False))) if tokenizer else (lambda s:len(s.encode('utf-8')))
                catalog=metadata.get('value_catalog',{'groups':[]})
                values,usage=budget_values(catalog,question['question'],count_tokens,
                    min(3000,pipeline.settings.get('values_token_budget',2500)))
                context['value_dictionaries']=values
                log['value_dictionary_budget']=usage
            context['calendar_defaults']=pipeline.settings.get('calendar',{})
            hints=None
            try:
                hints=temporal_facts(question,pipeline.settings['calendar'])
            except (PipelineError,ValueError,KeyError):
                pass  # Optional hints must never prevent model analysis.
            payload={'question':question,'business_context':context,'calendar_hints':hints}
            log['context_summary']={'format':context['format'],'tables':len(context['queryable_tables']),
                'schema_groups':len(context['schema_reference']),'characters':len(dumps(context))}
            log['calendar_hints']=hints
        with profiler.stage('call_1',model_call_index=1):
            if decision_mode:
                from .decisions import DECISION_SCHEMA
                analysis=pipeline.client.call('qa_decisions',payload,DECISION_SCHEMA)
            else:
                analysis=pipeline.client.call('qa_analysis',payload,ANALYSIS_SCHEMA)
            log['analysis']=analysis
        if decision_mode:
            from .decisions import resolve_decision_times,assemble_sources
            payload['resolved_time']=resolve_decision_times(question,analysis,metadata,pipeline.settings['calendar'])
            payload['data_assembly']=assemble_sources(metadata,payload['resolved_time'],analysis.get('requires_full_history',False))
            payload['decision_mode']=True
            log['resolved_time']=payload['resolved_time'];log['data_assembly']=payload['data_assembly']
        from .direct_entities import domain_columns,entity_hints,validate_literals
        with profiler.stage('entity_resolution'):
            tool=pipeline.tool_factory(metadata,pipeline.database,execution_guard,max_queries=12)
            cache=getattr(pipeline,'_direct_domains',{})
            cache_key=(metadata['business_id'],metadata.get('fingerprint'))
            if cache_key not in cache:
                columns=domain_columns(metadata)
                cache[cache_key]=tool.text_domains(columns) if columns else []
                pipeline._direct_domains=cache
            domains=cache[cache_key]
            payload['entity_catalog']=entity_hints(domains,question['question'],analysis)
            if decision_mode:
                # Low-cardinality lists already appear once, within their budget.
                payload['entity_catalog']['columns']=[{**d,'actual_values':d['matched_question_values'],'complete':False}
                    for d in payload['entity_catalog']['columns'] if d['matched_question_values']]
            log['entity_resolution']=payload['entity_catalog']
        with profiler.stage('call_2',model_call_index=2):
            plan=pipeline.client.call('qa_sql',{**payload,'analysis':analysis},SQL_SCHEMA)
            log['plan']=plan;log['model_sql']=plan.get('sql')
        if plan['status']!='answerable':
            final={'id':question['id'],'type':plan['status']}
            if plan['status']=='unanswerable':final['reason']=plan['reason']
        else:
            if not isinstance(plan['sql'],str) or not plan['sql'].strip():
                raise PipelineError('Answerable output requires SQL')
            with profiler.stage('entity_literal_validation'):
                checked,adjustments=validate_literals(plan['sql'],metadata,domains,tool)
                log['entity_literal_adjustments']=adjustments;log['sql']=checked
            with profiler.stage('postgres_execution'):
                result=tool.execute_readonly_sql(checked,purpose='answer')
                log['result']=result
            with profiler.stage('result_validation'):
                # Use cursor output, without a duplicate alias declaration.
                result_plan={**plan,'output_columns':result['columns'],'null_policy':'abstain',
                    '_timezone':pipeline.settings.get('calendar',{}).get('timezone','Asia/Ho_Chi_Minh')}
                final=serialize_result(question,result_plan,result)
    except Exception as error:
        log.update(error_type=type(error).__name__,error=str(error))
        final={'id':question.get('id'),'type':'abstain'}
    calls=[{**c,'global_call_index':c.get('model_call_index'),'model_call_index':i+1}
           for i,c in enumerate(pipeline.client.calls[call_start:])]
    log.update(answer=final,elapsed_ms=max(0,round((perf_counter()-started)*1000)),
        stages=profiler.stages,model_calls=calls,
        model_call_count=sum(not c.get('cache_hit',False) for c in calls),
        thinking=[c.get('thinking') for c in calls] if any(c.get('thinking') for c in calls) else None,
        code='datamate_v2/qa_pipeline/direct_sql.py::answer_direct',
        inputs={'question':question,'metadata_fingerprint':metadata.get('fingerprint')},
        output=log.get('result'),queries=tool.trace if tool else [])
    return final,log
