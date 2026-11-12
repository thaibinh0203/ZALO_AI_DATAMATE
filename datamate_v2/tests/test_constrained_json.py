from pathlib import Path
import pytest
from datamate_v2.common import dumps,PipelineError
from datamate_v2.contracts import REWRITE_SCHEMA
from datamate_v2.inference.structured_output import parse_output
from datamate_v2.inference.model_loader import LocalEngine


def rewrite(window):
    return {'id':'q','business_id':'b','original_question':'q','rewritten_question':'q','entities':{},'ambiguities':[],'time_windows':[window]}

@pytest.mark.parametrize('window',[
 {'name':'q4','interpretation':'Quý 4 năm 2025 theo lịch','parameters':{},'status':'proposed'},
 {'name':'month','interpretation':'absolute_month','parameters':{'year':2025},'status':'proposed'},
 {'name':'quarter','interpretation':'absolute_quarter','parameters':{'year':2025,'quarter':5},'status':'proposed'},
 {'name':'day','interpretation':'absolute_day','parameters':{'date':'2025-02-29'},'status':'proposed'}])
def test_bad_temporal_outputs_rejected_by_schema(window):
    with pytest.raises(PipelineError): parse_output(dumps(rewrite(window)),REWRITE_SCHEMA)


def test_correct_absolute_quarter_contract():
    window={'name':'q4_2025','interpretation':'absolute_quarter','parameters':{'year':2025,'quarter':4},'status':'proposed'}
    assert parse_output(dumps(rewrite(window)),REWRITE_SCHEMA)['time_windows'][0]['parameters']['quarter']==4


def test_actual_tiny_transformer_generates_schema_constrained_json(tmp_path):
    """Random weights prove actual token filtering; no downloads or QA benchmark."""
    from tokenizers import Tokenizer,decoders,pre_tokenizers
    from tokenizers.models import BPE
    from transformers import PreTrainedTokenizerFast,Qwen3Config,Qwen3ForCausalLM
    vocabulary={'[UNK]':0,'[EOS]':1,'[PAD]':2}
    for c in pre_tokenizers.ByteLevel.alphabet(): vocabulary.setdefault(c,len(vocabulary))
    backend=Tokenizer(BPE(vocab=vocabulary,merges=[],unk_token='[UNK]'))
    backend.pre_tokenizer=pre_tokenizers.ByteLevel(add_prefix_space=False)
    backend.decoder=decoders.ByteLevel()
    tokenizer=PreTrainedTokenizerFast(tokenizer_object=backend,unk_token='[UNK]',eos_token='[EOS]',pad_token='[PAD]')
    tokenizer.chat_template="{% for m in messages %}{{ m['content'] }}{% endfor %}"
    tokenizer.save_pretrained(tmp_path)
    config=Qwen3Config(vocab_size=len(vocabulary),hidden_size=16,intermediate_size=32,num_hidden_layers=1,num_attention_heads=2,num_key_value_heads=1,head_dim=8,max_position_embeddings=512,eos_token_id=1,pad_token_id=2)
    Qwen3ForCausalLM(config).save_pretrained(tmp_path)
    engine=LocalEngine({'path':str(tmp_path),'backend':'transformers','device':'cpu','dtype':'float32','model_class':'AutoModelForCausalLM','max_context_tokens':512,'max_new_tokens':160,'verify_checksums':False})
    schema={'type':'object','additionalProperties':False,'required':['code'],'properties':{'code':{'enum':['absolute_quarter']}}}
    result=engine.generate([{'role':'user','content':'JSON'}],schema=schema)
    assert result['finish_reason']=='stop',result
    assert parse_output(result['text'],schema)=={'code':'absolute_quarter'}
    assert result['constrained_json'] and engine.load_count==1
    index=engine.json_constraints.data
    engine.generate([{'role':'user','content':'JSON'}],schema=schema)
    assert engine.json_constraints.data is index

def test_vietnamese_character_allowed_at_start_of_required_string():
    import torch
    from datamate_v2.inference.json_constraints import JsonConstraints
    class Tokenizer:
        pieces=['0','{','}','"',':','Đ','a','n','m','[EOS]']
        all_special_ids=[9];eos_token_id=9
        def __len__(self): return len(self.pieces)
        def encode(self,text,**kwargs): return [0]
        def decode(self,ids,**kwargs): return ''.join(self.pieces[i] for i in ids)
    schema={'type':'object','additionalProperties':False,'required':['n'],'properties':{'n':{'type':'string','minLength':1}}}
    allowed=JsonConstraints(Tokenizer()).build(schema,1)
    prefix=[1,3,7,3,4,3]  # {"n":"
    for length in range(len(prefix)+1): choices=allowed(0,torch.tensor([0]+prefix[:length]))
    assert 5 in choices  # Đ is legal even before minLength is reached.
