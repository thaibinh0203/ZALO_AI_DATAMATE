"""Compare independently completed runs on the exact same questions/hardware."""
from pathlib import Path
from datamate_v2.common import read_json,write_json,PipelineError

def compare_runs(run_dirs,output,*,allow_mixed_ingestion=False):
    reports=[read_json(Path(p)/'evaluation/report.json') for p in run_dirs]
    if len(reports)<2: raise PipelineError('At least two evaluated runs are required')
    first=reports[0]['run_manifest']
    if any(r['run_manifest'].get('status')!='completed' for r in reports):
        raise PipelineError('Only completed runs without ingestion failures can be compared')
    for r in reports[1:]:
        m=r['run_manifest']
        for key in ('questions_sha256','source_fingerprint','hardware','warmup_calls','pipeline_config','source_code_fingerprint','runtime_versions'):
            if m[key]!=first[key]: raise PipelineError(f'Uncontrolled comparison: {key} differs')
        for key in ('max_context_tokens','max_new_tokens','seed','dtype','device','backend'):
            if m['model_settings'][key]!=first['model_settings'][key]: raise PipelineError(f'Uncontrolled inference budget: {key} differs')
        if m['model_settings'].get('task_max_new_tokens')!=first['model_settings'].get('task_max_new_tokens'):
            raise PipelineError('Uncontrolled inference budget: task_max_new_tokens differs')
        for key in ('quantization','attn_implementation','logits_to_keep'):
            if m['model_settings'].get(key)!=first['model_settings'].get(key):
                raise PipelineError(f'Uncontrolled inference configuration: {key} differs')
        if not allow_mixed_ingestion and bool(m.get('reused_ingestion'))!=bool(first.get('reused_ingestion')):
            raise PipelineError('Cannot compare fresh ingestion with reused ingestion as the same experiment')
        if r['references_sha256']!=reports[0]['references_sha256']: raise PipelineError('Reference datasets differ')
    rows=[]
    for r in reports:
        manifest=r['run_manifest'];reused=manifest.get('reused_ingestion')
        if allow_mixed_ingestion and reused:
            versions=reused.get('original_ingestion_model_versions',{})
            if not versions or any(v!=manifest.get('model_version') for v in versions.values()):
                raise PipelineError('Mixed comparison requires each model to reuse only its own ingestion')
            if reused.get('original_data_pipeline_config')!=manifest.get('pipeline_config',{}).get('data'):
                raise PipelineError('Reused ingestion data pipeline configuration differs')
        p=r['performance'];rows.append({'model':r['run_manifest']['model_key'],'n':r['n'],'official_score':r['official_score'],
             'correct':r['correct'],'wrong':r['wrong'],'abstain':r['abstain'],'accuracy':r['proxy_accuracy']['value'],
             'ingestion_mode':'reused_own' if reused and allow_mixed_ingestion else 'reused' if reused else 'fresh',
             'ingest_ms':None if reused else sum(manifest.get('ingest_ms',{}).values()),
             'cache_restore_ms':sum(manifest.get('ingest_ms',{}).values()) if reused else None,
             'cold_start_total_ms':None if reused else manifest.get('cold_start_total_ms'),
             'measured_run_ms':manifest.get('cold_start_total_ms'),
             'proxy_PA':r['proxy_PA']['value'],'sql_success':p['sql_execution_success']['value'],
             'semantic_error_rate':p['semantic_error_rate']['value'],'repair_rate':p['repair_rate']['value'],
             'latency_p50_ms':p['latency_ms']['p50'],'latency_p95_ms':p['latency_ms']['p95'],
             'mean_input_tokens':p['tokens']['input']['mean'],'mean_output_tokens':p['tokens']['output']['mean'],
             'mean_total_tokens':p['tokens']['total']['mean'],'mean_model_calls':p['model_calls_per_question']['mean']})
    out=Path(output);out.mkdir(parents=True,exist_ok=True);write_json(out/'comparison.json',rows)
    keys=list(rows[0]);lines=['# Model comparison (local proxy)','', '| '+' | '.join(keys)+' |','| '+' | '.join(['---']*len(keys))+' |']
    lines.extend('| '+' | '.join(str(row[k]) if row[k] is not None else 'unknown' for k in keys)+' |' for row in rows)
    lines+=['','Each model is run independently. No conclusion about relative accuracy is made before measurement.',
            ('Ingestion reused: this comparison measures QA on prepared data.' if first.get('reused_ingestion') else
             'Each model prepares its own ingestion before QA. Check logs/ingestion.json for cache hits when comparing cold-run latency.')]
    if allow_mixed_ingestion:
        lines+=['','Explicit mixed-ingestion comparison: reused runs must use their own model ingestion. Accuracy/QA metrics use the current code/config. Reused cache restore time is not ingestion time; end-to-end cold-run timing is unavailable for reused runs.']
    (out/'comparison.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return rows
