"""JSON-schema token filtering via LM Format Enforcer, compatible with Transformers 5.

Uses LMFE's public parser/token-enforcer API rather than its older Transformers
adapter, which imports a tokenizer class from a removed Transformers module.
"""
from lmformatenforcer import JsonSchemaParser,TokenEnforcer,TokenEnforcerTokenizerData,CharacterLevelParserConfig
from datamate_v2.common import PipelineError

class JsonConstraints:
    def __init__(self,tokenizer):
        self.tokenizer=tokenizer;self.data=None

    def tokenizer_data(self):
        if self.data is None:
            tokenizer=self.tokenizer
            zero=tokenizer.encode('0',add_special_tokens=False)
            if not zero: raise PipelineError('JSON constraints require a tokenizer that can encode digits')
            regular=[];special=set(tokenizer.all_special_ids)
            for idx in range(len(tokenizer)):
                if idx in special: continue
                # A sentinel preserves leading whitespace for SentencePiece-style vocabularies.
                prefix=tokenizer.decode([zero[-1]],skip_special_tokens=False)
                combined=tokenizer.decode([zero[-1],idx],skip_special_tokens=False)
                text=combined[len(prefix):]
                standalone=tokenizer.decode([idx],skip_special_tokens=False)
                regular.append((idx,text,len(text)>len(standalone)))
            def decode(ids): return tokenizer.decode(ids,skip_special_tokens=False).rstrip('\ufffd')
            self.data=TokenEnforcerTokenizerData(regular,decode,tokenizer.eos_token_id,False,len(tokenizer))
        return self.data

    def build(self,schema,prompt_length):
        try:
            data=self.tokenizer_data()
            # Initialize string-length parsers with the full tokenizer alphabet,
            # including Vietnamese characters, before LMFE creates its contexts.
            config=CharacterLevelParserConfig(alphabet=data.tokenizer_alphabet,max_consecutive_whitespaces=2,force_json_field_order=True)
            parser=JsonSchemaParser(schema,config=config)
            enforcer=TokenEnforcer(data,parser)
            parser.config.max_consecutive_whitespaces=2
            parser.config.force_json_field_order=True
        except Exception as error:
            raise PipelineError(f'Cannot prepare constrained JSON decoding: {error}') from error
        def allowed(batch_id,input_ids):
            # Never feed the prompt or previous question into the current JSON parser.
            ids=input_ids[prompt_length:].tolist()
            return enforcer.get_allowed_tokens(ids).allowed_tokens
        return allowed
