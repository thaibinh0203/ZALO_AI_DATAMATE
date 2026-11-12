"""Quantization is explicit and must never silently fall back to BF16."""
import pytest
from datamate_v2.common import PipelineError
from datamate_v2.inference.model_loader import quantization_options


def test_default_does_not_enable_quantization():
    assert quantization_options({'device':'cuda:0'}) == {}


@pytest.mark.parametrize('config',[
    {'backend':'unknown','bits':4},
    {'backend':'bitsandbytes','bits':8},
    {'backend':'bitsandbytes','bits':4,'type':'nf4','double_quant':False},
])
def test_unsupported_quantization_is_rejected(config):
    with pytest.raises(PipelineError,match='Supported quantization'):
        quantization_options({'device':'cuda:0','quantization':config})


def test_local_4bit_profile_requires_cuda():
    with pytest.raises(PipelineError,match='explicit CUDA'):
        quantization_options({'device':'cpu','quantization':{
            'backend':'bitsandbytes','bits':4,'type':'nf4','double_quant':True}})


def test_quick_smoke_boolean_constraint_accepts_json_lowercase():
    from lmformatenforcer import JsonSchemaParser
    from datamate_v2.scripts.smoke_model import QUICK_SCHEMA
    parser=JsonSchemaParser(QUICK_SCHEMA)
    for char in '{"ok":true}':
        assert char in parser.get_allowed_characters()
        parser=parser.add_character(char)
    assert parser.can_end()
    parser=JsonSchemaParser(QUICK_SCHEMA)
    for char in '{"ok":':
        parser=parser.add_character(char)
    assert 'T' not in parser.get_allowed_characters()
