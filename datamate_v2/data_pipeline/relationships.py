"""Verify code-based links against all rows and reject ambiguous target tables."""
from collections import defaultdict

KEYS={'order_id','product_id','customer_id','employee_id','supplier_id'}

def verify_relationships(tables,clean_tables,readme,rulebook):
    candidates=[]
    for left in tables:
        for lc in left['columns']:
            if lc.get('meaning') not in KEYS|{'product_name'} or lc['semantic_status']!='verified': continue
            lv=[r[lc['sql_column']] for r in clean_tables[left['sql_table']] if not r['_is_summary_row'] and r[lc['sql_column']] not in (None,'')]
            if not lv: continue
            for right in tables:
                if left is right: continue
                for rc in right['columns']:
                    if rc.get('meaning')!=lc['meaning'] or rc['semantic_status']!='verified': continue
                    if lc['meaning']=='product_name':
                        # An exact name can link to a product catalog only after
                        # proving catalog identity, unique codes and unique names.
                        ids=[c for c in right['columns'] if c.get('meaning')=='product_id' and c['semantic_status']=='verified']
                        if len(ids)!=1: continue
                        codes=[r.get(ids[0]['sql_column']) for r in clean_tables[right['sql_table']] if not r['_is_summary_row']]
                        if not codes or any(code in (None,'') for code in codes) or len(set(codes))!=len(codes): continue
                    rv=[r[rc['sql_column']] for r in clean_tables[right['sql_table']] if not r['_is_summary_row'] and r[rc['sql_column']] not in (None,'')]
                    if not rv or len(set(rv))!=len(rv) or not set(lv).issubset(set(rv)): continue
                    # Both code meanings must be explicitly mentioned in README, beyond an accidental column-name match.
                    if lc['original_column'].lower() not in readme.lower() or rc['original_column'].lower() not in readme.lower(): continue
                    candidates.append({'from_table':left['sql_table'],'from_column':lc['sql_column'],
                       'to_table':right['sql_table'],'to_column':rc['sql_column'],
                       'cardinality':'one_to_one' if len(set(lv))==len(lv) else 'many_to_one',
                       'status':'candidate','evidence':['verified code semantics in mapping registry','original code columns mentioned in README',
                         'target key unique across all non-summary rows','all non-null source keys have exact target match'],
                       'source_nonnull_count':len(lv),'target_nonnull_count':len(rv),'orphan_count':0})
                    if lc['meaning']=='product_name':
                        candidates[-1]['evidence'].append('catalog product_id complete and unique; product_name complete-domain exact match and target names unique')
    grouped=defaultdict(list)
    for r in candidates: grouped[(r['from_table'],r['from_column'])].append(r)
    relationships=[]
    for rows in grouped.values():
        if len(rows)==1: rows[0]['status']='verified'
        else:
            for r in rows: r['evidence'].append('Multiple matching targets; relationship is not verified')
        relationships.extend(rows)
    return relationships
