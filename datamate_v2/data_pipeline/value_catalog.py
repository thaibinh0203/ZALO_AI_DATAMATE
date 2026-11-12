"""Lossless, deduplicated low-cardinality text dictionaries; no model calls."""
import re
from datamate_v2.common import digest,dumps

VERSION=1
NOISY=re.compile(r'(^|_)(id|code|uuid|url|email|phone|note|notes|description|address|image|comment|ma)(_|$)')


def eligible_column(column):
    name=column['sql_column'];meaning=column.get('meaning') or name
    return column['type']=='TEXT' and not NOISY.search(name) and not NOISY.search(meaning)


def build_value_catalog(metadata,clean_tables,threshold=50):
    groups={};omitted=[]
    for table in metadata['tables']:
        for column in table['columns']:
            if not eligible_column(column):continue
            name=column['sql_column']
            values=sorted({str(r[name]) for r in clean_tables[table['sql_table']]
                if r.get(name) is not None and not r.get('_is_summary_row',False)})
            if not values or len(values)>=threshold:
                omitted.append({'table':table['sql_table'],'column':name,'nunique':len(values),'reason':'empty or above threshold'})
                continue
            # Do not spend context on numeric values merely stored as text.
            if all(re.fullmatch(r'[-+]?\d+(?:[.,]\d+)?',v.strip()) for v in values):continue
            semantics=column.get('meaning') or column.get('original_column') or name
            key=digest({'column':name,'semantics':semantics,'values':values})
            group=groups.setdefault(key,{'id':'values_'+key[:12],'column':name,'meaning':semantics,
                'values':values,'nunique':len(values),'complete':True,'tables':[]})
            group['tables'].append(table['sql_table'])
    return {'version':VERSION,'business_id':metadata['business_id'],'fingerprint':metadata['fingerprint'],
        'threshold_exclusive':threshold,'groups':list(groups.values()),'omitted':omitted,
        'provenance':'Distinct non-NULL text values from cleaned detail rows; no translation or inferred aliases.'}


def budget_values(catalog,question,count_tokens,budget=2500):
    """Count the full JSON (including table references); never clip a dictionary."""
    from datamate_v2.qa_pipeline.entities import entity_key
    q=entity_key(question)
    priority={'status':0,'category':1,'product_category':1,'unit':2,'payment_method':3,'customer_group':4}
    def rank(group):
        mentioned=any(entity_key(v) and re.search(r'(?<!\w)'+re.escape(entity_key(v))+r'(?!\w)',q) for v in group['values'])
        return (0 if mentioned else 1,priority.get(group['column'],10),len(dumps(group)),group['id'])
    result={'instruction':'Complete actual value dictionaries apply only to the listed tables/column. Shared dictionaries appear once. Omitted dictionaries are not empty domains. Do not translate labels.',
        'groups':[],'omitted_group_count':len(catalog.get('groups',[]))}
    for group in sorted(catalog.get('groups',[]),key=rank):
        candidate={**result,'groups':result['groups']+[group],'omitted_group_count':result['omitted_group_count']-1}
        if count_tokens(dumps(candidate))<=budget:result=candidate
    return result,{'tokens':count_tokens(dumps(result)),'budget':budget,
        'included_groups':len(result['groups']),'omitted_groups':result['omitted_group_count']}
