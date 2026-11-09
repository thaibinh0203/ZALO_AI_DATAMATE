"""P0-P2: parse sources, keep raw values/provenance, preliminary row decisions.

Nothing here fills, deduplicates or derives values. Parse attempts under the
rule-based column specs are recorded as evidence; later stages recompute typed values
for the current semantic revision from the raw cells.
"""
from collections import Counter
import hashlib
from pathlib import Path

from ..adapters import adapt
from ..parsers import dumps, parse_file
from ..pipeline import digest
from .rows import classify, decision_record, finalize_part
from .temporal import year_evidence
from .typed import typed_cell


def source_inputs(business):
    files = [];ignored = []
    for path in sorted(Path(business).iterdir()):
        if path.is_symlink():
            raise ValueError('Source symlinks are not allowed')
        if path.is_file() and path.suffix.lower() in {'.csv', '.xlsx', '.sqlite'}:
            files.append(path)
        elif path.name != 'README.md':
            ignored.append(path.name)
    if not files:
        raise ValueError('No supported data sources')
    return files, ignored


def row_id(file_name, file_sha, table_name, source_row):
    return hashlib.sha256(dumps([file_name, file_sha, table_name, source_row]).encode()).hexdigest()


def stage_business(business, output, *, readme, rules, header_resolver=None, csv_encoding='utf-8-sig'):
    """Write raw/<table>.jsonl and return staging metadata + non-default row decisions.

    ``rules`` maps rule signature names to rulebook unit IDs (for year evidence).
    """
    business = Path(business);output = Path(output)
    (output/'raw').mkdir(parents=True, exist_ok=False)
    files, ignored = source_inputs(business)
    shas = {p.name: digest(p) for p in files}
    tables = {};handles = {};decisions = {};blank_ranges = {}
    try:
        for source in files:
            for table in parse_file(source, encoding=csv_encoding):
                adapted = adapt(table, readme, header_resolver=header_resolver)
                if adapted is None:
                    continue
                layout, rows = adapted;name = layout['name']
                if name not in tables:
                    tables[name] = {'name': name, 'logical_name': layout['logical_name'],
                                    'table_role_hint': layout['role'], 'columns': layout['columns'],
                                    'parts': [], 'raw_path': f'raw/{name}.jsonl', 'rows': 0,
                                    'row_kinds': Counter(), 'blank_rows': 0}
                    handles[name] = (output/f'raw/{name}.jsonl').open('w', encoding='utf-8')
                    decisions[name] = [];blank_ranges[name] = []
                meta = tables[name]
                if meta['columns'] != layout['columns']:
                    raise ValueError(f'Conflicting column semantics in merged sources: {name}')
                part_index = len(meta['parts'])
                part = {'part': part_index, 'source_file': source.name, 'source_sha256': shas[source.name],
                        'source_table': table.name, 'format': table.format, 'header_row': layout['header_row'],
                        'header_context': layout['header_context'], 'header_confidence': layout['header_confidence'],
                        'header_evidence': layout.get('header_evidence'), 'layout': layout['layout']}
                part['year_info'] = year_evidence(part, rules)
                meta['parts'].append(part)
                headers = [c['original_name'] for c in layout['columns']]
                part_decisions = [];after_blank = False;blank_start = None
                for row in rows:
                    if row['kind'] == 'blank':
                        meta['blank_rows'] += 1;after_blank = True
                        blank_start = row['source_row'] if blank_start is None else blank_start
                        last_blank = row['source_row']
                        continue
                    if blank_start is not None:
                        blank_ranges[name].append([part_index, blank_start, last_blank]);blank_start = None
                    decision = classify(row['values'], headers=headers, columns=layout['columns'],
                                        flags=row['flags'], table_format=table.format)
                    rid = row_id(source.name, shas[source.name], table.name, row['source_row'])
                    seq = meta['rows'];meta['rows'] += 1
                    attempts = [typed_cell(v, c, row['cells'].get(str(i)), part['year_info'])
                                for i, (v, c) in enumerate(zip(row['values'], layout['columns']))]
                    record = {'row_id': rid, 'seq': seq, 'part': part_index,
                              'source': {'file': source.name, 'table_or_sheet': table.name,
                                         'row': row['source_row'], 'format': table.format},
                              'raw': row['values'], 'cells': row['cells'], 'flags': row['flags'],
                              'after_blank': after_blank, 'prelim': decision,
                              'attempt': [[a['status'], a['value']] for a in attempts]}
                    handles[name].write(dumps(record)+'\n')
                    after_blank = False
                    if decision['kind'] != 'data' or decision['rule'] != 'default_structured_row':
                        part_decisions.append(decision_record(rid, seq, part_index, decision))
                    elif decision['kind'] == 'data':
                        part_decisions.append({'seq': seq, 'kind': 'data', 'implicit': True})
                if blank_start is not None:
                    blank_ranges[name].append([part_index, blank_start, last_blank])
                finalize_part(part_decisions)
                for d in part_decisions:
                    if not d.get('implicit'):
                        decisions[name].append(d)
    finally:
        for handle in handles.values():
            handle.close()
    for name, meta in tables.items():
        meta['raw_sha256'] = digest(output/meta['raw_path'])
        kinds = Counter(d['kind'] for d in decisions[name])
        kinds['data'] += meta['rows']-sum(kinds.values())
        meta['row_kinds'] = dict(kinds)
        meta['blank_ranges'] = blank_ranges[name]
    return {'tables': tables, 'decisions': decisions, 'ignored_files': ignored,
            'sources': [{'name': p.name, 'sha256': shas[p.name], 'bytes': p.stat().st_size} for p in files]}


def iter_raw(output, table):
    import json
    with (Path(output)/table['raw_path']).open(encoding='utf-8') as stream:
        for line in stream:
            yield json.loads(line)
