from datamate_v2.qa_pipeline.result_validation.validator import verify_check
from datamate_v2.qa_pipeline.sql_validation.guard import parse_readonly,conjuncts
from sqlglot import exp


def row_existence_check(statement,answer_sql):
    """A zero count of the answer's own population is not proof of corrupt data."""
    check=parse_readonly(statement);answer=parse_readonly(answer_sql)
    if not isinstance(check,exp.Select) or not isinstance(answer,exp.Select): return False
    if any(check.args.get(k) for k in ('group','having','limit','offset','qualify')): return False
    if len(check.expressions)!=1: return False
    count=check.expressions[0]
    if isinstance(count,exp.Alias): count=count.this
    if not isinstance(count,exp.Count) or count.find(exp.Window): return False
    # COUNT(nullable_column)=0 does not prove there are no rows.
    if not isinstance(count.this,(exp.Star,exp.Literal)): return False
    def scope(tree):
        where=tree.args.get('where');predicates=[]
        for p in conjuncts(where.this) if where else []:
            if isinstance(p,exp.EQ) and isinstance(p.this,exp.Column) and p.this.name=='_is_summary_row' and isinstance(p.expression,exp.Boolean) and p.expression.this is False:
                continue
            predicates.append(p.sql(dialect='postgres'))
        sources=[]
        for k in ('from','joins','with'):
            value=tree.args.get(k) or tree.args.get(k+'_')
            sources.append([x.sql(dialect='postgres') for x in value] if isinstance(value,list) else value.sql(dialect='postgres') if value else None)
        return sources,sorted(predicates)
    return scope(check)==scope(answer)

def run_checks(tool,checks,*,answer_sql=None):
    results=[]
    for check in checks:
        result=tool.execute_readonly_sql(check['sql'],purpose='quality')
        if (answer_sql and check['expected']=='positive' and result.get('rows')==[[0]] and
                row_existence_check(check['sql'],answer_sql)):
            results.append({'check':check,'result':result,'status':'empty_population','assertion_applied':False})
            continue
        try:verify_check(result,check['expected'])
        except Exception as error:
            error.check_results=results+[{'check':check,'result':result,'status':'failed','assertion_applied':True}]
            raise
        results.append({'check':check,'result':result,'status':'passed','assertion_applied':True})
    return results
