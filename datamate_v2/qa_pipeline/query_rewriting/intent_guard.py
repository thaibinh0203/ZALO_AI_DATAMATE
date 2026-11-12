"""Conservative, source-question checks for explicit metric/time anchors.

Unknown phrasings remain model work. These checks reject demonstrated drift;
they never generate SQL, look at labels or substitute a guessed interpretation.
"""
import re
from datamate_v2.common import fold,PipelineError


def normalize_question(text):
    text=fold(text)
    for short,long in [('hqua','hom qua'),('hnay','hom nay'),('dthu','doanh thu'),('thang trc','thang truoc'),('tuan trc','tuan truoc')]:
        text=re.sub(r'\b'+short+r'\b',long,text)
    return text


def metric(text):
    text=normalize_question(text)
    if re.search(r'\b(doanh thu|so tien|bao nhieu tien|thu ve.*tien|ban.*tien)\b',text): return 'money'
    if re.search(r'\b(so (?:luong )?don|may don|bao nhieu don|dem (?:so )?don|nhieu don nhat)\b',text): return 'order_count'
    if 'so luong' in text: return 'quantity'
    return None


def time_anchors(text):
    text=normalize_question(text);anchors=set()
    for phrase,kind in [('hom qua','yesterday'),('hom nay','today'),('tuan roi','previous_calendar_week'),
        ('tuan truoc','previous_calendar_week'),('tuan nay','current_calendar_week'),
        ('thang nay','current_calendar_month'),('thang truoc','previous_calendar_month'),
        ('quy truoc','previous_calendar_quarter'),('quy vua qua','previous_calendar_quarter')]:
        if re.search(r'\b'+phrase+r'\b',text): anchors.add(kind)
    if re.search(r'\b(tinh den|den het)\b',text) and any(word in text for word in ('con no','ton kho','con hang','con ton')):
        anchors.discard('today');anchors.add('as_of')
    if {'current_calendar_month','previous_calendar_month'}<=anchors:
        anchors.remove('previous_calendar_month');anchors.add('previous_month_same_elapsed_days')
    return anchors


def validate_rewrite_intent(question,rewrite):
    if any(w.get('status')=='ambiguous' for w in rewrite['time_windows']): return rewrite
    original=question['question'];rewritten=rewrite['rewritten_question']
    a,b=metric(original),metric(rewritten)
    if a and b and a!=b:
        raise PipelineError(f'Rewrite changed metric: original={a}, rewritten={b}; preserve the original question')
    if a=='order_count' and re.search(r'ban (?:duoc )?nhieu nhat|ban chay|mat hang.*nhat',normalize_question(rewritten)):
        raise PipelineError('Rewrite changed order count into product ranking; preserve original metric')
    expected=time_anchors(original)
    # A same-month comparison also contains "hôm nay" as an endpoint, not
    # a separate today-only window.
    if 'current_calendar_month' in expected: expected.discard('today')
    actual={w['interpretation'] for w in rewrite['time_windows']}
    missing=expected-actual
    if missing:
        # A concrete absolute window can express the same relative period.
        # Compare Python arithmetic, never the model's proposed date strings.
        from ..temporal_validator import validate_windows
        candidates=[w for w in rewrite['time_windows'] if w['interpretation'].startswith('absolute_')]
        resolved=validate_windows(candidates,question['as_of_date']) if candidates else []
        for kind in list(missing):
            target=validate_windows([{'name':'expected','interpretation':kind,'parameters':{},'status':'proposed'}],question['as_of_date'])[0]
            if any((w['start_date'],w['end_date_exclusive'])==(target['start_date'],target['end_date_exclusive']) for w in resolved): missing.remove(kind)
    if missing:
        raise PipelineError(f'Rewrite changed time interpretation; original requires {sorted(expected)}, got {sorted(actual)}')
    stated=time_anchors(rewritten)
    if 'current_calendar_month' in stated: stated.discard('today')
    if expected and stated-expected:
        raise PipelineError(f'Rewritten text changes explicit time anchors: original={sorted(expected)}, rewritten={sorted(stated)}')
    text=normalize_question(original)
    for match in re.finditer(r'\b(thang|quy)\s+(\d{1,2})\s+(?:nam\s+)?(20\d{2})\b',text):
        label,value,year=match.groups();key='month' if label=='thang' else 'quarter'
        kind='absolute_month' if label=='thang' else 'absolute_quarter'
        if not any(w['interpretation']==kind and w.get('parameters',{}).get(key)==int(value) and w.get('parameters',{}).get('year')==int(year) for w in rewrite['time_windows']):
            raise PipelineError(f'Rewrite changed explicit {label} {value}/{year}; expected {kind} with original parameters')
    return rewrite
