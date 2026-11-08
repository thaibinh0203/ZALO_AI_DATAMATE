"""Business metadata + answer-oriented question context for baseline v2."""
import hashlib
import json
from pathlib import Path
from .context import ContextLoader, SYSTEM, sha

PROMPT_VERSION = 'baseline-grounded-v2'
SQL_SYSTEM = SYSTEM + '''
This is the grounded baseline. The original question and source rules take
precedence over candidate interpretations. Use structured_question.answer to
determine the FINAL projection and type, and schema_linking for exact identifiers.
Copy supplied table names exactly, including suffixes. Never invent aliases or
use an alias before declaring it. as_of_date is an application input, NOT a SQL
column. Use concrete date literals, never a bare as_of_date identifier.
Use the supplied time ranges when consistent with the question and rules.
Metadata contains CANDIDATES: consider evidence, measure scope and grouping keys.
heuristic_role_not_grain is a coarse legacy label, not a reliable business grain.
Compute per-order metrics per order before averaging. Daily revenue requires SUM
of valid sale lines per day, not MAX of individual line amounts. A delivery number
is not globally unique; group delivery events per order and delivery number.
Avoid joining two many-side tables before aggregating: this multiplies amounts.
The normalized status values are cancelled/returned/completed/delivering, not the
raw Vietnamese labels in README. Preserve unmapped statuses as uncertain.
For final type list, SELECT ONLY requested values (e.g. names); rank quantities
can live in a CTE or ORDER BY but must NOT be projected into the final list.
For number/string/date/percent/boolean, final SELECT MUST have exactly ONE column
and return ONE row. Month answers must be an ISO YYYY-MM string in that column.
Do not silently turn SQL NULL into zero. No result can indicate a wrong binding.
Only source-backed missing-data decisions from schema_linking may yield
unanswerable; otherwise use abstain for uncertainty or unsupported capability.
Output only the same four-key JSON SQL plan contract, no intermediate reasoning.
'''


class SemanticContextLoader(ContextLoader):
    def __init__(self, prepared_root, source_root, metadata_root=None):
        super().__init__(prepared_root,source_root)
        self.metadata_root=Path(metadata_root).resolve() if metadata_root else None

    def load(self,business_id):
        context=super().load(business_id)
        if 'business_metadata' in context:return context
        directory=self.metadata_root/business_id if self.metadata_root else self.prepared_root/business_id/'business_metadata'
        path=directory/'metadata.json';metadata=json.loads(path.read_text())
        if metadata.get('version')!='business-metadata-v1' or metadata['business_id']!=business_id or metadata['catalog_sha256']!=sha(context['catalog_path']) or metadata['readme_sha256']!=sha(self.source_root/'data'/business_id/'README.md') or metadata['rulebook_sha256']!=self.rules_sha:
            raise ValueError('Business metadata source/catalog mismatch')
        catalog=json.loads(Path(context['catalog_path']).read_text())
        for t in context['schema']:
            original=next(x for x in catalog['tables'].values() if x['clean_view']==t['table'])
            by_name={c['name']:c for c in original['columns']}
            for c in t['columns']:
                source=by_name.get(c['sql'],{})
                c['canonical_role']=source.get('canonical_role')
                c['semantic_role']=source.get('semantic_role')
                proposed=source.get('semantic',{}).get('normalized_proposal') or source.get('semantic',{}).get('proposed') or {}
                c['meaning']=source.get('description') or proposed.get('meaning')
                c['unit']=proposed.get('unit')
                c['derived_role']=c.get('role') if c['sql'] in {'__quantity_base','__unit_base'} else None
            if t['table'] not in metadata['tables']:raise ValueError('Missing table business metadata')
            t['grain']=metadata['tables'][t['table']]['grain']
        context['business_metadata']=metadata
        context['declared_coverage_end']=catalog.get('declared_coverage_end')
        names={t['name']:t['clean_view'] for t in catalog['tables'].values()}
        context['relationship_candidates']=[{**r,'source':names.get(r['source'],r['source']),
            'target':names.get(r['target'],r['target'])} for r in metadata['existing_relationship_candidates']]
        context['fingerprint']=hashlib.sha256((context['fingerprint']+sha(path)+PROMPT_VERSION).encode()).hexdigest()
        return context

    def grounding_payload(self,context):
        # All tables are available for linking. No raw rows, traces or QA labels.
        schema=[]
        fields=['sql','source','type','canonical_role','semantic_role','meaning','has_data','unit','derived_role']
        for t in context['schema']:
            schema.append({'table':t['table'],'heuristic_role_not_grain':t['role'],
                'data_quality':t['quality'],
                'columns':[[c.get(k) for k in fields] for c in t['columns']]})
        def compact(value):
            if isinstance(value,list):return [compact(v) for v in value]
            if isinstance(value,dict):return {k:compact(v) for k,v in value.items() if k!='note'}
            return value
        detailed=compact(context['business_metadata']['tables'])
        summaries={}
        for name,t in detailed.items():
            summary={'grain':t['grain'],'group_keys':t['group_keys'],'measures':t['measures'],'joins':[]}
            for j in t['joins']:
                checks=j.get('data_checks',{})
                summary['joins'].append({**j,'data_checks':{k:checks[k] for k in
                    ['status','cardinality','orphan_source_rows','fanout_risk','business_verified'] if k in checks}})
            # Exact evidence quotes are already present in the complete README.
            # Preserve verification status; detailed linked metadata is sent to SQL.
            def evidence_status(value):
                if isinstance(value,list):return [evidence_status(x) for x in value]
                if isinstance(value,dict):return {k:({'status':v['status']} if k=='evidence' and isinstance(v,dict) else evidence_status(v)) for k,v in value.items()}
                return value
            summaries[name]=evidence_status(summary)
        return {'schema':schema,'columns_format':fields,'business_metadata':summaries,
                'declared_coverage_end':context.get('declared_coverage_end'),
                'normalization':{'status_values':['completed','cancelled','returned','delivering'],
                    'unknown_status_values_preserved':True,'clean_views_exclude_export_duplicates_and_non_data_rows':True},
                'metadata_policy':'All table business summaries for linking; exact evidence in README and metadata review. Full linked metadata follows in SQL stage.',
                'relationship_candidates':context['relationship_candidates'],
                'readme':context['readme'],'rulebook':context['rulebook']}

    def messages_for_plan(self,context,request,analysis,links):
        referenced={r['table'] for name in ['answer_columns','required_columns','group_by'] for r in links[name]}
        for j in links['joins']:referenced.update([j['source'],j['target']])
        # Deterministic selection is explicit in trace; complete schema remains
        # visible, detailed business metadata is attached for all linked tables.
        full=self.grounding_payload(context)
        selected={t:m for t,m in context['business_metadata']['tables'].items() if t in referenced}
        payload={'business_id':context['business_id'],'schema':full['schema'],'columns_format':full['columns_format'],
            'declared_coverage_end':full['declared_coverage_end'],'normalization':full['normalization'],
            'business_metadata':selected,'metadata_selection':{'selected':sorted(selected),
                'other_tables':sorted(set(full['business_metadata'])-referenced),
                'policy':'All schema exposed; detailed metadata for explicitly linked tables.'},
            'relationship_candidates':full['relationship_candidates'],
            'readme':context['readme'],'rulebook':context['rulebook'],'request':request,
            'structured_question':analysis,'schema_linking':links,
            'capabilities':{'calendar_lookup':False,'sql_repairs':0,'sql_dialect':'postgres'}}
        return [{'role':'system','content':SQL_SYSTEM},{'role':'user','content':json.dumps(payload,ensure_ascii=False,separators=(',',':'))}]
