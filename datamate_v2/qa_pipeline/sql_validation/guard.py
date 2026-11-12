"""AST allowlist plus schema, scope, relationship and temporal checks."""
import sqlglot
from datetime import date,datetime,time,timedelta
from zoneinfo import ZoneInfo
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope,Scope
from datamate_v2.common import PipelineError

FUNCTIONS={'SUM','COUNT','AVG','MIN','MAX','COALESCE','NULLIF','ROUND','ABS','CAST','EXTRACT','DATE_TRUNC',
 'LOWER','UPPER','TRIM','LENGTH','REPLACE','CONCAT','CONCAT_WS','SUBSTRING','ROW_NUMBER','RANK','DENSE_RANK',
 'LAG','LEAD','GREATEST','LEAST','CASE','IF','ARRAY_AGG','STRING_AGG','BOOL_AND','BOOL_OR','CEIL','FLOOR',
 'POWER','MOD','DATE','TIMESTAMP','TIMESTAMPDIFF','TIMESTAMPADD','DATE_DIFF','TO_CHAR',
 'TIMESTAMP_TRUNC','TIME_TO_STR','GROUP_CONCAT','EXISTS'}
SYSTEM={'_source_row':'BIGINT','_is_summary_row':'BOOLEAN','_summary_status':'TEXT'}

def conjuncts(expression):
    if isinstance(expression, exp.Paren): return conjuncts(expression.this)
    if isinstance(expression, exp.And): return conjuncts(expression.this)+conjuncts(expression.expression)
    return [expression]

def bound_matches(value,expected,timezone):
    if value==expected: return True
    try:
        stamp=datetime.fromisoformat(value)
        if stamp.tzinfo is None: return False
        local=stamp.astimezone(ZoneInfo(timezone))
        return local.date().isoformat()==expected and local.time()==time(0)
    except ValueError: return False

def parse_readonly(statement):
    try: trees=sqlglot.parse(statement,read='postgres')
    except Exception as error: raise PipelineError(f'Invalid PostgreSQL SQL: {error}') from error
    if len(trees)!=1 or not isinstance(trees[0],(exp.Select,exp.Union,exp.Intersect,exp.Except)):
        raise PipelineError('Only a single SELECT query is allowed')
    tree=trees[0]
    forbidden=('Insert','Update','Delete','Create','Drop','Alter','Command','Copy','Into','Lock','Transaction','Set','Use','Merge','Grant','Revoke','Execute','CurrentDate','CurrentTimestamp','CurrentTime','NextValueFor','Parameter','Placeholder','SessionParameter')
    for name in forbidden:
        cls=getattr(exp,name,None)
        if cls and tree.find(cls): raise PipelineError(f'Forbidden SQL node: {name}')
    for node in tree.find_all(exp.Func):
        if isinstance(node,(exp.And,exp.Or,exp.Not)): continue
        name=node.name.upper() if isinstance(node,exp.Anonymous) else node.sql_name()
        if name not in FUNCTIONS: raise PipelineError(f'Function not allowed: {name}')
        if isinstance(node.parent,exp.Dot): raise PipelineError('Schema-qualified functions are not allowed')
    for star in tree.find_all(exp.Star):
        if not isinstance(star.parent,exp.Count): raise PipelineError('Explicit output columns required; SELECT * is forbidden')
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this,exp.Identifier): raise PipelineError('Table functions not allowed')
    return tree

def _base_scopes(tree,tables):
    for scope in traverse_scope(tree):
        bases={alias:src.name for alias,src in scope.sources.items() if isinstance(src,exp.Table) and src.name in tables}
        yield scope,bases

def _single_aggregate_row(source):
    if not isinstance(source,Scope) or source.external_columns:return False
    query=source.expression
    if not isinstance(query,exp.Select):return False
    if any(query.args.get(k) for k in ('group','having','qualify','offset','distinct','limit')):return False
    if query.find(exp.Window) or query.find(exp.Subquery):return False
    return bool(query.expressions) and all(e.find(exp.AggFunc) is not None and
        all(c.find_ancestor(exp.AggFunc) is not None for c in e.find_all(exp.Column)) for e in query.expressions)

def column_diagnostics(tree,tables):
    """Diagnose physical aliases in their own scope; never guess replacements."""
    result=[]
    for index,(scope,bases) in enumerate(_base_scopes(tree,tables)):
        for column in scope.columns:
            alias=column.table
            if not alias and len(scope.sources)==1 and len(bases)==1:alias=next(iter(bases))
            if alias not in bases:continue  # CTE/derived columns need full qualify.
            table=bases[alias]
            allowed=[c['sql_column'] for c in tables[table]['columns']]+list(SYSTEM)
            if column.name in allowed:continue
            entry={'scope':index,'alias':alias,'table':table,'column':column.name,'allowed_columns':allowed,
                   'visible_candidates':[{'alias':a,'table':t} for a,t in bases.items()
                       if column.name in {c['sql_column'] for c in tables[t]['columns']}],
                   'instruction':'Do not copy a column from another table automatically. Recheck the business source, remove an unnecessary JOIN, or use a real column from the correct table.'}
            if entry not in result:result.append(entry)
    return result

def validate_sql(statement,metadata,plan=None):
    tree=parse_readonly(statement)
    tables={t['sql_table']:t for t in metadata['tables']}
    ctes={x.alias for x in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if table.name in ctes and not table.db and not table.catalog: continue
        if table.name not in tables or table.catalog or (table.db and table.db!=metadata['schema']):
            raise PipelineError(f'SQL table {table.sql(dialect="postgres")} outside current business allowlist; allowed physical tables: '+', '.join(sorted(tables)))
        # The database connection pins search_path. Qualify validates actual column scopes.
        table.set('db',None)
    schema={name:{**{c['sql_column']:c['type'] for c in table['columns']},**SYSTEM} for name,table in tables.items()}
    try: checked=qualify(tree.copy(),dialect='postgres',schema=schema,validate_qualify_columns=True,identify=True)
    except Exception as error:
        involved={t.name for t in tree.find_all(exp.Table) if t.name in tables}
        details='; '.join(name+': '+', '.join(schema[name]) for name in sorted(involved))
        diagnostics=column_diagnostics(tree,tables)
        specific='; '.join(f"Alias {d['alias']} refers to {d['table']}, which has no column {d['column']}; allowed: {', '.join(d['allowed_columns'])}. "
                          +('Column exists on visible aliases '+', '.join(x['alias']+' ('+x['table']+')' for x in d['visible_candidates'])+'; verify its business meaning before changing the reference.' if d['visible_candidates'] else 'No physical source in this scope has that column.') for d in diagnostics)
        failure=PipelineError(f'Unknown/ambiguous SQL column: {error}. {specific} Allowed columns by table: {details}')
        failure.column_diagnostics=diagnostics
        raise failure from error
    if plan is not None:
        _validate_plan(checked,metadata,plan)
    return checked.sql(dialect='postgres')

def _validate_plan(tree,metadata,plan):
    tables={t['sql_table']:t for t in metadata['tables']}
    if not plan.get('calculation_plan'): raise PipelineError('Missing calculation plan')
    actual={name for _,bases in _base_scopes(tree,tables) for name in bases.values()}
    if set(plan.get('required_tables',[]))!=actual: raise PipelineError('SQL tables differ from plan')
    usage={(u['table'],u['column']):u for u in plan.get('column_usage',[])}
    relations=metadata.get('relationships',[])
    windows={w['name']:w for w in plan.get('_validated_windows',[])}
    bound=set();matched_bindings=set()
    for scope,bases in _base_scopes(tree,tables):
        for column in scope.columns:
            if column.table in bases and not column.name.startswith('_'):
                table=bases[column.table];entry=usage.get((table,column.name))
                if not entry: raise PipelineError(f'Column missing from plan: {table}.{column.name}')
                col=next((c for c in tables[table]['columns'] if c['sql_column']==column.name),None)
                if col and col['semantic_status']!='verified':
                    evidence=entry.get('source_quote','')
                    if not evidence or evidence not in metadata['readme']+metadata['rulebook'] or col['original_column'] not in evidence:
                        raise PipelineError('Uncertain semantic mapping lacks source-backed planning resolution')
        where=scope.expression.args.get('where')
        predicates=conjuncts(where.this) if where else []
        if bases:
            # Conservatively reject OR in a safety-critical filter rather than mistake one branch as global evidence.
            if where and where.find(exp.Or): raise PipelineError('OR filters need explicit equivalent UNION branches for validation')
            for alias,table in bases.items():
                if plan.get('summary_policy')=='exclude':
                    found=any(isinstance(eq,exp.EQ) and isinstance(eq.this,exp.Column) and eq.this.table==alias and eq.this.name=='_is_summary_row' and isinstance(eq.expression,exp.Boolean) and not eq.expression.this for eq in predicates)
                    if not found: raise PipelineError(f'Missing summary-row exclusion for {table}')
                elif plan.get('summary_policy')=='include_explicit':
                    evidence=plan.get('summary_evidence','')
                    if not evidence or evidence not in plan.get('_original_question',''): raise PipelineError('Including summary rows requires explicit question evidence')
                else: raise PipelineError('Missing summary policy')
                candidates=[b for b in plan.get('time_bindings',[]) if b['table']==table]
                scope_matched=set();scope_errors=[]
                for binding in candidates:
                    try:
                        window=windows.get(binding['window'])
                        if not window or window['status']!='validated': raise PipelineError('Unvalidated SQL time binding')
                        if not where: raise PipelineError('Missing time filter')
                        column=next((c for c in tables[table]['columns'] if c['sql_column']==binding['column']),None)
                        equalities=[]
                        for node in predicates:
                            if not isinstance(node,exp.EQ): continue
                            for col,value in ((node.this,node.expression),(node.expression,node.this)):
                                if isinstance(col,exp.Column) and col.table==alias and col.name==binding['column']:
                                    literal=value.this if isinstance(value,exp.Cast) and value.to.sql().upper()=='DATE' else value
                                    equalities.append(literal.this if isinstance(literal,exp.Literal) and literal.is_string else None)
                        single_day=(column is not None and column['type']=='DATE' and window['start_date'] is not None and
                            date.fromisoformat(window['end_date_exclusive'])-date.fromisoformat(window['start_date'])==timedelta(days=1))
                        if equalities and single_day:
                            if any(value!=window['start_date'] for value in equalities):
                                raise PipelineError(f'SQL date equality differs from validated {binding["window"]}; expected DATE {window["start_date"]}')
                            # The equality proves this DATE window. Other explicit >=/<
                            # predicates still pass through the bound checks below.
                        for cls,key in ((exp.GTE,'start_date'),(exp.LT,'end_date_exclusive')):
                            expected=window[key]
                            if expected is None: continue
                            matches=[]
                            for node in predicates:
                                if not isinstance(node,cls): continue
                                if isinstance(node.this,exp.Column) and node.this.table==alias and node.this.name==binding['column']:
                                    literals=list(node.expression.find_all(exp.Literal))
                                    if isinstance(node.expression,exp.Literal): literals=[node.expression]
                                    matches.append(len(literals)==1 and bound_matches(literals[0].this,expected,window.get('timezone','Asia/Ho_Chi_Minh')))
                            if (not matches and not (equalities and single_day)) or (matches and not all(matches)):
                                raise PipelineError(f'SQL date bound differs from validated {binding["window"]}; use {binding["column"]} >= {window["start_date"]} and < {window["end_date_exclusive"]}')
                        bound.add(binding['window'])
                        matched_bindings.add((table,binding['column'],binding['window']))
                        scope_matched.add(binding['column'])
                    except PipelineError as error:
                        alternatives=[b for b in candidates if b['column']==binding['column']]
                        if len(alternatives)==1: raise
                        scope_errors.append(str(error))
                if {b['column'] for b in candidates}-scope_matched:
                    raise PipelineError('No validated window matches this table/column scope: '+ '; '.join(scope_errors))
        joins=scope.expression.args.get('joins') or []
        for join in joins:
            # A proven one-row aggregate cannot multiply the left population.
            # This supports scalar revenue comparisons without allowing arbitrary
            # joins of derived tables or weakening their inner table guards.
            if (join.kind.upper()=='CROSS' and not join.args.get('on') and
                    _single_aggregate_row(scope.sources.get(join.this.alias_or_name))):
                continue
            if not isinstance(join.this,exp.Table) or join.this.alias_or_name not in bases:
                raise PipelineError('JOIN of derived scopes needs a verified relationship implementation')
            on=join.args.get('on')
            if on is None or on.find(exp.Or): raise PipelineError('JOIN requires verified equality keys')
            valid=False;matched=[]
            for eq in on.find_all(exp.EQ):
                a,b=eq.this,eq.expression
                if isinstance(a,exp.Column) and isinstance(b,exp.Column) and a.table in bases and b.table in bases:
                    pair={(bases[a.table],a.name),(bases[b.table],b.name)}
                    for relation in relations:
                        if relation.get('status')=='verified' and pair=={(relation['from_table'],relation['from_column']),(relation['to_table'],relation['to_column'])}:
                            valid=True;matched.append(relation)
            if not valid: raise PipelineError('JOIN key relationship not verified')
            for relation in matched:
                if relation.get('cardinality')=='many_to_many': raise PipelineError('JOIN fanout risk')
                if relation.get('cardinality')=='many_to_one':
                    parent_aliases={a for a,t in bases.items() if t==relation['to_table']}
                    for aggregate in scope.expression.find_all(exp.AggFunc):
                        if isinstance(aggregate,exp.Count) and isinstance(aggregate.this,exp.Distinct): continue
                        if any(c.table in parent_aliases for c in aggregate.find_all(exp.Column)):
                            raise PipelineError('Aggregation on the one side after JOIN risks double counting')
    if set(windows)-bound: raise PipelineError('Not every validated time window is represented in SQL')
    required_bindings={(b['table'],b['column'],b['window']) for b in plan.get('time_bindings',[])}
    if required_bindings-matched_bindings: raise PipelineError('Not every declared table/column/window binding is represented in SQL')
    if plan.get('temporal_override'): raise PipelineError('Temporal override needs source-backed revalidation before SQL')


def scoped_quality_queries(statement,metadata,plan):
    """Derive guards from actual WHERE/JOIN scope, independent of model-authored checks."""
    tree=parse_readonly(statement)
    tree=qualify(tree,dialect='postgres',schema={t['sql_table']:{**{c['sql_column']:c['type'] for c in t['columns']},**SYSTEM} for t in metadata['tables']},validate_qualify_columns=True)
    tables={t['sql_table']:t for t in metadata['tables']};queries=[]
    for scope,bases in _base_scopes(tree,tables):
        if not bases: continue
        checks=[]
        for alias,table in bases.items():
            used={c.name for c in scope.columns if c.table==alias and not c.name.startswith('_')}
            for constraint in tables[table]['quality'].get('constraints',[]):
                if constraint['kind']=='quantity_price_total' and constraint['status']=='verified' and used.intersection(constraint['columns'].values()):
                    names=constraint['columns']
                    bad=exp.NEQ(this=exp.Mul(this=exp.column(names['quantity'],table=alias),expression=exp.column(names['unit_price'],table=alias)),expression=exp.column(names['line_total'],table=alias))
                    if plan.get('summary_policy')=='exclude': bad=exp.and_(bad,exp.EQ(this=exp.column('_is_summary_row',table=alias),expression=exp.Boolean(this=False)))
                    q=exp.select(exp.Count(this=exp.Star()).as_('constraint_violations')).from_(exp.Table(this=exp.to_identifier(table),alias=exp.TableAlias(this=exp.to_identifier(alias)))).where(bad)
                    queries.append({'sql':q.sql(dialect='postgres'),'expected':'zero','reason':'source-verified cross-column constraint'})
            for col in tables[table]['columns']:
                if col['sql_column'] in used:
                    stats=next(s for s in tables[table]['stats'] if s['sql_column']==col['sql_column'])
                    if stats['null_count'] and (col['type']!='TEXT' or stats.get('unresolved_count',0)>0):
                        # Removing WHERE prevents unresolved filter values from escaping the completeness check.
                        # This intentionally conservative whole-table check can abstain outside affected scope.
                        c=exp.column(col['sql_column'],table=alias)
                        condition=exp.Is(this=c,expression=exp.Null())
                        if plan.get('summary_policy')=='exclude':
                            condition=exp.and_(condition,exp.EQ(this=exp.column('_is_summary_row',table=alias),expression=exp.Boolean(this=False)))
                        q=exp.select(exp.Count(this=exp.Star()).as_('invalid_count')).from_(exp.Table(this=exp.to_identifier(table),alias=exp.TableAlias(this=exp.to_identifier(alias)))).where(condition)
                        queries.append({'sql':q.sql(dialect='postgres'),'expected':'zero','reason':'missing/unresolved used numeric/date values; conservative whole-table guard'})
            if tables[table]['quality']['uncertain_summary_rows']:
                checks.append(exp.EQ(this=exp.column('_summary_status',table=alias),expression=exp.Literal.string('uncertain')))
        if checks:
            clone=scope.expression.copy()
            if not isinstance(clone,exp.Select): raise PipelineError('Unsupported quality scope')
            clone.set('expressions',[exp.Count(this=exp.Star()).as_('uncertain_count')])
            for name in ('group','having','order','limit','offset','distinct','qualify','windows'): clone.set(name,None)
            condition=checks[0]
            for check in checks[1:]: condition=exp.or_(condition,check)
            original=clone.args.get('where')
            clone.set('where',exp.Where(this=exp.and_(original.this.copy(),condition) if original else condition))
            queries.append({'sql':clone.sql(dialect='postgres'),'expected':'zero','reason':'uncertain summary rows inside query scope'})
    return queries
