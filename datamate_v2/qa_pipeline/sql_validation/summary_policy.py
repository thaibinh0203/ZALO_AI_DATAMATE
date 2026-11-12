"""Complete an explicit exclusion policy in proven-safe physical table scopes."""
from sqlglot import exp
from datamate_v2.common import fold,PipelineError
from .guard import validate_sql,parse_readonly,conjuncts,_base_scopes


def enforce_summary_policy(statement,metadata,plan,question):
    checked=validate_sql(statement,metadata)
    if plan.get('summary_policy')!='exclude' or 'dong tong' in fold(question):return checked,[]
    tree=parse_readonly(checked);tables={t['sql_table']:t for t in metadata['tables']};adjustments=[]
    for index,(scope,bases) in enumerate(_base_scopes(tree,tables)):
        query=scope.expression
        if not bases or not isinstance(query,exp.Select):continue
        # WHERE on the nullable side of an outer join can destroy unmatched rows.
        # Leave these scopes unchanged for the ordinary guard, never rewrite them.
        if any(j.side.upper() in ('LEFT','RIGHT','FULL') or j.kind.upper() not in ('','INNER','CROSS') or j.args.get('method')=='NATURAL'
               for j in query.args.get('joins',[])):
            continue
        where=query.args.get('where')
        if where and where.find(exp.Or):continue
        for alias,table in bases.items():
            where=query.args.get('where')
            predicates=conjuncts(where.this) if where else []
            found=False
            for predicate in predicates:
                col=None
                if isinstance(predicate,exp.EQ) and isinstance(predicate.this,exp.Column) and isinstance(predicate.expression,exp.Boolean) and predicate.expression.this is False:
                    col=predicate.this
                elif isinstance(predicate,exp.Is) and isinstance(predicate.this,exp.Column) and isinstance(predicate.expression,exp.Boolean) and predicate.expression.this is False:
                    col=predicate.this
                elif isinstance(predicate,exp.Not) and isinstance(predicate.this,exp.Column):col=predicate.this
                if col is not None and col.table==alias and col.name=='_is_summary_row':
                    if not isinstance(predicate,exp.EQ):
                        predicate.replace(exp.EQ(this=col.copy(),expression=exp.Boolean(this=False)))
                        adjustments.append({'action':'canonicalize_summary_exclusion','table':table,'alias':alias,'scope':index})
                    found=True
            if found:continue
            # Do not silently convert an explicit include/ambiguous summary condition
            # into an impossible query by appending FALSE.
            # scope.columns excludes columns owned by nested SELECTs, including
            # an IN/EXISTS subquery that happens to reuse this physical alias.
            if where and any(c.find_ancestor(exp.Select) is query and c.table==alias and c.name=='_is_summary_row' for c in where.find_all(exp.Column)):
                raise PipelineError(f'Conflicting/unsupported summary condition for {table} alias {alias}; summary_policy=exclude requires {alias}._is_summary_row = FALSE')
            condition=exp.EQ(this=exp.column('_is_summary_row',table=alias,quoted=True),expression=exp.Boolean(this=False))
            where=query.args.get('where')
            query.set('where',exp.Where(this=exp.and_(where.this.copy(),condition) if where else condition))
            where=query.args['where']
            adjustments.append({'action':'add_summary_exclusion','table':table,'alias':alias,'scope':index})
    # Validate all names/scopes again after mutation. Full plan validation still
    # runs later, including relationships, dates, uncertain columns and results.
    return validate_sql(tree.sql(dialect='postgres'),metadata),adjustments
