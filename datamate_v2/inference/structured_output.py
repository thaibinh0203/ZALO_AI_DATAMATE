"""JSON validation without hidden retries or fabricated outputs."""
import re
import jsonschema
from datamate_v2.common import loads, PipelineError

def split_generation(raw,special_tokens=(),prefix=''):
    """Preserve exact reasoning spans while removing protocol markers from JSON."""
    tagged=raw
    if prefix.rstrip().endswith('<think>') and '<think>' not in tagged:
        tagged='<think>'+tagged
    if prefix.rstrip().endswith('<|channel>thought') and '<|channel>thought' not in tagged:
        tagged='<|channel>thought'+tagged
    thoughts=[]
    def remove(match):
        thoughts.append(match.group(1));return ''
    tagged=re.sub(r'<think>(.*?)</think>',remove,tagged,flags=re.S)
    tagged=re.sub(r'<\|channel>thought(.*?)<channel\|>',remove,tagged,flags=re.S)
    if '<think>' in tagged or '<|channel>thought' in tagged:
        raise PipelineError('Unclosed model reasoning channel')
    for token in special_tokens:
        tagged=tagged.replace(token,'')
    return tagged.strip(),''.join(thoughts) if thoughts else None

def parse_output(text, schema):
    text=text.strip()
    if text.startswith("```"):
        match=re.fullmatch(r"```(?:json)?\s*(.*?)\s*```",text,re.S)
        if not match: raise PipelineError("Malformed JSON code fence")
        text=match.group(1)
    try:
        data=loads(text)
        jsonschema.validate(data,schema,format_checker=jsonschema.FormatChecker())
        return data
    except (ValueError,jsonschema.ValidationError) as error:
        if isinstance(error,jsonschema.ValidationError):
            detail='/'.join(str(p) for p in error.absolute_path)+': '+error.message
        else: detail=str(error)
        raise PipelineError(f"Structured output invalid: {detail[:600]}") from error
