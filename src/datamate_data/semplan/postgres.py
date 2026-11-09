"""P8: load materialized rows into a new schema and audit the loaded data.

Trusted ingestion SQL built with quoted identifiers; never model SQL. The analytic
view lists its columns explicitly (no __raw/__source/__quality); unknown/non-data and
duplicate-marked rows stay in the base table only.
"""
from decimal import Decimal
import json
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from ..parsers import dumps
from ..pipeline import digest
from ..postgres import relationships
from .materialize import COMPANIONS, companion_names, numeric_clean_sums

INTERNAL = [('__row_id', 'TEXT PRIMARY KEY'), ('__seq', 'BIGINT NOT NULL'), ('__raw', 'JSONB NOT NULL'),
            ('__source', 'JSONB NOT NULL'), ('__quality', 'JSONB NOT NULL'), ('__row_kind', 'TEXT NOT NULL'),
            ('__row_decision', 'JSONB NOT NULL'), ('__duplicate_of', 'TEXT'), ('__quantity_base', 'NUMERIC'),
            ('__unit_base', 'TEXT')]


def exposed_columns(meta):
    """Columns of the analytic view, in order, with SQL types."""
    cols = []
    for c in meta['columns']:
        cols.append((c['name'], c['sql_type']))
        if c['name'] in meta['temporal_columns']:
            cols += [(n, t) for n, (_, t) in zip(companion_names(c['name']), COMPANIONS)]
        if c['name'] in meta['status_columns']:
            cols.append((c['name']+'__raw', 'TEXT'))
    cols += [('__quantity_base', 'NUMERIC'), ('__unit_base', 'TEXT')]
    for name, _ in cols:
        if len(name.encode()) > 63:
            raise ValueError(f'Identifier too long for PostgreSQL: {name}')
    return cols


def _row_values(meta, row):
    values = []
    for i, c in enumerate(meta['columns']):
        values.append(row['typed'][i])
        if c['name'] in meta['temporal_columns']:
            values += row['temporal'].get(c['name']) or [None]*len(COMPANIONS)
        if c['name'] in meta['status_columns']:
            values.append(row['status_raw'].get(c['name']))
    return values


def load_published(dsn, root, catalog):
    root = Path(root);schema = catalog['schema']
    for meta in catalog['tables'].values():
        path = (root/meta['data_path']).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError('Materialized file escapes business directory')
        if digest(path) != meta['data_sha256']:
            raise ValueError('Materialized checksum mismatch; refusing database load')
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (int(catalog['fingerprint'][:15], 16),))
        exists = conn.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (schema,)).fetchone()
        if exists:
            stored = conn.execute(sql.SQL('SELECT catalog FROM {}.{}').format(sql.Identifier(schema), sql.Identifier('_catalog'))).fetchone()[0]
            if stored['fingerprint'] != catalog['fingerprint']:
                raise ValueError('Schema fingerprint conflict')
            for meta in stored['tables'].values():
                count = conn.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(sql.Identifier(schema), sql.Identifier(meta['name']))).fetchone()[0]
                if count != meta['rows']:
                    raise ValueError('Cached database row counts differ; refusing reuse')
            return {'schema': schema, 'cache_hit': True, 'catalog': stored}
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        for name, meta in catalog['tables'].items():
            exposed = exposed_columns(meta)
            source_cols = [n for n, _ in exposed if n not in {'__quantity_base', '__unit_base'}]
            types = dict(exposed)
            definitions = [sql.SQL('{} {}').format(sql.Identifier(n), sql.SQL(types[n])) for n in source_cols]
            definitions += [sql.SQL('{} {}').format(sql.Identifier(n), sql.SQL(t)) for n, t in INTERNAL]
            conn.execute(sql.SQL('CREATE TABLE {}.{} ({})').format(sql.Identifier(schema), sql.Identifier(name), sql.SQL(',').join(definitions)))
            columns = source_cols+[n for n, _ in INTERNAL]
            statement = sql.SQL('COPY {}.{} ({}) FROM STDIN').format(sql.Identifier(schema), sql.Identifier(name),
                                                                    sql.SQL(',').join(map(sql.Identifier, columns)))
            with conn.cursor().copy(statement) as copy, (root/meta['data_path']).open(encoding='utf-8') as stream:
                for line in stream:
                    row = json.loads(line)
                    copy.write_row(_row_values(meta, row)+[row['row_id'], row['seq'], Jsonb(row['raw']), Jsonb(row['source']),
                                   Jsonb(row['quality']), row['kind'], Jsonb(row['decision']), row['duplicate_of'],
                                   row['quantity_base'], row['unit_base']])
            if digest(root/meta['data_path']) != meta['data_sha256']:
                raise ValueError('Materialized rows changed during database load')
            view = name+'_clean'
            conn.execute(sql.SQL("CREATE VIEW {}.{} AS SELECT {} FROM {}.{} WHERE __row_kind='data' AND __duplicate_of IS NULL").format(
                sql.Identifier(schema), sql.Identifier(view), sql.SQL(',').join(sql.Identifier(n) for n, _ in exposed),
                sql.Identifier(schema), sql.Identifier(name)))
            meta['clean_view'] = view;meta['exposed_columns'] = [[n, t] for n, t in exposed]
            meta['profiles'] = {}
            for col_name, typ in exposed:
                c = sql.Identifier(col_name)
                count, non_null, distinct = conn.execute(sql.SQL('SELECT count(*), count({}), count(DISTINCT {}) FROM {}.{}').format(
                    c, c, sql.Identifier(schema), sql.Identifier(view))).fetchone()
                profile = {'clean_rows': count, 'non_null': non_null, 'distinct': distinct,
                           'unique_non_null_candidate': non_null > 0 and non_null == distinct}
                if typ in {'DATE', 'TIMESTAMP', 'NUMERIC', 'INTEGER'}:
                    low, high = conn.execute(sql.SQL('SELECT min({}),max({}) FROM {}.{}').format(c, c, sql.Identifier(schema), sql.Identifier(view))).fetchone()
                    profile['min'] = None if low is None else str(low);profile['max'] = None if high is None else str(high)
                meta['profiles'][col_name] = profile
            for c in meta['columns']:
                if c['role'].endswith('_id') or c['sql_type'] in {'DATE', 'TIMESTAMP'}:
                    conn.execute(sql.SQL('CREATE INDEX ON {}.{} ({})').format(sql.Identifier(schema), sql.Identifier(name), sql.Identifier(c['name'])))
            conn.execute(sql.SQL('ANALYZE {}.{}').format(sql.Identifier(schema), sql.Identifier(name)))
        catalog['relationships'] = relationships(conn, catalog)
        conn.execute(sql.SQL('CREATE TABLE {}.{} (catalog JSONB NOT NULL)').format(sql.Identifier(schema), sql.Identifier('_catalog')))
        conn.execute(sql.SQL('INSERT INTO {}.{} VALUES (%s)').format(sql.Identifier(schema), sql.Identifier('_catalog')), (Jsonb(json.loads(dumps(catalog))),))
    return {'schema': schema, 'cache_hit': False, 'catalog': catalog}


def audit(dsn, root, catalog):
    """Compare loaded data with materialized files; structural checks only."""
    root = Path(root);schema = catalog['schema'];checks = []
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        conn.execute('SET TRANSACTION READ ONLY');conn.execute("SET LOCAL statement_timeout='120s'")
        for name, meta in catalog['tables'].items():
            path = root/meta['data_path']
            if digest(path) != meta['data_sha256']:
                checks.append({'table': name, 'check': 'materialized_checksum', 'status': 'fail'});continue
            expected = numeric_clean_sums(path, meta['columns'])
            kinds = dict(conn.execute(sql.SQL('SELECT __row_kind, count(*) FROM {}.{} GROUP BY 1').format(
                sql.Identifier(schema), sql.Identifier(name))).fetchall())
            numeric = [c['name'] for c in meta['columns'] if c['sql_type'] == 'NUMERIC']
            aggregates = [sql.SQL('count(*)')]+[sql.SQL('sum({})').format(sql.Identifier(n)) for n in numeric]
            aggregates += [sql.SQL('sum(__quantity_base)'), sql.SQL('count(__quantity_base)')]
            actual = conn.execute(sql.SQL('SELECT {} FROM {}.{}').format(sql.SQL(',').join(aggregates), sql.Identifier(schema),
                                                                        sql.Identifier(meta['clean_view']))).fetchone()
            sums_ok = all((actual[1+i] is None and expected['sums'][n] is None) or
                          (actual[1+i] is not None and expected['sums'][n] is not None and Decimal(actual[1+i]) == Decimal(expected['sums'][n]))
                          for i, n in enumerate(numeric))
            base_ok = (actual[-2] is None and expected['quantity_base_sum'] is None) or (
                actual[-2] is not None and expected['quantity_base_sum'] is not None and Decimal(actual[-2]) == Decimal(expected['quantity_base_sum']))
            checks += [{'table': name, 'check': 'row_kinds_match', 'status': 'pass' if kinds == expected['kinds'] else 'fail',
                        'detail': {'database': kinds, 'materialized': expected['kinds']}},
                       {'table': name, 'check': 'clean_rows_match', 'status': 'pass' if actual[0] == expected['clean_rows'] else 'fail',
                        'detail': {'database': actual[0], 'materialized': expected['clean_rows']}},
                       {'table': name, 'check': 'numeric_sums_exact', 'status': 'pass' if sums_ok else 'fail'},
                       {'table': name, 'check': 'quantity_base_sum_exact', 'status': 'pass' if base_ok and actual[-1] == expected['quantity_base_non_null'] else 'fail'}]
            for col in meta['temporal_columns']:
                y, m, d, p, r = companion_names(col)
                bad = conn.execute(sql.SQL("SELECT count(*) FROM {}.{} WHERE ({} IS NULL AND {} IN ('day','datetime')) OR ({} IS NOT NULL AND {} IN ('month_day','month','year'))").format(
                    sql.Identifier(schema), sql.Identifier(name), sql.Identifier(col), sql.Identifier(p), sql.Identifier(col), sql.Identifier(p))).fetchone()[0]
                checks.append({'table': name, 'check': f'temporal_companions_consistent:{col}', 'status': 'pass' if bad == 0 else 'fail',
                               'detail': {'inconsistent_rows': bad}})
            definition = conn.execute('SELECT pg_get_viewdef(%s::regclass, true)',
                                      (sql.Identifier(schema, meta['clean_view']).as_string(conn),)).fetchone()[0]
            folded = ' '.join(definition.split())
            view_columns = [r[0] for r in conn.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=%s '
                                                       'AND table_name=%s ORDER BY ordinal_position', (schema, meta['clean_view'])).fetchall()]
            excludes = "__row_kind = 'data'::text" in folded and '__duplicate_of IS NULL' in folded and \
                view_columns == [n for n, _ in meta['exposed_columns']]
            checks.append({'table': name, 'check': 'view_excludes_non_data_duplicates_and_internal_fields',
                           'status': 'pass' if excludes else 'fail', 'detail': {'view_columns': view_columns}})
    status = 'passed' if all(c['status'] == 'pass' for c in checks) else 'failed'
    return {'status': status, 'checks': checks, 'schema': schema,
            'limitation': 'Faithful loading and structural consistency only; not metric correctness.'}
