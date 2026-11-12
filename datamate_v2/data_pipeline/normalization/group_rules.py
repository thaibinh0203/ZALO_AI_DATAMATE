"""Source-backed export group carry-forward and within-order duplicate handling."""
from datamate_v2.common import fold

GROUP_FIELDS={'order_date','date','created_at','customer_id','customer_name','customer_group','status','payment_method','branch','employee_id','employee_name','note'}

def apply_group_rules(rows,columns,rulebook):
    ids=[c['sql_column'] for c in columns if c.get('meaning')=='order_id' and c['semantic_status']=='verified']
    if len(ids)!=1: return []
    key=ids[0];current=None;group={};seen={};logs=[]
    fill_allowed='Ô để trống ở các dòng sau của cùng một nhóm dòng' in rulebook
    dedup_allowed='Hai dòng của **cùng một đơn** giống nhau ở mọi ô' in rulebook
    retained=[]
    for row in rows:
        raw_filled=[v for v in row['_raw'] if v not in (None,'')]
        footer=len(raw_filled)==1 and isinstance(raw_filled[0],str) and raw_filled[0].strip().lower().startswith(('ghi chú:','note:','nguồn:'))
        if footer or any(isinstance(v,str) and fold(v) in {'tong','tong cong','total','subtotal','grand total'} for v in row['_raw']):
            retained.append(row);current=None;group={};continue
        order=row.get(key)
        if order not in (None,''):
            if order!=current: current=order;group={}
        elif fill_allowed and current is not None:
            row[key]=current;row['_quality']=[q for q in row['_quality'] if q['column']!=key]
            logs.append({'row':row['_source_row'],'column':key,'operation':'group_carry_forward','source_order':current})
        if fill_allowed and current is not None:
            for col in columns:
                name=col['sql_column']
                if col.get('meaning') not in GROUP_FIELDS: continue
                if row.get(name) is None and name in group and any(q['column']==name and q['status']=='missing' for q in row['_quality']):
                    row[name]=group[name][0];row['_quality']=[q for q in row['_quality'] if q['column']!=name]
                    logs.append({'row':row['_source_row'],'column':name,'operation':'group_carry_forward','source_row':group[name][1]})
                elif row.get(name) is not None: group[name]=(row[name],row['_source_row'])
        # Raw rows are still preserved in the raw artifact. Never deduplicate across different order IDs.
        signature=repr(row['_raw'])
        if dedup_allowed and row.get(key) not in (None,'') and signature in seen.setdefault(row[key],set()):
            logs.append({'row':row['_source_row'],'operation':'duplicate_same_order','order_id':row[key]});continue
        if row.get(key) not in (None,''): seen.setdefault(row[key],set()).add(signature)
        retained.append(row)
    rows[:]=retained
    return logs
