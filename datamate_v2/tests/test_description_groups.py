import copy
import pytest
from datamate_v2.common import dumps,PipelineError
from datamate_v2.data_pipeline.schema_generation.description_groups import build_business_descriptions,compact_input,readme_structure
from datamate_v2.data_pipeline.schema_generation.schema_builder import verified_facts
from datamate_v2.tests.factories import schema_fixture


def monthly_metadata(metadata):
    result=copy.deepcopy(metadata)
    result['readme']='''# Fixture
Sổ bán hàng: mỗi dòng là một dòng hàng của đơn.
## Cột trong `So_ban_hang.xlsx`
- **Mã đơn**: mã đơn
- **Số lượng**: số lượng bán
## Cột trong `Giao_hang.xlsx`
- **Số lượng**: số lượng giao khác ý nghĩa
## Quy ước
- Hệ số riêng cần giữ ở business.
'''
    first=copy.deepcopy(result['tables'][0])
    first.update(sql_table='sales_feb',source_file='So_ban_hang.xlsx',source_table='T02-2025',row_count=20,
                 relationships=[],observed_domains={},sample_rows=[],quality={'unresolved_values':0},normalization_stats={'audit':'DO_NOT_SEND'},last_rows=[{'_raw':'DO_NOT_SEND'}])
    first['stats'][0]['distinct_count']=5
    second=copy.deepcopy(first)
    second.update(sql_table='sales_mar',source_table='T03-2025',row_count=100,quality={'unresolved_values':7})
    second['stats'][0]['distinct_count']=40
    second['stats'][1].update(min='2025-03-01',max='2025-03-31')
    result['tables']=[first,second]
    return result


class Client:
    def __init__(self,fail=False): self.calls=[];self.fail=fail
    def call(self,task,payload,schema,**kwargs):
        self.calls.append((task,copy.deepcopy(payload),schema))
        if self.fail: raise PipelineError('Model output hit max_new_tokens')
        return schema_fixture(payload['table'])


def test_months_share_one_description_without_copying_table_facts(metadata):
    md=monthly_metadata(metadata);client=Client()
    build_business_descriptions(md,client,md)
    a,b=md['tables']
    assert len(client.calls)==1 and len(md['description_groups'])==1
    assert a['description_group']==b['description_group']
    assert a['description']=={'group_ref':a['description_group']}
    assert a['verified_facts']['row_count']==20 and b['verified_facts']['row_count']==100
    assert b['verified_facts']['quality']['unresolved_values']==7
    assert a['verified_facts']['date_coverage']!=b['verified_facts']['date_coverage']
    assert md['description_generation']=={'tables':2,'groups':1,'reused_tables':1,'unavailable_groups':0}
    assert md['readme'].endswith('business.\n') and md['rulebook']=='Synthetic rules.'


@pytest.mark.parametrize('change',['meaning','original_column','type','candidate','family','grain','join','constraint','nonmonthly'])
def test_incompatible_tables_do_not_share(metadata,change):
    md=monthly_metadata(metadata);b=md['tables'][1]
    if change=='meaning': b['columns'][2]['meaning']='base_quantity'
    if change=='original_column': b['columns'][2]['original_column']='Số lượng giao'
    if change=='type': b['columns'][2]['type']='TEXT'
    if change=='candidate': b['columns'][2].update(semantic_status='uncertain',candidate_meanings=['quantity','base_quantity'])
    if change=='family': b['source_file']='Giao_hang.xlsx'
    if change=='grain': b['stats'][0]['distinct_count']=100
    if change=='join': md['relationships']=[{'from_table':b['sql_table'],'to_table':'products','from_column':'order_id','to_column':'id','status':'verified','cardinality':'many_to_one'}]
    if change=='constraint': b['quality']['constraints']=[{'kind':'different_formula'}]
    if change=='nonmonthly': b['source_table']='Returns'
    client=Client();build_business_descriptions(md,client,md)
    assert len(client.calls)==2 and len(md['description_groups'])==2


def test_group_failure_uses_one_repair_for_group_not_every_month(metadata):
    md=monthly_metadata(metadata);client=Client(fail=True)
    build_business_descriptions(md,client,md)
    assert len(client.calls)==2
    assert len(md['warnings'])==2 and md['description_generation']['unavailable_groups']==1
    assert all(t['description'] is None and t['verified_facts']['row_count']>0 for t in md['tables'])


def test_compact_input_has_no_rulebook_or_row_audit_or_table_counts(metadata):
    md=monthly_metadata(metadata);client=Client()
    build_business_descriptions(md,client,md)
    payload=client.calls[0][1];text=dumps(payload)
    assert 'rulebook' not in payload and 'source_fingerprint' not in payload
    assert 'DO_NOT_SEND' not in text and '2025-03-31' not in text
    assert 'normalization_stats' not in text and 'last_rows' not in text
    assert 'row_count' not in payload['table']['verified_facts']
    assert 'date_coverage' not in payload['table']['verified_facts']
    assert 'quality' not in payload['table']['verified_facts']
    assert 'Hệ số riêng' not in payload['readme'] and 'khác ý nghĩa' not in payload['readme']
    assert 'số lượng bán' in payload['readme']
    old=dumps({'table':md['tables'][0],**{k:md[k] for k in ('readme','rulebook')}})
    assert len(text)<len(old)


def test_readme_grain_does_not_spill_into_other_files(metadata):
    md=monthly_metadata(metadata)
    table=md['tables'][0];table['source_file']='Giao_hang.xlsx'
    excerpt=readme_structure(table,md['readme'])
    assert 'mỗi dòng là một dòng hàng' not in excerpt
    assert 'số lượng giao' in excerpt and 'số lượng bán' not in excerpt
