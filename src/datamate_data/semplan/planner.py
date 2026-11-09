"""P3-P6: draft the business semantic plan, check it, refine once, freeze decisions.

Order per table: row candidate review -> column semantics (v4 gates) -> recompute
checks -> table plan -> checks -> at most one table refinement -> status mappings ->
policy decisions. Every model output is validated; every acceptance cites checks.
Without a model (semantic_mode none) candidates stay unknown, unmapped columns stay
text and grain stays unknown; deterministic policies are still evaluated on data.
"""
from collections import Counter
from copy import deepcopy
import hashlib

from ..normalization import ALIASES, NUMERIC, key
from ..parsers import dumps
from .. import semantic as v4
from .checks import TableState, compute_checks, join_check
from .contracts import (ContractError, EvidenceCatalog, validate_row_review, validate_status_review,
                        validate_table_plan)
from .model import ColumnModelAdapter
from .policies import evaluate_base_quantity, evaluate_duplicates, evaluate_fill_forward, policy_record
from .prompts import REFINEMENT_INSTRUCTION, ROW_REVIEW_SYSTEM, STATUS_SYSTEM, TABLE_PLAN_SYSTEM
from .rows import candidate_groups
from .status import build_mapping, infer_domain, unknown_values
from .temporal import infer_day_month_order, readme_day_month_order

ORDER_LEVEL_ROLES = ('customer_id', 'customer_name', 'date', 'timestamp', 'status', 'store_id')
KEY_ROLES = ('order_id', 'product_id', 'customer_id', 'purchase_id', 'lot_id', 'receipt_id')


def _roles(columns):
    counts = Counter(c['role'] for c in columns)
    return {c['role']: c['name'] for c in columns if counts[c['role']] == 1}


def _strip_check(check):
    return {k: v for k, v in check.items() if not k.startswith('_')}


class Planner:
    def __init__(self, business_id, root, staged, *, readme, rulebook, rules, model, config, replay=None,
                 log=None):
        self.business_id = business_id;self.root = root;self.staged = staged
        self.readme = readme;self.rulebook = rulebook;self.rules = rules;self.model = model
        self.config = config;self.replay = replay;self.log = log or (lambda *_: None)
        self.catalog = EvidenceCatalog.from_sources(readme, rulebook)
        self.states = {n: TableState(t, root, staged['decisions'][n]) for n, t in staged['tables'].items()}
        self.checks = {};self.plans = {};self.events = []
        self.readme_order = readme_day_month_order(readme)

    # -- helpers -----------------------------------------------------------------
    def run_checks(self, state, requests):
        results = compute_checks(state, requests)
        for r in results:
            self.checks[r['check_id']] = r
        return results

    def add_check_evidence(self, check):
        return self.catalog.add('C'+check['check_id'][1:9], 'check', dumps(_strip_check(check)['result'])[:400],
                                location=check['check_id'])

    def event(self, kind, **detail):
        self.events.append({'event': kind, **detail})

    def selected_rule_units(self, state, extra_terms=()):
        """Question-independent selection: detected policy rules + term-overlap units."""
        wanted = set(self.rules.values())
        terms = set()
        for c in state.columns:
            terms |= set(key(w) for w in (c['original_name']+' '+(c.get('description') or '')).split())
        terms |= set(extra_terms);terms -= {'', 'ma', 'ten', 'cua', 'va', 'la'}
        scored = []
        for entry in self.catalog.entries.values():
            if entry['source'] != 'rulebook' or entry['id'] in wanted:
                continue
            words = set(key(w) for w in entry['quote'].split())
            score = len(terms & words)
            if score:
                scored.append((score, entry['id']))
        budget = self.config['rule_context_chars'];chosen = list(wanted);size = sum(len(self.catalog.get(i)['quote']) for i in wanted)
        for _, ref in sorted(scored, reverse=True):
            text = self.catalog.get(ref)['quote']
            if size+len(text) > budget:
                continue
            chosen.append(ref);size += len(text)
        return sorted(chosen, key=lambda r: int(r[1:]))

    # -- P2 ------------------------------------------------------------------------
    def date_orders(self, state):
        dated = [c for c in state.columns if c['sql_type'] in {'DATE', 'TIMESTAMP'}]
        if not dated:
            return
        # One streaming pass for all date columns of this revision.
        checks = self.run_checks(state, [{'kind': 'date_format', 'column': c['name']} for c in dated])
        for column, check in zip(dated, checks):
            decision = infer_day_month_order(check['result']['patterns'], self.readme_order)
            if column.get('date_order') != decision['order']:
                column['date_order'] = decision['order']
                column['date_order_evidence'] = {'basis': decision['basis'], 'check_id': check['check_id']}

    # -- P3a row candidates -----------------------------------------------------------
    def review_rows(self, state):
        candidates = [d for d in state.decisions.values() if d['decision_status'] == 'candidate']
        if not candidates:
            return []
        wanted = {d['row_id'] for d in candidates};rows = []
        for record in state.records():
            if record['row_id'] in wanted:
                rows.append({'row_id': record['row_id'], 'raw': record['raw'], 'source': record['source'],
                             'decision': state.decisions[record['row_id']]})
        groups, overflow = candidate_groups(rows, state.columns, max_groups=self.config['row_review_max_groups'],
                                            samples_per_group=self.config['row_review_samples_per_group'],
                                            ref_prefix=f'S{state.name[:6]}_')
        for g in groups:
            for s in g['samples']:
                self.catalog.add(s['ref'], 'source_row', dumps(s['values']), location=s['row_id'])
        decided = {};rejected = [];attempts = []
        if self.replay:
            decided = self.replay['tables'].get(state.name, {}).get('row_review', {}).get('decided', {})
        elif self.model and groups:
            payload = {'task': 'row_review', 'table': state.name,
                       'columns': [[c['original_name'], c['role']] for c in state.columns],
                       'decided_row_kinds': dict(Counter(d['kind'] for d in state.decisions.values() if d['decision_status'] == 'decided_rule')),
                       'groups': [{k: g[k] for k in ('group_id', 'candidate_kind', 'rule', 'label', 'rows')} |
                                  {'samples': [{'ref': s['ref'], 'source_row': s['source']['row'], 'values': s['values']} for s in g['samples']]}
                                  for g in groups],
                       'evidence_catalog': self.catalog.view(sources={'readme'})+
                       self.catalog.view(ids=set(self.selected_rule_units(state))),
                       'kinds': ['data', 'subtotal', 'grand_total', 'repeated_header', 'note', 'metadata', 'unknown']}
            holder = {}

            def validator(value):
                holder['result'] = validate_row_review(value, groups, self.catalog)
                return value
            value, attempts = self.model.ask('row_review', state.name, ROW_REVIEW_SYSTEM, payload, validator=validator)
            if value is not None:
                decided, rejected = holder['result']
        applied = Counter()
        for group in groups:
            decision = decided.get(group['group_id'])
            for row_id in group['row_ids']:
                d = state.decisions[row_id]
                if decision is None:
                    d.update(kind='unknown', decision_status='needs_review', method='unreviewed_candidate')
                    continue
                kind = decision['exceptions'].get(row_id, decision['kind'])
                if kind == 'data' and d.get('candidate_kind') in {'subtotal', 'grand_total'} and d['rule'] == 'unlabeled_numeric_row':
                    # Data without any identity/text cell contradicts structure: keep unknown.
                    d.update(kind='unknown', decision_status='needs_review', method='model_decision_rejected_by_structure')
                    continue
                d.update(kind=kind, decision_status='decided_model_validated' if kind != 'unknown' else 'needs_review',
                         method='model_group_review'+('_replayed' if self.replay else ''),
                         evidence_refs=decision['evidence_refs'], group_id=group['group_id'])
                applied[kind] += 1
        for row in overflow:
            state.decisions[row['row_id']].update(kind='unknown', decision_status='needs_review', method='review_group_cap_exceeded')
        self.event('row_review', table=state.name, groups=len(groups), overflow_rows=len(overflow),
                   applied=dict(applied), rejected=rejected, model_attempts=len(attempts))
        return [{'group_id': g['group_id'], 'rows': g['rows'], 'candidate_kind': g['candidate_kind'], 'rule': g['rule'],
                 'decision': decided.get(g['group_id']), 'sample_refs': [s['ref'] for s in g['samples']]} for g in groups]

    # -- P3b column semantics (v4 contract and gates) -------------------------------------
    def column_semantics(self, state, profiles):
        columns = state.columns
        targets = [c['original_name'] for c in columns if c['role'] == 'text' and c['evidence'] == 'preserved_text']
        reviewed_by_name = {}
        if self.replay:
            stored = {c['original_name']: c for c in self.replay['tables'].get(state.name, {}).get('columns', [])}
        proposed = {};proposed_refs = {};target_context = {};errors = []
        inventory = [{'table': n, 'columns': [c['original_name'] for c in s.columns]} for n, s in self.states.items()]
        if targets and self.model and not self.replay:
            for offset in range(0, len(targets), self.config['column_chunk_size']):
                chunk = targets[offset:offset+self.config['column_chunk_size']]
                selected_rules = v4.relevant_rules(self.rulebook, [c for c in columns if c['original_name'] in chunk])
                evidence = [{'id': f'H{i}', 'source': 'header', 'quote': col, 'location': col} for i, col in enumerate(chunk)]
                evidence += [{'id': f'R{i}', 'source': 'readme', 'quote': line, 'location': f'README.md:{i+1}'}
                             for i, line in enumerate(self.readme.splitlines()) if line.strip()]
                evidence += [{'id': f'B{i}', 'source': 'rulebook', 'quote': p, 'location': f'selected_rule_paragraph:{i}'}
                             for i, p in enumerate(selected_rules.split('\n\n')) if p.strip()]
                references = {e['id']: e for e in evidence}
                payload = {'task': 'columns', 'source_table': state.name, 'roles': sorted(ALIASES),
                           'role_definitions': v4.ROLE_DEFINITIONS,
                           'canonical_aliases': {r: a.split('|')[:3] for r, a in ALIASES.items()},
                           'alias_scope': 'First three name examples per canonical role; match meaning, not spelling only.',
                           'target_columns': chunk,
                           'target_descriptions': {c['original_name']: c['description'] for c in columns if c['original_name'] in chunk},
                           'context_columns': [{'original_name': c['original_name'], 'role': c['role'], 'sql_type': c['sql_type'],
                                                'description': c['description'], 'samples': profiles[c['name']]['samples'][:2]}
                                               for c in columns if c['original_name'] not in chunk],
                           'profiles': [profiles[c['name']] for c in columns if c['original_name'] in chunk],
                           'related_tables': inventory, 'readme': 'All README lines are in R entries.',
                           'rulebook': 'Selected rule paragraphs are in B entries.', 'evidence_catalog': evidence,
                           'profile_scope': 'provisional data rows of the current revision'}
                for target in chunk:
                    target_context[target] = (payload, references)
                holder = {}

                def validator(value, chunk=chunk, references=references, holder=holder):
                    if set(value) != {'columns', 'grain', 'relationships', 'handling'} or not isinstance(value['columns'], list):
                        raise ContractError('Invalid semantic response contract')
                    part = {};refs = {}
                    for entry in value['columns']:
                        if not isinstance(entry, list) or len(entry) != 7:
                            raise ContractError('Expected seven-field compact proposal')
                        name, role, semantic_role, typ, meaning, unit, ref = entry
                        role = 'other' if role is None else role
                        if not isinstance(name, str) or name not in chunk or name in part:
                            raise ContractError('Unknown or duplicate proposed column')
                        if not isinstance(ref, str) or ref not in references:
                            raise ContractError('Unknown evidence reference')
                        refs[name] = references[ref]
                        part[name] = {'column': name, 'role': 'text' if role == 'unresolved' else role,
                                      'semantic_role': semantic_role, 'sql_type': typ, 'meaning': meaning or '',
                                      'unit': unit, 'handling': value['handling'] if isinstance(value['handling'], str) else '',
                                      'status': 'unresolved' if role == 'unresolved' else 'proposed',
                                      'evidence': [{'source': references[ref]['source'], 'quote': references[ref]['quote']}]}
                    if set(part) != set(chunk):
                        raise ContractError('Missing column proposals')
                    holder['part'] = part;holder['refs'] = refs
                    return value
                value, attempts = self.model.ask('column_semantics', f'{state.name}:{offset}', v4.SYSTEM, payload,
                                                 validator=validator)
                if value is None:
                    errors.append(attempts[-1].get('error') or attempts[-1].get('budget_exhausted'))
                else:
                    proposed.update(holder['part']);proposed_refs.update(holder['refs'])
        reviewed = []
        for column in columns:
            name = column['original_name'];profile = profiles[column['name']]
            if name in targets and self.replay:
                spec = deepcopy(stored.get(name)) or {**column, 'semantic': {'status': 'unresolved', 'checks': ['replay_missing']}}
                spec['source_profile'] = profile
            elif name in targets:
                payload, references = target_context.get(name, (None, None))
                if payload is None:
                    spec = v4.validate_column(column, None, profile, self.readme, self.rulebook)
                    spec['semantic']['model_error'] = '; '.join(e for e in errors if e) or 'semantic_mode_none'
                else:
                    adapter = ColumnModelAdapter(self.model, v4.SYSTEM, f'{state.name}:{name}')
                    spec = v4.refine_column(column, proposed.get(name), profile, self.readme, self.rulebook,
                                            model=adapter, payload=payload, references=references, audit_path=None)
                    if name in proposed_refs:
                        spec['semantic'].setdefault('evidence_reference', proposed_refs[name])
            else:
                spec = {**column, 'source_profile': profile, 'semantic': {
                    'status': 'existing_mapping', 'proposed': None, 'checks': ['alias_readme_or_declared_schema'],
                    'semantic_certainty': 'deterministic_mapping_not_business_proof'}}
            reviewed.append(spec)
        if not self.replay:
            reviewed = v4.guard_refinement_collisions(reviewed, columns, self.readme, self.rulebook)
        for spec, base in zip(reviewed, columns):
            spec.setdefault('date_order', base.get('date_order'))
            v4.attach_roles(spec)
            reviewed_by_name[spec['name']] = spec['semantic']['status']
        state.columns = reviewed
        self.event('column_semantics', table=state.name, targets=len(targets), statuses=dict(Counter(reviewed_by_name.values())),
                   model_errors=[e for e in errors if e])

    # -- P3c/P4/P5 table plan -------------------------------------------------------------
    def grouped_samples(self, state, key_columns, *, groups=2, rows_per_group=4, max_tracked=400):
        """Rows sharing a candidate key (bounded scan; checks still use all rows)."""
        index = [state.index(c) for c in key_columns];tracked = {};singles = []
        for scanned, record in enumerate(state.records()):
            if scanned >= self.config.get('sample_scan_rows', 50000):
                break
            if state.kind(record) != 'data':
                continue
            group = tuple(record['raw'][i] for i in index)
            if any(v is None or (isinstance(v, str) and not v.strip()) for v in group):
                continue
            if group in tracked:
                if len(tracked[group]) < rows_per_group:
                    tracked[group].append(record)
            elif len(tracked) < max_tracked:
                tracked[group] = [record]
            if len(singles) < 4 and len(tracked) % 37 == 1:
                singles.append(record)
        multi = sorted((g for g, rows in tracked.items() if len(rows) > 1),
                       key=lambda g: hashlib.sha256(dumps(list(g)).encode()).hexdigest())[:groups]
        out = []
        for g in multi:
            out.append({'key_columns': key_columns, 'rows': [self._sample(state, r) for r in tracked[g]]})
        return out, [self._sample(state, r) for r in singles]

    def _sample(self, state, record):
        ref = f'S{record["row_id"][:10]}'
        values = {c['name']: (None if v is None or (isinstance(v, str) and not v.strip()) else str(v)[:self.config['sample_value_chars']])
                  for c, v in zip(state.columns, record['raw'])}
        self.catalog.add(ref, 'source_row', dumps(values), location=record['row_id'])
        return {'ref': ref, 'values': values}

    def candidate_key_sets(self, state):
        roles = _roles(state.columns);sets = []
        for role in KEY_ROLES:
            if role in roles:
                sets.append([roles[role]])
        if 'store_id' in roles and 'order_id' in roles:
            sets.append([roles['store_id'], roles['order_id']])
        return sets

    def base_checks(self, state):
        roles = _roles(state.columns);requests = [{'kind': 'column_profile'}]
        for keys in self.candidate_key_sets(state):
            requests.append({'kind': 'key_stats', 'columns': keys})
        numeric = [c['name'] for c in state.columns if c['sql_type'] == 'NUMERIC' and c['role'] in NUMERIC]
        if 'order_id' in roles:
            for col in numeric[:12]:
                requests.append({'kind': 'measure_repetition', 'column': col, 'group': [roles['order_id']]})
        for c in state.columns:
            if c['role'] == 'status':
                requests.append({'kind': 'status_vocabulary', 'column': c['name']})
        return self.run_checks(state, requests)

    def table_payload(self, state, base, extra=None):
        profile = base[0]['result']['columns']
        key_sets = self.candidate_key_sets(state)
        grouped, singles = self.grouped_samples(state, key_sets[0]) if key_sets else ([], self._plain_samples(state))
        summaries = []
        for check in base[1:]:
            ref = self.add_check_evidence(check)
            summaries.append({'id': ref, 'kind': check['kind'], 'request': check['request'], 'result': check['result']})
        payload = {'task': 'table_plan', 'table': state.name, 'logical_name': state.staged['logical_name'],
                   'table_role_hint_not_grain': state.staged['table_role_hint'],
                   'source_parts': [{'file': p['source_file'], 'sheet': p['source_table']} for p in state.parts][:6],
                   'business_tables': {n: [c['name'] for c in s.columns] for n, s in self.states.items()},
                   'target_columns': [{'name': c['name'], 'source': c['original_name'], 'type': c['sql_type'],
                                       'canonical_role': c.get('canonical_role'), 'semantic_role': c.get('semantic_role'),
                                       'meaning': c.get('description') or ((c.get('semantic') or {}).get('normalized_proposal') or {}).get('meaning'),
                                       'non_empty': profile[c['name']]['non_empty'],
                                       'distinct_lower_bound': profile[c['name']]['distinct_lower_bound'],
                                       'samples': profile[c['name']]['samples'][:2]} for c in state.columns],
                   'row_kinds': dict(Counter(d['kind'] for d in state.decisions.values())),
                   'grouped_samples': grouped, 'single_samples': singles, 'checks': summaries,
                   'evidence_catalog': self.catalog.view(sources={'readme'})+self.catalog.view(ids=set(self.selected_rule_units(state))),
                   'note': 'Samples are small; check results are computed over all provisional data rows.'}
        if extra:
            payload.update(extra)
        return payload

    def _plain_samples(self, state, limit=6):
        out = []
        for record in state.records():
            if state.kind(record) == 'data':
                out.append(self._sample(state, record))
                if len(out) >= limit:
                    break
        return out

    @staticmethod
    def compact(payload):
        reduced = dict(payload)
        reduced['grouped_samples'] = [{**g, 'rows': g['rows'][:2]} for g in payload.get('grouped_samples', [])[:1]]
        reduced['single_samples'] = payload.get('single_samples', [])[:2]
        reduced['business_tables'] = sorted(payload.get('business_tables', {}))
        reduced['context_compaction'] = 'Reduced optional samples and inventory columns to fit token budget; checks and evidence kept.'
        return reduced

    def table_plan(self, state, base):
        tables = {n: {'name': n, 'columns': s.columns} for n, s in self.states.items()}
        table = {'name': state.name, 'columns': state.columns}
        if self.replay:
            stored = self.replay['tables'].get(state.name, {}).get('table_proposal')
            return (deepcopy(stored) if stored else None), []
        if not self.model:
            return None, []
        holder = {}

        def validator(value):
            holder['plan'] = validate_table_plan(value, table, self.catalog, tables)
            return value
        payload = self.table_payload(state, base)
        value, attempts = self.model.ask('table_plan', state.name, TABLE_PLAN_SYSTEM, payload,
                                         validator=validator, compact=self.compact)
        return (holder['plan'] if value is not None else None), attempts

    def proposal_checks(self, state, proposal):
        """Checks for every proposal item; returns (failed list, checks by item)."""
        roles = _roles(state.columns);requests = [];failed = [];by_item = {}
        identity = proposal['grain']['identity_columns']
        if identity:
            requests.append({'kind': 'key_stats', 'columns': identity})
        for k in proposal['group_keys']:
            requests.append({'kind': 'key_stats', 'columns': k['columns']})
        keys = {k['name']: k['columns'] for k in proposal['group_keys']}
        order_key = [roles['order_id']] if 'order_id' in roles else None
        for m in proposal['measures']:
            if m['column'] == '__quantity_base':
                continue
            group = keys.get(m['measure_scope']) or (order_key if m['measure_scope'] in {'row', 'order', 'order_line'} else None)
            if group:
                requests.append({'kind': 'measure_repetition', 'column': m['column'], 'group': group})
        for p in proposal['policies']:
            if p['operation'] == 'fill_forward':
                requests.append({'kind': 'continuation', 'columns': p['columns'], 'key_columns': p['key_columns'], 'mode': p['mode']})
        results = self.run_checks(state, requests) if requests else []
        lookup = {dumps(r['request']): r for r in results}

        def get(request):
            return lookup.get(dumps(request))
        if identity:
            c = get({'kind': 'key_stats', 'columns': identity});by_item['identity'] = c['check_id']
            if not c['result']['row_key_supported']:
                failed.append({'item': 'grain.identity_columns', 'check_id': c['check_id'], 'kind': 'identity_not_unique_or_null',
                               'detail': {k: c['result'][k] for k in ('rows', 'non_null_rows', 'distinct_groups', 'max_rows_per_group')}})
        for k in proposal['group_keys']:
            c = get({'kind': 'key_stats', 'columns': k['columns']});by_item['group_key:'+k['name']] = c['check_id']
            if c['result']['non_null_rows'] == 0:
                failed.append({'item': 'group_key:'+k['name'], 'check_id': c['check_id'], 'kind': 'group_key_always_null'})
        for m in proposal['measures']:
            group = keys.get(m['measure_scope']) or (order_key if m['measure_scope'] in {'row', 'order', 'order_line'} else None)
            if not group or m['column'] == '__quantity_base':
                continue
            c = get({'kind': 'measure_repetition', 'column': m['column'], 'group': group})
            by_item['measure:'+m['column']] = c['check_id'];r = c['result']
            if m['aggregation'] == 'sum' and m['measure_scope'] in {'row', 'order_line'} and r['groups_with_multiple_rows'] >= self.config['repetition_min_groups'] and \
                    r['groups_constant_multi_row'] >= self.config['repetition_ratio']*r['groups_with_multiple_rows']:
                failed.append({'item': 'measure:'+m['column'], 'check_id': c['check_id'], 'kind': 'row_sum_but_value_repeats_within_group', 'detail': r})
            if m['aggregation'] == 'sum_once_per_group' and r['groups_varying'] > 0:
                failed.append({'item': 'measure:'+m['column'], 'check_id': c['check_id'], 'kind': 'group_value_varies_within_group', 'detail': r})
        for p in proposal['policies']:
            if p['operation'] == 'fill_forward':
                c = get({'kind': 'continuation', 'columns': p['columns'], 'key_columns': p['key_columns'], 'mode': p['mode']})
                by_item['policy:fill_forward'] = c['check_id']
                if c['result']['conflicting_values'] or c['result']['continuation_rows'] == 0:
                    failed.append({'item': 'policy:fill_forward', 'check_id': c['check_id'], 'kind': 'fill_forward_not_supported_by_data',
                                   'detail': {k: c['result'][k] for k in ('continuation_rows', 'conflicting_values')}})
        return failed, by_item

    def refine(self, state, base, proposal, failed):
        if self.replay or not self.model or self.config['table_refinement_requests'] < 1:
            return None, []
        tables = {n: {'name': n, 'columns': s.columns} for n, s in self.states.items()}
        table = {'name': state.name, 'columns': state.columns}
        extra_samples = []
        for f in failed:
            check = self.checks.get(f['check_id'])
            if check and check['kind'] in {'key_stats', 'measure_repetition'}:
                cols = check['request'].get('columns') or check['request'].get('group')
                grouped, _ = self.grouped_samples(state, cols, groups=2, rows_per_group=4)
                extra_samples.extend(grouped)
            if check:
                self.add_check_evidence(check)
        holder = {}

        def validator(value):
            holder['plan'] = validate_table_plan(value, table, self.catalog, tables)
            return value
        previous = {k: v for k, v in proposal.items() if k != 'rejected'}
        payload = self.table_payload(state, base, extra={'refinement': {
            'previous_proposal': previous, 'rejected_items': proposal.get('rejected', []),
            'failed_checks': failed, 'extra_grouped_samples': extra_samples[:4], 'instruction': REFINEMENT_INSTRUCTION}})
        value, attempts = self.model.ask('table_refinement', state.name, TABLE_PLAN_SYSTEM, payload,
                                         validator=validator, compact=self.compact)
        return (holder['plan'] if value is not None else None), attempts

    def finalize_items(self, state, proposal, failed, by_item):
        """Remove or downgrade items whose checks still fail after refinement."""
        failed_items = {f['item']: f for f in failed}
        grain = deepcopy(proposal['grain'])
        identity = {'columns': grain['identity_columns'], 'status': 'not_proposed', 'check_id': by_item.get('identity')}
        if grain['identity_columns']:
            if 'grain.identity_columns' in failed_items:
                identity.update(status='not_established', reason=failed_items['grain.identity_columns']['kind'])
                grain['identity_columns'] = []
            else:
                identity['status'] = 'supported_by_data_check'
        grain['checks'] = [v for k, v in by_item.items() if k == 'identity']
        group_keys = []
        for k in proposal['group_keys']:
            item = 'group_key:'+k['name']
            group_keys.append({**k, 'check_id': by_item.get(item),
                               'status': 'rejected_by_checks' if item in failed_items else 'candidate_checked'})
        measures = []
        for m in proposal['measures']:
            item = 'measure:'+m['column'];entry = {**m, 'check_id': by_item.get(item)}
            if item in failed_items:
                entry.update(aggregation='unknown', status='needs_review', failed_check=failed_items[item]['kind'])
            else:
                entry['status'] = 'candidate_checked' if by_item.get(item) else 'candidate_unchecked'
            measures.append(entry)
        return grain, identity, group_keys, measures

    def unknown_proposal(self):
        return {'grain': {'kind': 'unknown', 'semantic_kind': None, 'description': '', 'identity_columns': [],
                          'evidence_refs': [], 'status': 'unresolved'},
                'group_keys': [], 'measures': [], 'joins': [], 'status_columns': [], 'policies': [], 'rejected': []}

    # -- status mappings ------------------------------------------------------------------
    def status_mappings(self, state, proposal, base):
        result = {}
        proposed = {s['column']: s for s in proposal['status_columns']}
        vocab = {c['request']['column']: c for c in base if c['kind'] == 'status_vocabulary'}
        for column in state.columns:
            if column['role'] != 'status':
                continue
            check = vocab.get(column['name']) or self.run_checks(state, [{'kind': 'status_vocabulary', 'column': column['name']}])[0]
            p = proposed.get(column['name'])
            domain, basis = infer_domain(column, grain_kind=proposal['grain']['kind'],
                                         role_hint=state.staged['table_role_hint'], proposed=p['domain'] if p else None)
            reviewed = {}
            if domain:
                unknown = unknown_values(check, domain)
                if self.replay:
                    reviewed = self.replay['tables'].get(state.name, {}).get('status_mappings', {}).get(column['name'], {}).get('reviewed', {})
                elif unknown and self.model:
                    from .contracts import STATUS_DOMAINS
                    holder = {}
                    payload = {'task': 'status_mapping', 'table': state.name, 'column': column['original_name'],
                               'domain': domain, 'target_vocabulary': list(STATUS_DOMAINS[domain]),
                               'values': [[v, n] for v, n in check['result']['values'] if v in unknown][:60],
                               'evidence_catalog': self.catalog.view(sources={'readme'})+
                               self.catalog.view(ids=set(self.selected_rule_units(state, extra_terms=['trangthai'])))}

                    def validator(value, unknown=unknown, domain=domain, holder=holder):
                        holder['r'] = validate_status_review(value, unknown, domain, self.catalog)
                        return value
                    value, _ = self.model.ask('status_mapping', f'{state.name}:{column["name"]}', STATUS_SYSTEM, payload,
                                              validator=validator)
                    if value is not None:
                        reviewed, rejected = holder['r']
                        if rejected:
                            self.event('status_mapping_rejected', table=state.name, column=column['name'], rejected=rejected)
            refs = p['evidence_refs'] if p else []
            if domain == 'order' and self.rules.get('order_status_vocabulary'):
                refs = refs+[self.rules['order_status_vocabulary']]
            mapping = build_mapping(state.name, column['name'], domain, basis, check, reviewed=reviewed, evidence_refs=refs)
            mapping['reviewed'] = reviewed
            column['status_domain'] = domain
            result[column['name']] = mapping
        return result

    # -- policies -------------------------------------------------------------------------
    def policies(self, state, proposal, has_conversions):
        roles = _roles(state.columns);out = []
        proposals = [p for p in proposal['policies']]
        if 'order_id' in roles:
            fill_cols = [roles['order_id']]+[roles[r] for r in ORDER_LEVEL_ROLES if r in roles]
            rule_ref = self.rules.get('group_blank_cells_continue')
            if not any(p['operation'] == 'fill_forward' for p in proposals) and rule_ref:
                proposals.append({'operation': 'fill_forward', 'columns': fill_cols, 'key_columns': [roles['order_id']],
                                  'mode': 'key', 'evidence_refs': [rule_ref], 'method': 'rule_detector'})
            if not any(p['operation'] == 'mark_duplicates' for p in proposals):
                proposals.append({'operation': 'mark_duplicates', 'columns': [], 'key_columns': [roles['order_id']],
                                  'mode': 'same_group_identical', 'method': 'rule_detector',
                                  'evidence_refs': [r for r in [self.rules.get('same_order_identical_rows_once')] if r]})
        else:
            proposals.append({'operation': 'mark_duplicates', 'columns': [], 'key_columns': [], 'mode': 'same_group_identical',
                              'method': 'rule_detector', 'evidence_refs': [r for r in [self.rules.get('non_order_rows_counted')] if r]})
        fills = [p for p in proposals if p['operation'] == 'fill_forward']
        dupes = [p for p in proposals if p['operation'] == 'mark_duplicates']
        # One pass: continuation evidence plus identical-row counts with and without
        # group continuation, so the duplicate decision can follow the fill decision.
        requests = [{'kind': 'continuation', 'columns': p['columns'], 'key_columns': p['key_columns'], 'mode': p['mode']} for p in fills]
        requests += [{'kind': 'identical_rows', 'key_columns': p['key_columns'], 'continuation': flag} for p in dupes for flag in (True, False)]
        results = {dumps(c['request']): c for c in self.run_checks(state, requests)} if requests else {}
        for p in fills:
            check = results[dumps({'kind': 'continuation', 'columns': p['columns'], 'key_columns': p['key_columns'], 'mode': p['mode']})]
            out.append(evaluate_fill_forward(state.name, state.columns, p, check,
                                             rule_ref=self.rules.get('group_blank_cells_continue') if p['mode'] == 'key' else None))
        enabled_fill = next((p for p in out if p['operation'] == 'fill_forward' and p['decision_status'] == 'enabled'), None)
        for p in dupes:
            flag = bool(enabled_fill and set(p['key_columns']) <= set(enabled_fill['key_columns']))
            check = results[dumps({'kind': 'identical_rows', 'key_columns': p['key_columns'], 'continuation': flag})]
            out.append(evaluate_duplicates(state.name, state.columns, p, check,
                                           rule_ref=self.rules.get('same_order_identical_rows_once'),
                                           no_order_rule_ref=self.rules.get('non_order_rows_counted')))
        out.append(evaluate_base_quantity(state.name, state.columns, conversion_sources=has_conversions,
                                          default_rule_ref=self.rules.get('base_unit_default_factor_1')))
        for column in state.columns:
            if column['role'] == 'status':
                mapping = self.status[state.name][column['name']]
                out.append(policy_record(state.name, 'apply_status_mapping', columns=[column['name']], method='code_rule',
                                         evidence_refs=mapping['domain_evidence_refs'], decision_status=mapping['status'],
                                         reason=f"domain={mapping['domain']} ({mapping['domain_basis']}); "
                                                f"{len(mapping['mappings'])} mapped, {len(mapping['unresolved'])} unresolved kept raw"))
        for part in state.parts:
            info = part['year_info']
            if info['status'] == 'none':
                continue
            out.append(policy_record(state.name, 'resolve_source_year', columns=[c['name'] for c in state.columns if c['sql_type'] in {'DATE', 'TIMESTAMP'}],
                                     method='code_rule', evidence_refs=info['evidence_refs'],
                                     decision_status='enabled' if info['status'] == 'single' else 'needs_review',
                                     reason=f"part {part['part']} ({part['source_file']}/{part['source_table']}): year={info['year']} from {info['source']}; "
                                            f"conflicts={info['conflicting_sources']}",
                                     extra={'part': part['part'], 'year_info': info}))
        return out

    # -- orchestration -------------------------------------------------------------------
    def run(self, *, readme_conversions=False):
        self.status = {}
        proposals = {};attempts_log = {}
        for name, state in self.states.items():
            self.date_orders(state)
            review = self.review_rows(state)
            profiles = self.run_checks(state, [{'kind': 'column_profile'}])[0]['result']['columns']
            self.column_semantics(state, profiles)
            self.date_orders(state)
            base = self.base_checks(state)
            proposal, attempts = self.table_plan(state, base)
            refinement = {'used': False, 'requests': 0, 'accepted': False}
            if proposal is None:
                proposal = self.unknown_proposal()
            failed, by_item = self.proposal_checks(state, proposal)
            if failed:
                refined, refine_attempts = self.refine(state, base, proposal, failed)
                refinement.update(used=bool(refine_attempts), requests=len(refine_attempts), failed_before=failed)
                if refined is not None:
                    proposal = refined;refinement['accepted'] = True
                    failed, by_item = self.proposal_checks(state, proposal)
                refinement['failed_after'] = failed
            proposals[name] = (proposal, failed, by_item, refinement, review, base)
            attempts_log[name] = attempts
        self.join_checks(proposals)
        has_conversions = readme_conversions or any(c['role'] == 'conversion_factor' for s in self.states.values() for c in s.columns)
        for name, state in self.states.items():
            proposal, failed, by_item, refinement, review, base = proposals[name]
            self.status[name] = self.status_mappings(state, proposal, base)
            grain, identity, group_keys, measures = self.finalize_items(state, proposal, failed, by_item)
            policies = self.policies(state, proposal, has_conversions)
            self.plans[name] = {'name': name, 'logical_name': state.staged['logical_name'],
                'table_role_hint': state.staged['table_role_hint'], 'columns': state.columns,
                'source_parts': [{k: v for k, v in p.items() if k != 'header_context'} for p in state.parts],
                'grain': grain, 'row_identity': identity, 'group_keys': group_keys, 'measures': measures,
                'joins': proposal['joins'], 'status_mappings': self.status[name], 'policies': policies,
                'row_decisions': {'summary': dict(Counter(d['kind'] for d in state.decisions.values())),
                                  'reviewed_groups': review},
                'row_review': {'decided': {g['group_id']: g['decision'] for g in review if g['decision']}},
                'refinement': refinement, 'rejected_items': proposal.get('rejected', []),
                'table_proposal': {k: v for k, v in proposal.items() if k != 'rejected'},
                'revision': state.revision()}
        return self.plans

    def join_checks(self, proposals):
        for name, (proposal, failed, by_item, refinement, review, base) in proposals.items():
            state = self.states[name]
            for join in proposal['joins']:
                target = self.states[join['target']]
                s = self.run_checks(state, [{'kind': 'key_values', 'columns': join['source_columns']}])[0]
                t = self.run_checks(target, [{'kind': 'key_values', 'columns': join['target_columns']}])[0]
                result = join_check(state, join['source_columns'], s['_keys'], target, join['target_columns'], t['_keys'])
                join['data_checks'] = result;join['check_ids'] = [s['check_id'], t['check_id']]
                if result['status'] == 'type_mismatch' or result.get('matched_source_keys') == 0:
                    join['status'] = 'rejected_by_checks'
                elif result['cardinality'] == 'many_to_many':
                    join['status'] = 'needs_review_fanout'
                else:
                    join['status'] = 'candidate_checked'

    def decisions_file_records(self, name):
        return sorted(self.states[name].decisions.values(), key=lambda d: d['seq'])
