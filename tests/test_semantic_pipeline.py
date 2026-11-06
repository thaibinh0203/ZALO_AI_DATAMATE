"""Synthetic-only semantic proposal gates; these fixtures are not model results."""
from collections import Counter
from pathlib import Path
import importlib.util
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from datamate_data.normalization import column_spec
from datamate_data.pipeline import prepare_business
from datamate_data.semantic import ColumnProfile,LocalSemanticModel,build_semantic_plan,validate_column,guard_role_collisions,revalidate_semantic_plan


def proposal(name, role='other', typ='TEXT', **changes):
    value={'column':name,'role':role,'sql_type':typ,'meaning':'Synthetic meaning',
           'unit':None,'handling':'Preserve source','status':'proposed',
           'evidence':[{'source':'header','quote':name}]}
    value.update(changes)
    return value


class FakeSemanticModel:
    identity={'model':'synthetic_fixture_only'}
    def __init__(self):self.calls=[]
    def complete(self,payload,audit):
        self.calls.append(payload)
        if payload['task']=='header':return {'row':2,'reason':'Synthetic fixture header'},None
        roles={'Phiếu chốt':('order_id','TEXT'),'Ngày ghi nhận':('date','DATE'),
               'Tên vật tư':('product_name','TEXT'),'Lượng xuất':('quantity','NUMERIC'),
               'Tiền ròng':('line_amount','NUMERIC')}
        columns=[[n,*roles.get(n,('other','TEXT')),'Synthetic meaning',None,f'H{i}'] for i,n in enumerate(payload['target_columns'])]
        return {'columns':columns,'grain':'Synthetic row','relationships':[],'handling':'Preserve source'},None


class SemanticGates(unittest.TestCase):
    def profile(self,name,values):
        p=ColumnProfile(column_spec(name))
        for value in values:p.add(value)
        return p.finish()

    def test_unknown_field_maps_without_renaming_or_alias_mutation(self):
        name='Lượng xuất';spec=column_spec(name)
        result=validate_column(spec,proposal(name,'quantity','NUMERIC'),self.profile(name,['2','3']),'','')
        self.assertEqual((result['role'],result['sql_type']),('quantity','NUMERIC'))
        self.assertEqual(result['original_name'],name)
        self.assertEqual(result['semantic']['status'],'applied_candidate')
        self.assertEqual(column_spec(name)['role'],'text')

    def test_late_bad_value_blocks_retyping_not_just_first_samples(self):
        name='Lượng xuất';values=['1']*100+['not-a-number']
        result=validate_column(column_spec(name),proposal(name,'quantity','NUMERIC'),self.profile(name,values),'','')
        self.assertEqual(result['sql_type'],'TEXT')
        self.assertIn('full_column_parse_failed:100/101',result['semantic']['checks'])

    def test_identifiers_with_leading_zero_cannot_become_number(self):
        name='Số tham chiếu';profile=self.profile(name,['001','002'])
        result=validate_column(column_spec(name),proposal(name,'money','NUMERIC'),profile,'','')
        self.assertEqual(result['sql_type'],'TEXT')
        self.assertIn('leading_zero_identifier_risk',result['semantic']['checks'])

    def test_invented_evidence_and_rulebook_only_mapping_rejected(self):
        name='Lượng xuất';profile=self.profile(name,['2'])
        for evidence in ([{'source':'readme','quote':'not in README'}],
                         [{'source':'rulebook','quote':'Quantity rules'}]):
            value=proposal(name,'quantity','NUMERIC',evidence=evidence)
            result=validate_column(column_spec(name),value,profile,'README','Quantity rules')
            self.assertEqual(result['role'],'text')

    def test_existing_alias_conflict_visible_and_preserved(self):
        name='Ngày bán';spec=column_spec(name)
        result=validate_column(spec,proposal(name,'money','NUMERIC'),self.profile(name,['2025-01-01']),'','')
        self.assertEqual(result['sql_type'],'DATE')
        self.assertEqual(result['semantic']['status'],'conflict_existing')

    def test_new_concept_can_get_physical_type_without_forced_role(self):
        name='Điểm tích lũy';result=validate_column(column_spec(name),proposal(name,'other','NUMERIC'),
            self.profile(name,[2,3]),'','')
        self.assertEqual((result['role'],result['sql_type']),('text','NUMERIC'))
        self.assertEqual(result['semantic']['status'],'applied_candidate')

    def test_empty_or_ambiguous_numeric_does_not_get_guessed_type(self):
        name='Mức đo'
        for values in (['',None],['1,250','1.250']):
            result=validate_column(column_spec(name),proposal(name,'other','NUMERIC'),self.profile(name,values),'','')
            self.assertEqual(result['sql_type'],'TEXT')

    def test_conversion_factor_requires_positive_full_column(self):
        name='Quy đổi'
        for values in ([0,12],[-1,24]):
            result=validate_column(column_spec(name),proposal(name,'conversion_factor','NUMERIC'),self.profile(name,values),'','')
            self.assertEqual(result['role'],'text')
            self.assertIn('conversion_factor_must_be_positive',result['semantic']['checks'])

    def test_malformed_model_fields_do_not_crash_validator(self):
        name='New column';profile=self.profile(name,['x'])
        for value in (None,[],{},proposal(name,sql_type=[]),proposal(name,role={}),
                      proposal(name,status=[]),proposal(name,evidence=[{'source':{},'quote':'x'}])):
            with self.subTest(value=value):
                result=validate_column(column_spec(name),value,profile,'','')
                self.assertEqual(result['role'],'text')

    def test_role_collision_cannot_override_known_operand_or_role_parser(self):
        known=column_spec('Số lượng')
        baseline=column_spec('Another amount')
        profile=self.profile('Another amount',['2 bao'])
        guess=validate_column(baseline,proposal('Another amount','quantity','NUMERIC'),profile,'','')
        known['semantic']={'status':'existing_mapping'}
        guarded=guard_role_collisions([known,guess],[known,baseline])
        self.assertEqual(guarded[1]['role'],'text')
        self.assertEqual(guarded[1]['sql_type'],'TEXT')
        self.assertEqual(guarded[1]['semantic']['status'],'role_conflict')

    def test_two_new_guesses_for_single_role_have_no_arbitrary_winner(self):
        bases=[column_spec(n) for n in ['Amount A','Amount B']]
        reviewed=[validate_column(c,proposal(c['original_name'],'quantity','NUMERIC'),
            self.profile(c['original_name'],[2]),'','') for c in bases]
        result=guard_role_collisions(reviewed,bases)
        self.assertEqual([c['role'] for c in result],['text','text'])

    def test_samples_bounded_and_deterministic_whole_profile_counts(self):
        values=[f'item-{n}' for n in range(5000)]
        a=self.profile('Label',values);b=self.profile('Label',reversed(values))
        self.assertEqual(a['samples'],b['samples'])
        self.assertEqual(a['non_empty'],5000)
        self.assertLessEqual(len(a['samples']),4)
        self.assertTrue(a['distinct_capped'])


class SemanticIntegration(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.business=self.root/'demo';self.business.mkdir()
        (self.business/'README.md').write_text('Synthetic business only',encoding='utf-8')
        self.rules=self.root/'rules.md';self.rules.write_text('Synthetic rules')
        self.calendar=self.root/'calendar.csv';self.calendar.write_text('date\n2025-01-01\n')
    def tearDown(self):self.temp.cleanup()
    def plan(self,model=None):return build_semantic_plan(self.business,self.root/'semantics',
        rulebook=self.rules,calendar=self.calendar,model=model or FakeSemanticModel())
    def prepare(self,plan):return prepare_business(self.business,self.root/'prepared',
        rulebook=self.rules,calendar=self.calendar,semantic_plan=plan)

    def test_unknown_csv_columns_become_queryable_and_keep_all_values(self):
        (self.business/'export.csv').write_text('Phiếu chốt,Ngày ghi nhận,Tên vật tư,Lượng xuất,Tiền ròng,Diễn giải\n'
            '0001,2025-01-01,Nước,2,2000,new meaning\n',encoding='utf-8')
        model=FakeSemanticModel();plan=self.plan(model);catalog=self.prepare(plan)
        table=next(iter(catalog['tables'].values()))
        self.assertEqual(table['role'],'sale_lines')
        self.assertEqual([c['role'] for c in table['columns']][:5],['order_id','date','product_name','quantity','line_amount'])
        row=json.loads((self.root/'prepared'/table['data_path']).read_text().splitlines()[0])
        self.assertEqual(row['typed'][0],'0001')
        self.assertEqual(row['raw'][-1],'new meaning')
        self.assertFalse(plan['questions_read']);self.assertFalse(plan['answers_read'])
        self.assertIn('Synthetic business only',model.calls[0]['readme'])

    @unittest.skipUnless(importlib.util.find_spec('openpyxl'),'requires openpyxl')
    def test_unknown_excel_header_selected_from_existing_rows(self):
        from openpyxl import Workbook
        workbook=Workbook();sheet=workbook.active
        sheet.append(['Synthetic title']);sheet.append(['Phiếu chốt','Lượng xuất','Tiền ròng'])
        sheet.append(['001',2,2000]);workbook.save(self.business/'export.xlsx')
        plan=self.plan();self.assertEqual(len(plan['headers']),1)
        catalog=self.prepare(plan);table=next(iter(catalog['tables'].values()))
        self.assertEqual(table['rows'],1)
        self.assertEqual(table['source_parts'][0]['header_row'],2)
        self.assertEqual(table['source_parts'][0]['header_confidence'],'slm_candidate_existing_source_row')

    def test_changed_source_invalidates_semantic_plan(self):
        source=self.business/'export.csv';source.write_text('Phiếu chốt,Lượng xuất\n001,2\n')
        plan=self.plan();source.write_text('Phiếu chốt,Lượng xuất\n001,3\n')
        with self.assertRaisesRegex(ValueError,'snapshot'):self.prepare(plan)

    def test_cached_semantics_revalidated_and_known_base_quantity_retained(self):
        class Collision(FakeSemanticModel):
            def complete(self,payload,audit):
                value,error=super().complete(payload,audit)
                for entry in value['columns']:
                    if entry[0]=='Alternate count':entry[1:3]=['base_quantity','NUMERIC']
                return value,error
        source=self.business/'export.csv'
        source.write_text('Tên hàng,Số lượng quy đổi,Alternate count,ĐVT\nNước,24,2,thùng\n')
        plan=self.plan(Collision())
        cached=revalidate_semantic_plan(plan,self.business,rulebook=self.rules,calendar=self.calendar)
        catalog=self.prepare(cached);table=next(iter(catalog['tables'].values()))
        alt=next(c for c in table['columns'] if c['original_name']=='Alternate count')
        self.assertEqual((alt['role'],alt['sql_type']),('text','NUMERIC'))
        self.assertEqual(alt['semantic']['status'],'role_conflict')
        row=json.loads((self.root/'prepared'/table['data_path']).read_text().splitlines()[0])
        self.assertEqual(row['quantity_base'],'24')
        source.write_text('Tên hàng,Số lượng quy đổi,Alternate count,ĐVT\nNước,48,2,thùng\n')
        with self.assertRaisesRegex(ValueError,'snapshot'):
            revalidate_semantic_plan(plan,self.business,rulebook=self.rules,calendar=self.calendar)

    def test_invalid_generation_keeps_columns_and_records_error(self):
        class Broken(FakeSemanticModel):
            def complete(self,*args):return None,'JSONDecodeError: synthetic malformed result'
        (self.business/'export.csv').write_text('Unknown A,Unknown B\nx,y\n')
        plan=self.plan(Broken());catalog=self.prepare(plan)
        table=next(iter(catalog['tables'].values()))
        self.assertEqual(table['rows'],1)
        self.assertEqual(plan['model_error_tables'],1)
        self.assertEqual([c['sql_type'] for c in table['columns']],['TEXT','TEXT'])

    def test_unknown_evidence_reference_is_not_invented(self):
        class WrongReference(FakeSemanticModel):
            def complete(self,*args):
                value,error=super().complete(*args)
                value['columns'][0][-1]='NOT_A_SOURCE_REFERENCE'
                return value,error
        (self.business/'export.csv').write_text('Unknown A,Unknown B\nx,y\n')
        plan=self.plan(WrongReference())
        self.assertEqual(plan['model_error_tables'],1)
        self.assertIn('Unknown evidence reference',next(iter(plan['tables'].values()))['model_error'])


class SemanticCacheTests(unittest.TestCase):
    def test_context_dedup_retains_source_text_instead_of_truncating(self):
        class Tokenizer:
            def apply_chat_template(self,messages,**kwargs):return json.dumps(messages,ensure_ascii=False)
            def __call__(self,text,**kwargs):return {'input_ids':range(len(text))}
        worker=LocalSemanticModel.__new__(LocalSemanticModel)
        worker.model=SimpleNamespace(tokenizer=Tokenizer(),config=SimpleNamespace(max_new_tokens=1000,max_context_tokens=40000))
        readme='r'*16000;rules='b'*12000
        payload={'task':'columns','readme':readme,'rulebook':rules,'evidence_catalog':[
            {'id':'R0','source':'readme','quote':readme,'location':'README.md:1'},
            {'id':'B0','source':'rulebook','quote':rules,'location':'rule:0'}]}
        result=worker.budget_payload(payload)
        self.assertIn('context_compaction',result)
        self.assertEqual([e[2] for e in result['evidence_catalog']],[readme,rules])
        self.assertEqual(payload['readme'],readme)

    def worker(self,root,outputs):
        class Stub:
            def load(self):return self
            def metadata(self):return {'fixture':True}
            def generate(self,messages):
                return {'raw_output':outputs.pop(0),'hit_token_limit':False,'generation_ms':0}
        worker=LocalSemanticModel.__new__(LocalSemanticModel)
        worker.model=Stub();worker.cache=root/'cache';worker.cache.mkdir(exist_ok=True)
        worker.calls=worker.cache_hits=0;worker.identity={'fixture':'version1'}
        return worker

    def test_same_input_reuses_cache_and_identity_change_invalidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);worker=self.worker(root,['{}','{}'])
            worker.complete({'task':'fixture'},root/'calls.jsonl')
            worker.complete({'task':'fixture'},root/'calls.jsonl')
            self.assertEqual((worker.calls,worker.cache_hits),(1,1))
            worker.identity={'fixture':'version2'}
            worker.complete({'task':'fixture'},root/'calls.jsonl')
            self.assertEqual(worker.calls,2)

    def test_json_retry_keeps_both_raw_attempts_and_replays_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);worker=self.worker(root,['bad-json','{}'])
            value,error=worker.complete({'task':'fixture'},root/'calls.jsonl')
            self.assertEqual(value,{});self.assertIsNone(error)
            records=[json.loads(l) for l in (root/'calls.jsonl').read_text().splitlines()]
            self.assertEqual(len(records),2);self.assertIsNotNone(records[0]['parse_error'])
            worker.complete({'task':'fixture'},root/'calls.jsonl')
            self.assertEqual((worker.calls,worker.cache_hits),(2,2))

    def test_cache_context_tampering_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);worker=self.worker(root,['{}'])
            worker.complete({'task':'fixture'},root/'calls.jsonl')
            path=next(worker.cache.glob('*.json'));record=json.loads(path.read_text())
            record['messages']=[];path.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError,'cache'):worker.complete({'task':'fixture'},root/'calls.jsonl')

    def test_generation_failure_is_logged_and_not_called_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);worker=self.worker(root,[])
            def fail(messages):raise RuntimeError('synthetic generation failure')
            worker.model.generate=fail
            with self.assertRaisesRegex(RuntimeError,'synthetic'):worker.complete({'task':'fixture'},root/'calls.jsonl')
            record=json.loads((root/'calls.jsonl').read_text())
            self.assertIsNone(record['raw_output'])
            self.assertIn('RuntimeError',record['generation_error'])


if __name__=='__main__':unittest.main()
