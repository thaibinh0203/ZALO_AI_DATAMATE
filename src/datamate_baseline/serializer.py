"""Exact Decimal JSON and strict conversion of query shapes to answer types."""
from datetime import date, datetime
from decimal import Decimal
import json
import math
from datamate_eval.matching import validate_record


def dumps(value):
    if value is None:return 'null'
    if value is True:return 'true'
    if value is False:return 'false'
    if isinstance(value,Decimal):
        if not value.is_finite():raise ValueError('Non-finite Decimal')
        if abs(value.adjusted())>10000:raise ValueError('Decimal magnitude exceeds output budget')
        return format(value,'f')
    if isinstance(value,int):return str(value)
    if isinstance(value,float):
        if not math.isfinite(value):raise ValueError('Non-finite float')
        return json.dumps(value,allow_nan=False)
    if isinstance(value,(date,datetime)):return json.dumps(value.isoformat(),ensure_ascii=False)
    if isinstance(value,str):return json.dumps(value,ensure_ascii=False)
    if isinstance(value,(list,tuple)):return '['+','.join(dumps(x) for x in value)+']'
    if isinstance(value,dict):
        if any(not isinstance(k,str) for k in value):raise ValueError('JSON keys must be strings')
        return '{'+','.join(dumps(k)+':'+dumps(v) for k,v in value.items())+'}'
    raise ValueError('Unsupported JSON type: '+type(value).__name__)


def cell(value):
    if isinstance(value,datetime):
        if value.time().isoformat()!='00:00:00':raise ValueError('Datetime cannot silently lose time')
        return value.date().isoformat()
    if isinstance(value,date):return value.isoformat()
    return value


def format_answer(question_id,kind,result):
    if result['truncated']:raise ValueError('Query result exceeded row cap')
    rows=result['rows'];columns=result['columns']
    if kind=='table':answer={'columns':columns,'rows':[[cell(v) for v in row] for row in rows]}
    elif kind=='list':
        if len(columns)!=1:raise ValueError('List requires one result column')
        answer=[cell(row[0]) for row in rows]
    else:
        if len(columns)!=1 or len(rows)!=1:raise ValueError('Scalar requires exactly one row and column')
        answer=cell(rows[0][0])
    record={'id':question_id,'type':kind,'answer':answer}
    validate_record(record)
    # Validate that JSON serialization will not coerce numeric values into strings.
    dumps(record)
    return record
