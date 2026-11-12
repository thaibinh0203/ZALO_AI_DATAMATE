"""Two-call QA baseline; at most one explicit inference repair."""
from datetime import date
from time import perf_counter
from datamate_v2.common import PipelineError,ContextBudgetError,dumps
from datamate_v2.contracts import validate_question
from datamate_v2.database.tools import QueryTool
from datamate_v2.evaluation.latency_profiler import Profiler
from .query_rewriting.rewriter import rewrite_question
from .query_rewriting.temporal_facts import temporal_facts
from .query_rewriting.rule_selection import build_selection_context
from .temporal_validator import validate_windows
from .planning_sql.planner import generate_plan
from .planning_sql.compiler import compile_plan
from .planning_sql.feedback import repair_feedback
from .sql_validation.guard import validate_sql,scoped_quality_queries
from .sql_validation.summary_policy import enforce_summary_policy
from .execution.executor import run_checks
from .result_validation.validator import serialize_result
from .context import build_qa_context
from .entities import resolve_entities,validate_entity_literals,compact_resolutions
from .routing import route_metadata
from .sql_validation.ast_plan import derive_column_usage,project_answer,normalize_scalar_alias
from .sql_validation.guard import parse_readonly


def model_windows(windows):
    return [{k:v for k,v in w.items() if k not in ('proposed_start_date','proposed_end_date_exclusive','corrected')} for w in windows]

class QAPipeline:
    def __init__(self,client,database,settings,tool_factory=QueryTool):
        if settings.get('max_repairs',1) not in (0,1): raise PipelineError('QA permits at most one shared inference repair')
        self.client=client;self.database=database;self.settings=settings;self.tool_factory=tool_factory

    def answer(self,question,metadata):
        if self.settings.get('mode')=='direct_sql':
            from .direct_sql import answer_direct
            return answer_direct(self,question,metadata)
        started=perf_counter();call_start=len(self.client.calls)
        profiler=Profiler(question);log={'id':question.get('id'),'business_id':question.get('business_id'),'question':question,
             'model_version':self.client.version,'data_fingerprint':metadata.get('fingerprint'),'repair_attempts':0,'checks':[]}
        final={'id':question.get('id'),'type':'abstain'};tool=None
        try:
            with profiler.stage('input_validation'):
                validate_question(question)
                if metadata['business_id']!=question['business_id']: raise PipelineError('Business isolation mismatch')
                if self.database.get('timezone',self.settings['calendar']['timezone'])!=self.settings['calendar']['timezone']:
                    raise PipelineError('Database and QA business timezones must match')
            repair_budget=self.settings.get('max_repairs',1)
            selection_context=None
            if self.settings.get('rule_selection_in_call1',False):
                with profiler.stage('rule_selection_context'):
                    facts=temporal_facts(question,self.settings['calendar'])
                    preliminary=validate_windows(facts['windows'],question['as_of_date'],calendar_csv=metadata['calendar_csv'],**self.settings['calendar']) if facts else []
                    selection_metadata,_=route_metadata(metadata,preliminary)
                    selection_context=build_selection_context(selection_metadata)
                    log['rule_selection_context_summary']={'tables':len(selection_context['tables']),'characters':len(dumps(selection_context))}
            rewrite_error=None
            for rewrite_attempt in range(repair_budget+1):
                if rewrite_attempt:
                    log['repair_attempts']+=1
                try:
                    with profiler.stage('rewrite_repair' if rewrite_attempt else 'call_1',model_call_index=len(self.client.calls)-call_start+1,is_repair=bool(rewrite_attempt)):
                        rewrite=rewrite_question(question,self.client,self.settings['calendar'],repair=bool(rewrite_attempt),error=rewrite_error,ingestion_context=selection_context)
                        if '_temporal_facts' in rewrite:log['temporal_facts']=rewrite.pop('_temporal_facts')
                        log['rewritten']=rewrite
                        if selection_context is not None:log['selected_rules']=rewrite['selected_rules']
                    with profiler.stage('temporal_validation'):
                        if any(w['status']=='ambiguous' for w in rewrite['time_windows']):
                            raise PipelineError('Ambiguous time interpretation; cannot resolve without evidence')
                        windows=validate_windows(rewrite['time_windows'],question['as_of_date'],calendar_csv=metadata['calendar_csv'],**self.settings['calendar'])
                        log['validated_windows']=windows
                    break
                except PipelineError as error:
                    if isinstance(error,ContextBudgetError) or 'Ambiguous time interpretation' in str(error) or rewrite_attempt>=repair_budget: raise
                    rewrite_error=str(error)
                    log.setdefault('repair_details',[]).append({'stage':'call_1','error':rewrite_error})
            remaining_repairs=max(0,repair_budget-log['repair_attempts'])
            with profiler.stage('context_loading'):
                routed,routing=route_metadata(metadata,windows)
                context=build_qa_context(routed)
                if selection_context is not None:
                    context.pop('rulebook',None)
                    context['rulebook_selected_only']=True
                context['routing']=routing
                log['routing']=routing
                log['context_summary']={'format':context['format'],'tables':len(context['tables']),
                    'schema_groups':len(context['schema_groups']),'characters':len(dumps(context))}
                payload={'question':question,'rewrite':{**{k:v for k,v in rewrite.items() if k!='selected_rules'},'time_windows':model_windows(windows)},'validated_windows':model_windows(windows),'business_context':context,
                         'structured_planning':self.settings.get('structured_planning',False),
                         'temporal_policy':{'binding_names':[w['name'] for w in windows],
                            'instruction':'Use these exact window names and Python bounds. Omit temporal_override when the bounds are already correct; only a real change backed by README/Rulebook may override.'},
                         'execution_policy':{'summary_default':'exclude','required_predicate':'alias._is_summary_row = FALSE in the WHERE of every physical table scope; explicit summary-row questions require include_explicit and quote from the question'}}
                if selection_context is not None:
                    payload['rule_selection_from_call1']=True
                    payload['selected_rules']=rewrite['selected_rules']
                    payload['temporal_policy']['instruction']='Use only these Python-validated window names/bounds via time_bindings. Do not generate temporal_override or repeat date conditions in business_plan.filters.'
                tool=self.tool_factory(metadata,self.database,validate_sql,max_queries=self.settings['max_queries'])
            with profiler.stage('python_exploration'):
                explored=resolve_entities(rewrite['entities'],metadata,tool,self.settings.get('max_exploration_queries',2),original_question=question['question'])
                payload['exploration']=compact_resolutions(explored,context);log['exploration']=explored
            plan=None;previous_invalid_sql=None
            for attempt in range(1+remaining_repairs):
                is_repair=attempt>0
                stage='repair' if is_repair else 'call_2'
                try:
                    with profiler.stage(stage,model_call_index=len(self.client.calls)-call_start+1,is_repair=is_repair):
                        if is_repair:
                            log['repair_attempts']+=1
                        plan=generate_plan(payload,self.client,repair=is_repair);log['plan']=plan
                        log['quality_check_adjustments']=plan.get('_quality_check_adjustments',{})
                        if is_repair and plan.get('sql') and previous_invalid_sql:
                            log['repair_sql_changed']=parse_readonly(plan['sql']).sql(dialect='postgres')!=parse_readonly(previous_invalid_sql).sql(dialect='postgres')
                    if plan['status']!='answerable':
                        final={'id':question['id'],'type':plan['status']}
                        if plan['status']=='unanswerable': final['reason']=plan['reason']
                        break
                    with profiler.stage('temporal_revalidation'):
                        override=plan.get('temporal_override')
                        if override:
                            proposed=validate_windows(override['time_windows'],question['as_of_date'],calendar_csv=metadata['calendar_csv'],**self.settings['calendar'])
                            def boundaries(items):
                                return {w['name']:(w['start_date'],w['end_date_exclusive'],w['timezone'],w['calendar_rule']) for w in items}
                            if boundaries(proposed)==boundaries(windows):
                                # A no-op supplies no new temporal authority. Keep Python's
                                # existing windows even when the model quotes the question.
                                log['temporal_override_ignored']='Same boundaries as Python-validated windows'
                            else:
                                quote=override.get('source_quote','')
                                if not quote or not any(quote in source for source in (metadata['readme'],metadata['rulebook'])):
                                    raise PipelineError('Calendar override lacks literal source evidence')
                                if {w['name'] for w in proposed}!={w['name'] for w in windows}:
                                    raise PipelineError('Calendar override must preserve validated window names')
                                windows=proposed
                                log['temporal_override']=override;payload['validated_windows']=model_windows(windows)
                            plan.pop('temporal_override');log['validated_windows']=windows
                        plan['_validated_windows']=windows;plan['_original_question']=question['question']
                    with profiler.stage('sql_guard'):
                        log['model_sql']=plan['sql']
                        log['model_output_columns']=list(plan['output_columns'])
                        log['model_column_usage']=list(plan.get('column_usage',[]))
                        plan,compilation=compile_plan(plan,routed,windows,question)
                        log['plan']=plan;log['compilation']=compilation
                        normalized,alias_adjustments=normalize_scalar_alias(plan['sql'],plan)
                        governed,adjustments=enforce_summary_policy(normalized,routed,plan,question['question'])
                        log['sql_adjustments']=adjustments
                        governed=derive_column_usage(governed,routed,plan)
                        projected,projection=project_answer(governed,plan)
                        log['projection_adjustments']=alias_adjustments+projection
                        checked=validate_sql(projected,routed,plan=plan);log['sql']=checked
                        validate_entity_literals(checked,metadata,explored)
                        for check in plan['quality_checks']:
                            validate_entity_literals(validate_sql(check['sql'],metadata),metadata,explored)
                    break
                except PipelineError as error:
                    if getattr(error,'column_diagnostics',None):
                        log.setdefault('column_errors',[]).extend(error.column_diagnostics)
                    if is_repair and log.get('repair_sql_changed') is False:
                        log['repair_repeated_invalid_sql']=True
                    if isinstance(error,ContextBudgetError) or attempt>=remaining_repairs: raise
                    # Only structured generation / SQL guard failures may use the single repair.
                    previous=getattr(error,'plan',None) or plan or {}
                    previous_invalid_sql=previous.get('sql')
                    history={key:previous[key] for key in ('sql','applicable_rules','business_plan','answer_type','output_columns','required_tables','column_usage','time_bindings','quality_checks') if key in previous}
                    if previous.get('business_plan',{}).get('mode')=='compiled':
                        # The generated SQL is derivable from the DSL and can be
                        # large for monthly UNIONs. Repair the decisions, not a
                        # second copy of compiler output in the context budget.
                        history['sql']=None
                        history.pop('required_tables',None);history.pop('quality_checks',None)
                    payload={**payload,'repair':{'error':str(error),
                        'previous_plan':history,
                        **repair_feedback(previous,context,model_windows(windows)),
                        'instruction':'Fix the specific errors, use only catalog columns, exact validated boundaries and canonical entities. Every computed SELECT output needs AS alias; output_columns must contain alias names only, never SQL expressions or AS clauses. Return a complete compact plan: at most 6 short steps, 3 checks, check reason <=160 chars, overall reason <=300 chars. Do not repeat invalid SQL or explanations.'}}
                    if selection_context is not None:
                        payload['repair']['instruction']+=' Keep selected_rules from Call1; do not select/copy rules again. Put dates only in time_bindings, not business_plan.filters. In compiled mode return sql=null and quality_checks=[]; repair the structured operations.'
                    log['repair_error']=str(error)
            if plan and plan['status']=='answerable':
                with profiler.stage('data_coverage'):
                    bindings=plan.get('time_bindings',[]);out_of_coverage=[]
                    for binding in bindings:
                        table=next(t for t in metadata['tables'] if t['sql_table']==binding['table'])
                        stats=next(s for s in table['stats'] if s['sql_column']==binding['column'])
                        window=next(w for w in windows if w['name']==binding['window'])
                        business_coverage=metadata.get('data_coverage',{})
                        low=business_coverage.get('start_date') or (str(stats['min'])[:10] if stats['min'] is not None else None)
                        high=business_coverage.get('end_date_inclusive') or (str(stats['max'])[:10] if stats['max'] is not None else None)
                        if low and high:
                            outside=window['end_date_exclusive']<=low or (window['start_date'] is not None and window['start_date']>high)
                            out_of_coverage.append(outside)
                    if out_of_coverage and any(out_of_coverage):
                        final={'id':question['id'],'type':'unanswerable','reason':'Kỳ hỏi nằm hoàn toàn ngoài khoảng dữ liệu có sẵn.'}
                        log['coverage_status']='outside';plan=None
                if plan:
                    with profiler.stage('data_quality_validation'):
                        log['checks']=run_checks(tool,scoped_quality_queries(checked,metadata,plan))
                        model_checks=[{**c,'sql':validate_sql(c['sql'],metadata)} for c in plan['quality_checks']]
                        model_results=run_checks(tool,model_checks,answer_sql=checked)
                        log['checks']+=model_results
                        if plan.get('null_policy')=='zero_with_evidence':
                            evidence=plan.get('null_evidence','')
                            if not evidence or evidence not in metadata['rulebook']+metadata['readme'] or not any(c['status']=='passed' for c in model_results):
                                raise PipelineError('NULL-to-zero requires source evidence and independently executed check')
                            plan['_null_zero_validated']=True
                    with profiler.stage('postgres_execution'):
                        result=tool.execute_readonly_sql(checked,purpose='answer',plan=plan);log['result']=result
                    with profiler.stage('result_validation'):
                        final=serialize_result(question,plan,result)
        except Exception as error:
            if getattr(error,'check_results',None):log['checks']+=error.check_results
            log.update(error_type=type(error).__name__,error=str(error));final={'id':question.get('id'),'type':'abstain'}
        with profiler.stage('serialization'):
            log['answer']=final
        calls=[{**c,'global_call_index':c.get('model_call_index'),'model_call_index':i+1} for i,c in enumerate(self.client.calls[call_start:])]
        log.update(elapsed_ms=max(0,round((perf_counter()-started)*1000)),stages=profiler.stages,
                   model_calls=calls,model_call_count=sum(not c.get('cache_hit',False) for c in calls),
                   thinking=[c.get('thinking') for c in calls] if any(c.get('thinking') for c in calls) else None,
                   code='datamate_v2/qa_pipeline/pipeline.py::QAPipeline.answer',
                   inputs={'question':question,'metadata_fingerprint':metadata.get('fingerprint')},
                   output=log.get('result'),queries=tool.trace if tool else [])
        return final,log
