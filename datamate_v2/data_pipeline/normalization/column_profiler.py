"""Sample across a column, then check format evidence over every distinct value."""
from collections import Counter
import re
from datamate_v2.common import PipelineError

VI_GROUP=re.compile(r'^[+-]?\d{1,3}(?:\.\d{3}){2,}$')
EN_GROUP=re.compile(r'^[+-]?\d{1,3}(?:,\d{3}){2,}$')
VI_MIXED=re.compile(r'^[+-]?\d{1,3}(?:\.\d{3})+,\d+$')
EN_MIXED=re.compile(r'^[+-]?\d{1,3}(?:,\d{3})+\.\d+$')


def raw_key(value):
    # Keep strings/ints/bools, whitespace and exact spelling distinct. Never merge by similarity.
    return type(value).__name__+':'+repr(value)


def sample_positions(length,limit=32):
    if limit<3: raise PipelineError('profile_sample_size must be at least 3')
    if length<=limit: return list(range(length))
    return sorted({round(i*(length-1)/(limit-1)) for i in range(limit)})


def shape(value):
    if not isinstance(value,str): return type(value).__name__
    return re.sub(r'\d+','#',' '.join(value.lower().split()))[:160]


def locale_evidence(value):
    if not isinstance(value,str): return None
    text=re.sub(r'(?:vnd|vnđ|đ|₫)$','',value.strip().lower().replace(' ','').replace('\u00a0',''))
    if text.startswith('(') and text.endswith(')'): text=text[1:-1]
    if VI_GROUP.fullmatch(text) or VI_MIXED.fullmatch(text): return 'vi'
    if EN_GROUP.fullmatch(text) or EN_MIXED.fullmatch(text): return 'en'
    # Single separators such as 1,250 or 1.250 never establish a locale by themselves.
    return None


def profile_column(raw_rows,column,settings):
    index=column['index'];limit=int(settings.get('profile_sample_size',32))
    positions=sample_positions(len(raw_rows),limit)
    samples=[{'source_row':raw_rows[i]['index'],'raw':raw_rows[i]['values'][index] if index<len(raw_rows[i]['values']) else None} for i in positions]
    distinct={};styles=Counter();decimal_styles=Counter();evidence=[];patterns=Counter();requires_locale=False
    for row in raw_rows:
        value=row['values'][index] if index<len(row['values']) else None
        key=raw_key(value)
        if key in distinct: continue
        distinct[key]=value;patterns[shape(value)]+=1
        if column['type']=='NUMERIC':
            style=locale_evidence(value)
            if style:
                styles[style]+=1
                if sum(x['locale']==style for x in evidence)<4:
                    evidence.append({'locale':style,'source_row':row['index'],'raw':value,'kind':'unambiguous_grouping_pattern'})
            if isinstance(value,str):
                text=re.sub(r'(?:vnd|vnđ|đ|₫)$','',value.strip().lower().replace(' ',''))
                if re.fullmatch(r'[+-]?\d+[.,]\d{3}',text) or (',' in text and '.' in text): requires_locale=True
                if re.fullmatch(r'[+-]?\d+,\d{1,2}',text): decimal_styles['vi']+=1
                if re.fullmatch(r'[+-]?\d+\.\d{1,2}',text): decimal_styles['en']+=1
    configured=settings.get('numeric_locale')
    if configured not in (None,'vi','en'): raise PipelineError('numeric_locale must be null, vi or en')
    minimum=int(settings.get('min_locale_evidence',2))
    if minimum<2: raise PipelineError('min_locale_evidence must be at least 2 distinct values')
    locale=None;status='not_applicable' if column['type']!='NUMERIC' else 'ambiguous'
    if column['type']=='NUMERIC':
        conflicts=len(styles)>1 or (bool(styles) and any(style not in styles for style in decimal_styles))
        if conflicts:
            status='mixed';evidence.append({'kind':'conflicting_styles','counts':dict(styles)})
        elif configured:
            if styles and configured not in styles: status='mixed';evidence.append({'kind':'configured_locale_conflicts_with_data'})
            else: locale=configured;status='configured'
        elif len(styles)==1 and next(iter(styles.values()))>=minimum:
            locale=next(iter(styles));status='validated_from_patterns'
        elif not styles and not requires_locale: status='not_needed'
    return {'sql_column':column['sql_column'],'type':column['type'],'sample_rows':samples,
            'sample_strategy':'evenly spaced including head/middle/tail; format evidence checked over all distinct values',
            'row_count':len(raw_rows),'distinct_values':len(distinct),'format_counts':dict(patterns.most_common(12)),
            'other_format_distinct_values':sum(count for _,count in patterns.most_common()[12:]),
            'locale':locale,'status':status,'locale_evidence':evidence,'locale_evidence_counts':dict(styles),
            'decimal_style_counts':dict(decimal_styles)}
