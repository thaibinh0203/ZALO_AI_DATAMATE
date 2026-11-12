"""Explicit human annotations; do not label every wrong answer a semantic error."""
from datamate_v2.common import read_jsonl,PipelineError

ROOT_CAUSES={'unknown','correct','parser','header','normalization','summary','json','sql_syntax','execution','format',
             'question','metric','rulebook','schema','entity','time','grain','join','filter','calculation'}

def annotate(logs,path=None):
    annotations={x['id']:x for x in read_jsonl(path)} if path else {}
    unknown=set(annotations)-{x['id'] for x in logs}
    if unknown: raise PipelineError('Annotation ids outside selected questions')
    for log in logs:
        root=annotations.get(log['id'],{}).get('root_cause','unknown')
        if root not in ROOT_CAUSES: raise PipelineError('Unknown root cause category')
        log['root_cause']=root
    return logs
