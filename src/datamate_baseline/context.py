"""Business-bound compact schema context; no labels and no semantic inference."""
import hashlib
import json
from pathlib import Path
import re
from datamate_b.prompts import SYSTEM as B1_SYSTEM
from datamate_data.store import PreparedStore

PROMPT_VERSION='baseline-context-v1'
SYSTEM=B1_SYSTEM+'''
Additional execution rules for this baseline:
Use only the supplied clean view names (unqualified); never query schema-qualified
relations or system catalogs. Quote SQL identifiers, not source header names.
The metadata describes candidates, not verified business joins. README and the
rulebook determine metric definitions and source-specific joins. Do not substitute
customer payments or invoice totals for revenue when line revenue is required.
For status, source values already mapped by the pipeline use completed, returned,
cancelled, delivering; unknown source statuses are preserved, not necessarily valid.
Use __quantity_base for base-unit totals when supported, not quantity in mixed units.
Do not sum unlike units or infer missing product-specific conversions.
Use aliases for result columns. number/percent/string/date/boolean must return one
row and one column; list returns one column; table can return multiple columns.
Do all aggregation in SQL, never calculate an answer from a preview or this prompt.
No calendar lookup tool is available in this first baseline. If a required lunar or
holiday lookup is unsupported, abstain rather than invent dates or claim the source
calendar is absent. SQL execution failures are abstain, not data unanswerability.
No repair loop is available. Generate the best single JSON plan from source data.
'''


def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


class ContextLoader:
    def __init__(self,prepared_root,source_root):
        self.prepared_root=Path(prepared_root).resolve();self.source_root=Path(source_root).resolve();self.cache={}
        self.rules=(self.source_root/'rulebook.md').read_text(encoding='utf-8')
        self.rules_sha=sha(self.source_root/'rulebook.md')

    def load(self,business_id):
        if not isinstance(business_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',business_id):raise ValueError('Invalid business_id')
        if business_id in self.cache:return self.cache[business_id]
        prepared=(self.prepared_root/business_id).resolve()
        if prepared.parent!=self.prepared_root:raise ValueError('Business path escapes prepared root')
        path=prepared/'postgres_catalog.json';catalog=json.loads(path.read_text(encoding='utf-8'))
        if catalog['business_id']!=business_id:raise ValueError('Catalog business mismatch')
        readme_path=self.source_root/'data'/business_id/'README.md'
        readme=readme_path.read_text(encoding='utf-8')
        if catalog['rulebook_sha256']!=self.rules_sha:raise ValueError('Rulebook fingerprint mismatch')
        expected=next(x['sha256'] for x in catalog['sources'] if x['name']=='README.md')
        if sha(readme_path)!=expected:raise ValueError('README fingerprint mismatch')
        # No database access for list/describe; metadata was verified during ingestion.
        store=PreparedStore('',prepared)
        schema=[];ddl=[];allowed={}
        for listed in store.list_tables():
            t=store.describe_table(listed['name']);name=t['clean_view']
            cols=[];types={}
            for c in t['columns']:
                audit=c.get('semantic',{});prop=audit.get('proposed') or {}
                cols.append({'sql':c['name'],'source':c['original_name'],'type':c['sql_type'],
                    'role':c.get('canonical_role') or c.get('semantic_role'),
                    'meaning':c.get('description') or prop.get('meaning'),
                    'mapping':audit.get('status'),'has_data':c.get('has_data')})
                types[c['name']]=c['sql_type']
            types.update({'__quantity_base':'NUMERIC','__unit_base':'TEXT'})
            for n,typ in [('__quantity_base','NUMERIC'),('__unit_base','TEXT')]:cols.append({'sql':n,'type':typ,'role':'derived_base_quantity' if typ=='NUMERIC' else 'derived_base_unit'})
            schema.append({'table':name,'role':t['role'],'columns':cols,
                'quality':{'normalization_errors':t.get('errors',{}),'derived_errors':t.get('derived_errors',{}),
                    'base_quantity_unresolved':t.get('base_quantity_unresolved',0),
                    'unmapped_status_preserved':t.get('normalization_notes',{}).get('unmapped_status_preserved',0)}})
            def quote(s):return '"'+s.replace('"','""')+'"'
            ddl.append('CREATE TABLE '+quote(name)+' ('+', '.join(quote(n)+' '+typ for n,typ in types.items())+');')
            allowed[name]=types
        fingerprint=hashlib.sha256(json.dumps([catalog['fingerprint'],self.rules_sha,sha(readme_path),PROMPT_VERSION],ensure_ascii=False).encode()).hexdigest()
        result={'business_id':business_id,'pg_schema':catalog['schema'],'catalog_path':str(path),
            'fingerprint':fingerprint,'schema':schema,'ddl':'\n'.join(ddl),'allowed':allowed,
            'readme':readme,'rulebook':self.rules}
        self.cache[business_id]=result
        return result

    def messages(self,context,request):
        payload={k:context[k] for k in ['business_id','schema','readme','rulebook']}
        payload.update(question=request['question'],as_of_date=request['as_of_date'],capabilities={'calendar_lookup':False,'sql_dialect':'postgres','sql_repairs':0})
        return [{'role':'system','content':SYSTEM},{'role':'user','content':json.dumps(payload,ensure_ascii=False,separators=(',',':'))}]
