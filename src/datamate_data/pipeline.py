"""Business-scoped, streaming preparation. Outputs never include question labels."""
from collections import Counter
from contextlib import ExitStack
from datetime import datetime,timezone
from decimal import Decimal
import hashlib
from importlib.metadata import version,PackageNotFoundError
import json
from pathlib import Path
import re
import sqlite3
import sys
from time import perf_counter
import unicodedata

from . import PIPELINE_VERSION
from .adapters import adapt,role_of
from .normalization import identifier,key,normalize_value,parse_number
from .parsers import dumps,parse_file


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def implementation_hash():
    return hashlib.sha256(''.join(digest(p) for p in sorted(Path(__file__).parent.glob('*.py'))).encode()).hexdigest()


LOADED_IMPLEMENTATION_HASH=implementation_hash()


def name_key(value):return " ".join(unicodedata.normalize('NFC',str(value)).casefold().split())
def unit_key(value):return re.sub(r'\s+','',name_key(value))


def role_values(meta,record):
    out={}
    for column,value in zip(meta['columns'],record['typed']):
        # Staging numerics are canonical Decimal strings, not locale-formatted input.
        if value is not None and column['sql_type']=='NUMERIC' and isinstance(value,str):value=Decimal(value)
        if value is not None:out.setdefault(column['role'],[]).append(value)
    return {role:values[0] for role,values in out.items() if len(values)==1}


def source_files(business):
    files=[];ignored=[]
    for p in sorted(business.iterdir()):
        if p.is_symlink():raise ValueError('Source symlinks are not allowed')
        if p.is_file() and p.suffix.lower() in {'.csv','.xlsx','.sqlite'}:files.append(p)
        elif p.name!='README.md':ignored.append(p.name)
    if not files:raise ValueError('No supported data sources')
    return files,ignored


class Conversions:
    def __init__(self):self.factors={};self.bases={};self.conflicts=[];self.pending=[]
    def add(self,product,unit,factor,base,source,store=None):
        k=(store,name_key(product),unit_key(unit));item=(str(factor),str(base))
        if k in self.factors and self.factors[k]!=item:
            self.conflicts.append({'product':str(product),'unit':str(unit),'source':source})
            self.factors[k]=None
        else:self.factors[k]=item
        self.bases.setdefault((store,name_key(product)),set()).add(unit_key(base))
    def from_readme(self,text):
        for line in text.splitlines():
            match=re.match(r'\s*-\s+(.+?):\s*1\s+(.+?)\s*=\s*([0-9.,]+)\s+(.+?)\s*$',line)
            if match:
                product,unit,factor,base=match.groups()
                self.add(product,unit,parse_number(factor),base,'README.md')
    def resolve(self,products,unit,store=None):
        matches=set()
        for product in products:
            p=name_key(product);u=unit_key(unit)
            for scope in ({store,None} if store is not None else {None}):
                if (scope,p,u) in self.factors:
                    pair=self.factors[(scope,p,u)]
                    if pair is None:return None
                    matches.add(pair)
                elif u in self.bases.get((scope,p),set()):matches.add(('1',str(unit)))
        return next(iter(matches)) if len(matches)==1 else None

    def default_base_is_consistent(self,products,unit,store=None):
        bases=set()
        for product in products:
            for scope in ({store,None} if store is not None else {None}):
                if (scope,name_key(product),unit_key(unit)) in self.factors:return False
                bases.update(self.bases.get((scope,name_key(product)),set()))
        return not bases or bases=={unit_key(unit)}


def prepare_business(business_dir,output_dir,*,rulebook,calendar,csv_encoding='utf-8-sig',semantic_plan=None):
    started=perf_counter();business=Path(business_dir).resolve();output=Path(output_dir).resolve()
    if output.exists():raise FileExistsError('Output already exists; use a new run directory')
    if output==business or business in output.parents:raise ValueError('Output must be outside source business')
    readme_path=business/'README.md'
    if readme_path.is_symlink():raise ValueError('README symlink is not allowed')
    readme=readme_path.read_text(encoding='utf-8');files,ignored=source_files(business)
    rules_text=Path(rulebook).read_text(encoding='utf-8')
    default_base_unit_rule='donvikhongcodongquydoinaoladonvicobanheso1' in key(rules_text)
    inputs=[readme_path,*files,Path(rulebook).resolve(),Path(calendar).resolve()]
    fingerprints=[{'name':p.name,'sha256':digest(p),'bytes':p.stat().st_size} for p in inputs]
    code_hash=implementation_hash()
    if code_hash!=LOADED_IMPLEMENTATION_HASH:raise ValueError('Pipeline code changed after import; restart process')
    config={'version':PIPELINE_VERSION,'implementation_sha256':code_hash,'csv_encoding':csv_encoding,
            'numeric_policy':'vnd_grouped_currency_conservative_quantity_v1'}
    config['default_base_unit_rule_present']=default_base_unit_rule
    if semantic_plan is not None:
        from .semantic import source_snapshot,object_hash
        if semantic_plan['business_id']!=business.name or semantic_plan['sources']!=source_snapshot(business,rulebook,calendar):
            raise ValueError('Semantic plan does not match business/source snapshot')
        config['semantic_plan_sha256']=object_hash(semantic_plan)
        config['semantic_mode']='local_slm'
    else:config['semantic_mode']='deterministic'
    config['versions']={'python':'.'.join(map(str,sys.version_info[:3])),'sqlite':sqlite3.sqlite_version}
    for package in ('openpyxl','psycopg'):
        try:config['versions'][package]=version(package)
        except PackageNotFoundError:config['versions'][package]=None
    fingerprint=hashlib.sha256(dumps([fingerprints,config]).encode()).hexdigest()
    output.mkdir(parents=True);(output/'tables').mkdir()
    catalog={'version':PIPELINE_VERSION,'dialect':'postgres','business_id':business.name,
             'fingerprint':fingerprint,'config':config,'sources':fingerprints,'ignored_files':ignored,
             'schema':'dm_'+identifier(business.name)[:24]+'_'+fingerprint[:12],
             'readme_ref':'README.md','rulebook_sha256':digest(rulebook),'calendar_sha256':digest(calendar),
             'declared_coverage_end':None,'tables':{},'conversion_conflicts':[],
             'created_at_utc':datetime.now(timezone.utc).isoformat(),
             'limitations':['Typed source tables, not a complete business metric engine.',
                 'Mapping roles are candidates from headers/README, not universal semantic equivalence.',
                 'Clean views exclude duplicate/subtotal/header rows, not per-column parse failures. Inspect quality before aggregating.',
                 'Snapshots remain snapshots; no historical stock/debt reconstruction is inferred.']}
    coverage=re.search(r'đến hết\s+([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})',readme,re.I)
    if coverage:catalog['declared_coverage_end']=datetime.strptime(coverage[1],'%d/%m/%Y').date().isoformat()
    conversions=Conversions();conversions.from_readme(readme)
    resolver=None
    if semantic_plan is not None:
        from .semantic import header_resolver
        resolver=header_resolver(semantic_plan)
        catalog['semantic_summary']=semantic_plan['summary']
        catalog['semantic_model_error_tables']=semantic_plan['model_error_tables']
        catalog['semantic_model']=semantic_plan['model']
        (output/'semantic_plan.json').write_text(dumps(semantic_plan)+'\n',encoding='utf-8')
    total_rows=0
    with ExitStack() as stack:
        handles={}
        errors=stack.enter_context((output/'normalization_errors.jsonl').open('w',encoding='utf-8'))
        for source in files:
            for table in parse_file(source,encoding=csv_encoding):
                adapted=adapt(table,readme,header_resolver=resolver)
                if adapted is None:continue
                layout,rows=adapted;name=layout['name']
                semantics=None
                if semantic_plan is not None:
                    semantics=semantic_plan['tables'].get(name)
                    if semantics is None or semantics['baseline_columns']!=layout['columns']:
                        raise ValueError('Semantic plan layout mismatch')
                    layout['columns']=semantics['columns']
                    layout['role']=role_of(layout['columns'],layout['logical_name'])
                if name not in catalog['tables']:
                    catalog['tables'][name]={'name':name,'logical_name':layout['logical_name'],'role':layout['role'],
                        'columns':layout['columns'],'source_parts':[],'rows':0,'row_kinds':{},
                        'errors':{},'missing':{},'normalization_notes':{},'duplicates':0,
                        'derived_columns':{'__quantity_base':'NUMERIC','__unit_base':'TEXT'},
                        'data_path':f'tables/{name}.jsonl'}
                    handles[name]=stack.enter_context((output/f'tables/{name}.jsonl').open('w',encoding='utf-8'))
                    if semantics is not None:
                        catalog['tables'][name]['grain_candidate']=semantics['grain_candidate']
                        catalog['tables'][name]['semantic_relationship_proposals']=semantics['relationship_proposals']
                meta=catalog['tables'][name]
                if meta['columns']!=layout['columns']:
                    raise ValueError(f'Conflicting column semantics in merged sources: {name}')
                meta['source_parts'].append(layout)
                previous={}
                role_counts=Counter(c['role'] for c in layout['columns'])
                for row in rows:
                    if row['kind']=='blank':previous={};continue
                    values=row['values'];typed=[];quality={'errors':{},'notes':{},'source_flags':row['flags']}
                    for i,(value,col) in enumerate(zip(values,layout['columns'])):
                        result,status,unit=normalize_value(value,col,year=layout['year_evidence'],cell=row['cells'].get(str(i)))
                        if status.startswith('error:'):
                            quality['errors'][col['name']]=status[6:]
                            meta['errors'][col['name']]=meta['errors'].get(col['name'],0)+1
                            errors.write(dumps({'table':name,'file':source.name,'sheet_or_table':table.name,
                                                'row':row['source_row'],'column':col['name'],'error':status[6:]})+'\n')
                        elif status=='missing':meta['missing'][col['name']]=meta['missing'].get(col['name'],0)+1
                        elif status!='ok':
                            quality['notes'][col['name']]={'status':status,'unit':unit}
                            meta['normalization_notes'][status]=meta['normalization_notes'].get(status,0)+1
                        typed.append(result)
                    # Fill only confirmed sale-line order fields within a contiguous order group.
                    if row['kind']!='data':previous={}
                    if layout['role']=='sale_lines' and row['kind']=='data':
                        order_index=next(i for i,c in enumerate(layout['columns']) if c['role']=='order_id')
                        explicit=typed[order_index]
                        if explicit is not None and explicit!=previous.get('order_id'):previous={}
                        stores=[typed[i] for i,c in enumerate(layout['columns']) if c['role']=='store_id']
                        if stores and stores[0] is not None and stores[0]!=previous.get('store_id'):previous={}
                        for i,c in enumerate(layout['columns']):
                            role=c['role']
                            if role_counts[role]!=1 or role not in {'order_id','customer_id','customer_name','date','status','store_id'}:continue
                            if typed[i] is None and values[i] in (None,'') and role in previous:
                                typed[i]=previous[role];quality['notes'][c['name']]={'status':'filled_from_contiguous_order'}
                            elif typed[i] is not None:previous[role]=typed[i]
                    annotation={i:info for i,info in row['cells'].items() if info.get('formula') or info.get('number_format') not in {None,'General'}}
                    row_id=hashlib.sha256(dumps([source.name,digest_cache(fingerprints,source.name),table.name,row['source_row']]).encode()).hexdigest()
                    record={'row_id':row_id,'raw':values,'typed':typed,'kind':row['kind'],'quality':quality,
                            'source':{'file':source.name,'table_or_sheet':table.name,'row':row['source_row'],
                                      'cell_annotations':annotation,'sqlite_scan_ordinal':table.format=='sqlite'}}
                    handles[name].write(dumps(record)+'\n');meta['rows']+=1;total_rows+=1
                    meta['row_kinds'][row['kind']]=meta['row_kinds'].get(row['kind'],0)+1
                    roles=role_values(layout,record)
                    if layout['role']=='unit_conversions' and row['kind']=='data':
                        factor=roles.get('conversion_factor');base=roles.get('base_unit');unit=roles.get('unit')
                        if factor is not None and factor>0 and unit:
                            for product in (roles.get('product_id'),roles.get('product_name')):
                                if product:
                                    if base:conversions.add(product,unit,factor,base,f'{source.name}/{table.name}',roles.get('store_id'))
                                    else:conversions.pending.append((product,unit,factor,f'{source.name}/{table.name}',roles.get('store_id')))
                    if roles.get('base_unit'):
                        for product in (roles.get('product_id'),roles.get('product_name')):
                            if product:conversions.bases.setdefault((roles.get('store_id'),name_key(product)),set()).add(unit_key(roles['base_unit']))
    for product,unit,factor,source,store in conversions.pending:
        bases=conversions.bases.get((store,name_key(product)),set())
        if len(bases)==1:conversions.add(product,unit,factor,next(iter(bases)),source,store)
    catalog['conversion_conflicts']=conversions.conflicts
    # Second, bounded pass: conversions from all files are available; source is not re-read.
    for name,meta in catalog['tables'].items():
        old=output/meta['data_path'];new=old.with_suffix('.normalized.jsonl');seen={}
        for col in meta['columns']:col['safe_for_aggregation']=col['sql_type']!='NUMERIC' or meta['errors'].get(col['name'],0)==0
        meta['base_quantity_unresolved']=0;meta['derived_errors']={}
        with old.open(encoding='utf-8') as stream,new.open('w',encoding='utf-8') as dest:
            for line in stream:
                record=json.loads(line);roles=role_values(meta,record)
                record['duplicate_of']=None;record['quantity_base']=None;record['unit_base']=None
                if meta['role'] in {'sale_lines','orders_or_order_extension'} and roles.get('order_id') and record['kind']=='data':
                    signature=hashlib.sha256(dumps([roles.get('store_id'),roles['order_id'],record['raw']]).encode()).hexdigest()
                    if signature in seen:record['duplicate_of']=seen[signature];meta['duplicates']+=1
                    else:seen[signature]=record['row_id']
                if meta['role'] in {'sale_lines','sale_events'} and record['kind']=='data':
                    quantity=roles.get('quantity');existing=roles.get('base_quantity');unit=roles.get('unit')
                    extracted=[n.get('unit') for n in record['quality']['notes'].values() if n.get('unit')]
                    if not unit and len(set(extracted))==1:unit=extracted[0]
                    if unit and extracted and any(unit_key(u)!=unit_key(unit) for u in extracted):
                        record['quality']['errors']['__quantity_base']='quantity_unit_conflict'
                    else:
                        products=[p for p in [roles.get('product_id'),roles.get('product_name')] if p]
                        conversion=conversions.resolve(products,unit,roles.get('store_id')) if unit else None
                        if conversion is None and unit and default_base_unit_rule and conversions.default_base_is_consistent(products,unit,roles.get('store_id')):
                            conversion=('1',unit)
                            record['quality']['notes']['unit_default_rule']={'status':'factor_1_from_current_rulebook'}
                        derived=parse_number(quantity)*Decimal(conversion[0]) if quantity is not None and conversion else None
                        if existing is not None:
                            record['quantity_base']=str(existing)
                            if unit and quantity is not None and parse_number(quantity)!=0 and parse_number(existing)==parse_number(quantity):
                                record['unit_base']=unit
                            if derived is not None and parse_number(existing)!=derived:
                                record['quality']['errors']['__quantity_base']='supplied_and_computed_base_quantity_disagree'
                        elif derived is not None:record['quantity_base']=str(derived)
                        if conversion:record['unit_base']=conversion[1]
                    if record['quantity_base'] is None:meta['base_quantity_unresolved']+=1
                    if quantity is not None and parse_number(quantity)<0:
                        record['quality']['notes']['negative_quantity_preserved']=True
                    for col,error in record['quality']['errors'].items():
                        if col.startswith('__'):meta['derived_errors'][col]=meta['derived_errors'].get(col,0)+1
                dest.write(dumps(record)+'\n')
        new.replace(old)
        meta['data_sha256']=digest(old)
    catalog['total_source_records']=total_rows
    catalog['capabilities']={
        'line_cogs_sources':[m['name'] for m in catalog['tables'].values() if m['role'] in {'sale_lines','sale_events'} and any(c['role']=='unit_cogs' for c in m['columns'])],
        'expense_candidates':[m['name'] for m in catalog['tables'].values() if m['role']=='expenses_candidate'],
        'payment_sources':[m['name'] for m in catalog['tables'].values() if m['role']=='payments'],
        'note':'Source presence only, not proof a specific question is answerable.'}
    for p,expected in zip(inputs,fingerprints):
        if digest(p)!=expected['sha256']:raise ValueError('Source changed during preparation')
    if implementation_hash()!=code_hash:raise ValueError('Pipeline code changed during preparation')
    catalog['preparation_ms']=round((perf_counter()-started)*1000,3)
    (output/'catalog.json').write_text(dumps(catalog)+'\n',encoding='utf-8')
    return catalog


def digest_cache(records,name):return next(r['sha256'] for r in records if r['name']==name)
