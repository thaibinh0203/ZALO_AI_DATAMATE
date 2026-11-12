"""Derive mechanical dependencies and preserve scalar final projections."""
from sqlglot import exp
from datamate_v2.common import PipelineError
from .guard import validate_sql,parse_readonly,_base_scopes

def normalize_scalar_alias(statement,plan):
    """Name a sole scalar expression without changing its calculation or rows."""
    tree=parse_readonly(statement)
    targets=plan.get('output_columns',[])
    if (plan.get('answer_type') not in ('number','percent','string','date','boolean') or
            not isinstance(tree,exp.Select) or len(tree.expressions)!=1 or len(targets)!=1):
        return statement,[]
    expression=tree.expressions[0]
    if isinstance(expression,exp.Column):
        return statement,[]
    if isinstance(expression,exp.Alias):
        # An explicitly named different target may indicate a real mismatch.
        # Only accept a declaration that spells this exact expression.
        if targets==[expression.alias]: return statement,[]
        try:
            declared=parse_readonly('SELECT '+targets[0])
            candidate=declared.expressions[0]
            if isinstance(candidate,exp.Alias): candidate=candidate.this
            equivalent=(len(declared.expressions)==1 and not declared.args.get('from') and not declared.args.get('from_') and
                        candidate.sql(dialect='postgres',normalize=True)==expression.this.sql(dialect='postgres',normalize=True))
        except PipelineError:
            equivalent=False
        if not equivalent: return statement,[]
        alias=expression.alias
    else:
        # Avoid shadowing a physical name or existing ORDER BY/CTE identifier.
        names={node.name for node in tree.find_all(exp.Identifier)}
        alias='_dm_answer_value';suffix=0
        while alias in names:
            suffix+=1;alias=f'_dm_answer_value_{suffix}'
        tree.set('expressions',[exp.alias_(expression.copy(),alias,quoted=True)])
    original=list(targets);plan['output_columns']=[alias]
    return tree.sql(dialect='postgres'),[{'action':'normalize_scalar_alias','declared_output_columns':original,
        'output_columns':[alias],'note':'Only the result name changed; aggregate, filters, grouping and cardinality still require validation.'}]


def derive_column_usage(statement,metadata,plan):
    checked=validate_sql(statement,metadata)
    tree=parse_readonly(checked);tables={t['sql_table']:t for t in metadata['tables']}
    supplied={(item['table'],item['column']):item for item in plan.get('column_usage',[])}
    used=set()
    for scope,bases in _base_scopes(tree,tables):
        used.update((bases[c.table],c.name) for c in scope.columns if c.table in bases and not c.name.startswith('_'))
    derived=[]
    for table,column in sorted(used):
        # Source evidence for uncertain semantics still has to come from the
        # plan. AST existence is not evidence of a business meaning.
        entry={'table':table,'column':column,'purpose':'SQL AST dependency'}
        if 'source_quote' in supplied.get((table,column),{}): entry['source_quote']=supplied[(table,column)]['source_quote']
        derived.append(entry)
    plan['column_usage']=derived
    return checked


def project_answer(statement,plan):
    tree=parse_readonly(statement)
    if not isinstance(tree,exp.Select):
        return statement,[]
    actual=[e.alias_or_name for e in tree.expressions]
    targets=plan['output_columns']
    if actual==targets: return statement,[]
    if not targets or len(set(actual))!=len(actual) or any(target not in actual for target in targets):
        raise PipelineError(f'Final projection mismatch: declared {targets}, SQL exposes {actual}; use explicit output aliases')
    if plan['answer_type'] in ('number','percent','string','date','boolean') and len(targets)==1:
        # Keep the entire original calculation, DISTINCT, ORDER BY and LIMIT
        # inside. Scalar validation still rejects zero/multiple result rows.
        query=exp.select(exp.column(targets[0],table='answer_projection',quoted=True)).from_(tree.subquery('answer_projection'))
        return query.sql(dialect='postgres'),[{'action':'project_scalar_answer','output_columns':targets,'calculation_columns':actual}]
    raise PipelineError('Final list/table projection must be explicit in SQL; cannot silently discard sort columns or ordering')
