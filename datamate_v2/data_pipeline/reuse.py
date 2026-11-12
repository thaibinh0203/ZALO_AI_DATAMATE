"""Explicit QA-only reuse of a prior run; never falls back to model ingestion."""
from pathlib import Path
import re
from psycopg import sql
from datamate_v2.common import PipelineError,read_json,read_jsonl,file_hash,source_fingerprint,schema_name
from datamate_v2.contracts import validate_question
from datamate_v2.database.connection import connect
from datamate_v2.database.loader import load_business,SYSTEM_COLUMNS
from datamate_v2.progress import emit


def inspect_reusable_ingestion(run,split,questions,cache):
    run=Path(run).resolve();split=Path(split).resolve();cache=Path(cache).resolve()
    for question in questions: validate_question(question)
    manifest=read_json(run/'run_manifest.json')
    original_questions=read_jsonl(run/'questions.jsonl')
    for question in original_questions: validate_question(question)
    if not original_questions or file_hash(run/'questions.jsonl')!=manifest.get('questions_sha256'):
        raise PipelineError('Prior run question checksum is invalid')
    original_businesses={q['business_id'] for q in original_questions}
    if source_fingerprint(split,original_businesses)!=manifest.get('source_fingerprint'):
        raise PipelineError('Source data/README/Rulebook/calendar changed since the prior ingestion; refusing QA-only reuse')
    ingestion=read_json(run/'logs/ingestion.json')
    prepared={};fingerprints=manifest.get('data_fingerprints',{})
    for business in sorted({q['business_id'] for q in questions}):
        if business not in original_businesses or business in ingestion.get('failed',{}):
            raise PipelineError(f'No successful prior ingestion for {business}')
        fingerprint=fingerprints.get(business,'')
        if not re.fullmatch(r'[a-f0-9]{64}',fingerprint) or business not in manifest.get('ingest_ms',{}):
            raise PipelineError(f'Missing successful data fingerprint for {business}')
        directory=cache/'businesses'/business/fingerprint
        metadata=read_json(directory/'metadata.json')
        rows=directory/'clean_rows.json'
        if not rows.is_file(): raise PipelineError(f'Missing cached clean data for {business}: {rows}')
        if metadata.get('business_id')!=business or metadata.get('fingerprint')!=fingerprint or metadata.get('schema')!=schema_name(business,fingerprint):
            raise PipelineError(f'Cached metadata identity mismatch for {business}')
        if not metadata.get('tables'): raise PipelineError(f'Cached metadata has no tables for {business}')
        sources={'readme':split/'data'/business/'README.md','rulebook':split/'rulebook.md','calendar_csv':split/'calendar.csv'}
        if any(metadata.get(key)!=path.read_text(encoding='utf-8') for key,path in sources.items()):
            raise PipelineError(f'Cached business sources do not match current documents: {business}')
        prepared[business]=(metadata,rows)
    provenance={'from_run':str(run),'source_run_status':manifest.get('status'),
                'original_data_pipeline_config':manifest.get('reused_ingestion',{}).get('original_data_pipeline_config') or manifest.get('pipeline_config',{}).get('data'),
                'original_source_code_fingerprint':manifest.get('reused_ingestion',{}).get('original_source_code_fingerprint') or manifest.get('source_code_fingerprint'),
                'original_ingestion_model_versions':{b:md.get('model_version') for b,(md,_) in prepared.items()},
                'data_fingerprints':{b:md['fingerprint'] for b,(md,_) in prepared.items()},
                'source_fingerprint':manifest['source_fingerprint']}
    return prepared,provenance


def database_ready(metadata,settings):
    with connect(settings,admin=True) as db:
        names={row[0] for row in db.execute('SELECT tablename FROM pg_tables WHERE schemaname=%s',(metadata['schema'],))}
        if not {table['sql_table'] for table in metadata['tables']}.issubset(names): return False
        for table in metadata['tables']:
            columns={row[0] for row in db.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s',
                                               (metadata['schema'],table['sql_table']))}
            if columns!={c['sql_column'] for c in table['columns']}|set(SYSTEM_COLUMNS): return False
            count=db.execute(sql.SQL('SELECT COUNT(*) FROM {}.{}').format(sql.Identifier(metadata['schema']),sql.Identifier(table['sql_table']))).fetchone()[0]
            if count!=table['row_count']: return False
    return True


def load_reused_business(metadata,rows_path,settings):
    if not database_ready(metadata,settings):
        emit('database_load',f'{metadata["business_id"]}: restoring cached clean rows into PostgreSQL; no model ingestion')
        rows=read_json(rows_path)
        if set(rows)!={t['sql_table'] for t in metadata['tables']} or any(len(rows[t['sql_table']])!=t['row_count'] for t in metadata['tables']):
            raise PipelineError('Cached clean rows do not match the prepared table manifest')
        load_business(metadata,rows,settings)
    else:
        emit('data_cache',f'{metadata["business_id"]}: reusing existing PostgreSQL tables and cached metadata')
    # Enrich old immutable caches without changing their metadata/fingerprint.
    from .value_catalog import build_value_catalog,VERSION
    if not Path(rows_path).is_file():return metadata
    path=Path(rows_path).with_name('value_catalog.json')
    catalog=read_json(path) if path.exists() else None
    if not catalog or catalog.get('version')!=VERSION or catalog.get('fingerprint')!=metadata['fingerprint']:
        from datamate_v2.common import write_json
        catalog=build_value_catalog(metadata,read_json(rows_path))
        write_json(path,catalog)
    metadata['value_catalog']=catalog
    return metadata
