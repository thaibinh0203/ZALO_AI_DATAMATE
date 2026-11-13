"""Offline-only artifact validation and one reusable Transformers engine."""
import os
from pathlib import Path
from time import perf_counter
from datamate_v2.common import ROOT, PipelineError, ContextBudgetError, read_json, file_hash

def quantization_options(settings):
    """Explicit opt-in only; leave existing BF16 runs unchanged."""
    config=settings.get('quantization')
    if not config:
        return {}
    if config != {'backend':'bitsandbytes','bits':4,'type':'nf4','double_quant':True}:
        raise PipelineError('Supported quantization: bitsandbytes 4-bit NF4 with double_quant=true')
    if not str(settings['device']).startswith('cuda'):
        raise PipelineError('This 4-bit configuration requires an explicit CUDA device')
    import torch
    from transformers import BitsAndBytesConfig
    from importlib.metadata import version,PackageNotFoundError
    try:
        version('bitsandbytes')
    except PackageNotFoundError as error:
        raise PipelineError('Install datamate_v2/requirements-4bit.txt before using 4-bit models') from error
    return {'quantization_config':BitsAndBytesConfig(load_in_4bit=True,
        bnb_4bit_quant_type='nf4',bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=getattr(torch,settings['dtype']))}

def validate_artifacts(settings):
    path=Path(settings["path"])
    if not path.is_absolute(): path=ROOT/path
    if not path.is_dir(): raise PipelineError(f"Missing model directory: {path}. Prepare weights online first.")
    for name in ("config.json","tokenizer_config.json"):
        if not (path/name).is_file(): raise PipelineError(f"Missing model artifact: {path/name}")
    if not any((path/name).is_file() for name in ("tokenizer.json","tokenizer.model","spiece.model")):
        raise PipelineError(f"Missing tokenizer vocabulary: {path}")
    index=path/"model.safetensors.index.json"
    weights=list(path.glob("*.safetensors"))
    if index.exists():
        for name in set(read_json(index)["weight_map"].values()):
            if Path(name).name!=name or not (path/name).is_file(): raise PipelineError(f"Missing/unsafe weight shard: {name}")
    if not weights: raise PipelineError(f"Missing safetensors weights: {path}")
    manifest=path/"artifact_manifest.json"
    if settings.get("verify_checksums",False):
        if not manifest.exists(): raise PipelineError("Run prepare_model.py to produce artifact_manifest.json")
        recorded=read_json(manifest)
        required={'config.json','tokenizer_config.json'}|{p.name for p in weights}
        required|={name for name in ('tokenizer.json','tokenizer.model','spiece.model','model.safetensors.index.json','chat_template.jinja') if (path/name).exists()}
        if not required.issubset(recorded['sha256']): raise PipelineError('Artifact manifest does not cover all required model files')
        if settings.get('repo_id') and recorded.get('repo_id')!=settings['repo_id']: raise PipelineError('Model artifact repo_id differs from configuration')
        if settings.get('revision') and recorded.get('revision')!=settings['revision']: raise PipelineError('Model artifact revision differs from configuration')
        for name,expected in recorded["sha256"].items():
            target=path/name
            if not target.resolve().is_relative_to(path.resolve()) or not target.is_file() or file_hash(target)!=expected:
                raise PipelineError(f"Model checksum mismatch: {name}")
    return path

class LocalEngine:
    supports_structured_output=True
    def __init__(self, settings):
        self.settings=settings; self.load_count=0
        if settings.get("backend")!="transformers": raise PipelineError("Supported backend: transformers")
        if not settings.get("local_files_only",True) or settings.get("trust_remote_code",False):
            raise PipelineError("Inference requires local_files_only=true and trust_remote_code=false")
        self.path=validate_artifacts(settings)
        os.environ.update(HF_HUB_OFFLINE="1",TRANSFORMERS_OFFLINE="1",HF_DATASETS_OFFLINE="1")
        started=perf_counter()
        import torch
        import transformers
        from importlib.metadata import version
        self.runtime_versions={'torch':torch.__version__,'transformers':transformers.__version__,'lm-format-enforcer':version('lm-format-enforcer')}
        device=settings["device"]
        if str(device).startswith("cuda") and not torch.cuda.is_available():
            raise PipelineError("CUDA unavailable; select device: cpu explicitly for CPU use")
        dtype=getattr(torch,settings["dtype"],None)
        if dtype not in (torch.float32,torch.float16,torch.bfloat16): raise PipelineError("Unsupported dtype")
        if device=="cpu" and dtype==torch.float16: raise PipelineError("Use float32/bfloat16 on CPU")
        config=read_json(self.path/"config.json")
        actual_limit=config.get('text_config',config).get('max_position_embeddings')
        if actual_limit and settings['max_context_tokens']>actual_limit:
            raise PipelineError(f'Configured context exceeds checkpoint limit {actual_limit}; prepare an explicitly supported context configuration')
        model_type=config["model_type"]
        if model_type not in transformers.models.auto.configuration_auto.CONFIG_MAPPING:
            raise PipelineError(f"Installed Transformers {transformers.__version__} lacks {model_type}; update build dependencies")
        kwargs=dict(local_files_only=True,trust_remote_code=False)
        self.tokenizer=transformers.AutoTokenizer.from_pretrained(self.path,**kwargs)
        class_name=settings.get("model_class","AutoModelForCausalLM")
        if class_name not in ("AutoModelForCausalLM","AutoModelForMultimodalLM","AutoModelForImageTextToText"):
            raise PipelineError("Unsupported model_class")
        cls=getattr(transformers,class_name,None)
        if cls is None: raise PipelineError(f"Transformers lacks {class_name}; update dependencies")
        torch.manual_seed(settings.get("seed",42))
        dispatch=settings.get('device_map',device)
        if isinstance(dispatch,str) and dispatch not in (device,'auto','balanced','balanced_low_0','sequential'):
            raise PipelineError('Unsupported device_map strategy')
        options=dict(dtype=dtype,device_map=dispatch,**kwargs)
        if settings.get('attn_implementation'):
            options['attn_implementation']=settings['attn_implementation']
        options.update(quantization_options(settings))
        if settings.get('quantization'):
            self.runtime_versions['bitsandbytes']=version('bitsandbytes')
        if settings.get("max_memory"): options["max_memory"]=settings["max_memory"]
        self.model=cls.from_pretrained(self.path,**options).eval()
        self.load_count+=1; self.load_ms=(perf_counter()-started)*1000
        self.peak_vram_bytes=None
        from .json_constraints import JsonConstraints
        self.json_constraints=JsonConstraints(self.tokenizer)

    def generate(self,messages,*,schema=None,max_new_tokens=None):
        import torch
        from time import perf_counter
        options=self.settings
        template=options.get("chat_template_kwargs",{})
        text=self.tokenizer.apply_chat_template(messages,tokenize=False,add_generation_prompt=True,**template)
        inputs=self.tokenizer(text,return_tensors="pt",truncation=False,add_special_tokens=False)
        count=inputs.input_ids.shape[-1]
        if count==0: raise PipelineError('Tokenizer produced no tokens; check vocabulary and chat template artifacts')
        output_limit=max_new_tokens if max_new_tokens is not None else options.get("max_new_tokens",2048)
        if count+output_limit>options["max_context_tokens"]:
            raise ContextBudgetError(f"Full context requires {count}+{output_limit} tokens; configured limit {options['max_context_tokens']}. Context was not truncated.")
        inputs={k:v.to(self.model.device) for k,v in inputs.items()}
        from datamate_v2.progress import emit
        generation_options={}
        if options.get('logits_to_keep') is not None:
            generation_options['logits_to_keep']=options['logits_to_keep']
        constraint_setup_ms=0
        if schema is not None:
            if template.get('enable_thinking',False): raise PipelineError('Constrained JSON generation requires enable_thinking=false')
            if self.json_constraints.data is None: emit('json_constraints','Preparing tokenizer index for constrained JSON (once per worker)')
            constraint_start=perf_counter()
            generation_options['prefix_allowed_tokens_fn']=self.json_constraints.build(schema,count)
            constraint_setup_ms=(perf_counter()-constraint_start)*1000
        emit('generation',f'Inference: {count} input tokens, max {output_limit} output tokens; prefill/decode running',input_tokens=count,output_tokens=None)
        start=perf_counter()
        with torch.inference_mode():
            result=self.model.generate(**inputs,max_new_tokens=output_limit,do_sample=False,
                pad_token_id=self.tokenizer.pad_token_id if self.tokenizer.pad_token_id is not None else self.tokenizer.eos_token_id,
                use_cache=True,**generation_options)
        ids=result[0][count:]
        raw=self.tokenizer.decode(ids,skip_special_tokens=False)
        from .structured_output import split_generation
        decoding_error=None
        try: clean,thinking=split_generation(raw,self.tokenizer.all_special_tokens,text)
        except PipelineError as error:
            # Return the actual generated bytes/text to the client log even when a channel is unfinished.
            clean=self.tokenizer.decode(ids,skip_special_tokens=True)
            thinking=raw if text.rstrip().endswith(('<think>','<|channel>thought')) else None
            decoding_error=str(error)
        stop="length" if len(ids)>=output_limit else "stop"
        if torch.cuda.is_available(): self.peak_vram_bytes=torch.cuda.max_memory_allocated()
        duration_ms=(perf_counter()-start)*1000
        return {"text":clean,"raw_text":raw,"thinking":thinking,"decoding_error":decoding_error,"input_tokens":count,"output_tokens":len(ids),"duration_ms":duration_ms,
                "constrained_json":schema is not None,"constraint_setup_ms":constraint_setup_ms,
                "output_tokens_per_second":len(ids)/(duration_ms/1000) if duration_ms else None,
                "finish_reason":stop,"prefill_ms":None,"decode_ms":None}
