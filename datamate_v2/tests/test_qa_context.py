import copy
from datamate_v2.common import dumps
from datamate_v2.qa_pipeline.context import build_qa_context


def test_compact_context_shares_schema_and_keeps_sources_and_quality(metadata):
    table=metadata['tables'][0]
    table.update(row_count=100,verified_facts={'distinct_order_ids':30,'multiple_rows_per_order':True},
        normalization_stats={'debug':'AUDIT_ONLY'},column_profiles=[{'debug':'AUDIT_ONLY'}],
        sample_rows=[{'order_id':'a','quantity':2,'_raw':['AUDIT_ONLY'],'_quality':['AUDIT_ONLY']}]*5,
        last_rows=[{'order_id':'b','quantity':3,'_raw':['AUDIT_ONLY'],'_quality':['AUDIT_ONLY']}]*5)
    table['stats'][2].update(null_count=3,unresolved_count=2)
    metadata['tables']=[dict(copy.deepcopy(table),sql_table='sales_'+str(i)) for i in range(20)]
    before=copy.deepcopy(metadata);context=build_qa_context(metadata)
    assert metadata==before
    assert context['readme']==metadata['readme'] and context['rulebook']==metadata['rulebook']
    assert len(context['schema_groups'])==1 and len(context['tables'])==20
    group=next(iter(context['schema_groups'].values()))
    assert len(group['sample_rows'])==2 and 'AUDIT_ONLY' not in dumps(context)
    assert len(dumps(context))<len(dumps(metadata))/2
    assert context['system_columns']['_is_summary_row']=='BOOLEAN'
    assert all(t['column_quality']['quantity']=={'null_count':3,'unresolved_count':2} for t in context['tables'])
    assert all(t['grain_facts']['multiple_rows_per_order'] for t in context['tables'])
    assert context['tables'][0]['coverage']==[{'sql_column':'order_date','min':'2026-01-01','max':'2026-12-31'}]


def test_domains_deduplicate_values_without_losing_truncation(metadata):
    table=metadata['tables'][0]
    table['observed_domains']={'status':{'values':[{'value':'Xong','count':100}], 'truncated':True,'distinct_count':41}}
    second=copy.deepcopy(table);second['sql_table']='sales2';second['observed_domains']['status']['values'][0]['count']=5
    metadata['tables'].append(second)
    context=build_qa_context(metadata)
    assert len(context['domains'])==1
    domain=next(iter(context['domains'].values()))
    assert domain=={'values':['Xong'],'truncated':True,'distinct_count':41}


def test_uncertain_semantics_not_collapsed_and_source_quote_retained(metadata):
    table=metadata['tables'][0];second=copy.deepcopy(table);second['sql_table']='sales2'
    second['columns'][2].update(meaning=None,semantic_status='uncertain',candidate_meanings=['quantity','base_quantity'],source_quote='Exact definition')
    metadata['tables'].append(second)
    context=build_qa_context(metadata)
    assert len(context['schema_groups'])==2
    columns=context['schema_groups'][context['tables'][1]['schema_group']]['columns']
    assert next(c for c in columns if c['sql_column']=='quantity')['source_quote']=='Exact definition'
