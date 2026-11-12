from pathlib import Path
from types import SimpleNamespace
import copy
import pytest
from datamate_v2.common import ROOT,PipelineError,dumps,digest,write_json,read_json,read_jsonl,file_hash,source_fingerprint,schema_name
from datamate_v2.data_pipeline.reuse import inspect_reusable_ingestion,load_reused_business


@pytest.fixture
def prior_run(tmp_path,metadata,question):
    split=tmp_path/'split';business=split/'data/synthetic';business.mkdir(parents=True)
    (business/'README.md').write_text(metadata['readme'])
    (business/'sales.csv').write_text('Mã đơn,Số lượng\na,7\n')
    (split/'rulebook.md').write_text(metadata['rulebook'])
    (split/'calendar.csv').write_text('date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n')
    run=tmp_path/'prior';run.mkdir();(run/'questions.jsonl').write_text(dumps(question)+'\n')
    fingerprint=digest({'fixture':'prior'})
    metadata=copy.deepcopy(metadata)
    metadata.update(fingerprint=fingerprint,schema=schema_name('synthetic',fingerprint),calendar_csv=(split/'calendar.csv').read_text(),model_version='old-ingestion-model')
    metadata['tables'][0]['row_count']=1
    cache=tmp_path/'cache';directory=cache/'businesses/synthetic'/fingerprint
    write_json(directory/'metadata.json',metadata)
    write_json(directory/'clean_rows.json',{'sales':[{'order_id':'a','quantity':7}]})
    # A run interrupted during QA can still have complete, successful ingestion.
    write_json(run/'run_manifest.json',{'status':'running','questions_sha256':file_hash(run/'questions.jsonl'),
               'source_fingerprint':source_fingerprint(split,['synthetic']),
               'data_fingerprints':{'synthetic':fingerprint},'ingest_ms':{'synthetic':12}})
    write_json(run/'logs/ingestion.json',{'failed':{}})
    return split,run,cache,metadata,[question]


def test_reuse_interrupted_qa_does_not_depend_on_new_code_fingerprint(prior_run):
    split,run,cache,metadata,questions=prior_run
    prepared,provenance=inspect_reusable_ingestion(run,split,questions,cache)
    assert prepared['synthetic'][0]==metadata
    assert provenance['original_ingestion_model_versions']=={'synthetic':'old-ingestion-model'}
    assert provenance['source_run_status']=='running'


@pytest.mark.parametrize('failure',['source','questions','failed','fingerprint','metadata_identity','clean_data'])
def test_invalid_reuse_fails_instead_of_reingesting(prior_run,failure):
    split,run,cache,metadata,questions=prior_run
    directory=cache/'businesses/synthetic'/metadata['fingerprint']
    if failure=='source': (split/'rulebook.md').write_text('Changed rulebook')
    if failure=='questions': (run/'questions.jsonl').write_text(dumps({**questions[0],'id':'changed'})+'\n')
    if failure=='failed': write_json(run/'logs/ingestion.json',{'failed':{'synthetic':{'error':'failed'}}})
    if failure=='fingerprint':
        manifest=read_json(run/'run_manifest.json');manifest['data_fingerprints']={};write_json(run/'run_manifest.json',manifest)
    if failure=='metadata_identity':
        metadata['business_id']='other';write_json(directory/'metadata.json',metadata)
    if failure=='clean_data': (directory/'clean_rows.json').unlink()
    with pytest.raises(PipelineError): inspect_reusable_ingestion(run,split,questions,cache)


def test_existing_database_reuse_does_not_read_clean_rows(prior_run,monkeypatch):
    split,run,cache,metadata,questions=prior_run
    monkeypatch.setattr('datamate_v2.data_pipeline.reuse.database_ready',lambda *args:True)
    monkeypatch.setattr('datamate_v2.data_pipeline.reuse.load_business',lambda *args:pytest.fail('No reload expected'))
    assert load_reused_business(metadata,Path('missing-clean-data-not-read.json'),{}) is metadata


def test_missing_database_restores_cache_without_model(prior_run,monkeypatch):
    split,run,cache,metadata,questions=prior_run
    prepared,_=inspect_reusable_ingestion(run,split,questions,cache);loaded=[]
    monkeypatch.setattr('datamate_v2.data_pipeline.reuse.database_ready',lambda *args:False)
    monkeypatch.setattr('datamate_v2.data_pipeline.reuse.load_business',lambda md,rows,settings:loaded.append(rows))
    enriched=load_reused_business(*prepared['synthetic'],{})
    assert {k:v for k,v in enriched.items() if k!='value_catalog'}==metadata
    assert enriched['value_catalog']['fingerprint']==metadata['fingerprint']
    assert (prepared['synthetic'][1].parent/'value_catalog.json').is_file()
    assert loaded==[{'sales':[{'order_id':'a','quantity':7}]}]


def test_cli_qa_only_never_invokes_ingestion(prior_run,tmp_path,monkeypatch):
    from datamate_v2.cli import main
    split,run,cache,metadata,questions=prior_run
    client=SimpleNamespace(settings={},version='new-qa-model',calls=[],engine=SimpleNamespace(
        runtime_versions={},load_ms=0,peak_vram_bytes=0))
    monkeypatch.setattr('datamate_v2.cli.create_client',lambda args:('fixture',client))
    monkeypatch.setattr('datamate_v2.data_pipeline.pipeline.DataPipeline.prepare',lambda *args:pytest.fail('Ingestion must not run'))
    monkeypatch.setattr('datamate_v2.data_pipeline.reuse.database_ready',lambda *args:True)
    monkeypatch.setattr('datamate_v2.qa_pipeline.pipeline.QAPipeline.answer',lambda self,q,md:(
        {'id':q['id'],'type':'number','answer':7},{'id':q['id'],'business_id':q['business_id'],'elapsed_ms':0,'model_calls':[],'queries':[],
                                              'answer':{'id':q['id'],'type':'number','answer':7}}))
    output=tmp_path/'qa-only'
    assert main(['run','--split',str(split),'--reuse-ingestion-from',str(run),'--cache',str(cache),
                 '--output',str(output),'--warmup-calls','0'])==0
    assert read_jsonl(output/'submission.jsonl')[0]['answer']==7
    manifest=read_json(output/'run_manifest.json')
    assert manifest['data_fingerprints']=={'synthetic':metadata['fingerprint']}
    assert manifest['reused_ingestion']['from_run']==str(run)
    assert read_json(output/'logs/ingestion.json')['calls']==[]


def test_cli_invalid_reuse_rejected_before_loading_model(prior_run,tmp_path,monkeypatch):
    from datamate_v2.cli import main
    split,run,cache,metadata,questions=prior_run
    (split/'rulebook.md').write_text('Changed source')
    monkeypatch.setattr('datamate_v2.cli.create_client',lambda *args:pytest.fail('Must fail before model load'))
    assert main(['run','--split',str(split),'--reuse-ingestion-from',str(run),'--cache',str(cache),'--output',str(tmp_path/'output')])==2


def test_partial_reuse_only_reingests_failed_business(prior_run,tmp_path,monkeypatch):
    from datamate_v2.cli import main
    split,run,cache,metadata,questions=prior_run
    other=split/'data/synthetic2';other.mkdir()
    (other/'README.md').write_text('Second business');(other/'sales.csv').write_text('Mã đơn,Số lượng\nb,2\n')
    q2={**questions[0],'id':'q_other','business_id':'synthetic2'}
    (run/'questions.jsonl').write_text(''.join(dumps(q)+'\n' for q in questions+[q2]))
    manifest=read_json(run/'run_manifest.json')
    manifest.update(questions_sha256=file_hash(run/'questions.jsonl'),source_fingerprint=source_fingerprint(split,['synthetic','synthetic2']))
    write_json(run/'run_manifest.json',manifest)
    write_json(run/'logs/ingestion.json',{'failed':{'synthetic2':{'error':'Old mapping failure'}}})
    client=SimpleNamespace(settings={},version='fixture',calls=[],engine=SimpleNamespace(runtime_versions={},load_ms=0,peak_vram_bytes=0))
    monkeypatch.setattr('datamate_v2.cli.create_client',lambda args:('fixture',client))
    monkeypatch.setattr('datamate_v2.data_pipeline.reuse.database_ready',lambda *args:True)
    ingested=[]
    def prepare(self,path,split):
        ingested.append(path.name);self.last_ingest_ms=1
        return {**metadata,'business_id':path.name,'fingerprint':digest({'fresh':path.name})}
    monkeypatch.setattr('datamate_v2.data_pipeline.pipeline.DataPipeline.prepare',prepare)
    def answer(self,q,md):
        final={'id':q['id'],'type':'number','answer':7}
        return final,{'id':q['id'],'answer':final,'elapsed_ms':0,'model_calls':[],'queries':[]}
    monkeypatch.setattr('datamate_v2.qa_pipeline.pipeline.QAPipeline.answer',answer)
    output=tmp_path/'partial'
    assert main(['run','--split',str(split),'--reuse-successful-ingestion-from',str(run),'--cache',str(cache),
                 '--output',str(output),'--warmup-calls','0','--stop-on-ingestion-error'])==0
    assert ingested==['synthetic2']
    assert read_json(output/'logs/ingestion.json')['reused_businesses']==['synthetic']
    assert len(read_jsonl(output/'submission.jsonl'))==2
    assert read_json(output/'run_manifest.json')['data_fingerprints']['synthetic']==metadata['fingerprint']


def test_stop_on_ingestion_error_prevents_misleading_qa_abstains(prior_run,tmp_path,monkeypatch):
    from datamate_v2.cli import main
    split,run,cache,metadata,questions=prior_run
    client=SimpleNamespace(settings={},version='fixture',calls=[],engine=SimpleNamespace(runtime_versions={},load_ms=0,peak_vram_bytes=0))
    monkeypatch.setattr('datamate_v2.cli.create_client',lambda args:('fixture',client))
    def failed(*args): raise PipelineError('synthetic ingestion failure')
    monkeypatch.setattr('datamate_v2.data_pipeline.pipeline.DataPipeline.prepare',failed)
    monkeypatch.setattr('datamate_v2.qa_pipeline.pipeline.QAPipeline.answer',lambda *args:pytest.fail('Must not run QA after ingestion failure'))
    output=tmp_path/'failed'
    assert main(['run','--split',str(split),'--questions',str(run/'questions.jsonl'),'--cache',str(cache),
                 '--output',str(output),'--warmup-calls','0','--stop-on-ingestion-error'])==2
    assert not (output/'submission.jsonl').exists()
    assert read_json(output/'run_manifest.json')['status']=='failed_ingestion'
    assert read_json(output/'logs/ingestion.json')['failed']['synthetic']['error']=='synthetic ingestion failure'
