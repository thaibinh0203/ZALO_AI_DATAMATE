"""Resolve source layouts using source headers and README evidence, never IDs."""
from collections import Counter
from itertools import chain,islice
import hashlib
import re

from .normalization import BY_KEY,column_spec,identifier,key


def readme_column_sections(text):
    sections={};current=None
    for line in text.splitlines():
        heading=re.match(r"##\s+Cột trong\s+`([^`]+)`",line)
        if heading:
            sheet=re.search(r"sheet\s+`([^`]+)`",line,re.I)
            current=(heading[1],sheet[1]) if sheet else heading[1]
            sections[current]={};continue
        if line.startswith("##"): current=None
        pair=re.match(r"-\s+\*\*(.+?)\*\*:\s*(.*)",line)
        if current and pair: sections[current][key(pair[1])]=pair[2]
    return sections


def role_of(columns, name):
    roles={c["role"] for c in columns}
    if "conversion_factor" in roles: return "unit_conversions"
    if "purchase_id" in roles and ({"quantity","base_quantity"}&roles): return "purchase_lines"
    if ("quantity" in roles or "base_quantity" in roles) and ({"product_name","product_id"}&roles):
        return "sale_lines" if 'order_id' in roles else 'sale_events'
    if "order_id" in roles and "receipt_id" in roles: return "payments"
    if "purchase_id" in roles and "quantity" in roles: return "purchase_lines"
    if "order_id" in roles and "quantity" not in roles and "lot_id" not in roles: return "orders_or_order_extension"
    if "balance" in roles and "order_id" not in roles: return "inventory_or_stock_event"
    if "chiphi" in key(name): return "expenses_candidate"
    if {"product_id","product_name"}<=roles and "quantity" not in roles: return "products_or_product_extension"
    return "unclassified"


def adapt(table, readme, *, header_resolver=None):
    iterator=iter(table.rows);preview=list(islice(iterator,20))
    if not preview: return None
    sections=readme_column_sections(readme)
    descriptions=sections.get((table.path.name,table.name),sections.get(table.path.name,{}))
    known=set(BY_KEY)|set(descriptions)
    def score(row):
        texts=[v for v in row.values if isinstance(v,str) and v.strip()]
        hits=sum(key(v) in known for v in texts)
        return hits*10+len(texts)-sum(not isinstance(v,str) and v is not None for v in row.values)*2
    if table.format=='sqlite':header_index=0
    elif table.format=='csv':header_index=next((i for i,r in enumerate(preview) if any(v is not None and str(v).strip() for v in r.values)),0)
    else:header_index=max(range(len(preview)),key=lambda i:score(preview[i]))
    header=preview[header_index]
    header_evidence=None
    if table.format=='xlsx' and sum(key(v) in known for v in header.values if v is not None)<2:
        if header_resolver is None:
            raise ValueError(f'Excel header needs explicit mapping: {table.path.name}/{table.name}')
        header_index,header_evidence=header_resolver(table,preview,readme)
        if type(header_index) is not int or not 0<=header_index<len(preview):
            raise ValueError('Invalid header resolver index')
        header=preview[header_index]
    if len([v for v in header.values if v is not None])<2:
        raise ValueError(f"Header not confidently identified: {table.path.name}/{table.name}")
    # Trim unused trailing columns, preserving internal positions/empty header cells.
    width=max(i+1 for i,v in enumerate(header.values) if v is not None and str(v).strip())
    headers=[str(v).strip() if v is not None and str(v).strip() else f"unnamed_{i+1}" for i,v in enumerate(header.values[:width])]
    columns=[];used=set()
    for i,name in enumerate(headers):
        spec=column_spec(name,descriptions.get(key(name),""),table.declared[i] if table.declared else "")
        candidate=spec["name"]
        if candidate in used: candidate=candidate[:43]+"_"+hashlib.sha256(f"{name}:{i}".encode()).hexdigest()[:8]
        spec["name"]=candidate;used.add(candidate);columns.append(spec)
    years=re.findall(r"(?<![0-9])(20[0-9]{2})(?![0-9])",table.name)
    if not years: years=re.findall(r"(?<![0-9])(20[0-9]{2})(?![0-9])",table.path.stem)
    year=int(years[0]) if len(set(years))==1 else None
    period_sheet=bool(re.search(r"(?:tháng|thang|t)[ _-]*[0-9]{1,2}[_ -]+20[0-9]{2}",table.name,re.I))
    period_sheet=period_sheet or bool(re.fullmatch(r'(?:[0-9]{1,2}[./_-]20[0-9]{2}|20[0-9]{2}[./_-][0-9]{1,2})',table.name))
    base=re.sub(r"_(?:20[0-9]{2}|[0-9]+)$","",table.path.stem)
    logical=base if period_sheet or table.name in {"Sheet1",table.path.stem} else base+"_"+table.name
    signature=hashlib.sha256("\0".join(headers).encode()).hexdigest()[:8]
    name=identifier(logical)[:40]+"_"+signature
    meta={"name":name,"logical_name":logical,"role":role_of(columns,logical),"columns":columns,
          "source_file":table.path.name,"source_table":table.name,"format":table.format,
          "header_row":header.number,"header_context":[r.values for r in preview[:header_index+1]],
          "year_evidence":year,"layout":table.metadata,
          "header_confidence":"source_schema" if table.format=="sqlite" else "readme_or_header_vocabulary",
          "blank_rows":0,"source_records":0}
    if header_evidence:
        meta['header_confidence']='slm_candidate_existing_source_row'
        meta['header_evidence']=header_evidence
    def rows():
        for row in chain(preview[header_index+1:],iterator):
            meta["source_records"]+=1
            if not any(v is not None and str(v).strip() for v in row.values):
                meta["blank_rows"]+=1
                yield {"values":[],"kind":"blank","source_row":row.number,"cells":{},"flags":[]}
                continue
            values=row.values[:width]+[None]*max(0,width-len(row.values))
            flags=list(row.flags)
            extra=row.values[width:]
            if len(row.values)<width and table.format=="csv" or any(v is not None and str(v).strip() for v in extra):
                flags.append({"error":"ragged_row","extra_cells":extra,"observed_width":len(row.values)})
            normalized=[key(v) if v is not None else "" for v in values]
            first=next((v for v in normalized if v),"")
            kind="repeated_header" if normalized==[key(v) for v in headers] else "data"
            if first in {"tong","tongcong","cong","subtotal","grandtotal"}: kind="subtotal_candidate"
            if any(flag.get('error')=='ragged_row' for flag in flags):kind='quarantine'
            yield {"values":values,"kind":kind,"source_row":row.number,"cells":row.cells,"flags":flags}
    return meta,rows()
