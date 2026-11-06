"""Human-readable audit of applied, conflicting and unresolved semantic proposals."""
from collections import Counter
from pathlib import Path
from .parsers import dumps


def write_semantic_review(catalog, prepared_dir):
    columns=[]
    for table in catalog['tables'].values():
        for col in table['columns']:
            audit=col.get('semantic', {})
            columns.append({'table':table['name'], 'column':col['original_name'],
                'sql_column':col['name'], 'role':col['role'], 'sql_type':col['sql_type'],
                'description':col['description'], 'source_profile':col.get('source_profile'),
                **audit})
    report={'business_id':catalog['business_id'], 'schema':catalog['schema'],
            'summary':dict(Counter(c.get('status','not_run') for c in columns)),
            'model_error_tables':catalog.get('semantic_model_error_tables',0),
            'note':'All model meanings/joins remain candidates. Passing type checks does not prove metric correctness. Raw values and source names are retained.',
            'columns':columns}
    root=Path(prepared_dir)
    (root/'semantic_review.json').write_text(dumps(report)+'\n',encoding='utf-8')
    def cell(value):return str(value or '').replace('|','\\|').replace('\n',' ')
    lines=[f"# Semantic review — {catalog['business_id']}",'',report['note'],'',
           '| Bảng | Cột nguồn | Role / kiểu áp dụng | Trạng thái | Ý nghĩa SLM đề xuất | Kiểm tra |',
           '|---|---|---|---|---|---|']
    for c in columns:
        proposal=c.get('proposed') or {}
        lines.append('| '+' | '.join(map(cell,[c['table'],c['column'],c['role']+' / '+c['sql_type'],
            c.get('status'),proposal.get('meaning',''),'; '.join(c.get('checks',[]))]))+' |')
    (root/'semantic_review.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return report
