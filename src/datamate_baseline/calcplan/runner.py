"""Per-question calculation-plan flow with hard budgets (design §11).

understand (rewrite+analysis) -> deterministic time resolution -> bind loop (model
tools or plan) -> plan validation, literal grounding, quality/coverage gate -> SQL ->
AST gates + plan-SQL checks -> EXPLAIN -> execute -> result checks -> serializer.
Repairs target the failing layer and share a round budget. Exhausted budgets,
ambiguity, quality uncertainty or technical failures end in abstain with a reason;
only verified missing information yields unanswerable.
"""
from collections import Counter
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
import hashlib
import json
from pathlib import Path
import re
from time import perf_counter

from datamate_b.contracts import strict_json, validate_request
from datamate_data.semplan.contracts import STATUS_DOMAINS
from ..serializer import dumps, format_answer
from ..query_tool import checked_sql
from . import VERSION
from .context import schema_for_prompt
from .contracts import (concept_present, reconcile_time, string_filters, validate_analysis, validate_calc_plan)
from .plan_sql import check_sql, summarize
from .prompts import BIND_SYSTEM, PLAN_REPAIR, PROMPT_VERSION, SQL_REPAIR, SQL_SYSTEM, UNDERSTAND_SYSTEM
from .time_resolver import TimeResolver, comparison_requested, extract_time, fold
from .tools import ToolBox, ToolBudgetExceeded

MONEY_WORDS = {'revenue', 'money', 'amount', 'cost', 'price', 'profit', 'debt', 'doanh', 'tien', 'gia', 'lai', 'von', 'no', 'phi', 'vnd', 'dong'}
COMPARABLE = {'month', 'week', 'quarter', 'year', 'year_to_date', 'day', 'range', 'last_n_days', 'tet'}


class Abstain(Exception):
    def __init__(self, reason, detail=None, kind='abstain'):
        super().__init__(reason);self.reason = reason;self.detail = detail;self.kind = kind


class QuestionModel:
    """Every model request (including contract retries) counts against one cap."""

    def __init__(self, model, trace, cap):
        self.model = model;self.trace = trace;self.cap = cap;self.used = 0
        trace.setdefault('understanding_calls', [])

    def ask(self, stage, system, payload):
        if self.used >= self.cap:
            raise Abstain('BUDGET_EXHAUSTED', {'budget': 'model_calls', 'cap': self.cap})
        self.used += 1
        messages = [{'role': 'system', 'content': system},
                    {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, default=str, separators=(',', ':'))}]
        entry = {'stage': stage, 'attempt': self.used, 'messages': messages}
        self.trace['understanding_calls'].append(entry)
        try:
            generated = self.model.generate(messages);entry.update(generated)
        except ValueError as exc:
            entry['error'] = str(exc)
            if 'context' in str(exc).lower():
                raise Abstain('CONTEXT_BUDGET', str(exc))
            return None, str(exc)
        try:
            if generated.get('hit_token_limit'):
                raise ValueError('output token limit reached')
            value = strict_json(generated['raw_output'])
            if not isinstance(value, dict):
                raise ValueError('expected a JSON object')
        except (ValueError, TypeError, KeyError) as exc:
            entry['error'] = f'{type(exc).__name__}: {exc}';return None, entry['error']
        entry['parsed'] = value
        return value, None


class Rounds:
    def __init__(self, cap):
        self.cap = cap;self.used = []

    def take(self, layer, issues):
        if len(self.used) >= self.cap:
            return False
        self.used.append({'layer': layer, 'issues': issues});return True


def _evidence_view(context, question):
    """README lines plus rulebook units relevant to the question (lexical, no labels)."""
    evidence = context['evidence'];words = set(fold(question).split())
    readme = [[e['id'], e['quote']] for e in evidence.entries.values() if e['source'] == 'readme']
    scored = []
    for e in evidence.entries.values():
        if e['source'] != 'rulebook':
            continue
        overlap = len(words & set(fold(e['quote']).split()))
        always = any(k in fold(e.get('context') or '') for k in ('don hang va doanh thu', 'thoi gian', 'cach ghi', 'xep hang', 'khong tra loi'))
        scored.append((overlap+(3 if always else 0), e['id'], e))
    chosen = [e for _, _, e in sorted(scored, key=lambda x: (-x[0], int(x[1][1:])))[:28]]
    rules = [[e['id'], e.get('context') or '', e['quote']] for e in sorted(chosen, key=lambda e: int(e['id'][1:]))]
    return {'readme': readme, 'rulebook': rules}


def _compact_tool(result):
    out = {k: v for k, v in result.items() if k not in {'rows'}}
    if 'rows' in result:
        out['rows'] = result['rows'][:10]
    if 'values' in result:
        out['values'] = result['values'][:30]
    return out


def answer_question(request, *, context, model, toolbox, resolver, config, trace):
    """Return the final prediction record (never raises for per-question failures)."""
    question = request['question'];as_of = request['as_of_date']
    qm = QuestionModel(model, trace, config['max_model_calls']);rounds = Rounds(config['max_repair_rounds'])
    trace['budgets'] = {'model_calls_cap': config['max_model_calls'], 'repair_rounds_cap': config['max_repair_rounds'],
                        'tool_calls_cap': config['max_tool_calls'], 'answer_executions_cap': config['max_answer_executions']}
    try:
        try:
            return _answer(request, question, as_of, context, qm, rounds, toolbox, resolver, config, trace)
        except ToolBudgetExceeded as exc:
            raise Abstain('BUDGET_EXHAUSTED', {'budget': str(exc)})
    except Abstain as exc:
        trace['decision'] = {'type': exc.kind, 'reason': exc.reason, 'detail': exc.detail}
        record = {'id': request['id'], 'type': exc.kind, 'answer': None, 'reason': exc.reason}
        return record
    finally:
        trace['budgets'].update(model_calls_used=qm.used, repair_rounds_used=len(rounds.used), repairs=rounds.used,
                                tool_calls_used=toolbox.used['total'], answer_executions_used=toolbox.used['execute_sql'])


def _answer(request, question, as_of, context, qm, rounds, toolbox, resolver, config, trace):
    evidence = _evidence_view(context, question)
    business = {'tables': [{'table': v, 'grain': t['grain'].get('description') or t['grain']['kind'],
                            'columns': [c['name'] for c in t['columns'] if c['semantic_role'] != 'derived_companion']}
                           for v, t in context['tables'].items()]}
    # 1. rewrite + analysis (one contract retry, counted)
    analysis = None;payload = {'question': question, 'as_of_date': as_of, 'business': business, 'evidence': evidence}
    for _ in range(2):
        value, error = qm.ask('understand', UNDERSTAND_SYSTEM, payload)
        if value is not None:
            try:
                analysis, corrections = validate_analysis(value, question)
                trace['analysis_corrections'] = corrections;break
            except ValueError as exc:
                error = str(exc)
        payload = {**payload, 'contract_retry': {'previous_error': error}}
    if analysis is None:
        raise Abstain('MODEL_CONTRACT', 'question analysis failed after retry')
    trace['question_analysis'] = analysis
    # 2. deterministic time resolution
    specs, conflicts = reconcile_time(extract_time(question), analysis['time'])
    intervals = {};time_rows = []
    want_previous = analysis['operation'] in {'growth', 'compare', 'difference'} or comparison_requested(question)
    for index, spec in enumerate(specs, 1):
        if spec['kind'] == 'shift':
            raise Abstain('CAPABILITY_UNSUPPORTED', 'time-of-day shift filters are not supported by this plan contract')
        resolution = toolbox.call('resolve_time', {'spec': spec, 'as_of_date': as_of})['resolution']
        intervals[f'T{index}'] = resolution;time_rows.append({'id': f'T{index}', **resolution})
        if resolution['status'] != 'resolved':
            raise Abstain('UNSUPPORTED_TIME' if resolution['status'] == 'unsupported' else 'AMBIGUOUS_TIME', resolution)
        if want_previous and spec['kind'] in COMPARABLE and not any(s['kind'] == 'before_tet' for s in specs) and len(
                [s for s in specs if s['kind'] in COMPARABLE]) == 1:
            previous = resolver.previous(resolution, spec, as_of)
            if previous['status'] == 'resolved':
                intervals[f'T{index}P'] = previous;time_rows.append({'id': f'T{index}P', **previous})
    this_month = next((i for i, s in enumerate(specs, 1) if s['kind'] == 'month' and s.get('relative') == 'this'), None)
    last_month = next((i for i, s in enumerate(specs, 1) if s['kind'] == 'month' and s.get('relative') == 'last'), None)
    if want_previous and this_month and last_month:
        # Rulebook §2: "tháng này" compared with last month uses the same number of days.
        aligned = resolver.previous(intervals[f'T{this_month}'], specs[this_month-1], as_of)
        aligned['phrase'] = specs[last_month-1]['phrase'];aligned['notes'] = aligned['notes']+['aligned_to_month_to_date_comparison']
        intervals[f'T{last_month}'] = aligned
        time_rows = [{'id': f'T{last_month}', **aligned} if r['id'] == f'T{last_month}' else r for r in time_rows]
    trace['time_resolution'] = {'specs': specs, 'conflicts': conflicts, 'intervals': time_rows}
    # 3. bind loop: tools or plan, validated; repairs share the round budget
    tool_results = [];issues_feedback = None;plan = None;dependencies = None
    bind_calls = 0
    while plan is None:
        if bind_calls >= config['max_bind_calls']:
            raise Abstain('BUDGET_EXHAUSTED', {'budget': 'bind_calls'})
        bind_calls += 1
        payload = {'question': question, 'as_of_date': as_of,
                   'rewrite': analysis['rewrite']['text'] if analysis['rewrite_status'] == 'accepted' else None,
                   'analysis': {k: analysis[k] for k in ('target', 'metric', 'operation', 'filters', 'grouping', 'answer')},
                   'intervals': [{k: r[k] for k in ('id', 'phrase', 'start', 'end_exclusive', 'notes')} for r in time_rows],
                   'schema': schema_for_prompt(context), 'evidence': evidence, 'tool_results': tool_results,
                   'remaining': toolbox.remaining()}
        if issues_feedback:
            payload['repair'] = {'instruction': PLAN_REPAIR, 'issues': issues_feedback}
        value, error = qm.ask('plan_repair' if issues_feedback else 'bind', BIND_SYSTEM, payload)
        if value is None or value.get('action') not in {'tools', 'plan', 'missing_data', 'ambiguous'}:
            issues = [{'code': 'MODEL_CONTRACT', 'error': error or 'unknown action'}]
            if not rounds.take('plan', issues):
                raise Abstain('MODEL_CONTRACT', issues)
            issues_feedback = issues;continue
        action = value['action']
        if action == 'ambiguous':
            raise Abstain('AMBIGUOUS_QUESTION', value.get('reason'))
        if action == 'tools':
            requests = value.get('requests') if isinstance(value.get('requests'), list) else []
            for item in requests[:3]:
                if not isinstance(item, dict) or not isinstance(item.get('args'), dict):
                    continue
                result = toolbox.call(str(item.get('tool')), item['args'], by='model')
                tool_results.append({'id': f'tool:{len(tool_results)+1}', 'tool': item.get('tool'), 'args': item['args'],
                                     'result': _compact_tool(result)})
            continue
        if action == 'missing_data':
            issues = _missing_data(value.get('missing'), context, trace)
            if not rounds.take('plan', issues):
                raise Abstain('AMBIGUOUS_QUESTION', issues)
            issues_feedback = issues;continue
        candidate = value.get('plan')
        try:
            candidate, issues, dependencies = validate_calc_plan(candidate, context, analysis, intervals)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            issues = [{'code': 'PLAN_CONTRACT', 'error': str(exc)}]
        if not issues:
            issues = _ground(candidate, context, toolbox, trace)
        if not issues:
            if candidate['feasibility']['status'] == 'missing_data':
                issues = _missing_data(candidate['feasibility'].get('missing'), context, trace)
            elif candidate['feasibility']['status'] == 'ambiguous':
                raise Abstain('AMBIGUOUS_QUESTION', candidate['feasibility'])
            else:
                plan = candidate;break
        trace.setdefault('plan_attempts', []).append({'plan': candidate, 'issues': issues})
        if not rounds.take('plan', issues):
            code = 'SELF_JOIN_UNSUPPORTED' if any(i.get('code') == 'SELF_JOIN_UNSUPPORTED' for i in issues) else (
                'AMBIGUOUS_LITERAL' if any(i.get('code') == 'AMBIGUOUS_LITERAL' for i in issues) else (
                    'LITERAL_NOT_FOUND' if any(i.get('code') == 'LITERAL_NOT_FOUND' for i in issues) else 'PLAN_INVALID'))
            raise Abstain(code, issues)
        issues_feedback = issues
    trace['schema_linking'] = plan
    # 4. quality and coverage gate
    gate = toolbox.call('check_data_requirements', {'dependencies': dependencies})
    trace['quality_gate'] = gate
    if gate.get('status') == 'error':
        raise Abstain('QUALITY_BLOCKED', gate)
    outside = [c for c in gate['coverage'] if c['status'] == 'outside']
    if outside and len(outside) == len([c for c in gate['coverage'] if c['status'] in {'outside', 'overlaps'}]):
        rule = context['rules_detected'].get('outside_coverage')
        raise Abstain('OUTSIDE_DATA_COVERAGE', {'coverage': outside, 'rule': rule}, kind='unanswerable')
    if gate['quality']['decision'] == 'block':
        raise Abstain('QUALITY_BLOCKED', [i for i in gate['quality']['items'] if i['impact'] != 'not_affecting'])
    # 5. SQL with plan-SQL checks, EXPLAIN, execution and result checks
    used_views = {s['from'] for s in plan['stages'] if s['from'] in context['tables']} | {j['table'] for s in plan['stages'] for j in s['joins']}
    sql_payload = {'question': question, 'as_of_date': as_of, 'plan': plan, 'schema': schema_for_prompt(context, used_views),
                   'dialect': 'postgres'}
    sql_errors = None
    while True:
        payload = {**sql_payload, **({'repair': {'instruction': SQL_REPAIR, 'errors': sql_errors}} if sql_errors else {})}
        value, error = qm.ask('sql_repair' if sql_errors else 'sql', SQL_SYSTEM, payload)
        trace['raw_output'] = trace['understanding_calls'][-1].get('raw_output')
        trace['json_valid'] = value is not None
        errors = None
        if value is None or set(value) != {'sql'} or not isinstance(value['sql'], str) or not value['sql'].strip():
            errors = [{'code': 'MODEL_CONTRACT', 'error': error or 'expected {"sql": "..."}'}]
        else:
            query = value['sql'];trace['parsed_output'] = value;trace['contract_valid'] = True
            trace['plan'] = {'sql': query}
            try:
                qualified = checked_sql(query, context['ddl'], context['allowed']);trace['sql_valid'] = True
            except ValueError as exc:
                errors = [{'code': 'SQL_GATE', 'error': str(exc)}];trace['sql_valid'] = False
            if errors is None:
                checks = check_sql(qualified, plan, answer_shape=plan['answer']['shape'], allowed=context['allowed'])
                status, failing = summarize(checks, unverified_policy=config['unverified_critical'])
                trace.setdefault('plan_sql_checks', []).append({'status': status, 'checks': checks})
                if status == 'fail':
                    errors = [{'code': 'PLAN_SQL_MISMATCH', 'failed_checks': failing}]
                elif status == 'unverified':
                    raise Abstain('UNVERIFIED_CRITICAL_CHECK', failing)
            if errors is None:
                explained = toolbox.call('explain_sql', {'sql_text': query})
                trace.setdefault('explain', []).append(explained)
                if explained.get('status') == 'error':
                    errors = [{'code': 'SQL_ERROR', 'stage': 'explain', 'error': explained['error']}]
            if errors is None:
                result = toolbox.call('execute_sql', {'sql_text': query})
                if result.get('status') == 'error':
                    if result.get('sqlstate') == '57014':
                        raise Abstain('QUERY_TIMEOUT', result)
                    errors = [{'code': 'SQL_ERROR', 'stage': 'execute', 'error': result['error']}]
                else:
                    trace['sql_executed'] = True;trace['query_result'] = {k: result[k] for k in ('columns', 'rows', 'truncated', 'sql_executed')}
                    try:
                        return _result_answer(request['id'], result, plan, analysis, gate, trace)
                    except ValueError as exc:
                        errors = [{'code': 'RESULT_SHAPE', 'error': str(exc)}]
        if not rounds.take('sql', errors):
            code = errors[0]['code'] if errors[0]['code'] in {'SQL_GATE', 'SQL_ERROR', 'PLAN_SQL_MISMATCH', 'RESULT_SHAPE', 'MODEL_CONTRACT'} else 'SQL_ERROR'
            raise Abstain(code, errors)
        sql_errors = errors


def _missing_data(missing, context, trace):
    """Raise unanswerable only if no column carrying the concept has data and the
    claim cites existing README/rulebook evidence; otherwise return repair issues."""
    if not isinstance(missing, list) or not missing:
        trace['missing_data_check'] = {'status': 'invalid_claim'}
        return [{'code': 'MISSING_DATA_REJECTED', 'detail': 'missing list required'}]
    hits = [];refs = []
    for item in missing:
        if not isinstance(item, dict):
            continue
        found = [h for h in concept_present(item.get('concepts') or [item.get('requirement')], context) if h['has_data'] is not False]
        hits += found;refs += [r for r in item.get('evidence') or [] if context['evidence'].has(r)]
    trace['missing_data_check'] = {'columns_with_data': hits, 'evidence_refs': refs}
    if not hits and refs:
        raise Abstain('MISSING_DATA', {'missing': missing, 'evidence_refs': refs,
                                       'inventory': 'no column with this concept has data in the published business'},
                      kind='unanswerable')
    return [{'code': 'MISSING_DATA_REJECTED', 'columns_with_data': hits[:10],
             'note': 'columns with data exist for this concept' if hits else 'claim needs README/rulebook evidence IDs'}]


def _ground(plan, context, toolbox, trace):
    """Ground text literals and status labels; never accept fuzzy candidates."""
    issues = [];grounding = trace.setdefault('literal_grounding', [])
    for stage, f in string_filters(plan):
        view = f['view'];column = f['column_name']
        mapping = context['tables'][view]['status_mappings'].get(column)
        values = f['value'] if isinstance(f['value'], list) else [f['value']]
        grounded = []
        for literal in values:
            if mapping:
                observed = {m['target'] for m in mapping['mappings'].values()}
                labels = set(STATUS_DOMAINS.get(mapping.get('domain'), ())) | observed
                if literal in labels or literal in mapping['unresolved']:
                    grounded.append(literal)
                    if literal in labels and literal not in observed:
                        grounding.append({'column': column, 'literal': literal, 'note': 'domain label not observed in this data'})
                    continue
                raw = {k.casefold(): m['target'] for k, m in mapping['mappings'].items()}
                if str(literal).casefold() in raw:
                    grounded.append(raw[str(literal).casefold()]);grounding.append({'literal': literal, 'status_label': grounded[-1]});continue
                issues.append({'code': 'AMBIGUOUS_LITERAL', 'column': column, 'literal': literal,
                               'allowed_status_labels': sorted(labels), 'unmapped_raw_values': mapping['unresolved'][:20]})
                continue
            result = toolbox.call('resolve_value', {'table': view, 'column': column, 'literal': literal})
            grounding.append({'view': view, 'column': column, 'literal': literal, 'result': result})
            if result.get('status') == 'exact':
                grounded.append(literal)
            elif result.get('status') == 'normalized_unique':
                grounded.append(result['value'])
            elif result.get('status') in {'not_found_complete'}:
                issues.append({'code': 'LITERAL_NOT_FOUND', 'column': column, 'literal': literal,
                               'note': 'all distinct values scanned; no exact/normalized match'})
            else:
                issues.append({'code': 'AMBIGUOUS_LITERAL', 'column': column, 'literal': literal,
                               'candidates': result.get('candidates', []), 'status': result.get('status')})
        if len(grounded) == len(values):
            f['value'] = grounded if isinstance(f['value'], list) else grounded[0]
    for stage in plan['stages']:
        for j in stage['joins']:
            if j.get('relation') is None:
                left = [p[0].split('.')[-1] for p in j['on']];right = [p[1].split('.')[-1] for p in j['on']]
                profile = toolbox.call('profile_keys_and_joins', {'table': stage['from'], 'columns': left,
                                                                  'target_table': j['table'], 'target_columns': right})
                j['relation'] = {'source': 'profile_tool', **(profile.get('join') or {})}
                if profile.get('status') == 'error' or not profile.get('join'):
                    issues.append({'code': 'PLAN_JOIN_UNVERIFIED', 'join': j['table'], 'error': profile.get('error')})
                elif profile['join']['fanout_risk'] and any(a['fn'] in {'sum', 'count', 'avg'} for a in stage['aggregates']):
                    issues.append({'code': 'PLAN_FANOUT_RISK', 'join': j['table'], 'profile': profile['join']})
    return issues


def _round(value, places):
    return Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _result_answer(question_id, result, plan, analysis, gate, trace):
    kind = plan['answer']['type'];rows = result['rows'];columns = result['columns']
    if result['truncated']:
        raise ValueError('result exceeded the row cap')
    if kind in {'list', 'table'}:
        if kind == 'list' and len(columns) != 1:
            raise ValueError('list answers need exactly one column')
        return format_answer(question_id, kind, result)
    if len(columns) != 1 or len(rows) != 1:
        raise ValueError(f'scalar answer needs one row and one column, got {len(rows)}x{len(columns)}')
    value = rows[0][0]
    if value is None:
        refs = [r for item in plan['output']['select'] for r in item['expr'].split() if '.' in r]
        aggregates = {f"{s['id']}.{a['name']}": a['fn'] for s in plan['stages'] for a in s['aggregates']}
        fns = {aggregates.get(r.strip('()')) for r in refs}
        if fns and fns <= {'sum', 'count', 'count_distinct'} and gate['quality']['decision'] != 'block':
            trace['empty_population'] = 'sum/count over no matching rows within data coverage -> 0'
            value = 0
        else:
            raise ValueError('NULL scalar result')
    if kind in {'number', 'percent'}:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError('numeric answer type needs a numeric value')
        places = plan['output']['round']
        words = set(re.split(r'[^a-z0-9]+', fold(str(analysis['metric'].get('concept', ''))+' '+str(analysis['metric'].get('phrase', '')))))
        if kind == 'percent':
            places = 2 if places is None else places
        elif places is None and Decimal(value) != Decimal(value).to_integral_value():
            # Rulebook §9: money (incl. averages of money) to whole đồng; other averages 2 decimals.
            places = 0 if words & MONEY_WORDS else 2
        if places is not None:
            value = _round(value, places)
            value = int(value) if places == 0 else value
            trace['rounding'] = {'places': places, 'rule': 'rulebook §9: money to đồng; percent/averages 2 decimals'}
    if kind == 'date' and isinstance(value, str) and len(value) not in (7, 10):
        raise ValueError('date answer must be YYYY-MM-DD or YYYY-MM')
    return format_answer(question_id, kind, {'columns': columns, 'rows': [[value]], 'truncated': False})


def file_sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run_batch(requests, *, model, executor, loader, output_dir, config, external_startup_ms=0):
    output = Path(output_dir);output.mkdir(parents=True, exist_ok=False)
    started = perf_counter();outcomes = Counter();reasons = Counter();resolvers = {}
    manifest = {'status': 'running', 'started_at_utc': datetime.now(timezone.utc).isoformat(),
                'question_ids': [r['id'] for r in requests], 'questions': len(requests),
                'business_ids': sorted({r['business_id'] for r in requests}), 'config': config,
                'prompt_version': VERSION+'/'+PROMPT_VERSION, 'model': model.metadata(), 'labels_read': False,
                'budgets_per_question': {k: config[k] for k in ('max_model_calls', 'max_repair_rounds', 'max_tool_calls',
                                                                'max_answer_executions', 'max_bind_calls')},
                'prepared_root': str(loader.prepared_root), 'source_root': str(loader.source_root),
                'code_sha256': {p.name: file_sha(p) for p in Path(__file__).parent.glob('*.py')},
                'published_manifests': {b: loader.load(b)['manifest_sha256'] for b in sorted({r['business_id'] for r in requests})}}
    (output/'questions.jsonl').write_text(''.join(dumps(r)+'\n' for r in requests), encoding='utf-8')
    processed = 0
    try:
        model.load()
        manifest['model_load_ms'] = getattr(model, 'load_ms', None)
        manifest['startup_ms'] = round(external_startup_ms+(perf_counter()-started)*1000, 3)
        with (output/'predictions.jsonl').open('w', encoding='utf-8') as predictions, \
                (output/'timings.jsonl').open('w') as timings, (output/'traces.jsonl').open('w', encoding='utf-8') as traces:
            for request in requests:
                begin = perf_counter()
                trace = {'id': request['id'], 'business_id': request['business_id'], 'request': request, 'raw_output': None,
                         'parsed_output': None, 'json_valid': False, 'contract_valid': False, 'sql_valid': None,
                         'sql_executed': False, 'plan': None, 'query_result': None, 'error': None, 'timing_ms': {}}
                try:
                    validate_request(request)
                    context = loader.load(request['business_id']);loader.verify_pinned(request['business_id'])
                    trace['context_fingerprint'] = context['fingerprint'];trace['semantic_revision'] = context['semantic_revision']
                    resolver = resolvers.get(request['business_id'])
                    if resolver is None:
                        resolver = resolvers[request['business_id']] = TimeResolver(
                            context['calendar_path'], rule_refs={'time': context['rules_detected'].get('records_after_today_not_happened')})
                    toolbox = ToolBox(context, executor, caps=config, resolver=resolver, trace=trace)
                    answer = answer_question(request, context=context, model=model, toolbox=toolbox, resolver=resolver,
                                             config=config, trace=trace)
                except ValueError as exc:
                    reason = 'CONTEXT_CHANGED' if 'changed' in str(exc) else 'CONTEXT_ERROR'
                    trace['decision'] = {'type': 'abstain', 'reason': reason, 'detail': str(exc)}
                    answer = {'id': request['id'], 'type': 'abstain', 'answer': None, 'reason': reason}
                decision = trace.get('decision') or {}
                if answer['type'] == 'abstain':
                    trace['error'] = {'category': decision.get('reason') or answer.get('reason'),
                                      'message': json.dumps(decision.get('detail'), ensure_ascii=False, default=str)[:2000]}
                    reasons[trace['error']['category']] += 1
                trace['answer'] = answer;elapsed = round((perf_counter()-begin)*1000, 3);trace['elapsed_ms'] = elapsed
                predictions.write(dumps(answer)+'\n');predictions.flush()
                timings.write(dumps({'id': request['id'], 'elapsed_ms': elapsed})+'\n');timings.flush()
                traces.write(json.dumps(trace, ensure_ascii=False, default=_default)+'\n');traces.flush()
                processed += 1;outcomes[answer['type']] += 1
                print(dumps({'processed': processed, 'total': len(requests), 'id': request['id'], 'type': answer['type'],
                             'reason': answer.get('reason'), 'elapsed_ms': elapsed}), flush=True)
        manifest['status'] = 'complete'
    except KeyboardInterrupt:
        manifest.update(status='interrupted', fatal_error_type='KeyboardInterrupt');raise
    except Exception as exc:
        manifest.update(status='failed', fatal_error_type=type(exc).__name__);raise
    finally:
        manifest.update(processed=processed, outcomes=dict(outcomes), errors=dict(reasons),
                        elapsed_ms=round(external_startup_ms+(perf_counter()-started)*1000, 3),
                        inference_wall_ms=round((perf_counter()-started)*1000, 3))
        (output/'run.json').write_text(json.dumps(manifest, ensure_ascii=False, default=_default)+'\n', encoding='utf-8')
    return manifest


def _default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    if callable(value):
        return None
    return str(value)
