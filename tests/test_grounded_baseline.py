"""Synthetic checks for grounded metadata and answer-oriented question linking."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
import psycopg
from psycopg import sql
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from datamate_data.business_metadata import validate_proposal,key_stats,check_table
from datamate_baseline.understanding import alias_hints,validate_analysis,validate_links,interpret
from datamate_baseline.runner import run_batch,validate_answer_projection

ANALYSIS={'metric':{'phrase':'dthu','concept':'revenue','meaning':'Revenue per source rules'},
          'entity':'customer','operation':'rank','filters':[],
          'time':[{'start':'2026-03-01','end_exclusive':'2026-04-01','meaning':'March'}],
          'answer':{'type':'list','target':'customer names','granularity':'one customer name'},'unresolved':[]}
REF={'table':'sales_clean','column':'customer','reason':'Requested customer names'}
LINKS={'status':'grounded','answer_columns':[REF],'required_columns':[],
       'group_by':[REF],'joins':[],'missing_requirements':[],'reasoning':'Group sales by customer, rank by revenue.'}
CONTEXT={'allowed':{'sales_clean':{'customer':'TEXT','amount':'NUMERIC'}},'readme':'One sale line.',
         'rulebook':'Use sale amounts.','fingerprint':'synthetic','pg_schema':'test',
         'ddl':'CREATE TABLE sales_clean (customer TEXT, amount NUMERIC);'}


class UnderstandingTests(unittest.TestCase):
    def test_alias_hints_use_word_boundaries_and_do_not_mutate_aliases(self):
        from datamate_data.normalization import ALIASES
        before=copy.deepcopy(ALIASES)
        self.assertIn({'phrase':'dthu','concept':'revenue'},alias_hints('dthu tháng 3'))
        self.assertNotIn({'phrase':'sp','concept':'product'},alias_hints('display'))
        a=validate_analysis(copy.deepcopy(ANALYSIS))
        self.assertEqual(a['alias_resolution']['persistent_alias_changes'],False)
        self.assertEqual(ALIASES,before)

    def test_time_and_answer_contract(self):
        a=copy.deepcopy(ANALYSIS);a['time'][0]['end_exclusive']='2026-02-01'
        with self.assertRaises(ValueError):validate_analysis(a)
        a=copy.deepcopy(ANALYSIS);a['answer']['type']='customer_name'
        with self.assertRaises(ValueError):validate_analysis(a)

    def test_links_require_exact_business_identifiers(self):
        self.assertEqual(validate_links(copy.deepcopy(LINKS),CONTEXT)['status'],'grounded')
        l=copy.deepcopy(LINKS);l['answer_columns'][0]['table']='another_biz'
        with self.assertRaises(ValueError):validate_links(l,CONTEXT)

    def test_missing_data_needs_source_evidence_not_failed_name_match(self):
        l=copy.deepcopy(LINKS);l.update(status='missing_data',answer_columns=[],missing_requirements=[{'requirement':'customer','evidence':'Not found in schema'}])
        with self.assertRaises(ValueError):validate_links(l,CONTEXT)
        l['missing_requirements'][0]['evidence']='Customer information is not recorded.'
        context={**CONTEXT,'readme':'Customer information is not recorded.'}
        self.assertEqual(validate_links(l,context)['status'],'missing_data')
        # Literal evidence is a necessary contract check, not semantic proof.

    def test_projection_is_checked_not_silently_trimmed(self):
        p={'answer_type':'list'}
        with self.assertRaises(ValueError):validate_answer_projection(p,ANALYSIS,'SELECT customer,sum(amount) FROM sales_clean GROUP BY customer')
        validate_answer_projection(p,ANALYSIS,'WITH ranked AS (SELECT customer,sum(amount) total FROM sales_clean GROUP BY customer) SELECT customer FROM ranked ORDER BY total DESC')

    def test_bounded_refinement_rechecks_requested_subject(self):
        bad=copy.deepcopy(LINKS);bad['answer_columns'][0]['column']='notes'
        replies=[ANALYSIS,bad,LINKS]
        class Model:
            def generate(self,messages):return {'raw_output':json.dumps(replies.pop(0))}
        class Loader:
            def grounding_payload(self,c):return {'schema':'synthetic','business_metadata':{}}
        trace={};a,l=interpret(Model(),CONTEXT,{'question':'top khách theo dthu'},trace,Loader())
        self.assertEqual(l['answer_columns'][0]['column'],'customer')
        self.assertEqual(len(trace['understanding_calls']),3)
        self.assertTrue(trace['refinement_used'])
        self.assertIn('customer names',trace['understanding_calls'][1]['messages'][1]['content'])

    def test_persistent_link_errors_do_not_create_unanswerable_or_retry_forever(self):
        bad=copy.deepcopy(LINKS);bad['answer_columns'][0]['column']='invented'
        replies=[ANALYSIS,bad,bad]
        class Model:
            def generate(self,messages):return {'raw_output':json.dumps(replies.pop(0))}
        class Loader:
            def grounding_payload(self,c):return {'schema':'synthetic'}
        trace={}
        with self.assertRaises(ValueError):interpret(Model(),CONTEXT,{'question':'synthetic'},trace,Loader())
        self.assertEqual(len(trace['understanding_calls']),3)
        self.assertNotIn('schema_linking',trace)

    def test_grounded_runner_loads_once_has_no_labels_and_single_sql(self):
        plan={'status':'sql','answer_type':'list','sql':'SELECT customer FROM sales_clean ORDER BY amount DESC','missing_requirements':[]}
        replies=[copy.deepcopy(ANALYSIS),copy.deepcopy(LINKS),plan]
        class Model:
            load_ms=1;loads=0
            def load(self):self.loads+=1
            def metadata(self):return {'synthetic':True}
            def generate(self,messages):return {'raw_output':json.dumps(replies.pop(0))}
        class Loader:
            prepared_root=Path('/synthetic');source_root=Path('/synthetic')
            def load(self,b):return CONTEXT
            def grounding_payload(self,c):return {'schema':'synthetic'}
            def messages_for_plan(self,c,r,a,l):return [{'role':'user','content':json.dumps({'request':r,'analysis':a,'links':l})}]
        class Tool:
            calls=0
            def execute_sql(self,c,s):self.calls+=1;return {'columns':['customer'],'rows':[['Alice']],'truncated':False}
        with tempfile.TemporaryDirectory() as d:
            m=Model();q=Tool();out=Path(d)/'run'
            result=run_batch([{'id':'synthetic','business_id':'demo','question':'top khách theo dthu','as_of_date':'2026-04-02'}],model=m,query_tool=q,loader=Loader(),output_dir=out,config={'question_understanding':True})
            self.assertEqual((m.loads,q.calls),(1,1));self.assertFalse(result['labels_read'])
            self.assertEqual(json.loads((out/'predictions.jsonl').read_text())['answer'],['Alice'])


class MetadataContractTests(unittest.TestCase):
    def fixture(self):
        t={'clean_view':'sales','columns':[{'name':'order_id'},{'name':'amount'}]}
        p={'grain':{'kind':'order_line','description':'One sale line','evidence':'One sale line'},
           'group_keys':[{'name':'order','columns':['order_id'],'purpose':'order','evidence':None}],
           'measures':[{'column':'amount','scope':'row','aggregation':'sum','unit':'VND','evidence':None}], 'joins':[]}
        return t,p
    def test_grain_is_candidate_even_with_literal_evidence(self):
        t,p=self.fixture();r=validate_proposal(p,t,{'sales':t},'One sale line')
        self.assertEqual(r['grain']['status'],'candidate')
        self.assertEqual(r['grain']['evidence']['status'],'readme_quote_verified')
    def test_unknown_keys_and_cross_business_joins_rejected(self):
        t,p=self.fixture();p['group_keys'][0]['columns']=['invented']
        with self.assertRaises(ValueError):validate_proposal(p,t,{'sales':t},'')
        t,p=self.fixture();p['joins']=[{'target':'other_biz','source_columns':['order_id'],'target_columns':['id'],'evidence':None}]
        with self.assertRaises(ValueError):validate_proposal(p,t,{'sales':t},'')

    def test_repeated_measure_scopes_are_not_accepted_as_native_grain(self):
        t,p=self.fixture();p['measures'].append({**p['measures'][0],'scope':'order'})
        with self.assertRaisesRegex(ValueError,'Duplicate measure'):validate_proposal(p,t,{'sales':t},'')


class SemanticContextTests(unittest.TestCase):
    def test_roles_grain_keys_and_answer_selection_reach_prompt_without_profiles(self):
        import hashlib
        from datamate_baseline.semantic_context import SemanticContextLoader
        h=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source';business=source/'data/biz_demo';business.mkdir(parents=True)
            prepared=root/'prepared/biz_demo';prepared.mkdir(parents=True)
            (business/'README.md').write_text('One sale line.');(source/'rulebook.md').write_text('Synthetic source rules')
            col={'name':'customer','original_name':'Khách hàng','role':'customer_name','canonical_role':'customer_name','semantic_role':None,'sql_type':'TEXT','description':'customer name','has_data':True,'source_profile':{'samples':['PRIVATE_PROFILE_VALUE']}}
            catalog={'business_id':'biz_demo','schema':'test','fingerprint':'test','rulebook_sha256':h(source/'rulebook.md'),
                'sources':[{'name':'README.md','sha256':h(business/'README.md')}],
                'tables':{'sales':{'name':'sales','clean_view':'sales_clean','role':'sale_lines','columns':[col],'rows':1,'duplicates':0}}}
            cp=prepared/'postgres_catalog.json';cp.write_text(json.dumps(catalog))
            meta=prepared/'business_metadata';meta.mkdir()
            table={'grain':{'kind':'order_line','status':'candidate'},'group_keys':[],'measures':[],'joins':[]}
            value={'version':'business-metadata-v1','business_id':'biz_demo','catalog_sha256':h(cp),'readme_sha256':h(business/'README.md'),
                   'rulebook_sha256':h(source/'rulebook.md'),'existing_relationship_candidates':[],'tables':{'sales_clean':table}}
            (meta/'metadata.json').write_text(json.dumps(value))
            loader=SemanticContextLoader(root/'prepared',source);context=loader.load('biz_demo')
            messages=loader.messages_for_plan(context,{'question':'synthetic'},ANALYSIS,LINKS)
            text=str(messages)
            self.assertIn('customer names',text);self.assertIn('order_line',text)
            self.assertIn('canonical_role',text);self.assertIn('semantic_role',text)
            self.assertNotIn('PRIVATE_PROFILE_VALUE',text)
            value['catalog_sha256']='tampered';(meta/'metadata.json').write_text(json.dumps(value))
            with self.assertRaises(ValueError):SemanticContextLoader(root/'prepared',source).load('biz_demo')


@unittest.skipUnless(os.environ.get('DATAMATE_TEST_PG_DSN'),'Set test PostgreSQL DSN')
class MetadataDatabaseTests(unittest.TestCase):
    def test_group_key_can_repeat_and_join_fanout_is_detected(self):
        schema='metadata_test_'+uuid.uuid4().hex[:10]
        with psycopg.connect(os.environ['DATAMATE_TEST_PG_DSN'],autocommit=True) as conn:
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            try:
                conn.execute(sql.SQL('CREATE TABLE {}.sales (order_id TEXT, amount NUMERIC)').format(sql.Identifier(schema)))
                conn.execute(sql.SQL("INSERT INTO {}.sales VALUES ('a',10),('a',20),('b',30),(NULL,40)").format(sql.Identifier(schema)))
                stats=key_stats(conn,schema,'sales',['order_id'])
                self.assertFalse(stats['row_key_supported']);self.assertEqual(stats['distinct_non_null_groups'],2)
                conn.execute(sql.SQL('CREATE TABLE {}.payments (order_id TEXT)').format(sql.Identifier(schema)))
                conn.execute(sql.SQL("INSERT INTO {}.payments VALUES ('a'),('a')").format(sql.Identifier(schema)))
                table={'clean_view':'sales','columns':[{'name':'order_id','sql_type':'TEXT'},{'name':'amount','sql_type':'NUMERIC'}]}
                target={'clean_view':'payments','columns':[{'name':'order_id','sql_type':'TEXT'}]}
                meta={'group_keys':[{'name':'order','columns':['order_id']}], 'measures':[{'column':'amount'}],
                      'joins':[{'target':'payments','source_columns':['order_id'],'target_columns':['order_id']}]}
                with conn.transaction():
                    conn.execute('SET TRANSACTION READ ONLY')
                    checked=check_table(conn,{'schema':schema},table,meta,{'payments':target})
                j=checked['joins'][0]['data_checks']
                self.assertEqual(j['cardinality'],'many_to_many');self.assertTrue(j['fanout_risk'])
                self.assertEqual(j['orphan_source_rows'],1)
                self.assertEqual(checked['measures'][0]['data_checks'][0]['groups_with_multiple_values'],1)
            finally:conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


class ComparisonTests(unittest.TestCase):
    def test_comparison_requires_same_question_and_counts_paired_outcomes(self):
        import subprocess
        from datamate_eval.runner import evaluate
        q={'id':'synthetic','business_id':'demo','question':'Synthetic amount','as_of_date':'2026-01-01'}
        ref={'id':'synthetic','type':'number','answer':4}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for name,answer in [('before',5),('after',4)]:
                folder=root/name;folder.mkdir();(folder/'evaluation').mkdir()
                prediction={'id':'synthetic','type':'number','answer':answer}
                trace={'id':'synthetic','request':q,'answer':prediction,'plan':{'sql':'SELECT amount FROM synthetic'},'elapsed_ms':1,'error':None,'sql_executed':True,'json_valid':True,'raw_output':'{}'}
                (folder/'questions.jsonl').write_text(json.dumps(q)+'\n')
                (folder/'traces.jsonl').write_text(json.dumps(trace)+'\n')
                (folder/'run.json').write_text(json.dumps({'status':'complete','elapsed_ms':2}))
                report,_=evaluate([q],[ref],[prediction],timings=[{'id':'synthetic','elapsed_ms':1}])
                (folder/'evaluation/report.json').write_text(json.dumps(report))
                (folder/'evaluation/diagnostics.jsonl').write_text(json.dumps({'id':'synthetic','outcome':'correct' if answer==4 else 'wrong','expected':ref})+'\n')
            cmd=[sys.executable,str(ROOT/'scripts/compare_baseline_runs.py'),'--before',str(root/'before'),'--after',str(root/'after'),'--output-dir',str(root/'comparison')]
            result=subprocess.run(cmd,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            comparison=json.loads((root/'comparison/comparison.json').read_text())
            self.assertEqual(comparison['transitions'],{'wrong -> correct':1})
            self.assertEqual(comparison['after']['pa'],1)
            (root/'after/questions.jsonl').write_text(json.dumps({**q,'question':'Changed question'})+'\n')
            cmd[-1]=str(root/'invalid_comparison')
            self.assertNotEqual(subprocess.run(cmd,capture_output=True).returncode,0)


if __name__=='__main__':unittest.main()
