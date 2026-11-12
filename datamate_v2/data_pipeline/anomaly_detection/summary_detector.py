"""Scan all rows; a word such as 'Tổng' alone is insufficient evidence."""
from decimal import Decimal
from datamate_v2.common import fold

LABELS={"tong","tong cong","tong ket","cong","subtotal","total","grand total","tong nhom"}

def detect_summary(rows,columns):
    result=[];numeric=[c["sql_column"] for c in columns if c["type"]=="NUMERIC"]
    group=[];all_details=[]
    for row in rows:
        labels=[v for k,v in row.items() if not k.startswith('_') and isinstance(v,str) and fold(v) in LABELS]
        has_label=bool(labels)
        is_summary=False;status="detail";evidence=[]
        filled=[v for k,v in row.items() if not k.startswith('_') and v not in (None,'')]
        if len(filled)==1 and isinstance(filled[0],str) and filled[0].strip().lower().startswith(('ghi chú:','note:','nguồn:')):
            is_summary=True;status='verified';evidence=['explicit footer prefix in a structurally isolated text row']
        if has_label:
            comparable=[k for k in numeric if row.get(k) is not None and any(r.get(k) is not None for r in all_details)]
            equal=[k for k in comparable if (group and sum((r.get(k) or Decimal(0)) for r in group)==row[k]) or
                   (all_details and sum((r.get(k) or Decimal(0)) for r in all_details)==row[k])]
            sparse=sum(v not in (None,"") for k,v in row.items() if not k.startswith('_'))<=max(3,len(columns)//2)
            if equal and sparse:
                is_summary=True;status="verified";evidence=["exact summary label","matches preceding detail sum: "+','.join(equal)]
            else: status="uncertain";evidence=["summary label without arithmetic/structure confirmation"]
        row.update(_is_summary_row=is_summary,_summary_status=status)
        result.append({"source_row":row["_source_row"],"status":status,"evidence":evidence})
        if is_summary: group=[]
        elif status=="detail": group.append(row);all_details.append(row)
    return result
