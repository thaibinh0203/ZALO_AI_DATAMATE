"""Concrete catalog and temporal facts for the single bounded repair."""
def repair_feedback(plan,context,windows):
    plan=plan if isinstance(plan,dict) else {};catalog=context.get('table_catalog',{})
    if not catalog:
        catalog={t['sql_table']:t['schema_group'] for t in context.get('tables',[])}
    entries={field:plan.get(field,[]) if isinstance(plan.get(field,[]),list) else [] for field in ('column_usage','time_bindings')}
    used={name for name in plan.get('required_tables',[]) if isinstance(name,str)} if isinstance(plan.get('required_tables',[]),list) else set()
    used.update(e.get('table') for field in entries for e in entries[field] if isinstance(e,dict) and isinstance(e.get('table'),str))
    tree=None;diagnostics=[];scope_catalog=[]
    try:
        import sqlglot
        from sqlglot import exp
        from ..sql_validation.guard import column_diagnostics
        tree=sqlglot.parse_one(plan.get('sql') or '',read='postgres')
        ctes={node.alias for node in tree.find_all(exp.CTE)}
        used.update(node.name for node in tree.find_all(exp.Table) if node.name not in ctes)
        tables={name:{'columns':context['schema_groups'][group]['columns']} for name,group in catalog.items()}
        diagnostics=column_diagnostics(tree,tables)
        from sqlglot.optimizer.scope import traverse_scope,Scope
        for index,scope in enumerate(traverse_scope(tree)):
            sources={}
            selected={alias:source for alias,(_,source) in scope.selected_sources.items()}
            for alias,source in selected.items():
                if isinstance(source,exp.Table) and source.name in tables:
                    sources[alias]={'kind':'physical','table':source.name}
                elif isinstance(source,Scope):
                    sources[alias]={'kind':'derived','columns':source.expression.named_selects}
            unbound=sorted({c.table for c in scope.columns if c.table and c.table not in selected})
            scope_catalog.append({'scope':index,'visible_sources':sources,'unbound_aliases':unbound,
                'available_ctes':sorted(scope.cte_sources)})
    except Exception:
        pass
    allowed={table:[c['sql_column'] for c in context['schema_groups'][catalog[table]]['columns']]
             for table in used if table in catalog}
    errors=[]
    for d in diagnostics:
        errors.append(f"Alias {d['alias']} refers to {d['table']}, which has no column {d['column']!r}; allowed columns: "+', '.join(d['allowed_columns']))
    for table in sorted(t for t in used if t and t not in catalog):
        errors.append(f'Table {table!r} does not exist. Use exact physical names from table_catalog; schema groups are not SQL tables.')
    for field in ('column_usage','time_bindings'):
        for entry in entries[field]:
            if isinstance(entry,dict) and isinstance(entry.get('table'),str) and entry['table'] in allowed and entry.get('column') not in allowed[entry['table']]:
                errors.append(f'Table {entry["table"]} has no column {entry.get("column")!r}; allowed columns: '+', '.join(allowed[entry['table']]))
    projection={'declared':plan.get('output_columns',[]),
                'instruction':'Use SELECT expression AS alias_name and output_columns=[alias_name]. Do not put COUNT(...), arithmetic or AS inside output_columns. Keep the calculation and return shape required by the question.'}
    try:
        if isinstance(tree,exp.Select):
            projection['actual']=[e.alias_or_name for e in tree.expressions]
            projection['select_expressions']=[e.sql(dialect='postgres') for e in tree.expressions]
    except Exception:
        pass
    return {'catalog_errors':errors,'column_errors':diagnostics,'scope_catalog':scope_catalog,
            'scope_instruction':'An output alias of a CTE replaces its input expression. Only columns projected by that CTE are visible outside it. A table/CTE must occur in the local FROM/JOIN before its alias can be referenced; aliases inside another CTE are not visible here.',
            'allowed_columns_for_used_tables':allowed,'output_contract':projection,
            'validated_windows':[{key:w.get(key) for key in ('name','start_date','end_date_exclusive')} for w in windows],
            'catalog_location':'business_context.table_catalog maps exact SQL names to schema_groups; never invent or shorten table names'}
