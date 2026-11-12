"""Bounded direct psycopg queries; no free-running model tools or MCP."""
from time import perf_counter
from psycopg import sql
from datamate_v2.common import PipelineError,BudgetError
from .connection import connect

class QueryTool:
    def __init__(self,metadata,settings,guard,max_queries=8):
        self.metadata=metadata;self.settings=settings;self.guard=guard
        self.remaining=max_queries;self.trace=[];self.schema=metadata["schema"]
        self.tables={t["sql_table"]:t for t in metadata["tables"]}

    def list_tables(self): return list(self.tables)
    def describe_table(self,table): return self.tables[table]

    def text_domains(self,columns,limit=2048):
        """Read actual per-column domains in one bounded query, not sample rows."""
        from sqlglot import exp
        limit=max(1,min(int(limit),4096));branches=[]
        for table,column in sorted(set(columns)):
            if table not in self.tables or column not in {c['sql_column'] for c in self.tables[table]['columns'] if c['type']=='TEXT'}:
                raise PipelineError('Text domain outside business catalog')
            tq=exp.to_identifier(table,quoted=True).sql(dialect='postgres')
            cq=exp.to_identifier(column,quoted=True).sql(dialect='postgres')
            tl=exp.Literal.string(table).sql(dialect='postgres')
            cl=exp.Literal.string(column).sql(dialect='postgres')
            branches.append(f'SELECT {tl} AS table_name, {cl} AS column_name, ARRAY('
                f'SELECT DISTINCT {cq} FROM {tq} WHERE {cq} IS NOT NULL AND _is_summary_row = FALSE '
                f'ORDER BY {cq} LIMIT {limit+1}) AS domain_values')
        if not branches:return []
        result=self.execute_readonly_sql(' UNION ALL '.join(branches),purpose='entity_domains')
        return [{'table':t,'column':c,'values':values[:limit],'complete':len(values)<=limit}
                for t,c,values in result['rows']]

    def execute_readonly_sql(self,statement,*,purpose="answer",plan=None):
        if self.remaining<=0: raise BudgetError("PostgreSQL query budget exhausted")
        checked=self.guard(statement,self.metadata,plan=plan)
        self.remaining-=1;start=perf_counter()
        entry={"sql":checked,"purpose":purpose,"status":"started"};self.trace.append(entry)
        try:
            with connect(self.settings) as db:
                db.execute("SET TRANSACTION READ ONLY")
                db.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(self.schema)))
                db.execute("SELECT set_config('statement_timeout',%s,true)",(str(self.settings["statement_timeout_ms"]),))
                db.execute("SELECT set_config('lock_timeout','2000',true)")
                db.execute("SELECT set_config('TimeZone',%s,true)",(self.settings.get('timezone','UTC'),))
                role=db.execute("SELECT rolsuper,rolcreaterole,rolcreatedb,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
                owner=db.execute("SELECT nspowner=current_user::regrole FROM pg_namespace WHERE nspname=%s",(self.schema,)).fetchone()
                if any(role) or not owner or owner[0]: raise PipelineError("QA connection must use a non-owner, non-privileged read-only role")
                if db.execute("SELECT has_schema_privilege(current_user,%s,'CREATE')",(self.schema,)).fetchone()[0]:
                    raise PipelineError("QA role must not have CREATE on the business schema")
                cursor=db.execute(checked)
                names=[c.name for c in cursor.description]
                limit=self.settings["max_result_rows"]
                rows=cursor.fetchmany(limit+1)
                if len(rows)>limit: raise PipelineError("Result truncated: narrow query explicitly")
                result={"columns":names,"rows":[list(r) for r in rows],"row_count":len(rows)}
                entry.update(status="success",result=result)
                return result
        except Exception as error:
            entry.update(status="error",error_type=type(error).__name__);raise
        finally: entry["duration_ms"]=(perf_counter()-start)*1000

    def get_sample_rows(self,table,limit=5):
        cols=[c['sql_column'] for c in self.tables[table]['columns']]
        return self.execute_readonly_sql('SELECT '+','.join('"'+c+'"' for c in cols)+' FROM "'+table+'" LIMIT '+str(min(int(limit),20)),purpose="sample")

    def lookup_entity(self,table,column,value):
        # Literal encoding is delegated to SQLGlot, not string interpolation of user text.
        import sqlglot.expressions as exp
        if column not in {c['sql_column'] for c in self.tables[table]['columns']} : raise PipelineError("Unknown lookup column")
        literal=exp.Literal.string(str(value)).sql(dialect="postgres")
        q=f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" = {literal} LIMIT 20'
        return self.execute_readonly_sql(q,purpose="entity_lookup")

    def inspect_column_values(self,table,column):
        if column not in {c['sql_column'] for c in self.tables[table]['columns']}: raise PipelineError("Unknown column")
        return self.execute_readonly_sql(f'SELECT DISTINCT "{column}" FROM "{table}" LIMIT 20',purpose="column_values")

    def entity_values(self,columns,limit=2048):
        """Bounded distinct domain, across real tables only; never silently truncate."""
        import sqlglot.expressions as exp
        if not columns: return {'values':[],'complete':True}
        limit=min(int(limit),int(self.settings['max_result_rows'])-1)
        if limit<1: raise PipelineError('Entity lookup result budget is too small')
        branches=[]
        for table,column in sorted(set(columns)):
            if table not in self.tables or column not in {c['sql_column'] for c in self.tables[table]['columns'] if c['type']=='TEXT'}:
                raise PipelineError('Entity lookup outside current business schema')
            branches.append(exp.select(exp.column(column,quoted=True).as_('value')).distinct().from_(exp.Table(this=exp.to_identifier(table,quoted=True))).where(
                exp.and_(exp.EQ(this=exp.column('_is_summary_row'),expression=exp.Boolean(this=False)),
                         exp.Not(this=exp.Is(this=exp.column(column,quoted=True),expression=exp.Null())))))
        query=branches[0]
        for branch in branches[1:]: query=exp.Union(this=query,expression=branch,distinct=True)
        query=query.limit(limit+1)
        result=self.execute_readonly_sql(query.sql(dialect='postgres'),purpose='entity_lookup')
        return {'values':[row[0] for row in result['rows']],'complete':result['row_count']<=limit}

    def check_data_coverage(self,table,column):
        return self.execute_readonly_sql(f'SELECT MIN("{column}") AS start_date, MAX("{column}") AS end_date FROM "{table}" WHERE _is_summary_row = FALSE',purpose="coverage")

    def inspect_data_quality(self,table): return self.tables[table]["quality"]
