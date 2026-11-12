"""Reuse structural insights within compatible monthly table families.

Business sources and per-table measurements remain authoritative in metadata.
Only the optional semantic description is shared; no rows or SQL names are merged.
"""
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
import re

from datamate_v2.common import digest,fold
from datamate_v2.progress import emit

# Deliberately recognize explicit month labels, not arbitrary numbers in names.
PERIOD=re.compile(r'(?i)(?<![a-z0-9])(?:t|thang[ _-]*)(?:0?[1-9]|1[0-2])[ _/-]+20\d{2}(?!\d)')
COLUMN_KEYS=('sql_column','original_column','type','meaning','semantic_status','candidate_meanings','unit')


def source_family(table):
    path=Path(table.get('source_file',''))
    file=str(path.with_suffix('')) if path.name else ''
    sheet=str(table.get('source_table',''))
    normalized_file,nf=PERIOD.subn('MONTH',file)
    normalized_sheet,ns=PERIOD.subn('MONTH',sheet)
    return {'file':normalized_file,'sheet':normalized_sheet,'periodic':bool(nf or ns)}


def readme_structure(table,readme):
    """Select the source file's column section and applicable grain introduction.

    Business conventions (conversions, status rules, coverage) stay at biz level.
    With unstructured text retain only grain statements; uncertainty is preferable
    to silently importing definitions from another file's identically named column.
    """
    stem=fold(Path(source_family(table)['file']).name)
    sections=re.split(r'(?m)(?=^##\s)',readme)
    selected=[]
    for section in sections:
        heading=section.splitlines()[0] if section.splitlines() else ''
        if heading.startswith('## '):
            if stem and stem in fold(heading) and fold(heading).startswith('cot '):
                selected.append(section.strip())
        else:
            for line in section.splitlines():
                if 'moi dong' not in fold(line): continue
                # A named table's grain must not spill over to other file families.
                if (stem and stem in fold(line)) or 'mot file phang' in fold(line):
                    selected.append(line.strip())
    return '\n'.join(selected)


def compact_input(table,context):
    facts=table['verified_facts']
    source=readme_structure(table,context.get('readme',''))
    # In direct callers without file identity, generic grain text can still help.
    if not table.get('source_file'):
        source='\n'.join(line for line in context.get('readme','').splitlines() if 'moi dong' in fold(line))
    compact_facts={key:deepcopy(facts[key]) for key in
                   ('multiple_rows_per_order','mapping_issues','relationships')}
    # Exact relationships apply only to singleton groups (see compatibility_key).
    compact_facts['relationships']=[{k:r[k] for k in
        ('from_table','from_column','to_table','to_column','cardinality','status') if k in r}
        for r in facts['relationships']]
    for relationship in compact_facts['relationships']:
        for key in ('from_table','to_table'):
            if relationship.get(key)==table['sql_table']: relationship[key]='SELF'
    examples=[]
    sample_columns=[c['sql_column'] for c in table['columns']
                    if c.get('meaning') in ('product_name','unit','status') and c['type']=='TEXT']
    for row in table.get('sample_rows',[])[:2]:
        example={key:str(row[key])[:100] for key in sample_columns if row.get(key) is not None}
        if example and example not in examples: examples.append(example)
    compact={'family':source_family(table),
        'columns':[{key:deepcopy(c[key]) for key in COLUMN_KEYS if key in c} for c in table['columns']],
        'verified_facts':compact_facts,'sample_values':examples,
        'per_table_facts_location':'Each member table.verified_facts and stats: exact counts, coverage, domains, quality, relationships. Never copy these into shared prose.'}
    return compact,source


def compatibility_key(table,context):
    family=source_family(table)
    compact,source=compact_input(table,context)
    # Unknown semantics or table-specific JOINs are intentionally not shared.
    isolated=(not family['periodic'] or
              any(c['semantic_status']!='verified' for c in table['columns']))
    return digest({'family':family,'columns':sorted(compact['columns'],key=lambda c:c['sql_column']),
        'readme_structure':source,'multiple_rows_per_order':compact['verified_facts']['multiple_rows_per_order'],
        'relationship_templates':compact['verified_facts']['relationships'],
        'constraints':table.get('quality',{}).get('constraints',[]),
        'singleton':table['sql_table'] if isolated else None})


def build_business_descriptions(metadata,client,context,max_repairs=1):
    from .schema_builder import verified_facts,build_schema
    groups=OrderedDict()
    for table in metadata['tables']:
        table['relationships']=[r for r in metadata.get('relationships',[])
                               if table['sql_table'] in (r['from_table'],r['to_table'])]
        table['verified_facts']=verified_facts(table,context)
        key=compatibility_key(table,context)
        groups.setdefault(key,[]).append(table)
    metadata['description_groups']={}
    summary={'tables':len(metadata['tables']),'groups':len(groups),'reused_tables':0,'unavailable_groups':0}
    for index,(key,tables) in enumerate(groups.items(),1):
        group_id='dg_'+key[:16]
        representative=deepcopy(tables[0])
        # Pick deterministic text examples across the group, not a single month's
        # complete rows. Samples are never facts about every member.
        representative['sample_rows']=[row for t in tables for row in t.get('sample_rows',[])[:1]][:2]
        emit('schema',f'{metadata["business_id"]}: description group {index}/{len(groups)}; {len(tables)} tables',
             table=representative['sql_table'],description_group=group_id,group_tables=len(tables))
        build_schema(representative,client,context,max_repairs=max_repairs)
        group={'tables':[t['sql_table'] for t in tables],'scope':'shared_structure_only',
               'status':representative['description_status'],'description':representative['description']}
        if group['status']=='unavailable':
            group['error']=representative['description_error']
            summary['unavailable_groups']+=1
        metadata['description_groups'][group_id]=group
        summary['reused_tables']+=len(tables)-1
        for table in tables:
            table['description_group']=group_id
            table['description_status']=group['status']
            table['description']={'group_ref':group_id} if group['status']=='available' else None
            if group['status']=='unavailable':
                table['description_error']=deepcopy(group['error'])
                metadata['warnings'].append(f'{table["sql_table"]}: description unavailable; QA must use original sources and verified metadata')
            else:
                table.pop('description_error',None)
        emit('schema_group_done',f'{group_id}: {group["status"]}; {len(tables)-1} duplicate table descriptions avoided')
    metadata['description_generation']=summary
    emit('schema_done',f'Description groups: {summary["groups"]} for {summary["tables"]} tables; {summary["reused_tables"]} reused')
    return metadata
