"""Bounded, logged model access for planning stages.

Every logical request is counted against a per-stage/per-item cap and a global cap,
including cache hits and retries. Outputs are cached by the exact messages plus model
identity, so any change in sources, revision or prompt invalidates the cache.
"""
from collections import Counter
from pathlib import Path
from time import perf_counter

from datamate_b.contracts import strict_json
from ..parsers import dumps
from .contracts import ContractError, object_sha


class BudgetExceeded(RuntimeError):
    pass


class ContextBudgetError(ValueError):
    pass


class PlanModel:
    """Wrap a LocalModel-like object (load/generate/metadata) with caps and logs."""

    def __init__(self, model, *, audit_path, caps, cache_dir=None, identity=None):
        self.model = model;self.audit_path = Path(audit_path);self.caps = dict(caps)
        self.cache = Path(cache_dir) if cache_dir else None
        if self.cache:
            self.cache.mkdir(parents=True, exist_ok=True)
        self.used = Counter();self.calls = 0;self.cache_hits = 0;self.generations = 0
        self.identity = {'model': model.metadata() if hasattr(model, 'metadata') else repr(model),
                         **(identity or {})}

    def _fits(self, messages):
        tokenizer = getattr(self.model, 'tokenizer', None);config = getattr(self.model, 'config', None)
        if config is None:
            return True
        if tokenizer is None:
            from transformers import AutoTokenizer
            if not hasattr(self, '_tokenizer'):
                self._tokenizer = AutoTokenizer.from_pretrained(str(self.model.path), local_files_only=True,
                                                                trust_remote_code=False)
            tokenizer = self._tokenizer
        text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        return len(tokenizer(text, add_special_tokens=False)['input_ids'])+config.max_new_tokens <= config.max_context_tokens

    def _charge(self, stage, item):
        cap = self.caps.get(stage)
        if cap is None:
            raise BudgetExceeded(f'no cap configured for stage {stage}')
        if self.used[(stage, item)] >= cap:
            raise BudgetExceeded(f'{stage} cap {cap} reached for {item}')
        if self.calls >= self.caps['max_model_calls_per_business']:
            raise BudgetExceeded('global model call cap reached')
        self.used[(stage, item)] += 1;self.calls += 1

    def ask(self, stage, item, system, payload, *, validator=None, compact=None, retry_instruction=None):
        """Request a JSON object; one contract retry is allowed if the cap permits.

        Returns (value, attempts). ``value`` is None if every attempt failed; failures
        are logged and the caller keeps the safe fallback (unknown/unresolved).
        """
        attempts = [];current = payload
        while True:
            try:
                self._charge(stage, item)
            except BudgetExceeded as exc:
                record = {'stage': stage, 'item': item, 'budget_exhausted': str(exc), 'sent': False}
                attempts.append(record)
                with self.audit_path.open('a', encoding='utf-8') as stream:
                    stream.write(dumps(record)+'\n')
                return None, attempts
            messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': dumps(current)}]
            record = {'stage': stage, 'item': item, 'attempt': len(attempts)+1, 'cache_hit': False,
                      'payload_sha256': object_sha(current)}
            if not self._fits(messages):
                reduced = compact(current) if compact else None
                if reduced is not None:
                    messages[1]['content'] = dumps(reduced);record['context_compaction'] = reduced.get('context_compaction')
                if reduced is None or not self._fits(messages):
                    record['error'] = 'context_budget_exceeded_no_silent_truncation'
                    self._log(record, messages);attempts.append(record)
                    return None, attempts
            key = object_sha([self.identity, stage, messages])
            cached = self.cache/f'{key}.json' if self.cache else None
            started = perf_counter()
            try:
                if cached and cached.exists():
                    generated = strict_json(cached.read_text(encoding='utf-8'))
                    if generated.get('cache_key') != key:
                        raise ValueError('Invalid model cache record')
                    record['cache_hit'] = True;self.cache_hits += 1
                else:
                    if hasattr(self.model, 'load'):
                        self.model.load()
                    generated = {'cache_key': key, **self.model.generate(messages)}
                    self.generations += 1
                    if cached:
                        cached.write_text(dumps(generated)+'\n', encoding='utf-8')
            except Exception as exc:  # generation errors are logged, then re-raised
                record['generation_error'] = f'{type(exc).__name__}: {exc}'
                self._log(record, messages)
                raise
            record.update({k: generated.get(k) for k in ('raw_output', 'prompt_tokens', 'output_tokens',
                                                          'generation_ms', 'hit_token_limit')})
            record['elapsed_ms'] = round((perf_counter()-started)*1000, 3)
            error = None;value = None
            try:
                if generated.get('hit_token_limit'):
                    raise ContractError('generation hit the output token limit')
                value = strict_json(generated['raw_output'])
                if not isinstance(value, dict):
                    raise ContractError('expected a JSON object')
                if validator is not None:
                    value = validator(value)
            except (ValueError, TypeError, KeyError) as exc:
                error = f'{type(exc).__name__}: {exc}';value = None
            record['error'] = error
            self._log(record, messages);attempts.append(record)
            if error is None:
                return value, attempts
            current = {**payload, 'contract_retry': {'previous_error': error,
                       'instruction': retry_instruction or 'Return only the required JSON contract. Use null/[] when unsupported; never guess.'}}

    def _log(self, record, messages):
        with self.audit_path.open('a', encoding='utf-8') as stream:
            stream.write(dumps({**record, 'messages': messages, 'identity_sha256': object_sha(self.identity)})+'\n')

    def summary(self):
        by_stage = Counter()
        for (stage, _), n in self.used.items():
            by_stage[stage] += n
        return {'logical_requests': self.calls, 'generations': self.generations, 'cache_hits': self.cache_hits,
                'by_stage': dict(by_stage), 'caps': self.caps}


class ColumnModelAdapter:
    """Lets the v4 ``refine_column`` call this bounded model with its own contract."""

    def __init__(self, plan_model, system, item):
        self.plan_model = plan_model;self.system = system;self.item = item

    def complete(self, payload, audit_path=None):
        value, attempts = self.plan_model.ask('column_refinement', self.item, self.system, payload)
        if value is None:
            error = attempts[-1].get('error') or attempts[-1].get('budget_exhausted') or 'no output'
            if 'context_budget' in str(error):
                raise ValueError('Semantic context exceeds budget for refinement')
            return None, error
        return value, None
