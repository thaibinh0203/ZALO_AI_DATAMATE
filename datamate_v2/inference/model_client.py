"""One instance shared by preprocessing and QA, serialized across threads."""
from threading import RLock
import re
from datamate_v2.common import ROOT,dumps,digest,read_json,write_json,PipelineError,utc_now,file_hash
from .structured_output import parse_output
from datamate_v2.progress import emit

PROMPTS={"column_normalization":"data_pipeline/column_normalization.txt","value_normalization":"data_pipeline/value_normalization.txt",
 "schema_description":"data_pipeline/schema_description.txt","query_rewriting":"qa_pipeline/query_rewriting.txt",
 "planning_sql":"qa_pipeline/planning_sql_generation.txt",
 "qa_analysis":"qa_pipeline/direct_analysis.txt","qa_decisions":"qa_pipeline/decisions.txt","qa_sql":"qa_pipeline/direct_sql.txt"}

class ModelClient:
    def __init__(self,engine,settings,cache_dir=None):
        self.engine=engine; self.settings=settings; self.cache_dir=cache_dir
        self.lock=RLock(); self.calls=[]
        artifact=engine.path/"artifact_manifest.json" if hasattr(engine,"path") else None
        self.version=digest({"settings":settings,"runtime_versions":getattr(engine,'runtime_versions',None),"manifest":read_json(artifact) if artifact and artifact.exists() else None})

    def call(self,task,payload,schema,*,cache=False,repair=False):
        prompt=(ROOT/"prompts"/PROMPTS[task]).read_text(encoding="utf-8")
        key=digest({"task":task,"payload":payload,"schema":schema,"model":self.version,"prompt":prompt})
        path=self.cache_dir/f"{key}.json" if self.cache_dir else None
        with self.lock:
            if cache and path and path.exists():
                entry=read_json(path)
                parsed=parse_output(dumps(entry["data"]),schema)
                emit('model_cache',f'SLM {task}: cache hit',task=task)
                self.calls.append({"task":task,"cache_hit":True,"model_call_index":len(self.calls)+1,"input_tokens":0,"output_tokens":0,
                                   "duration_ms":0,"is_repair":repair,"status":"cached","thinking":None,"prompt_version":digest(prompt)})
                return parsed
            record={"task":task,"cache_hit":False,"is_repair":repair,"timestamp":utc_now(),"prompt_version":digest(prompt),"model_version":self.version,
                    "model_call_index":len(self.calls)+1,"status":"started"}
            self.calls.append(record)
            emit('model_call',f'SLM #{record["model_call_index"]}: {task} started (waiting for generation)',task=task,input_tokens=None,output_tokens=None)
            messages=[{"role":"system","content":prompt+"\nRequired output JSON schema:\n"+dumps(schema)},
                      {"role":"user","content":dumps(payload)}]
            try:
                generation_options={}
                if getattr(self.engine,'supports_structured_output',False):
                    budget_task={'qa_analysis':'query_rewriting','qa_decisions':'query_rewriting','qa_sql':'planning_sql'}.get(task,task)
                    generation_options={'schema':schema,'max_new_tokens':self.settings.get('task_max_new_tokens',{}).get(budget_task,self.settings.get('max_new_tokens',4096))}
                generated=self.engine.generate(messages,**generation_options);record.update(generated)
                raw=generated["text"]
                thoughts=re.findall(r"<think>(.*?)</think>",raw,re.S)
                record["thinking"]=generated.get('thinking') or ("\n".join(thoughts) if thoughts else None)
                cleaned=re.sub(r"<think>.*?</think>","",raw,flags=re.S).strip()
                if generated.get("finish_reason")=="length": raise PipelineError("Model output hit max_new_tokens")
                if generated.get('decoding_error'): raise PipelineError(generated['decoding_error'])
                data=parse_output(cleaned,schema)
                if cache and path: write_json(path,{"data":data,"model_version":self.version,"prompt_version":digest(prompt)})
                record["status"]="success"
                emit('model_done',f'SLM {task} done: {generated.get("input_tokens", "?")} input / {generated.get("output_tokens", "?")} output tokens; {generated.get("duration_ms",0)/1000:.1f}s',input_tokens=generated.get('input_tokens'),output_tokens=generated.get('output_tokens'))
                return data
            except Exception as error:
                emit('model_error',f'SLM {task} failed ({type(error).__name__})')
                record.update(status="error",error_type=type(error).__name__,error=str(error));raise
