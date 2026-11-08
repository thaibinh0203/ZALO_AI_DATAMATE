"""Source-grounded business metadata sidecar; never reads questions or labels.

SLM proposes semantics. Fixed read-only queries check keys, measure repetition and
join cardinality. Structural checks never upgrade a proposal to business truth.
"""
import hashlib
import json
from pathlib import Path
from time import perf_counter
import psycopg
from psycopg import sql
from datamate_b.contracts import strict_json
from .parsers import dumps

VERSION = 'business-metadata-v1'
KINDS = {'order', 'order_line', 'product', 'customer', 'delivery_item', 'delivery',
         'payment', 'purchase_line', 'lot', 'snapshot', 'expense', 'price_change', 'unknown'}
AGGREGATIONS = {'sum', 'weighted_average', 'per_group_once', 'snapshot', 'none', 'unknown'}
SYSTEM = '''You annotate ONE source table, using the complete business schema,
README and rulebook. Return ONLY JSON with exactly grain, group_keys, measures,
joins. grain={kind,description,evidence}; kind is one of the provided grain_kinds.
Annotate ONLY target_schema, not all business tables. Never use another table's
columns in group_keys/measures. target_allowed_columns is the exact allowlist.
heuristic_role_not_grain is a coarse legacy hint and can be WRONG (for example
a delivery item table can also look like sale lines). Determine grain from actual
source table meaning in README, never copy that heuristic as business truth.
evidence is an exact short quote from README (not an invented explanation), or
null when source meaning is uncertain. A row grain is NOT automatically an order.
group_keys=[{name,columns,purpose,evidence}], at most 4. Columns are exact SQL
names in this table. Grouping keys may repeat; do not claim they are row keys.
purpose describes the entity/event identified, e.g. order, delivery event.
measures=[{column,scope,aggregation,unit,evidence}], at most 12, EXACTLY ONE entry
per numeric measure column (never repeat a column for different group-bys).
scope is its NATIVE measurement grain, NOT every grouping that could be used in
a query. Sale line amount/quantity are row scope; repeated invoice total is order
scope; current stock is snapshot scope. Do not enumerate aggregation scenarios.
scope is row, product, order, snapshot, a justified group key name or unknown. aggregation is one of
sum, weighted_average, per_group_once, snapshot, none, unknown. Distinguish line
amount from invoice total repeated on lines; prices are not additive amounts.
joins=[{target,source_columns,target_columns,evidence}], at most 4; target is an
exact other clean view, columns exist on their respective tables, equal nonzero
length. Only propose joins supported by source meaning. Never join same-named
notes or descriptions just for spelling. Use [] when no evidence. A delivery
event can span product rows; order_id + delivery_number can identify the event,
not necessarily an individual row. Infer no foreign/business facts. No SQL.
Use compact JSON; leave uncertain information unknown/null rather than inventing.
'''


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compact_schema(catalog):
    return [{'table': t['clean_view'], 'source': t['logical_name'], 'heuristic_role_not_grain': t['role'],
             'columns': [{'sql': c['name'], 'source': c['original_name'],
                          'type': c['sql_type'], 'canonical_role': c.get('canonical_role'),
                          'semantic_role': c.get('semantic_role'), 'meaning': c.get('description') or
                          ((c.get('semantic', {}).get('normalized_proposal') or
                            c.get('semantic', {}).get('proposed') or {}).get('meaning')),
                          'has_data': c.get('has_data')} for c in t['columns']]}
            for t in catalog['tables'].values()]


def evidence(value, readme):
    valid = isinstance(value, str) and bool(value.strip()) and value in readme
    return {'quote': value if isinstance(value, str) else None,
            'status': 'readme_quote_verified' if valid else 'unverified_proposal'}


def validate_proposal(value, table, tables, readme):
    if not isinstance(value, dict) or set(value) != {'grain', 'group_keys', 'measures', 'joins'}:
        raise ValueError('Expected grain/group_keys/measures/joins')
    cols = {c['name'] for c in table['columns']} | {'__quantity_base', '__unit_base'}
    g = value['grain']
    if not isinstance(g, dict) or set(g) != {'kind', 'description', 'evidence'} or g['kind'] not in KINDS or not isinstance(g['description'], str):
        raise ValueError('Invalid grain contract')
    out = {'grain': {'kind': g['kind'], 'description': g['description'],
                     'evidence': evidence(g['evidence'], readme), 'status': 'candidate'},
           'group_keys': [], 'measures': [], 'joins': []}
    for key, cap in [('group_keys', 4), ('measures', 12), ('joins', 4)]:
        if not isinstance(value[key], list) or len(value[key]) > cap:
            raise ValueError('Invalid proposal list: ' + key)
    def column_list(items, allowed):
        return isinstance(items, list) and 0 < len(items) <= 4 and all(isinstance(c, str) and c in allowed for c in items) and len(set(items)) == len(items)
    names = set()
    for k in value['group_keys']:
        if not isinstance(k, dict) or set(k) != {'name', 'columns', 'purpose', 'evidence'} or not isinstance(k['name'], str) or not k['name'].strip() or k['name'] in names or not isinstance(k['purpose'], str) or not column_list(k['columns'], cols):
            raise ValueError('Invalid grouping key or unknown column')
        names.add(k['name']);out['group_keys'].append({**k, 'evidence': evidence(k['evidence'], readme), 'status': 'candidate'})
    measures = set()
    for m in value['measures']:
        if isinstance(m,dict) and m.get('column') in measures:
            raise ValueError('Duplicate measure '+m['column']+': emit exactly ONE entry per column at its NATIVE grain, not one per group-by')
        if not isinstance(m, dict) or set(m) != {'column', 'scope', 'aggregation', 'unit', 'evidence'} or m['column'] not in cols or m['aggregation'] not in AGGREGATIONS or not isinstance(m['scope'], str) or (m['unit'] is not None and not isinstance(m['unit'], str)):
            raise ValueError('Invalid measure contract')
        measures.add(m['column']);out['measures'].append({**m, 'evidence': evidence(m['evidence'], readme), 'status': 'candidate'})
    for j in value['joins']:
        if not isinstance(j, dict) or set(j) != {'target', 'source_columns', 'target_columns', 'evidence'} or j['target'] not in tables or j['target'] == table['clean_view']:
            raise ValueError('Unknown join target')
        tc = {c['name'] for c in tables[j['target']]['columns']}
        if not column_list(j['source_columns'], cols) or not column_list(j['target_columns'], tc) or len(j['source_columns']) != len(j['target_columns']):
            raise ValueError('Invalid join columns')
        out['joins'].append({**j, 'evidence': evidence(j['evidence'], readme), 'status': 'candidate'})
    return out


def key_stats(conn, schema, view, columns):
    identifiers = sql.SQL(',').join(sql.Identifier(c) for c in columns)
    nonnull = sql.SQL(' AND ').join(sql.SQL('{} IS NOT NULL').format(sql.Identifier(c)) for c in columns)
    total, populated, distinct = conn.execute(sql.SQL(
        'SELECT count(*), count(*) FILTER (WHERE {}), count(DISTINCT ROW({})) FILTER (WHERE {}) FROM {}.{}'
    ).format(nonnull, identifiers, nonnull, sql.Identifier(schema), sql.Identifier(view))).fetchone()
    return {'rows': total, 'non_null_rows': populated, 'distinct_non_null_groups': distinct,
            'null_key_rows': total-populated, 'unique_when_non_null': populated == distinct,
            'row_key_supported': total > 0 and populated == total and distinct == total,
            'note': 'Repeated grouping keys are valid; uniqueness alone does not prove business identity.'}


def check_table(conn, catalog, table, metadata, tables):
    schema = catalog['schema'];view = table['clean_view']
    for k in metadata['group_keys']:
        k['data_checks'] = key_stats(conn, schema, view, k['columns'])
    for m in metadata['measures']:
        m['data_checks'] = []
        for k in metadata['group_keys']:
            group = sql.SQL(',').join(sql.Identifier(c) for c in k['columns'])
            n = conn.execute(sql.SQL('SELECT count(*) FROM (SELECT 1 FROM {}.{} GROUP BY {} HAVING count(DISTINCT {})>1) q').format(
                sql.Identifier(schema), sql.Identifier(view), group, sql.Identifier(m['column']))).fetchone()[0]
            m['data_checks'].append({'group_key': k['name'], 'groups_with_multiple_values': n,
                                    'note': 'Constant within group is evidence of repetition, not proof that SUM is safe.'})
    for j in metadata['joins']:
        target = tables[j['target']]
        sc = {c['name']: c['sql_type'] for c in table['columns']}
        tc = {c['name']: c['sql_type'] for c in target['columns']}
        if any(sc.get(a) != tc.get(b) for a, b in zip(j['source_columns'], j['target_columns'])):
            j['data_checks'] = {'status': 'type_mismatch'};continue
        s = key_stats(conn, schema, view, j['source_columns'])
        t = key_stats(conn, schema, j['target'], j['target_columns'])
        predicate = sql.SQL(' AND ').join(sql.SQL('s.{}=t.{}').format(sql.Identifier(a), sql.Identifier(b))
                                           for a, b in zip(j['source_columns'], j['target_columns']))
        nonnull = sql.SQL(' AND ').join(sql.SQL('s.{} IS NOT NULL').format(sql.Identifier(a)) for a in j['source_columns'])
        orphans = conn.execute(sql.SQL('SELECT count(*) FROM {}.{} s WHERE {} AND NOT EXISTS (SELECT 1 FROM {}.{} t WHERE {})').format(
            sql.Identifier(schema), sql.Identifier(view), nonnull, sql.Identifier(schema), sql.Identifier(j['target']), predicate)).fetchone()[0]
        cardinality = ('one' if s['unique_when_non_null'] else 'many') + '_to_' + ('one' if t['unique_when_non_null'] else 'many')
        j['data_checks'] = {'status': 'checked', 'source': s, 'target': t,
                            'cardinality': cardinality, 'orphan_source_rows': orphans,
                            'fanout_risk': not t['unique_when_non_null'],
                            'business_verified': False}
    return metadata


def build_business_metadata(prepared_dir, source_root, *, model, dsn, output_dir=None):
    prepared = Path(prepared_dir);source = Path(source_root)
    out = Path(output_dir) if output_dir else prepared/'business_metadata'
    out.mkdir(parents=True, exist_ok=False)
    catalog_path = prepared/'postgres_catalog.json'
    catalog = json.loads(catalog_path.read_text())
    readme_path = source/'data'/catalog['business_id']/'README.md';rules_path = source/'rulebook.md'
    readme = readme_path.read_text();rules = rules_path.read_text()
    expected_readme = next(s['sha256'] for s in catalog['sources'] if s['name'] == 'README.md')
    if sha(readme_path) != expected_readme or sha(rules_path) != catalog['rulebook_sha256']:
        raise ValueError('Business metadata source fingerprint mismatch')
    tables = {t['clean_view']: t for t in catalog['tables'].values()}
    result = {'version': VERSION, 'business_id': catalog['business_id'], 'catalog_sha256': sha(catalog_path),
              'readme_sha256': sha(readme_path), 'rulebook_sha256': sha(rules_path),
              'questions_read': False, 'labels_read': False, 'model': model.metadata(), 'tables': {},
              'existing_relationship_candidates': catalog.get('relationships', []),
              'limitations': ['SLM proposals and structural checks are not business proof.', 'No alias learning or source mutation.']}
    started = perf_counter();schema = compact_schema(catalog)
    model.load()
    with psycopg.connect(dsn, autocommit=True) as conn:
        for name, table in tables.items():
            payload = {'task': 'business_metadata', 'business_schema': schema,
                       'readme': readme, 'rulebook': rules, 'grain_kinds': sorted(KINDS),
                       'target_table': name, 'target_schema': next(t for t in schema if t['table']==name),
                       'target_allowed_columns': [c['name'] for c in table['columns']],
                       'instruction': 'Annotate ONLY target_schema. Other tables are JOIN context, not columns to copy.'}
            attempts = [];validated = None
            for attempt in range(2):
                messages = [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': dumps(payload)}]
                record = {'table': name, 'attempt': attempt+1, 'messages': messages}
                try:
                    generated = model.generate(messages);record.update(generated)
                    if generated.get('hit_token_limit'):raise ValueError('Metadata output token limit')
                    validated = validate_proposal(strict_json(generated['raw_output']), table, tables, readme)
                except (ValueError, KeyError, TypeError) as exc:
                    record['error'] = str(exc);payload['contract_retry'] = str(exc)
                attempts.append(record)
                with (out/'model_calls.jsonl').open('a') as f:f.write(dumps(record)+'\n')
                if validated is not None:break
            if validated is None:
                validated = {'grain': {'kind': 'unknown', 'description': table.get('grain_candidate', 'unknown'),
                                       'status': 'unresolved'}, 'group_keys': [], 'measures': [], 'joins': [],
                             'error': attempts[-1].get('error')}
            with conn.transaction():
                conn.execute('SET TRANSACTION READ ONLY');conn.execute("SET LOCAL statement_timeout='30s'")
                validated = check_table(conn, catalog, table, validated, tables)
            result['tables'][name] = validated
            print(dumps({'business_metadata': catalog['business_id'], 'table': name,
                         'grain': validated['grain']['kind'], 'attempts': len(attempts)}), flush=True)
    if sha(catalog_path) != result['catalog_sha256'] or sha(readme_path) != result['readme_sha256'] or sha(rules_path) != result['rulebook_sha256']:
        raise ValueError('Sources changed during metadata preparation')
    result['elapsed_ms'] = round((perf_counter()-started)*1000, 3)
    (out/'metadata.json').write_text(dumps(result)+'\n')
    lines = ['# Business metadata: '+catalog['business_id'], '', 'Các mô tả là candidates; kiểm tra dữ liệu không chứng minh nghĩa nghiệp vụ.', '']
    for name, t in result['tables'].items():
        lines += ['## '+name, '', '```json', json.dumps(t, ensure_ascii=False, indent=2), '```', '']
    (out/'review.md').write_text('\n'.join(lines))
    return result
