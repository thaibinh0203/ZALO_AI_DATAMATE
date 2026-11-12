"""Cached business ingestion, with optional SLM semantic descriptions."""
from pathlib import Path
from collections import Counter
from time import perf_counter
import re
from datamate_v2.common import ROOT,VERSION,PipelineError,digest,file_hash,write_json,read_json,identifier,schema_name
from .parsers.csv_parser import parse_csv
from .parsers.excel_parser import parse_excel
from .parsers.sqlite_parser import parse_sqlite
from .adapters.table_extractor import extract_tables
from .normalization.column_normalizer import normalize_columns
from .normalization.table_normalizer import normalize_table
from .normalization.validators import cross_column_checks
from .normalization.group_rules import apply_group_rules
from .relationships import verify_relationships
from .anomaly_detection.summary_detector import detect_summary
from .schema_generation.description_groups import build_business_descriptions
from datamate_v2.database.loader import load_business,schema_exists
from datamate_v2.progress import emit

SUPPORTED={".csv":parse_csv,".xlsx":parse_excel,".xls":parse_excel,".sqlite":parse_sqlite,".sqlite3":parse_sqlite,".db":parse_sqlite}

class DataPipeline:
    def __init__(self,client,database,settings,cache_dir):
        self.client=client;self.database=database;self.settings=settings;self.cache=Path(cache_dir)
        self.aliases=read_json(ROOT/'metadata/aliases/columns.json')
        self.last_ingest_ms=0

    def prepare(self,business_dir,split_root,*,load_database=True):
        start=perf_counter();business_dir=Path(business_dir).resolve();split_root=Path(split_root).resolve()
        if business_dir.parent!=split_root/'data': raise PipelineError("Business directory must be inside split_root/data")
        readme=(business_dir/'README.md').read_text(encoding='utf-8')
        rulebook=(split_root/'rulebook.md').read_text(encoding='utf-8')
        calendar=(split_root/'calendar.csv').read_text(encoding='utf-8')
        sources=sorted(p for p in business_dir.rglob('*') if p.is_file() and p.suffix.lower() in SUPPORTED)
        if not sources: raise PipelineError("No supported business data files")
        for p in sources:
            if not p.resolve().is_relative_to(business_dir): raise PipelineError("Source symlink escapes business")
        manifest={str(p.relative_to(business_dir)):file_hash(p) for p in sources}
        code_hash={str(p.relative_to(ROOT)):file_hash(p) for directory in ('data_pipeline','inference','prompts/data_pipeline') for p in (ROOT/directory).rglob('*') if p.is_file() and p.suffix in ('.py','.txt')}
        fingerprint=digest({"sources":manifest,"readme":readme,"rulebook":rulebook,"calendar":calendar,
                            "model":self.client.version,"rules":self.aliases,"settings":self.settings,"code":code_hash,"version":VERSION})
        output=self.cache/business_dir.name/fingerprint
        metadata_path=output/'metadata.json';rows_path=output/'clean_rows.json'
        if metadata_path.exists() and rows_path.exists():
            emit('data_cache',f'{business_dir.name}: reusing prepared business cache',business_id=business_dir.name)
            metadata=read_json(metadata_path)
            if load_database and not schema_exists(metadata['schema'],self.database): load_business(metadata,read_json(rows_path),self.database)
            self.last_ingest_ms=(perf_counter()-start)*1000
            return metadata
        context={"readme":readme,"rulebook":rulebook,"source_fingerprint":fingerprint}
        metadata={"business_id":business_dir.name,"fingerprint":fingerprint,"schema":schema_name(business_dir.name,fingerprint),
                  "model_version":self.client.version,"readme":readme,"rulebook":rulebook,"calendar_csv":calendar,
                  "source_manifest":manifest,"tables":[],"relationships":[],"warnings":[],"version":VERSION}
        clean_tables={};registry=[];logs=[]
        for file_index,path in enumerate(sources,1):
            emit('parse',f'{business_dir.name}: file {file_index}/{len(sources)}: {path.name}',business_id=business_dir.name,table=None)
            parse=SUPPORTED[path.suffix.lower()]
            parsed=parse(path,encoding=self.settings.get('csv_encoding','utf-8-sig')) if path.suffix.lower()=='.csv' else parse(path)
            for source in parsed:
                for block in extract_tables(source,self.aliases):
                    table_seed=str(path.relative_to(business_dir))+':'+block['name']
                    emit('normalize',f'{business_dir.name}: table {block["name"]}; {len(block["data_rows"])} data rows',table=block['name'])
                    table_name=identifier(path.stem+'_'+block['name'],prefix='t')+'_'+digest(table_seed)[:6]
                    if block['header'] is None:
                        metadata['warnings'].append({"table":table_name,"reason":"unresolved_header","evidence":block['header_detection']})
                        write_json(output/'raw'/f'{table_name}.json',{k:v for k,v in block.items() if k!='rows'});continue
                    headers=list(block['header']['values'])
                    width=max([len(headers)]+[len(r['values']) for r in block['data_rows']])
                    headers += [None]*(width-len(headers))
                    columns=normalize_columns(headers,block['data_rows'],self.aliases,self.client,{"table":table_name,**context})
                    for column in columns:
                        if column.get('mapping_warnings'):
                            metadata['warnings'].append({'table':table_name,'column':column['sql_column'],
                                'reason':'column_mapping_proposal_rejected','issues':column['mapping_warnings']})
                    year_match=re.search(r'(?<!\d)(20\d{2})(?!\d)',source['name'])
                    rows,norm_logs,table_slm_logs,column_profiles,normalization_stats=normalize_table(
                        block['data_rows'],columns,self.client,{'table':table_name,**context},self.settings,
                        year=int(year_match[1]) if year_match else None)
                    logs.extend({'table':table_name,**entry} for entry in table_slm_logs)
                    group_logs=apply_group_rules(rows,columns,rulebook)
                    summaries=detect_summary(rows,columns)
                    equations={'quantity * unit_price = line_total','line_total = quantity * unit_price',
                               'số lượng × đơn giá = thành tiền','thành tiền = số lượng × đơn giá'}
                    formula_quote=next((line.strip() for line in readme.splitlines() if line.strip().lstrip('- ').strip('* ').lower() in equations),None)
                    constraints=[];constraint_violations=[]
                    meaning_names={c.get('meaning'):c['sql_column'] for c in columns if c['semantic_status']=='verified'}
                    if formula_quote and all(sum(c.get('meaning')==k and c['semantic_status']=='verified' for c in columns)==1 for k in ('quantity','unit_price','line_total')):
                        constraints=[{'kind':'quantity_price_total','status':'verified','source_quote':formula_quote,
                                      'columns':{k:meaning_names[k] for k in ('quantity','unit_price','line_total')}}]
                        for row in rows:
                            if row['_is_summary_row']: continue
                            errors=cross_column_checks(row,columns,formula_verified=True)
                            if errors:
                                row['_quality'].append({'column':meaning_names['line_total'],'status':'constraint_violation','reason':errors[0]})
                                constraint_violations.append({'row':row['_source_row'],'errors':errors})
                    stats=[]
                    for col in columns:
                        name=col['sql_column'];values=[r[name] for r in rows if r[name] is not None]
                        statuses=Counter(f['status'] for r in rows for f in r['_quality'] if f['column']==name)
                        stats.append({"sql_column":name,"null_count":len(rows)-len(values),"unresolved_count":statuses['unresolved'],
                                      "missing_count":statuses['missing'],"distinct_count":len(set(values)),
                                      "min":min(values) if values else None,"max":max(values) if values else None})
                    quality={"unresolved_values":sum(x['unresolved_count'] for x in stats),
                             "mapping_proposal_warnings":sum(len(c.get('mapping_warnings',[])) for c in columns),
                             "normalization_batch_errors":normalization_stats['slm_batch_errors'],
                             "constraints":constraints,"constraint_violations":len(constraint_violations),
                             "summary_rows":sum(r['_is_summary_row'] for r in rows),
                             "uncertain_summary_rows":sum(r['_summary_status']=='uncertain' for r in rows),
                             "uncertain_mappings":sum(c['semantic_status']!='verified' for c in columns)}
                    observed_domains={}
                    for col in columns:
                        if col.get('meaning') in ('status','unit'):
                            counts=Counter(row[col['sql_column']] for row in rows if row[col['sql_column']] is not None)
                            observed_domains[col['sql_column']]={'values':[{'value':value,'count':count} for value,count in sorted(counts.items(),key=lambda pair:str(pair[0]))[:40]],
                                                                 'distinct_count':len(counts),'truncated':len(counts)>40}
                    table={"sql_table":table_name,"observed_domains":observed_domains,"source_file":str(path.relative_to(business_dir)),"source_table":source['name'],
                           "system_columns":{"_source_row":"BIGINT","_is_summary_row":"BOOLEAN","_summary_status":"TEXT","_raw":"JSONB (audit only; not allowed in answer SQL)","_quality":"JSONB (audit only; use metadata/NULL checks for QA)"},
                           "columns":columns,"column_profiles":[{k:p[k] for k in ('sql_column','status','locale','locale_evidence','locale_evidence_counts')} for p in column_profiles if p['type']!='TEXT'],"normalization_stats":{k:v for k,v in normalization_stats.items() if k!='duration_ms'},"row_count":len(rows),"sample_rows":rows[:5],"last_rows":rows[-5:],
                           "stats":stats,"quality":quality,"header_detection":block['header_detection'],
                           "provenance":{"column_offset":block.get('column_offset',0),"header_row":block['header']['index']},
                           "relationships":[],"warnings":["Relationships are unverified unless listed explicitly; do not infer joins from column names."]}
                    # Semantic descriptions enrich metadata; verified facts remain usable without them.
                    metadata['tables'].append(table)
                    clean_tables[table_name]=rows
                    registry.append({"table":table_name,"columns":columns})
                    write_json(output/'raw'/f'{table_name}.json',{k:v for k,v in block.items() if k!='rows'})
                    write_json(output/'normalization'/f'{table_name}.json',norm_logs)
                    write_json(output/'column_profiles'/f'{table_name}.json',column_profiles)
                    write_json(output/'normalization_stats'/f'{table_name}.json',normalization_stats)
                    write_json(output/'summaries'/f'{table_name}.json',summaries)
                    write_json(output/'group_rules'/f'{table_name}.json',group_logs)
                    write_json(output/'constraints'/f'{table_name}.json',constraint_violations)
        if not metadata['tables']: raise PipelineError('No verified table headers; inspect raw/header warnings')
        relationships=verify_relationships(metadata['tables'],clean_tables,readme,rulebook)
        metadata['relationships']=[r for r in relationships if r['status']=='verified']
        metadata['candidate_relationships']=[r for r in relationships if r['status']!='verified']
        event_stats=[s for t in metadata['tables'] for s in t['stats'] if any(c['sql_column']==s['sql_column'] and c.get('meaning') in {'order_date','date','payment_date','created_at'} for c in t['columns']) and s['min'] is not None]
        metadata['data_coverage']={'start_date':min(str(s['min'])[:10] for s in event_stats) if event_stats else None,
                                   'end_date_inclusive':max(str(s['max'])[:10] for s in event_stats) if event_stats else None,
                                   'evidence':'all observed event-date columns; excludes expiry/due dates'}
        build_business_descriptions(metadata,self.client,context)
        from .value_catalog import build_value_catalog
        metadata['value_catalog']=build_value_catalog(metadata,clean_tables)
        if load_database:
            emit('database_load',f'{business_dir.name}: loading {len(clean_tables)} clean tables into PostgreSQL')
            load_business(metadata,clean_tables,self.database)
        write_json(rows_path,clean_tables);write_json(output/'mapping_registry.json',registry)
        write_json(output/'slm_normalization.json',logs);write_json(metadata_path,metadata)
        self.last_ingest_ms=(perf_counter()-start)*1000
        write_json(output/'ingest.json',{"duration_ms":self.last_ingest_ms,"model_calls":self.client.calls})
        return metadata
