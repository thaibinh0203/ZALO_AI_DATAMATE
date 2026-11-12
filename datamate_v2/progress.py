"""Progress events contain counters/stage names, never prompts or credentials."""
from datetime import datetime,timezone
from pathlib import Path
from threading import RLock
from time import perf_counter
import sys
from datamate_v2.common import dumps,read_json,write_json,utc_now

_reporter=None

class ProgressReporter:
    def __init__(self,directory):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.started=perf_counter();self.lock=RLock();self.state={}

    def emit(self,stage,message,**details):
        with self.lock:
            self.state.update(details)
            self.state.update(timestamp=utc_now(),stage=stage,message=message,elapsed_seconds=round(perf_counter()-self.started,1))
            write_json(self.directory/'progress.json',self.state)
            with (self.directory/'progress.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(dumps(self.state)+'\n')
            print(f"[{self.state['timestamp'][11:19]} UTC] {message}",file=sys.stderr,flush=True)


def configure(directory):
    global _reporter
    _reporter=ProgressReporter(directory)
    return _reporter


def emit(stage,message,**details):
    if _reporter is not None: _reporter.emit(stage,message,**details)


def inspect_run(directory):
    directory=Path(directory)
    manifest=read_json(directory/'run_manifest.json') if (directory/'run_manifest.json').exists() else {}
    total=(directory/'questions.jsonl').read_bytes().count(b'\n') if (directory/'questions.jsonl').exists() else 0
    completed=(directory/'submission.jsonl').read_bytes().count(b'\n') if (directory/'submission.jsonl').exists() else 0
    live=read_json(directory/'progress.json') if (directory/'progress.json').exists() else None
    if live:
        phase=live.get('phase','unknown');message=live['message']
        updated=datetime.fromisoformat(live['timestamp'])
        since=max(0,(datetime.now(timezone.utc)-updated).total_seconds())
    else:
        phase='qa' if (directory/'submission.jsonl').exists() else 'data_preparation' if manifest else 'startup_or_not_started'
        message='Legacy run: no detailed live events. Ingestion may still be running; completed answers are counted from disk.'
        since=None
    if manifest.get('status','').startswith('completed'): phase=manifest['status']
    return {'run':str(directory),'phase':phase,'message':message,'completed_questions':completed,
            'total_questions':total or (live or {}).get('total_questions'),
            'completed_businesses':list(manifest.get('ingest_ms',{})),
            'seconds_since_last_event':round(since,1) if since is not None else None,
            'live':live,'note':'No live events does not prove the process is stuck. Legacy runs cannot expose their current model call.'}


def print_status(directory):
    status=inspect_run(directory)
    print('Run:',status['run'])
    print('Phase:',status['phase'])
    print(f"QA completed: {status['completed_questions']}/{status['total_questions'] or '?'}")
    print('Businesses prepared successfully:',', '.join(status['completed_businesses']) or '(none recorded yet)')
    print('Stage:',status['message'])
    if status['live']:
        for key in ('business_id','table','question_id','input_tokens','output_tokens'):
            if status['live'].get(key) is not None: print(key+':',status['live'][key])
    if status['seconds_since_last_event'] is not None:
        print('Seconds since last event:',status['seconds_since_last_event'])
    else: print('This run started before detailed progress logging was added.')
