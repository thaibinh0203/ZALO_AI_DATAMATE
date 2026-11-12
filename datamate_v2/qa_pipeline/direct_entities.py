"""Column-scoped values from PostgreSQL, not translated labels or samples."""
import re
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope,traverse_scope
from datamate_v2.common import PipelineError
from .entities import entity_key

ENTITY_COLUMNS={'product_name','category','product_category','status','customer_name',
    'customer_group','employee_name','branch_name','supplier_name','payment_method','channel','unit'}
CATEGORICAL={'status','category','product_category','customer_group','payment_method','channel','unit'}


def domain_columns(metadata):
    return [(t['sql_table'],c['sql_column']) for t in metadata['tables'] for c in t['columns']
        if c['type']=='TEXT' and (c['sql_column'] in ENTITY_COLUMNS or c.get('meaning') in ENTITY_COLUMNS)]


def entity_hints(domains,question,analysis):
    mentions=[s for s in analysis.get('entity_mentions',[]) if isinstance(s,str)]
    texts=[entity_key(question)]+[entity_key(s) for s in mentions]
    result=[];groups={}
    for d in domains:
        matches=[v for v in d['values'] if entity_key(v) and any(
            re.search(r'(?<!\w)'+re.escape(entity_key(v))+r'(?!\w)',s) for s in texts)]
        values=d['values'] if d['column'] in CATEGORICAL and len(d['values'])<=128 else matches
        key=(d['column'],tuple(values),d['complete'])
        if key in groups:
            groups[key]['tables'].append(d['table']);continue
        item={'tables':[d['table']],'column':d['column'],'actual_values':values,
            'matched_question_values':matches,'complete':d['complete'] and values==d['values']}
        groups[key]=item;result.append(item)
    return {'instruction':'Values are read from the actual stated tables/columns. Use actual_values verbatim. Do not translate labels. Missing matches do not prove unanswerability. Decide business eligibility from Rulebook/README.',
        'columns':result}


def validate_literals(statement,metadata,domains,tool):
    """Check column predicates, with CTE/UNION lineage; fix unique spelling only.

    Never infer business-state equivalence or translations such as Xong -> Đã giao.
    """
    tree=sqlglot.parse_one(statement,read='postgres')
    catalog={(t['sql_table'],c['sql_column']) for t in metadata['tables'] for c in t['columns']}
    lookup={(d['table'],d['column']):d for d in domains};adjustments=[]

    def origins(node,scope,seen=frozenset()):
        if isinstance(node,(exp.Paren,exp.Alias)):return origins(node.this,scope,seen)
        if isinstance(node,(exp.Lower,exp.Upper,exp.Trim)):
            op='lower' if isinstance(node,exp.Lower) else 'upper' if isinstance(node,exp.Upper) else 'strip'
            return [(t,c,ops+(op,)) for t,c,ops in origins(node.this,scope,seen)]
        if isinstance(node,exp.Cast):
            return origins(node.this,scope,seen) if node.to.is_type(exp.DataType.Type.TEXT,exp.DataType.Type.VARCHAR) else []
        if not isinstance(node,exp.Column):return []
        mark=(id(scope),node.table,node.name)
        if mark in seen:return []
        seen=seen|{mark};result=[]
        sources=[scope.sources[node.table]] if node.table in scope.sources else list(scope.sources.values()) if not node.table else []
        for source in sources:
            if isinstance(source,exp.Table) and (source.name,node.name) in catalog:
                result.append((source.name,node.name,()))
            elif isinstance(source,Scope):
                query=source.expression
                output_names=source.outer_columns or query.named_selects
                if isinstance(query,exp.Union):
                    names=output_names
                    if node.name in names:
                        index=names.index(node.name)
                        for branch in source.union_scopes:
                            if index<len(branch.expression.selects):
                                result+=origins(branch.expression.selects[index],branch,seen)
                else:
                    for index,projection in enumerate(query.selects):
                        name=output_names[index] if index<len(output_names) else projection.alias_or_name
                        if name==node.name:
                            result+=origins(projection,source,seen)
                        elif isinstance(projection,exp.Star) or isinstance(projection,exp.Column) and projection.is_star:
                            result+=origins(exp.column(node.name),source,seen)
        return list(dict.fromkeys(result))

    def transformed(value,ops):
        for op in ops:value=getattr(value,op)()
        return value

    def database_match(table,column,ops,literal,pattern=False,ignore_case=False):
        expression=exp.column(column,quoted=True)
        for op in ops:
            expression={'lower':exp.Lower,'upper':exp.Upper,'strip':exp.Trim}[op](this=expression)
        predicate=(exp.ILike if ignore_case else exp.Like)(this=expression,expression=exp.Literal.string(literal)) if pattern else exp.EQ(this=expression,expression=exp.Literal.string(literal))
        query=exp.select(exp.column(column,quoted=True)).from_(exp.Table(this=exp.to_identifier(table,quoted=True))).where(predicate).limit(1)
        return bool(tool.execute_readonly_sql(query.sql(dialect='postgres'),purpose='entity_lookup')['rows'])

    def check(value,locations,pattern=False,ignore_case=False):
        if not locations:return
        candidates=[];original=value.this
        for table,column,ops in locations:
            domain=lookup.get((table,column))
            if domain is None:continue
            actual={transformed(v,ops) for v in domain['values']}
            if pattern:
                regex=''.join('.*' if c=='%' else '.' if c=='_' else re.escape(c) for c in original)
                if any(re.fullmatch(regex,v,re.I if ignore_case else 0) for v in actual):continue
                if not domain['complete'] and database_match(table,column,ops,original,True,ignore_case):continue
                raise PipelineError(f'Entity pattern {original!r} matches no actual value in {table}.{column}; use stored values, not translated labels')
            if original in actual:continue
            if not domain['complete']:
                found=database_match(table,column,ops,original) if ops else bool(tool.lookup_entity(table,column,original)['rows'])
                if found:continue
                raise PipelineError(f'Entity literal {original!r} not found in {table}.{column}; domain is incomplete, no automatic mapping')
            matches={v for v in actual if entity_key(v)==entity_key(original)}
            if len(matches)==1:
                candidates.append((table,column,next(iter(matches))))
            else:
                raise PipelineError(f'Entity literal {original!r} is not an actual value in {table}.{column}; allowed examples: {sorted(actual)[:12]}. Do not translate or guess a business-equivalent label.')
        replacements={v for _,_,v in candidates}
        if len(replacements)>1:
            raise PipelineError(f'Entity literal {original!r} has conflicting spellings across source columns; use per-table predicates')
        if replacements:
            replacement=next(iter(replacements))
            for t,c,ops in locations:
                d=lookup.get((t,c))
                if d and d['complete'] and replacement not in {transformed(v,ops) for v in d['values']}:
                    raise PipelineError(f'No single canonical literal is valid in all UNION sources for {original!r}')
            value.set('this',replacement)
            adjustments.append({'original':original,'canonical_value':replacement,
                'columns':[{'table':t,'column':c} for t,c,_ in candidates],
                'reason':'unique accent/case/whitespace match in actual column domain'})

    for scope in traverse_scope(tree):
        if not isinstance(scope.expression,exp.Select):continue
        for predicate in scope.expression.find_all(exp.EQ,exp.NEQ,exp.In,exp.Like,exp.ILike):
            if predicate.find_ancestor(exp.Select) is not scope.expression:continue
            if isinstance(predicate,exp.In):
                locations=origins(predicate.this,scope)
                for v in predicate.expressions:
                    if isinstance(v,exp.Literal) and v.is_string:check(v,locations)
            else:
                pattern=isinstance(predicate,(exp.Like,exp.ILike))
                for column,value in [(predicate.this,predicate.expression)]+([] if pattern else [(predicate.expression,predicate.this)]):
                    if isinstance(value,exp.Literal) and value.is_string:
                        check(value,origins(column,scope),pattern,isinstance(predicate,exp.ILike))
    return tree.sql(dialect='postgres'),adjustments
