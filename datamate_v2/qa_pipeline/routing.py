"""Exclude only dated tables proven disjoint from all validated windows."""
from copy import copy

EVENTS={'order_date','date','payment_date','created_at'}


def route_metadata(metadata,windows):
    if not windows: return metadata,{'excluded':[],'reason':'no temporal restriction'}
    kept=set();excluded=[];dated_kept=False
    for table in metadata['tables']:
        columns={c['sql_column'] for c in table['columns'] if c.get('meaning') in EVENTS and c['type'] in ('DATE','TIMESTAMPTZ')}
        stats=[s for s in table.get('stats',[]) if s['sql_column'] in columns]
        known=bool(stats) and all(s.get('min') is not None and s.get('max') is not None and not s.get('null_count') for s in stats)
        overlaps=not known or any(
            str(s['min'])[:10]<w['end_date_exclusive'] and
            (w['start_date'] is None or str(s['max'])[:10]>=w['start_date'])
            for s in stats for w in windows)
        if overlaps:
            kept.add(table['sql_table']);dated_kept|=bool(columns)
        else: excluded.append({'table':table['sql_table'],'reason':'all verified event-date coverage is outside every validated window'})
    if not dated_kept:
        # Preserve the catalog when the whole question may be out of coverage;
        # the existing source-backed coverage check can then return UA.
        return metadata,{'excluded':[],'reason':'no dated candidate overlaps; retain catalog for answerability validation'}
    # Keep lookup targets, not every other monthly fact table linked to the
    # same catalog. Undated catalogs are always retained above.
    while True:
        targets={r['to_table'] for r in metadata.get('relationships',[]) if r.get('status')=='verified' and r['from_table'] in kept}
        if targets<=kept: break
        kept|=targets
    result=copy(metadata)
    result['tables']=[t for t in metadata['tables'] if t['sql_table'] in kept]
    result['relationships']=[r for r in metadata.get('relationships',[]) if r['from_table'] in kept and r['to_table'] in kept]
    return result,{'excluded':[e for e in excluded if e['table'] not in kept],
                   'retained_tables':sorted(kept),'reason':'verified event-date overlap plus undated/unknown coverage and lookup targets'}
