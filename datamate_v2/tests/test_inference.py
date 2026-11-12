from pathlib import Path
import pytest
from datamate_v2.common import PipelineError,dumps,write_json
from datamate_v2.inference.model_loader import validate_artifacts,LocalEngine
from datamate_v2.inference.model_client import ModelClient
from datamate_v2.inference.structured_output import parse_output

@pytest.mark.parametrize('content',['{"a":1,"a":2}','{"a":NaN}','answer: {"a":1}','{"a":"wrong"}'])
def test_strict_json(content):
    with pytest.raises(PipelineError): parse_output(content,{'type':'object','properties':{'a':{'type':'integer'}}})

def test_missing_model_fail_early(tmp_path):
    with pytest.raises(PipelineError,match='Missing model directory'): validate_artifacts({'path':str(tmp_path/'missing')})

def test_missing_tokenizer_fail_early(tmp_path):
    write_json(tmp_path/'config.json',{});write_json(tmp_path/'tokenizer_config.json',{})
    with pytest.raises(PipelineError,match='tokenizer vocabulary'): validate_artifacts({'path':str(tmp_path)})

def test_all_five_tasks_reuse_one_engine(tmp_path):
    class Engine:
        load_count=1
        count=0
        def generate(self,messages):
            self.count+=1
            return {'text':'{"ok": true}','input_tokens':8,'output_tokens':5,'finish_reason':'stop','duration_ms':1}
    engine=Engine();client=ModelClient(engine,{'fixture':True},tmp_path)
    schema={'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}}
    for task in ['column_normalization','value_normalization','schema_description','query_rewriting','planning_sql']:
        assert client.call(task,{},schema)['ok']
    assert engine.load_count==1 and engine.count==5
    client.call('schema_description',{},schema,cache=True);client.call('schema_description',{},schema,cache=True)
    assert client.calls[-1]['cache_hit'] and engine.count==6

@pytest.mark.parametrize('architecture',['qwen3','qwen2','gemma4_unified'])
def test_real_tiny_qwen_loader_is_offline_and_reused(tmp_path,monkeypatch,architecture):
    """Random tiny weights validate mechanics only; this is not a model benchmark."""
    import torch
    from transformers import Qwen3Config,Qwen3ForCausalLM,PreTrainedTokenizerFast
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    # No downloads and no actual competition questions.
    tokenizer=Tokenizer(WordLevel({'[UNK]':0,'[EOS]':1,'[PAD]':2,'hello':3,'user':4,'assistant':5},unk_token='[UNK]'));tokenizer.pre_tokenizer=Whitespace()
    fast=PreTrainedTokenizerFast(tokenizer_object=tokenizer,unk_token='[UNK]',eos_token='[EOS]',pad_token='[PAD]')
    fast.chat_template="{% for message in messages %}{{ message['role'] }} {{ message['content'] }} {% endfor %}assistant"
    fast.save_pretrained(tmp_path)
    config=Qwen3Config(vocab_size=6,hidden_size=16,intermediate_size=32,num_hidden_layers=1,num_attention_heads=2,num_key_value_heads=1,head_dim=8,max_position_embeddings=128,eos_token_id=1,pad_token_id=2)
    model_class='AutoModelForCausalLM'
    if architecture=='qwen2':
        from transformers import Qwen2Config,Qwen2ForCausalLM,Qwen2Tokenizer
        vocab={'[UNK]':0,'[EOS]':1,'[PAD]':2}
        for letter in sorted(set('userhelloassistantĠ')): vocab.setdefault(letter,len(vocab))
        qtokenizer=Qwen2Tokenizer(vocab=vocab,merges=[],unk_token='[UNK]',eos_token='[EOS]',pad_token='[PAD]')
        qtokenizer.chat_template=fast.chat_template
        qtokenizer.save_pretrained(tmp_path)
        Qwen2ForCausalLM(Qwen2Config(vocab_size=len(vocab),hidden_size=16,intermediate_size=32,num_hidden_layers=1,num_attention_heads=2,num_key_value_heads=1,max_position_embeddings=128,eos_token_id=1,pad_token_id=2)).save_pretrained(tmp_path)
    elif architecture=='gemma4_unified':
        from transformers import Gemma4UnifiedConfig,Gemma4UnifiedTextConfig,Gemma4UnifiedForConditionalGeneration
        text=Gemma4UnifiedTextConfig(vocab_size=6,hidden_size=16,intermediate_size=32,num_hidden_layers=1,num_attention_heads=2,num_key_value_heads=1,head_dim=8,max_position_embeddings=128,sliding_window=64,layer_types=['full_attention'],eos_token_id=1,pad_token_id=2,num_kv_shared_layers=0)
        config=Gemma4UnifiedConfig(text_config=text,vision_config={'mm_embed_dim':16,'output_proj_dims':16,'mm_posemb_size':16,'patch_size':2},audio_config={'audio_embed_dim':16},boi_token_id=7,eoi_token_id=8,image_token_id=9,video_token_id=10,boa_token_id=11,eoa_token_index=12,audio_token_id=13)
        Gemma4UnifiedForConditionalGeneration(config).save_pretrained(tmp_path)
        model_class='AutoModelForMultimodalLM'
    else: Qwen3ForCausalLM(config).save_pretrained(tmp_path)
    settings={'path':str(tmp_path),'backend':'transformers','device':'cpu','dtype':'float32','model_class':model_class,'max_context_tokens':128,'max_new_tokens':2,'verify_checksums':False}
    engine=LocalEngine(settings)
    for _ in range(2):
        result=engine.generate([{'role':'user','content':'hello'}]);assert result['input_tokens']>0 and result['output_tokens']>0
    assert engine.load_count==1
    assert __import__('os').environ['HF_HUB_OFFLINE']=='1'

@pytest.mark.parametrize('raw,prefix,thought',[
 ('<think> exact reasoning\n</think>{"ok":true}','',' exact reasoning\n'),
 (' exact reasoning\n</think>{"ok":true}','<think>\n',' exact reasoning\n'),
 ('<|channel>thought\nexact reasoning\n<channel|>{"ok":true}<|turn_end>','','\nexact reasoning\n'),
 ('\nexact reasoning\n<channel|>{"ok":true}<|turn_end>','<|channel>thought\n','\nexact reasoning\n')])
def test_reasoning_protocol_preserved(raw,prefix,thought):
    from datamate_v2.inference.structured_output import split_generation
    text,reasoning=split_generation(raw,['<|turn_end>'],prefix)
    assert text=='{"ok":true}' and reasoning==thought
