"""Resolve exact and unique accent/case variants from actual DB values, not samples."""
import unicodedata
from datamate_v2.common import PipelineError
from datamate_v2.contracts import normalize_entities
import re

ENTITY_MEANINGS={'products':{'product_name'},'customers':{'customer_name'},
 'employees':{'employee_name'},'branches':{'branch_name'},'suppliers':{'supplier_name'},
 'categories':{'category','product_category'},'statuses':{'status'}}


def entity_key(value):
    value=unicodedata.normalize('NFKD',value.casefold().replace('đ','d'))
    return ' '.join(''.join(c for c in value if not unicodedata.combining(c)).split())


def resolve_entities(entities,metadata,tool,max_queries=2,*,original_question=None):
    entities=normalize_entities(entities)
    results=[];queries=0
    for kind,meanings in ENTITY_MEANINGS.items():
        values=list(dict.fromkeys(v for v in entities.get(kind,[]) if isinstance(v,str) and v.strip()))
        if not values: continue
        columns=[(t['sql_table'],c['sql_column']) for t in metadata['tables'] for c in t['columns']
                 if c['type']=='TEXT' and c.get('meaning') in meanings and c.get('semantic_status')=='verified']
        if not columns: continue
        if queries>=max_queries: raise PipelineError('Entity resolution query budget exhausted; cannot guess a canonical entity')
        domain=tool.entity_values(columns);queries+=1
        if not domain['complete']: raise PipelineError('Entity domain exceeds lookup limit; cannot resolve from partial results')
        for value in values:
            exact=[v for v in domain['values'] if v==value]
            matches=sorted(set(exact or [v for v in domain['values'] if entity_key(v)==entity_key(value)]))
            recovered=False
            if not matches and original_question:
                # Only complete DB names occurring as whole phrases in the
                # original question. Never fuzzy-rank a guessed product name.
                literal_key=lambda s:' '.join(unicodedata.normalize('NFKC',s).casefold().split())
                text=literal_key(original_question)
                matches=sorted({v for v in domain['values'] if re.search(r'(?<!\w)'+re.escape(literal_key(v))+r'(?!\w)',text)})
                if not matches:
                    text=entity_key(original_question)
                    matches=sorted({v for v in domain['values'] if re.search(r'(?<!\w)'+re.escape(entity_key(v))+r'(?!\w)',text)})
                # Do not map a malformed entity onto one of several distinct
                # names merely because that name happens to occur nearby.
                if len({entity_key(v) for v in matches})>1:
                    raise PipelineError(f'Ambiguous original-question entity phrases for {value!r}: {matches[:10]}')
                recovered=bool(matches)
            record={'kind':kind,'input':value,'status':'resolved' if len(matches)==1 else 'ambiguous' if matches else 'not_found',
                    'canonical_value':matches[0] if len(matches)==1 else None,'matches':matches[:10],
                    'columns':[{'table':t,'column':c} for t,c in columns],
                    'provenance':'complete distinct domain from read-only DB; exact match preferred, otherwise unique accent/case/whitespace match'}
            if recovered: record['recovered_from']='unique complete DB-name phrase in original question'
            results.append(record)
            if len(matches)>1: raise PipelineError(f'Ambiguous entity {value!r}: {matches[:10]}; no automatic choice')
    return results


def validate_entity_literals(statement,metadata,resolutions):
    """Reject reuse of a noncanonical resolved name; never rewrite SQL literals."""
    from sqlglot import exp
    from .sql_validation.guard import parse_readonly,_base_scopes
    tables={t['sql_table']:t for t in metadata['tables']}
    for scope,bases in _base_scopes(parse_readonly(statement),tables):
        for node in scope.expression.find_all(exp.EQ,exp.In):
            pairs=[(node.this,node.expression),(node.expression,node.this)] if isinstance(node,exp.EQ) else [(node.this,v) for v in node.expressions]
            for column,literal in pairs:
                if not isinstance(column,exp.Column) or not isinstance(literal,exp.Literal) or not literal.is_string: continue
                for entity in resolutions:
                    if entity['status']!='resolved': continue
                    locations={(item['table'],item['column']) for item in entity['columns']}
                    if (bases.get(column.table),column.name) not in locations: continue
                    canonical=entity['canonical_value']
                    if entity_key(literal.this)==entity_key(canonical) and literal.this!=canonical:
                        raise PipelineError(f'Entity literal {literal.this!r} does not use the DB value {canonical!r} for {bases[column.table]}.{column.name}; use exploration canonical_value exactly')
                    if entity.get('recovered_from') and literal.this==entity['input']:
                        raise PipelineError(f'Malformed entity {literal.this!r}; original question resolves uniquely to DB value {canonical!r}')


def compact_resolutions(resolutions,context):
    result=[]
    for record in resolutions:
        locations={(context['table_catalog'][c['table']],c['column']) for c in record['columns'] if c['table'] in context['table_catalog']}
        result.append({**{k:record[k] for k in ('kind','input','status','canonical_value','matches')},
            'schema_columns':[{'schema_group':group,'column':column} for group,column in sorted(locations)],
            'provenance':'verified from complete read-only DB domain; applies to these schema columns via table_catalog'})
    return result
