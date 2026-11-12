"""Compile a bounded relational plan. The SLM, not Python, chooses business rules."""
from copy import deepcopy
from decimal import Decimal
import jsonschema
from sqlglot import exp
from datamate_v2.common import PipelineError
from .business_schema import BUSINESS_PLAN


def rule_ids(plan):
    """Check references only. Excerpts may be paraphrased, as requested."""
    rules=plan.get('selected_rules',plan.get('applicable_rules',[]));ids=set()
    for rule in rules:
        if rule['id'] in ids or rule['id'] in ('question','facts'):raise PipelineError('Duplicate/reserved business rule id')
        ids.add(rule['id'])
    return ids|{'question','facts'}


def compile_plan(plan,metadata,windows,question):
    business=plan.get('business_plan')
    if business is None:return plan,{'mode':'legacy_sql'}
    bases=rule_ids(plan)
    if business['mode']=='raw_sql':return plan,{'mode':'raw_sql','reason':business['fallback_reason']}
    try:jsonschema.validate(business,BUSINESS_PLAN)
    except jsonschema.ValidationError as error:raise PipelineError('Invalid structured plan: '+error.message) from error
    if plan['summary_policy']!='exclude':raise PipelineError('Compiled mode supports summary_policy=exclude only; use explicit raw_sql for summary-row questions')
    tables={t['sql_table']:t for t in metadata['tables']};sources=business['sources'];lookups=business['lookups']
    business=deepcopy(business);ignored_date_filters=[]
    if plan.get('_rule_selection_from_call1'):
        bound_dates={b['column'] for b in plan['time_bindings'] if b['table'] in sources}
        filters=[]
        for condition in business['filters']:
            if condition['relation']=='source' and condition['column'] in bound_dates:
                ignored_date_filters.append(condition)
            else:filters.append(condition)
        business['filters']=filters
    if any(t not in tables for t in sources):raise PipelineError('Structured source table is not in the routed catalog')
    lookup=lookups[0] if lookups else None
    if lookup and (lookup['table'] not in tables or lookup['table'] in sources):raise PipelineError('Invalid structured lookup table')
    fields={}
    def register(field):
        key=(field['relation'],field['column'])
        locations=sources if key[0]=='source' else [lookup['table']] if lookup else []
        if not locations:raise PipelineError('Plan references lookup fields without a lookup JOIN')
        columns=[]
        for table in locations:
            column=next((c for c in tables[table]['columns'] if c['sql_column']==key[1]),None)
            if column is None:raise PipelineError(f'Structured column {table}.{key[1]} does not exist')
            columns.append(column)
        # Reusing the same physical name across months needs compatible meanings.
        if len({(c['type'],c.get('meaning')) for c in columns})!=1:
            raise PipelineError('UNION fields have incompatible type/meaning; use explicit raw_sql')
        fields.setdefault(key,f'_v{len(fields)}')
        return key
    def evidence(item):
        if item['basis'] not in bases:raise PipelineError(f"Unknown business basis {item['basis']!r}; use a selected rule id, question, or facts")
    for group in business['group_by']:register(group);evidence(group)
    for aggregate in business['aggregates']:
        evidence(aggregate)
        fn=aggregate['function'];columns=aggregate['columns']
        if (fn=='ROWS' and columns) or (fn!='ROWS' and len(columns)!=(2 if fn=='COUNT_DISTINCT' and len(columns)==2 else 1)):
            raise PipelineError('Unsupported aggregate arity; ROWS uses no columns, other aggregates one (COUNT_DISTINCT may use two)')
        for col in columns:register(col)
        if fn in ('SUM','AVG'):
            key=(columns[0]['relation'],columns[0]['column']);table=sources[0] if key[0]=='source' else lookup['table']
            typ=next(c['type'] for c in tables[table]['columns'] if c['sql_column']==key[1])
            if typ not in ('NUMERIC','BIGINT','INTEGER','DOUBLE PRECISION','REAL'):raise PipelineError('SUM/AVG requires a numeric column')
    for condition in business['filters']:register(condition);evidence(condition)
    for condition in business['having']:evidence(condition)
    if plan.get('_rule_selection_from_call1'):
        for condition in business['filters']:
            key=(condition['relation'],condition['column']);table=sources[0] if key[0]=='source' else lookup['table']
            typ=next(c['type'] for c in tables[table]['columns'] if c['sql_column']==key[1])
            if typ in ('DATE','TIMESTAMPTZ'):
                raise PipelineError('Use verified time_bindings for date constraints; additional date fields require raw_sql')
    if lookup:
        relations=metadata.get('relationships',[])
        for source in sources:
            pair=((source,lookup['source_column']),(lookup['table'],lookup['lookup_column']))
            def match(r):
                endpoints=((r.get('from_table'),r.get('from_column')),(r.get('to_table'),r.get('to_column')))
                return r.get('status')=='verified' and (endpoints==pair or endpoints==pair[::-1])
            if not any(match(r) for r in relations):raise PipelineError('Structured lookup JOIN is not a verified relationship')
        # Existence/type checks apply to join keys too, even if not projected.
        register({'relation':'source','column':lookup['source_column']});register({'relation':'lookup','column':lookup['lookup_column']})
    def value(v):
        if isinstance(v,bool):return exp.Boolean(this=v)
        if isinstance(v,str):return exp.Literal.string(v)
        if isinstance(v,(int,float,Decimal)):return exp.Literal.number(str(v))
        raise PipelineError('Unsupported filter literal')
    operators={'eq':exp.EQ,'ne':exp.NEQ,'gt':exp.GT,'gte':exp.GTE,'lt':exp.LT,'lte':exp.LTE}
    def predicate(left,op,values):
        if op in ('is_null','is_not_null'):
            if values:raise PipelineError('NULL predicate must have no values')
            term=exp.Is(this=left,expression=exp.Null())
            return exp.Not(this=term) if op=='is_not_null' else term
        if op in ('in','not_in'):
            if not values:raise PipelineError('IN requires nonempty values')
            term=exp.In(this=left,expressions=[value(v) for v in values])
            return exp.Not(this=term) if op=='not_in' else term
        if op not in operators or len(values)!=1:raise PipelineError('Comparison requires exactly one value')
        return operators[op](this=left,expression=value(values[0]))
    def and_all(items):
        combined=items[0]
        for item in items[1:]:combined=exp.and_(combined,item)
        return combined
    window_map={w['name']:w for w in windows};bindings=plan['time_bindings'];used_windows=set()
    if any(b['table'] not in sources for b in bindings):raise PipelineError('Compiled mode only binds dates of source tables')
    branches=[]
    for source in sources:
        def col(field):return exp.column(field['column'],table='s' if field['relation']=='source' else 'l',quoted=True)
        conditions=[exp.EQ(this=exp.column('_is_summary_row',table='s',quoted=True),expression=exp.Boolean(this=False))]
        local=[b for b in bindings if b['table']==source]
        if windows and len(local)!=1:raise PipelineError('Compiled mode needs one validated time binding per source; use raw_sql for multiple windows on one table')
        if not windows and local:raise PipelineError('Unexpected time binding without validated windows')
        for binding in local:
            if binding['window'] not in window_map:raise PipelineError('Unknown compiled window binding')
            date_column=next((c for c in tables[source]['columns'] if c['sql_column']==binding['column']),None)
            if not date_column or date_column['type'] not in ('DATE','TIMESTAMPTZ'):raise PipelineError('Compiled time binding requires a real date/timestamp column')
            w=window_map[binding['window']];used_windows.add(binding['window'])
            date_col=exp.column(binding['column'],table='s',quoted=True)
            for op,key in [(exp.GTE,'start_date'),(exp.LT,'end_date_exclusive')]:
                if w[key] is not None:conditions.append(op(this=date_col.copy(),expression=exp.Cast(this=exp.Literal.string(w[key]),to=exp.DataType.build('DATE'))))
        for condition in business['filters']:
            conditions.append(predicate(col(condition),condition['operator'],condition['values']))
        projected=[exp.alias_(col({'relation':rel,'column':name}),alias,quoted=True) for (rel,name),alias in fields.items()]
        if not projected:projected=[exp.alias_(exp.Literal.number(1),'_row',quoted=True)]
        query=exp.select(*projected).from_(exp.Table(this=exp.to_identifier(source,quoted=True),alias=exp.TableAlias(this=exp.to_identifier('s'))))
        if lookup:
            query=query.join(exp.Table(this=exp.to_identifier(lookup['table'],quoted=True),alias=exp.TableAlias(this=exp.to_identifier('l'))),
                on=exp.EQ(this=exp.column(lookup['source_column'],table='s',quoted=True),expression=exp.column(lookup['lookup_column'],table='l',quoted=True)),join_type='INNER')
            conditions.append(exp.EQ(this=exp.column('_is_summary_row',table='l',quoted=True),expression=exp.Boolean(this=False)))
        query.set('where',exp.Where(this=and_all(conditions)));branches.append(query)
    if used_windows!=set(window_map):raise PipelineError('Compiled sources do not cover all validated windows')
    records=branches[0]
    for branch in branches[1:]:records=exp.Union(this=records,expression=branch,distinct=False)
    def outer_col(field):return exp.column(fields[(field['relation'],field['column'])],table='_records',quoted=True)
    expressions={};group_expressions=[]
    for group in business['group_by']:
        alias=group['alias']
        if alias in expressions:raise PipelineError('Duplicate structured output alias')
        expressions[alias]=outer_col(group);group_expressions.append(expressions[alias].copy())
    aggregate_names=set()
    for aggregate in business['aggregates']:
        alias=aggregate['alias'];fn=aggregate['function'];columns=[outer_col(c) for c in aggregate['columns']]
        if alias in expressions:raise PipelineError('Duplicate structured output alias')
        if fn=='ROWS':expr=exp.Count(this=exp.Star())
        elif fn=='COUNT_DISTINCT':
            arg=columns[0] if len(columns)==1 else exp.Tuple(expressions=columns)
            expr=exp.Count(this=exp.Distinct(expressions=[arg]))
        else:expr={'SUM':exp.Sum,'COUNT':exp.Count,'AVG':exp.Avg,'MIN':exp.Min,'MAX':exp.Max}[fn](this=columns[0])
        expressions[alias]=expr;aggregate_names.add(alias)
    targets=plan['output_columns'];count_groups=business['result_mode']=='count_groups';output_adjusted=False
    if count_groups:
        if not group_expressions or len(targets)!=1 or plan['answer_type']!='number' or business['order_by'] or business['limit'] is not None:
            raise PipelineError('count_groups requires groups, one numeric output and no ranking/limit')
        select_names=list(expressions)
    else:
        if any(t not in expressions for t in targets):
            if len(expressions)==len(targets)==1 and plan['answer_type'] in ('number','percent','string','date','boolean'):
                targets=list(expressions);output_adjusted=True
            else:raise PipelineError('output_columns must name structured group/aggregate aliases')
        select_names=targets
    query=exp.select(*[exp.alias_(expressions[a].copy(),a,quoted=True) for a in select_names]).from_(records.subquery('_records'))
    if group_expressions:query.set('group',exp.Group(expressions=group_expressions))
    having=[]
    for condition in business['having']:
        if condition['aggregate'] not in aggregate_names:raise PipelineError('HAVING must reference a declared aggregate')
        having.append(predicate(expressions[condition['aggregate']].copy(),condition['operator'],[condition['value']]))
    if having:query.set('having',exp.Having(this=and_all(having)))
    order=[]
    for item in business['order_by']:
        if item['column'] not in expressions:raise PipelineError('Ordering references an undeclared result')
        order.append(exp.Ordered(this=expressions[item['column']].copy(),desc=item['direction']=='DESC',nulls_first=False))
    if order:query.set('order',exp.Order(expressions=order))
    if business['limit'] is not None:query=query.limit(business['limit'])
    if count_groups:query=exp.select(exp.alias_(exp.Count(this=exp.Star()),targets[0],quoted=True)).from_(query.subquery('_groups'))
    result=deepcopy(plan);result['business_plan']=business;result['sql']=query.sql(dialect='postgres');result['output_columns']=targets
    result['required_tables']=list(dict.fromkeys(sources+([lookup['table']] if lookup else [])))
    return result,{'mode':'compiled','sources':sources,'lookup':lookup['table'] if lookup else None,
                   'filters_compiled':len(business['filters']),'rules_provided':len(plan.get('selected_rules',plan.get('applicable_rules',[]))),'output_alias_adjusted':output_adjusted,
                   'ignored_model_date_filters':ignored_date_filters,
                   'note':'SQL follows the structured decisions; source selection and business interpretation remain model decisions.'}
