"""Business-bound SELECT execution with AST gates and a non-owner DB role."""
from time import perf_counter
import psycopg
from psycopg import sql
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope
from datamate_b.contracts import validate_sql

ALLOWED_FUNCTIONS={'sum','count','avg','min','max','coalesce','nullif','round','abs',
    'cast','extract','timestamptrunc','datetrunc','lower','upper','trim','length',
    'rownumber','rank','denserank','lag','lead','ceil','floor','timetostr',
    'case','if','exists','greatest','least','concat','substring'}
SAFE_TYPES={'TEXT','VARCHAR','CHAR','INT','INTEGER','BIGINT','SMALLINT','NUMERIC','DECIMAL',
    'DOUBLE','FLOAT','REAL','BOOLEAN','DATE','TIMESTAMP','TIMESTAMPTZ'}


def _checked_sql(query,ddl,allowed):
    validate_sql(query,ddl,'postgres')
    tree=sqlglot.parse_one(query,read='postgres')
    for node in tree.walk():
        if node.key in {'lock','dot','placeholder','parameter','tablesample','lateral','unnest'}:
            raise ValueError('Disallowed SQL construct: '+node.key)
        if isinstance(node,exp.With) and node.args.get('recursive'):raise ValueError('Recursive CTE is unsupported')
        if isinstance(node,exp.Func) and not isinstance(node,(exp.And,exp.Or)) and node.key not in ALLOWED_FUNCTIONS:raise ValueError('Function is not allowlisted: '+node.key)
        if isinstance(node,exp.Cast) and isinstance(node.this,exp.Literal) and node.this.is_string and node.this.this.strip().lower() in {'now','today','yesterday','tomorrow','infinity','-infinity','epoch'}:
            raise ValueError('System-relative/special date literal is unsupported')
        if isinstance(node,exp.DataType):
            kind=node.sql(dialect='postgres').split('(')[0].strip().upper()
            if kind not in SAFE_TYPES:raise ValueError('Unsupported SQL cast type: '+kind)
    # Resolve lexical CTE scope, not a flat set of CTE names that could hide pg_class.
    for scope in traverse_scope(tree):
        for _,source in scope.selected_sources.values():
            if isinstance(source,exp.Table) and (source.db or source.catalog or source.name not in allowed):
                raise ValueError('Relation outside this business: '+source.name)
    # Expand projection stars to exposed columns, never internal __raw/__source fields.
    qualified=qualify(tree.copy(),dialect='postgres',schema=allowed,validate_qualify_columns=True)
    return qualified.sql(dialect='postgres')


def checked_sql(query,ddl,allowed):
    try:return _checked_sql(query,ddl,allowed)
    except sqlglot.errors.SqlglotError as exc:raise ValueError(str(exc)) from exc


class QueryTool:
    def __init__(self,dsn,*,timeout_ms=10000,max_rows=1000):
        if type(timeout_ms)is not int or not 1<=timeout_ms<=60000:raise ValueError('timeout_ms outside 1..60000')
        if type(max_rows)is not int or not 1<=max_rows<=10000:raise ValueError('max_rows outside 1..10000')
        self.timeout_ms=timeout_ms;self.max_rows=max_rows
        self.conn=psycopg.connect(dsn,autocommit=True,connect_timeout=10)
        row=self.conn.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
        if row is None or any(row):self.conn.close();raise ValueError('Baseline requires a non-privileged SELECT role, not an owner/admin DSN')

    def close(self):self.conn.close()

    def _permissions(self,context):
        schema=context['pg_schema']
        if self.conn.execute("SELECT has_schema_privilege(current_user,%s,'CREATE')",(schema,)).fetchone()[0]:
            raise ValueError('Reader must not have CREATE privilege in business schema')
        for name in context['allowed']:
            qualified=sql.Identifier(schema,name).as_string(self.conn)
            writable=self.conn.execute("SELECT has_table_privilege(current_user,%s,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')",(qualified,)).fetchone()[0]
            if writable:raise ValueError('Reader has write privilege on business view')
            if not self.conn.execute("SELECT has_table_privilege(current_user,%s,'SELECT')",(qualified,)).fetchone()[0]:raise ValueError('Reader missing SELECT grant')

    def execute_sql(self,context,query,*,max_rows=None):
        executed=checked_sql(query,context['ddl'],context['allowed'])
        self._permissions(context)
        limit=self.max_rows if max_rows is None else max_rows
        if type(limit)is not int or not 1<=limit<=self.max_rows:raise ValueError('Invalid result cap')
        started=perf_counter()
        with self.conn.transaction():
            self.conn.execute('SET TRANSACTION READ ONLY')
            self.conn.execute(sql.SQL('SET LOCAL search_path = pg_catalog, {}, pg_temp').format(sql.Identifier(context['pg_schema'])))
            self.conn.execute(sql.SQL('SET LOCAL statement_timeout = {}').format(sql.Literal(self.timeout_ms)))
            self.conn.execute(sql.SQL('SET LOCAL lock_timeout = {}').format(sql.Literal(min(self.timeout_ms,2000))))
            with self.conn.cursor(name='datamate_baseline_query') as cursor:
                cursor.execute(executed)
                rows=cursor.fetchmany(limit+1)
                columns=[c.name for c in cursor.description]
        return {'columns':columns,'rows':rows[:limit],'truncated':len(rows)>limit,
            'sql_executed':executed,'elapsed_ms':round((perf_counter()-started)*1000,3)}

    def preview_rows(self,context,table,limit=3):
        if table not in context['allowed'] or type(limit)is not int or not 1<=limit<=5:raise ValueError('Preview table/limit invalid')
        result=self.execute_sql(context,'SELECT * FROM '+sql.Identifier(table).as_string(self.conn)+' ORDER BY 1',max_rows=limit)
        return {**result,'sample_only':True}
