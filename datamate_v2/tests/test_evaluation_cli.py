from pathlib import Path
import pytest
from datamate_v2.common import write_json,dumps,file_hash,read_json
from datamate_v2.evaluation.evaluator import evaluate
from datamate_v2.evaluation.compare_variants import compare_runs
from datamate_v2.cli import main

def make_run(path,key):
    path.mkdir();(path/'logs').mkdir()
    question={'id':'q_fixture','business_id':'synthetic','question':'Synthetic only','as_of_date':'2026-03-04'}
    for name,value in [('questions.jsonl',question),('submission.jsonl',{'id':'q_fixture','type':'number','answer':2})]:
        (path/name).write_text(dumps(value)+'\n')
    log={'id':'q_fixture','answer':{'type':'number','answer':2},'elapsed_ms':10,'model_calls':[{'task':'planning_sql','cache_hit':False,'input_tokens':5,'output_tokens':4}],
         'stages':[{'stage':'postgres_execution','duration_ms':1.25,'status':'success'}],'queries':[{'purpose':'answer','status':'success'}]}
    (path/'logs/questions.jsonl').write_text(dumps(log)+'\n')
    settings={'max_context_tokens':128,'max_new_tokens':20,'seed':42,'dtype':'float32','device':'cpu','backend':'transformers'}
    write_json(path/'run_manifest.json',{'model_key':key,'model_settings':settings,'questions_sha256':file_hash(path/'questions.jsonl'),
      'source_fingerprint':'fixture','source_code_fingerprint':'fixture','runtime_versions':{'transformers':'fixture'},'hardware':{'cpu':'fixture'},'warmup_calls':1,'pipeline_config':{},'status':'completed'})

def test_evaluation_and_comparison_roundtrip(tmp_path):
    refs=tmp_path/'refs.jsonl';refs.write_text(dumps({'id':'q_fixture','type':'number','answer':2})+'\n')
    a=tmp_path/'a';b=tmp_path/'b';make_run(a,'one');make_run(b,'two')
    report=evaluate(a,refs);evaluate(b,refs)
    assert report['official_score'] is None and report['proxy_PA']['value']==1
    assert report['performance']['stage_latency_ms']['postgres_execution']['mean']==1.25
    assert len(compare_runs([a,b],tmp_path/'comparison'))==2
    assert (tmp_path/'comparison/comparison.md').exists()

def test_prepare_eval_uses_only_questions(tmp_path):
    p=tmp_path/'questions.jsonl'
    qs=[{'id':f'q_{i}','business_id':'synthetic','question':'Fixture','as_of_date':'2026-03-04'} for i in range(3)]
    p.write_text(''.join(dumps(q)+'\n' for q in qs))
    output=tmp_path/'selected.jsonl'
    assert main(['prepare-eval','--questions',str(p),'--count','2','--output',str(output)])==0
    assert len(output.read_text().splitlines())==2
    assert main(['prepare-eval','--questions',str(p),'--count','2','--output',str(output)])==2

def test_select_two_businesses_balanced_and_repeatable(tmp_path):
    from collections import Counter
    from datamate_v2.common import read_jsonl
    p=tmp_path/'questions.jsonl'
    qs=[{'id':f'{biz}_{i}','business_id':biz,'question':'Fixture','as_of_date':'2026-03-04'} for biz in ['a','b','unused'] for i in range(6)]
    p.write_text(''.join(dumps(q)+'\n' for q in qs))
    a=tmp_path/'a.jsonl';b=tmp_path/'b.jsonl'
    args=['prepare-eval','--questions',str(p),'--business','a','--business','b','--count','5','--seed','42']
    assert main(args+['--output',str(a)])==0
    assert main(args+['--output',str(b)])==0
    assert a.read_text()==b.read_text()
    assert Counter(q['business_id'] for q in read_jsonl(a))=={'a':3,'b':2}
    assert read_json(a.with_suffix('.manifest.json'))['selection_policy']=='balanced_businesses'


def test_unknown_business_rejected_before_output(tmp_path):
    p=tmp_path/'questions.jsonl';out=tmp_path/'out.jsonl'
    p.write_text(dumps({'id':'q','business_id':'known','question':'Fixture','as_of_date':'2026-03-04'})+'\n')
    assert main(['prepare-eval','--questions',str(p),'--business','missing','--count','1','--output',str(out)])==2
    assert not out.exists()


def test_source_fingerprint_ignores_unselected_businesses(tmp_path):
    from datamate_v2.cli import source_fingerprint
    for biz in ['a','b']:
        path=tmp_path/'data'/biz;path.mkdir(parents=True)
        (path/'data.csv').write_text('id,value\nx,1\n')
    (tmp_path/'rulebook.md').write_text('Fixture')
    (tmp_path/'calendar.csv').write_text('Fixture')
    before=source_fingerprint(tmp_path,['a'])
    (tmp_path/'data/b/data.csv').write_text('id,value\nx,2\n')
    assert source_fingerprint(tmp_path,['a'])==before
    (tmp_path/'data/a/data.csv').write_text('id,value\nx,3\n')
    assert source_fingerprint(tmp_path,['a'])!=before


@pytest.mark.parametrize('field,value',[
    ('quantization',{'backend':'bitsandbytes','bits':4}),
    ('attn_implementation','sdpa'),
])
def test_compare_rejects_different_inference_precision(tmp_path,field,value):
    refs=tmp_path/'refs.jsonl';refs.write_text(dumps({'id':'q_fixture','type':'number','answer':2})+'\n')
    a=tmp_path/'a';b=tmp_path/'b';make_run(a,'one');make_run(b,'two')
    manifest=read_json(b/'run_manifest.json');manifest['model_settings'][field]=value
    write_json(b/'run_manifest.json',manifest)
    evaluate(a,refs);evaluate(b,refs)
    with pytest.raises(Exception,match=field):compare_runs([a,b],tmp_path/'comparison')


def test_compare_rejects_reused_vs_fresh_ingestion(tmp_path):
    refs=tmp_path/'refs.jsonl';refs.write_text(dumps({'id':'q_fixture','type':'number','answer':2})+'\n')
    a=tmp_path/'a';b=tmp_path/'b';make_run(a,'one');make_run(b,'two')
    manifest=read_json(b/'run_manifest.json');manifest['reused_ingestion']={'source':'fixture'}
    write_json(b/'run_manifest.json',manifest)
    evaluate(a,refs);evaluate(b,refs)
    with pytest.raises(Exception,match='fresh ingestion'):compare_runs([a,b],tmp_path/'comparison')


def test_explicit_mixed_comparison_labels_reuse_without_cold_timing(tmp_path):
    refs=tmp_path/'refs.jsonl';refs.write_text(dumps({'id':'q_fixture','type':'number','answer':2})+'\n')
    a=tmp_path/'a';b=tmp_path/'b';make_run(a,'one');make_run(b,'two')
    m=read_json(b/'run_manifest.json');m.update(model_version='fixture-model',ingest_ms={'synthetic':10},cold_start_total_ms=100)
    m['pipeline_config']={'data':{'fixture':True}}
    m['reused_ingestion']={'original_ingestion_model_versions':{'synthetic':'fixture-model'},'original_data_pipeline_config':{'fixture':True}}
    write_json(b/'run_manifest.json',m)
    am=read_json(a/'run_manifest.json');am['pipeline_config']=m['pipeline_config'];write_json(a/'run_manifest.json',am)
    evaluate(a,refs);evaluate(b,refs)
    rows=compare_runs([a,b],tmp_path/'comparison',allow_mixed_ingestion=True)
    assert rows[1]['ingestion_mode']=='reused_own'
    assert rows[1]['ingest_ms'] is None and rows[1]['cold_start_total_ms'] is None
    assert rows[1]['cache_restore_ms']==10
    m['reused_ingestion']['original_ingestion_model_versions']['synthetic']='other-model'
    write_json(b/'run_manifest.json',m);evaluate(b,refs)
    with pytest.raises(Exception,match='own ingestion'):
        compare_runs([a,b],tmp_path/'bad',allow_mixed_ingestion=True)
