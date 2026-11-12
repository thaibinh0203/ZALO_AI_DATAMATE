from datamate_v2.common import read_json,write_json
from datamate_v2.progress import ProgressReporter,inspect_run
from datamate_v2.cli import main


def test_legacy_run_during_ingestion(tmp_path):
    write_json(tmp_path/'run_manifest.json',{'status':'running','ingest_ms':{}})
    (tmp_path/'questions.jsonl').write_bytes(b'{}\n'*50)
    status=inspect_run(tmp_path)
    assert status['phase']=='data_preparation' and status['completed_questions']==0
    assert status['total_questions']==50 and status['live'] is None


def test_counts_only_completed_lines(tmp_path):
    write_json(tmp_path/'run_manifest.json',{'status':'running','ingest_ms':{'biz_a':100}})
    (tmp_path/'questions.jsonl').write_bytes(b'{}\n'*50)
    (tmp_path/'submission.jsonl').write_bytes(b'{}\n{}\n{"partial": "\xc3')
    status=inspect_run(tmp_path)
    assert status['completed_questions']==2 and status['phase']=='qa'
    assert status['completed_businesses']==['biz_a']


def test_live_events_persist_stage_and_counters(tmp_path,capsys):
    reporter=ProgressReporter(tmp_path)
    reporter.emit('question_start','QA 1/50 started',phase='qa',total_questions=50,completed_questions=0,question_id='q_a')
    reporter.emit('generation','Generating plan',input_tokens=123,output_tokens=None)
    live=read_json(tmp_path/'progress.json')
    assert live['phase']=='qa' and live['question_id']=='q_a' and live['input_tokens']==123
    assert len((tmp_path/'progress.jsonl').read_text().splitlines())==2
    status=inspect_run(tmp_path)
    assert status['total_questions']==50 and status['seconds_since_last_event']>=0
    assert 'Generating plan' in capsys.readouterr().err


def test_cli_status_does_not_load_model(tmp_path,capsys,monkeypatch):
    monkeypatch.setattr('datamate_v2.cli.create_client',lambda *a: (_ for _ in ()).throw(AssertionError('Must not load a model')))
    write_json(tmp_path/'run_manifest.json',{'status':'running','ingest_ms':{}})
    (tmp_path/'questions.jsonl').write_bytes(b'{}\n'*50)
    assert main(['status','--run',str(tmp_path)])==0
    assert 'QA completed: 0/50' in capsys.readouterr().out
