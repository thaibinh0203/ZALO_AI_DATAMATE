from datetime import date,datetime
from decimal import Decimal,ROUND_HALF_UP
from zoneinfo import ZoneInfo
from datamate_v2.common import PipelineError
from datamate_v2.evaluation.matching import validate_payload

def verify_check(result,expected):
    if len(result['rows'])!=1 or len(result['rows'][0])!=1: raise PipelineError('Quality check must return one scalar')
    value=result['rows'][0][0]
    passed=(type(value) is bool and value) if expected=='true' else (value is not None and type(value) is not bool and value==0) if expected=='zero' else (value is not None and type(value) is not bool and value>0)
    if not passed: raise PipelineError('Data quality/coverage check failed')

def serialize_result(question,plan,result):
    if result['columns']!=plan['output_columns']: raise PipelineError('SQL output columns differ from plan')
    kind=plan['answer_type'];rows=result['rows']
    digits=plan.get('round_digits')
    if kind=='percent': digits=2
    if plan.get('unit')=='VND': digits=0
    def convert(v):
        if v is None: raise PipelineError('NULL result cannot be silently replaced')
        if isinstance(v,(date,datetime)): return v.isoformat()
        if isinstance(v,Decimal):
            if not v.is_finite(): raise PipelineError('Non-finite result')
            if digits is not None: v=v.quantize(Decimal(1).scaleb(-digits),rounding=ROUND_HALF_UP)
            return int(v) if v==v.to_integral_value() else v
        if isinstance(v,float): return convert(Decimal(str(v)))
        return v
    if kind=='table': answer={'columns':result['columns'],'rows':[[convert(v) for v in row] for row in rows]}
    elif kind=='list':
        mode=plan.get('list_mode','rows')
        if mode=='row':
            if len(rows)!=1: raise PipelineError('Multi-number list needs exactly one row')
            answer=[convert(v) for v in rows[0]]
        else:
            if len(result['columns'])!=1: raise PipelineError('Entity list requires one output column')
            answer=[convert(row[0]) for row in rows]
    else:
        if len(rows)!=1 or len(rows[0])!=1: raise PipelineError('Scalar answer requires exactly one cell')
        value=rows[0][0]
        if value is None and plan.get('null_policy')=='zero_with_evidence':
            # A plan assertion alone is insufficient; one passed explicit non-empty quality check is required by orchestrator.
            if not plan.get('_null_zero_validated'): raise PipelineError('NULL-to-zero evidence not independently verified')
            value=0
        if kind=='date' and isinstance(value,datetime):
            if value.tzinfo is not None:
                value=value.astimezone(ZoneInfo(plan.get('_timezone','Asia/Ho_Chi_Minh')))
            value=value.date()
        answer=convert(value)
    validate_payload(kind,answer)
    return {'id':question['id'],'type':kind,'answer':answer}
