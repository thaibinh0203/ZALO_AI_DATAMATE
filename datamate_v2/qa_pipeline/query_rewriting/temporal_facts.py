"""Conservative time facts from the original question, never model prose or labels."""
import re
from copy import deepcopy
from .intent_guard import normalize_question,time_anchors
from ..temporal_contract import window_schema,PARAMETER_SCHEMAS


def temporal_facts(question,calendar_settings):
    if calendar_settings.get('calendar_rule','MONDAY_TO_SUNDAY')!='MONDAY_TO_SUNDAY': return None
    text=normalize_question(question['question'])
    # Leave special calendars, partial periods, ranges and exclusions to the SLM.
    if re.search(r'\b(am lich|tai chinh|nien do|tet|nua dau|nua cuoi|giua thang|ngay dau|ngay cuoi|dau tuan|cuoi tuan|ngoai tru|khong tinh ngay|truoc hom|sau hom|ca thang|tron thang|toan bo thang|chu nhat)\b|(?<!doanh )\bthu\s+(hai|ba|tu|nam|sau|bay|[2-7])\b',text): return None
    absolute=list(re.finditer(r'\b(thang|quy)\s+(\d{1,2})\s+(?:nam\s+)?(20\d{2})\b',text))
    rolling=list(re.finditer(r'\b(\d{1,5})\s+ngay\s+(?:gan nhat|gan day|vua qua)\b',text))
    relative=time_anchors(text)
    if 'current_calendar_month' in relative: relative.discard('today')
    windows=[];evidence=[]
    def add(kind,parameters,expression):
        windows.append({'name':kind,'interpretation':kind,'parameters':parameters,'status':'proposed'})
        evidence.append(expression)
    if absolute:
        # Do not reinterpret a range/comparison/partial absolute month as a full month.
        if len(absolute)!=1 or relative or rolling or re.search(r'\b(dau thang|cuoi thang|tu ngay|den|cung ky|truoc|sau)\b|\bngay\s+\d',text): return None
        match=absolute[0];label,value,year=match.groups();value=int(value);year=int(year)
        if not 1900<=year<=2200 or not 1<=value<=(12 if label=='thang' else 4):return None
        # Another numbered temporal phrase means this is not the whole expression.
        rest=text[:match.start()]+' '+text[match.end():]
        if re.search(r'\b(thang|quy|tuan|nam)\s+\d',rest):return None
        add('absolute_month' if label=='thang' else 'absolute_quarter',
            {'year':year,'month' if label=='thang' else 'quarter':value},match.group())
    elif rolling:
        if len(rolling)!=1 or relative:return None
        match=rolling[0];n=int(match.group(1));rest=text[:match.start()]+' '+text[match.end():]
        if not 1<=n<=36600 or re.search(r'\b(thang|quy|tuan|nam|ngay)\s+\d|\b(cung ky|truoc|sau)\b',rest):return None
        add('rolling_days',{'n':n},match.group())
    elif relative:
        checked_text=text.replace('tu dau thang nay','thang nay')
        if re.search(r'\b(thang|quy|tuan|nam|ngay)\s+\d|\b(dau thang|cuoi thang|dau nam|cuoi nam|cung ky nam)\b',checked_text):return None
        if len(relative)>1 and relative!={'current_calendar_month','previous_month_same_elapsed_days'}:return None
        if 'cung ky' in text and relative!={'current_calendar_month','previous_month_same_elapsed_days'}:return None
        for kind in sorted(relative):add(kind,{},text)
    if not windows:return None
    return {'source_question':question['question'],'normalized_expressions':evidence,
            'reference_date':question['as_of_date'],'windows':windows}


def constrain_time_schema(schema,facts):
    if not facts:return schema
    variants=[]
    for window in facts['windows']:
        variant=window_schema(window['interpretation'])
        for name,value in window['parameters'].items():
            variant['properties']['parameters']['properties'][name]={'enum':[value]}
        variant['properties']['reference_date']={'enum':[facts['reference_date']]}
        variants.append(variant)
    schema['properties']['time_windows']={'type':'array','minItems':len(variants),'maxItems':len(variants),
        'items':variants[0] if len(variants)==1 else {'oneOf':variants}}
    return schema


def apply_temporal_facts(rewrite,facts):
    if not facts:return None
    before=deepcopy(rewrite.get('time_windows',[]));used=set();canonical=[]
    for expected in facts['windows']:
        window=deepcopy(expected)
        # Preserve already-correct window names for bindings and adapters.
        match=next((w for w in before if w.get('interpretation')==expected['interpretation'] and
                    w.get('parameters',{})==expected['parameters'] and isinstance(w.get('name'),str) and
                    w['name'] and w['name'] not in used),None)
        if match:window['name']=match['name']
        elif len(before)==len(facts['windows'])==1 and isinstance(before[0].get('name'),str) and before[0]['name'] and before[0]['name'] not in PARAMETER_SCHEMAS:
            window['name']=before[0]['name']  # A neutral binding label has no time authority.
        while window['name'] in used:window['name']+='_'  # No duplicated binding names.
        used.add(window['name']);window['reference_date']=facts['reference_date']
        canonical.append(window)
    rewrite['time_windows']=canonical
    def meaning(windows):
        return [(w.get('interpretation'),w.get('parameters',{}),w.get('status')) for w in windows]
    recovered=meaning(before)!=meaning(canonical) or any(w.get('reference_date',facts['reference_date'])!=facts['reference_date'] for w in before)
    return {**facts,'model_windows':before,'recovered':recovered}
