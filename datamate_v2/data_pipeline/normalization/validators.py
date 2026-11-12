from decimal import Decimal
from datamate_v2.common import PipelineError
from .value_normalizer import parse_value,MONEY,normalize_unit
from copy import deepcopy

VALUE_SCHEMA={'type':'object','additionalProperties':False,'required':['proposals'],'properties':{
 'proposals':{'type':'array','uniqueItems':True,'items':{'type':'object','additionalProperties':False,
 'required':['item_id','clean_value','unit','source_quote'],'properties':{
 'item_id':{'type':'string'},'clean_value':{'type':['number','string','null']},
 'unit':{'type':'string','maxLength':64},'source_quote':{'type':'string','maxLength':512},
 'reason':{'type':'string','maxLength':160}}}}}}

def value_schema(item_ids):
    schema=deepcopy(VALUE_SCHEMA)
    schema['properties']['proposals']['maxItems']=len(item_ids)
    schema['properties']['proposals']['items']['properties']['item_id']={'enum':list(item_ids)}
    return schema

def validate_value_proposal(proposal,raw,column,context,*,profile=None,year=None,timezone=None,allowed_units=None):
    """No semantic value repair without a source-backed deterministic equivalence."""
    quote=proposal["source_quote"]
    if not quote or quote not in context.get("readme","")+context.get("rulebook",""): return None
    if column.get('meaning') in MONEY and proposal['unit'].lower() not in {'vnd','đồng','đ','vnđ'}: return None
    if column.get('meaning')=='percent' and proposal['unit'].lower() not in {'%','percent','percentage_points'}: return None
    if column.get('meaning')=='quantity' and allowed_units and normalize_unit(proposal['unit']) not in {normalize_unit(u) for u in allowed_units}: return None
    # Resolve separator conventions only when explicitly documented in the quoted rule.
    locale=(profile or {}).get('locale')
    if "dấu chấm phân cách hàng nghìn" in quote.lower() or "dấu phẩy thập phân" in quote.lower(): locale="vi"
    elif "dấu phẩy phân cách hàng nghìn" in quote.lower() or "dấu chấm thập phân" in quote.lower(): locale="en"
    parsed,status=parse_value(raw,column,locale=locale,year=year,timezone=timezone,allowed_units=allowed_units)
    if status!="verified": return None
    proposed,status2=parse_value(proposal["clean_value"],column,locale=locale,year=year,timezone=timezone,allowed_units=allowed_units)
    if status2!="verified" or proposed!=parsed: return None
    return parsed

def cross_column_checks(row,columns,*,formula_verified=False):
    if not formula_verified: return []
    names={c.get("meaning"):c["sql_column"] for c in columns}
    if not all(k in names for k in ("quantity","unit_price","line_total")): return []
    q,p,t=(row.get(names[k]) for k in ("quantity","unit_price","line_total"))
    if None not in (q,p,t) and q*p!=t: return ["quantity_price_total_mismatch"]
    return []
