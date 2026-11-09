"""Bounded, logged tools for the calculation-plan baseline (design §12).

Model-visible tools only read metadata or run code-built, identifier-quoted SELECTs
through the SELECT-only role. Answer SQL goes through the existing AST gates. Every
logical request (including cache hits and code-initiated calls) is counted.
"""
from collections import Counter
from decimal import Decimal
import difflib
import json
from time import perf_counter
import unicodedata

from psycopg import sql

from datamate_data.semplan.quality import assess
from ..query_tool import QueryTool, checked_sql
from .time_resolver import fold

MODEL_TOOLS = ('describe_tables', 'get_source_context', 'preview_rows', 'get_column_values', 'resolve_value',
               'profile_keys_and_joins')


class ToolBudgetExceeded(RuntimeError):
    pass


class ReadOnlyExecutor(QueryTool):
    """QueryTool plus EXPLAIN (never ANALYZE) and fixed code-built metadata queries."""

    def __init__(self, dsn, *, timeout_ms=10000, max_rows=1000, metadata_timeout_ms=5000):
        super().__init__(dsn, timeout_ms=timeout_ms, max_rows=max_rows);self.metadata_timeout_ms = metadata_timeout_ms

    def _txn(self, context, timeout):
        self.conn.execute('SET TRANSACTION READ ONLY')
        self.conn.execute(sql.SQL('SET LOCAL search_path = pg_catalog, {}, pg_temp').format(sql.Identifier(context['pg_schema'])))
        self.conn.execute(sql.SQL('SET LOCAL statement_timeout = {}').format(sql.Literal(timeout)))
        self.conn.execute(sql.SQL('SET LOCAL lock_timeout = {}').format(sql.Literal(min(timeout, 2000))))

    def explain(self, context, query):
        executed = checked_sql(query, context['ddl'], context['allowed'])
        self._permissions(context)
        with self.conn.transaction():
            self._txn(context, self.metadata_timeout_ms)
            plan = self.conn.execute('EXPLAIN (FORMAT JSON) '+executed).fetchone()[0]
        top = plan[0]['Plan'] if isinstance(plan, list) else plan['Plan']
        return {'node_type': top.get('Node Type'), 'total_cost': top.get('Total Cost'), 'plan_rows': top.get('Plan Rows'),
                'analyze': False}

    def fixed(self, context, query, params=(), *, max_rows=None):
        """Run a code-built psycopg.sql.Composed SELECT with parameters (read-only)."""
        self._permissions(context)
        limit = max_rows or self.max_rows
        with self.conn.transaction():
            self._txn(context, self.metadata_timeout_ms)
            cursor = self.conn.execute(query, params)
            rows = cursor.fetchmany(limit+1);columns = [c.name for c in cursor.description]
        return {'columns': columns, 'rows': rows[:limit], 'truncated': len(rows) > limit}


def _text(value):
    return unicodedata.normalize('NFC', str(value)).strip()


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return value


class ToolBox:
    def __init__(self, context, executor, *, caps, resolver, trace):
        self.context = context;self.executor = executor;self.caps = caps;self.resolver = resolver
        self.trace = trace;self.used = Counter();self.cache = {}
        trace.setdefault('tool_calls', [])

    def remaining(self):
        return {'tool_calls': self.caps['max_tool_calls']-self.used['total'],
                'answer_executions': self.caps['max_answer_executions']-self.used['execute_sql']}

    def call(self, name, args, *, by='code'):
        if self.used['total'] >= self.caps['max_tool_calls']:
            raise ToolBudgetExceeded('tool call budget exhausted')
        if name == 'execute_sql' and self.used['execute_sql'] >= self.caps['max_answer_executions']:
            raise ToolBudgetExceeded('answer execution budget exhausted')
        self.used['total'] += 1
        if by == 'model' and name not in MODEL_TOOLS:
            # A rejected request is still a logical tool call and counts against the budget.
            self.used['rejected'] += 1
            result = {'status': 'error', 'error': f'tool {name} is not available to the model'}
            self._log(name, args, by, result, 0, False);return result
        self.used[name] += 1
        cache_key = json.dumps([name, args], sort_keys=True, default=str)
        started = perf_counter();cache_hit = cache_key in self.cache and name not in {'execute_sql', 'explain_sql'}
        try:
            result = self.cache[cache_key] if cache_hit else getattr(self, '_'+name)(**args)
            if not cache_hit:
                self.cache[cache_key] = result
        except ToolBudgetExceeded:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            result = {'status': 'error', 'error': f'{type(exc).__name__}: {exc}'}
        except Exception as exc:  # database errors become tool errors, connection is rolled back
            result = {'status': 'error', 'error': f'{type(exc).__name__}: {getattr(getattr(exc, "diag", None), "message_primary", None) or exc}',
                      'sqlstate': getattr(exc, 'sqlstate', None)}
        self._log(name, args, by, result, round((perf_counter()-started)*1000, 3), cache_hit)
        return result

    def _log(self, name, args, by, result, ms, cache_hit):
        summary = {k: v for k, v in result.items() if k not in {'rows', 'values'}} if isinstance(result, dict) else result
        self.trace['tool_calls'].append({'tool': name, 'by': by, 'args': args, 'cache_hit': cache_hit, 'elapsed_ms': ms,
                                         'result': json.loads(json.dumps(summary, default=_jsonable)),
                                         'rows_returned': len(result.get('rows', result.get('values', []))) if isinstance(result, dict) else None})

    def _view(self, table):
        if table not in self.context['tables']:
            raise ValueError(f'unknown table {table}')
        return self.context['tables'][table]

    def _column(self, table, column):
        if column not in self.context['allowed'][table]:
            raise ValueError(f'unknown column {table}.{column}')
        return column

    # -- metadata tools ---------------------------------------------------------------
    def _describe_tables(self, tables=None):
        from .context import schema_for_prompt
        return {'status': 'ok', **schema_for_prompt(self.context, set(tables) if tables else None)}

    def _get_source_context(self, refs=None, search=None):
        evidence = self.context['evidence'];items = []
        if refs:
            items = [[r, evidence.get(r)['quote']] for r in refs[:20] if evidence.has(r)]
        if search:
            needle = fold(search)
            items += [[e['id'], e['quote']] for e in evidence.entries.values() if needle in fold(e['quote'])][:20]
        return {'status': 'ok', 'evidence': items}

    def _get_column_values(self, table, column, search=None, limit=30):
        self._view(table);self._column(table, column);limit = max(1, min(int(limit), 50))
        cap = self.caps['value_scan_cap']
        query = sql.SQL('SELECT {c}, count(*) FROM {t} WHERE {c} IS NOT NULL GROUP BY {c} ORDER BY count(*) DESC, {c} LIMIT %s').format(
            c=sql.Identifier(column), t=sql.Identifier(table))
        result = self.executor.fixed(self.context, query, (cap+1,), max_rows=cap+1)
        total = self.executor.fixed(self.context, sql.SQL('SELECT count(DISTINCT {c}) FROM {t}').format(
            c=sql.Identifier(column), t=sql.Identifier(table)))['rows'][0][0]
        values = [[_jsonable(v), n] for v, n in result['rows'][:cap]]
        if search:
            needle = fold(search);values = [v for v in values if needle in fold(v[0])]
        mapping = self._view(table)['status_mappings'].get(column)
        return {'status': 'ok', 'values': values[:limit], 'distinct_total': total, 'scanned': min(len(result['rows']), cap),
                'truncated': total > cap, 'status_domain': mapping['domain'] if mapping else None,
                'note': 'Counts over the analytic view; not an answer.'}

    def _resolve_value(self, table, column, literal):
        self._view(table);self._column(table, column);cap = self.caps['value_scan_cap']
        query = sql.SQL('SELECT DISTINCT {c} FROM {t} WHERE {c} IS NOT NULL LIMIT %s').format(c=sql.Identifier(column), t=sql.Identifier(table))
        result = self.executor.fixed(self.context, query, (cap+1,), max_rows=cap+1)
        values = [_text(r[0]) for r in result['rows'][:cap]];truncated = len(result['rows']) > cap
        target = _text(literal)
        exact = [v for v in values if v == target]
        casefold = [v for v in values if v.casefold() == target.casefold()]
        folded = [v for v in values if fold(v) == fold(target)]
        contains = [v for v in values if fold(target) and fold(target) in fold(v)]
        similar = difflib.get_close_matches(fold(target), [fold(v) for v in values], n=5, cutoff=0.8)
        similar_values = [v for v in values if fold(v) in similar][:5]
        if exact:
            status, value = 'exact', exact[0]
        elif len(set(casefold)) == 1:
            status, value = 'normalized_unique', casefold[0]
        elif len(set(folded)) == 1:
            status, value = 'normalized_unique', folded[0]
        elif casefold or folded or contains or similar_values:
            status, value = 'ambiguous', None
        else:
            status, value = ('not_found_truncated' if truncated else 'not_found_complete'), None
        return {'status': status, 'value': value, 'literal': literal,
                'candidates': list(dict.fromkeys(casefold+folded+contains[:5]+similar_values))[:8],
                'scanned_distinct': len(values), 'truncated': truncated,
                'note': 'Fuzzy/contains candidates are never treated as exact matches.'}

    def _preview_rows(self, table, columns=None, key_columns=None, limit=5, groups=2):
        self._view(table);limit = max(1, min(int(limit), 5));groups = max(1, min(int(groups), 3))
        cols = [self._column(table, c) for c in (columns or list(self.context['allowed'][table])[:12])]
        select = sql.SQL(',').join(map(sql.Identifier, cols))
        if key_columns:
            keys = [self._column(table, c) for c in key_columns]
            kid = sql.SQL(',').join(map(sql.Identifier, keys))
            query = sql.SQL('WITH g AS (SELECT {k} FROM {t} WHERE {nn} GROUP BY {k} HAVING count(*) > 1 '
                            'ORDER BY md5(CAST(ROW({k}) AS TEXT)) LIMIT %s) '
                            'SELECT {s} FROM {t} JOIN g USING ({k}) ORDER BY {k} LIMIT %s').format(
                k=kid, t=sql.Identifier(table), s=select,
                nn=sql.SQL(' AND ').join(sql.SQL('{} IS NOT NULL').format(sql.Identifier(c)) for c in keys))
            result = self.executor.fixed(self.context, query, (groups, groups*limit), max_rows=groups*limit)
        else:
            query = sql.SQL('SELECT {s} FROM {t} ORDER BY md5(CAST(ROW({s}) AS TEXT)) LIMIT %s').format(s=select, t=sql.Identifier(table))
            result = self.executor.fixed(self.context, query, (limit,), max_rows=limit)
        return {'status': 'ok', 'columns': result['columns'], 'rows': [[_jsonable(v) for v in r] for r in result['rows']],
                'sample_only': True, 'note': 'Samples are not answers; aggregate in SQL over all rows.'}

    def _profile_keys_and_joins(self, table, columns, target_table=None, target_columns=None):
        self._view(table);cols = [self._column(table, c) for c in columns]
        ids = sql.SQL(',').join(map(sql.Identifier, cols))
        nn = sql.SQL(' AND ').join(sql.SQL('{} IS NOT NULL').format(sql.Identifier(c)) for c in cols)
        total, populated, distinct = self.executor.fixed(self.context, sql.SQL(
            'SELECT count(*), count(*) FILTER (WHERE {nn}), count(DISTINCT ROW({ids})) FILTER (WHERE {nn}) FROM {t}').format(
            nn=nn, ids=ids, t=sql.Identifier(table)))['rows'][0]
        out = {'status': 'ok', 'rows': total, 'non_null_rows': populated, 'distinct_groups': distinct,
               'unique_when_non_null': populated == distinct}
        if target_table:
            self._view(target_table);tcols = [self._column(target_table, c) for c in target_columns]
            if len(tcols) != len(cols):
                raise ValueError('join column lists differ in length')
            tid = sql.SQL(',').join(map(sql.Identifier, tcols))
            tn = sql.SQL(' AND ').join(sql.SQL('{} IS NOT NULL').format(sql.Identifier(c)) for c in tcols)
            t_pop, t_distinct = self.executor.fixed(self.context, sql.SQL(
                'SELECT count(*) FILTER (WHERE {nn}), count(DISTINCT ROW({ids})) FILTER (WHERE {nn}) FROM {t}').format(
                nn=tn, ids=tid, t=sql.Identifier(target_table)))['rows'][0]
            predicate = sql.SQL(' AND ').join(sql.SQL('s.{}=t.{}').format(sql.Identifier(a), sql.Identifier(b)) for a, b in zip(cols, tcols))
            orphans = self.executor.fixed(self.context, sql.SQL('SELECT count(*) FROM {s} s WHERE {nn} AND NOT EXISTS (SELECT 1 FROM {t} t WHERE {p})').format(
                s=sql.Identifier(table), t=sql.Identifier(target_table), p=predicate,
                nn=sql.SQL(' AND ').join(sql.SQL('s.{} IS NOT NULL').format(sql.Identifier(c)) for c in cols)))['rows'][0][0]
            out['join'] = {'target_unique': t_pop == t_distinct, 'orphan_source_rows': orphans,
                           'cardinality': ('one' if populated == distinct else 'many')+'_to_'+('one' if t_pop == t_distinct else 'many'),
                           'fanout_risk': t_pop != t_distinct, 'business_verified': False}
        return out

    # -- code-only tools ------------------------------------------------------------------
    def _resolve_time(self, spec, as_of_date):
        return {'status': 'ok', 'resolution': self.resolver.resolve(spec, as_of_date)}

    def _check_data_requirements(self, dependencies):
        gate = assess(self.context['quality'], dependencies)
        coverage = []
        for d in dependencies:
            bounds = self.context['tables'][d['view']]['date_bounds']
            for w in d['time_filters']:
                b = bounds.get(w['column'])
                if not b:
                    coverage.append({'view': d['view'], 'column': w['column'], 'status': 'no_full_dates_in_view'});continue
                outside = (w['end_exclusive'] is not None and w['end_exclusive'] <= b[0]) or (w['start'] is not None and w['start'] > b[1])
                coverage.append({'view': d['view'], 'column': w['column'], 'data_min': b[0], 'data_max': b[1],
                                 'window': [w['start'], w['end_exclusive']], 'status': 'outside' if outside else 'overlaps'})
        return {'status': 'ok', 'quality': gate, 'coverage': coverage}

    def _explain_sql(self, sql_text):
        return {'status': 'ok', **self.executor.explain(self.context, sql_text)}

    def _execute_sql(self, sql_text):
        result = self.executor.execute_sql(self.context, sql_text)
        return {'status': 'ok', **result}
