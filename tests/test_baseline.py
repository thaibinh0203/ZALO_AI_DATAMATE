"""Baseline tests use synthetic fixtures, never real QA labels in model prompts."""
from datetime import date,datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict,make_conninfo
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from datamate_baseline.serializer import dumps,format_answer
from datamate_baseline.query_tool import QueryTool,checked_sql
from datamate_baseline.runner import run_batch,requests_from_file

DDL='CREATE TABLE sales_clean (amount NUMERIC, item TEXT, day DATE);'
ALLOWED={'sales_clean':{'amount':'NUMERIC','item':'TEXT','day':'DATE'}}


class SQLGateTests(unittest.TestCase):
    def test_aggregate_cte_and_columns(self):
        for query in ['SELECT sum(amount) FROM sales_clean',
            "SELECT amount FROM sales_clean WHERE amount>1 AND (item='a' OR item='b')",
            'SELECT item FROM sales_clean a WHERE EXISTS(SELECT 1 FROM sales_clean b WHERE b.item=a.item)',
            'WITH totals AS (SELECT item,sum(amount) AS total FROM sales_clean GROUP BY item) SELECT item,total FROM totals ORDER BY total DESC',
            "SELECT coalesce(sum(amount),0) FROM sales_clean WHERE day>=DATE '2026-01-01'"]:
            self.assertTrue(checked_sql(query,DDL,ALLOWED))

    def test_projection_star_is_expanded_to_exposed_columns(self):
        q=checked_sql('SELECT * FROM sales_clean',DDL,ALLOWED)
        self.assertNotIn('*',q)
        self.assertIn('amount',q)

    def test_writes_external_and_unknown_relations_are_rejected(self):
        queries=['DROP TABLE sales_clean','SELECT 1; DELETE FROM sales_clean',
            'WITH t AS (DELETE FROM sales_clean RETURNING *) SELECT * FROM t',
            'SELECT * INTO TEMP stolen FROM sales_clean','SELECT * FROM other.sales_clean',
            'SELECT * FROM pg_catalog.pg_class','SELECT * FROM pg_class',
            "SELECT pg_read_file('/etc/passwd')",'SELECT pg_catalog.pg_sleep(1)',
            "SELECT set_config('search_path','public',true)",'SELECT * FROM hidden_clean',
            'SELECT absent_column FROM sales_clean',
            "SELECT 'pg_class'::regclass",'SELECT CURRENT_DATE',"SELECT 'today'::date",
            'SELECT amount FROM sales_clean FOR UPDATE']
        for q in queries:
            with self.subTest(q=q),self.assertRaises(Exception):checked_sql(q,DDL,ALLOWED)

    def test_cte_name_cannot_hide_catalog_relation(self):
        with self.assertRaises(ValueError):checked_sql('WITH pg_class AS (SELECT COUNT(*) AS n FROM pg_class) SELECT n FROM pg_class',DDL,ALLOWED)


class SerializationTests(unittest.TestCase):
    def test_decimal_json_does_not_round_or_quote(self):
        raw=dumps({'n':Decimal('9007199254740993.123456789')})
        self.assertEqual(json.loads(raw,parse_float=Decimal)['n'],Decimal('9007199254740993.123456789'))
        self.assertNotIn('"9007',raw)

    def result(self,rows,cols=None,truncated=False):return {'columns':cols or ['x'],'rows':rows,'truncated':truncated}

    def test_shapes_and_types(self):
        self.assertEqual(format_answer('q','number',self.result([(Decimal('12.30'),)]))['answer'],Decimal('12.30'))
        self.assertEqual(format_answer('q','boolean',self.result([(True,)]))['answer'],True)
        self.assertEqual(format_answer('q','date',self.result([(date(2026,1,2),)]))['answer'],'2026-01-02')
        self.assertEqual(format_answer('q','list',self.result([('a',),('b',)]))['answer'],['a','b'])
        self.assertEqual(format_answer('q','table',self.result([('a',2)],['name','total']))['answer']['rows'],[['a',2]])

    def test_null_truncation_or_wrong_scalar_shape_never_becomes_zero(self):
        for r in [self.result([(None,)]),self.result([]),self.result([(1,),(2,)]),self.result([(1,)],truncated=True)]:
            with self.assertRaises(ValueError):format_answer('q','number',r)
        with self.assertRaises(ValueError):format_answer('q','number',self.result([(True,)]))
        with self.assertRaises(ValueError):format_answer('q','date',self.result([(datetime(2026,1,1,12),)]))


class RunnerTests(unittest.TestCase):
    class Loader:
        prepared_root=ROOT;source_root=ROOT
        def load(self,biz):return {'fingerprint':'synthetic','pg_schema':'synthetic','ddl':DDL,'allowed':ALLOWED}
        def messages(self,context,request):return [{'role':'system','content':'Synthetic fixture'},{'role':'user','content':json.dumps(request)}]
    class Model:
        load_ms=0
        def __init__(self,outputs):self.outputs=iter(outputs);self.loads=0;self.calls=0
        def load(self):self.loads+=1
        def metadata(self):return {'fixture':True}
        def generate(self,messages):
            self.calls+=1
            return {'raw_output':next(self.outputs),'hit_token_limit':False,'generation_ms':0}
    class Tool:
        def __init__(self):self.calls=0
        def execute_sql(self,context,q):self.calls+=1;return {'columns':['x'],'rows':[(Decimal('123.45'),)],'truncated':False}
    def requests(self,n):return [{'id':str(i),'business_id':'synthetic','question':'Synthetic sum','as_of_date':'2026-01-01'} for i in range(n)]

    def test_batch_loads_once_and_one_query_only_for_sql(self):
        outputs=[json.dumps({'status':'sql','answer_type':'number','sql':'SELECT SUM(amount) FROM sales_clean','missing_requirements':[]}),
            json.dumps({'status':'unanswerable','answer_type':'unanswerable','sql':None,'missing_requirements':['Synthetic missing source']})]
        m=self.Model(outputs);tool=self.Tool()
        with tempfile.TemporaryDirectory() as t:
            run=run_batch(self.requests(2),model=m,query_tool=tool,loader=self.Loader(),output_dir=Path(t)/'run',config={})
            self.assertEqual((m.loads,m.calls,tool.calls),(1,2,1))
            self.assertEqual(run['status'],'complete')
            self.assertFalse(run['labels_read'])

    def test_malformed_json_and_sql_failure_abstain_without_retry(self):
        outputs=['not-json',json.dumps({'status':'sql','answer_type':'number','sql':'SELECT missing FROM sales_clean','missing_requirements':[]})]
        m=self.Model(outputs);tool=self.Tool()
        with tempfile.TemporaryDirectory() as t:
            run=run_batch(self.requests(2),model=m,query_tool=tool,loader=self.Loader(),output_dir=Path(t)/'run',config={})
            rows=[json.loads(l) for l in (Path(t)/'run/predictions.jsonl').read_text().splitlines()]
            self.assertEqual([r['type'] for r in rows],['abstain','abstain'])
            self.assertEqual((m.calls,tool.calls),(2,0))

    def test_labels_and_duplicate_ids_are_rejected_at_input(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'questions.jsonl';r=self.requests(1)[0]
            p.write_text(json.dumps({**r,'answer':42})+'\n')
            with self.assertRaises(ValueError):requests_from_file(p)
            p.write_text(json.dumps(r)+'\n'+json.dumps(r)+'\n')
            with self.assertRaises(ValueError):requests_from_file(p)


@unittest.skipUnless(os.environ.get('DATAMATE_TEST_PG_DSN'),'Set test admin PostgreSQL DSN')
class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin=os.environ['DATAMATE_TEST_PG_DSN'];cls.schema='baseline_test_'+uuid.uuid4().hex[:12];cls.role='datamate_test_'+uuid.uuid4().hex[:12]
        with psycopg.connect(cls.admin,autocommit=True) as c:
            c.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT').format(sql.Identifier(cls.role)))
            c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(cls.schema)))
            c.execute(sql.SQL('CREATE TABLE {}.raw (amount NUMERIC,item TEXT,day DATE,__raw TEXT)').format(sql.Identifier(cls.schema)))
            c.execute(sql.SQL("INSERT INTO {}.raw VALUES (10,'a','2026-01-01','hidden'),(20,'b','2026-01-02','hidden'),(30,'c','2026-01-03','hidden')").format(sql.Identifier(cls.schema)))
            c.execute(sql.SQL('CREATE VIEW {}.sales_clean AS SELECT * FROM {}.raw').format(sql.Identifier(cls.schema),sql.Identifier(cls.schema)))
            c.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(cls.schema),sql.Identifier(cls.role)))
            c.execute(sql.SQL('GRANT SELECT ON {}.sales_clean TO {}').format(sql.Identifier(cls.schema),sql.Identifier(cls.role)))
        kwargs=conninfo_to_dict(cls.admin);kwargs['user']=cls.role;kwargs.pop('password',None)
        cls.reader=make_conninfo(**kwargs);cls.context={'pg_schema':cls.schema,'ddl':DDL,'allowed':ALLOWED}
    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(cls.admin,autocommit=True) as c:
            c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(cls.schema)))
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(cls.role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(cls.role)))

    def test_sum_uses_all_rows_but_output_cap_detects_truncation(self):
        q=QueryTool(self.reader,max_rows=2)
        try:
            self.assertEqual(q.execute_sql(self.context,'SELECT SUM(amount) FROM sales_clean')['rows'],[(Decimal(60),)])
            r=q.execute_sql(self.context,'SELECT * FROM sales_clean ORDER BY item')
            self.assertTrue(r['truncated']);self.assertNotIn('__raw',r['columns'])
        finally:q.close()

    def test_owner_dsn_is_rejected(self):
        with self.assertRaises(ValueError):QueryTool(self.admin)

    def test_db_denies_write_and_raw_table_access(self):
        with psycopg.connect(self.reader,autocommit=True) as c:
            with self.assertRaises(psycopg.Error):c.execute(sql.SQL("UPDATE {}.sales_clean SET amount=0").format(sql.Identifier(self.schema)))
            with self.assertRaises(psycopg.Error):c.execute(sql.SQL('SELECT * FROM {}.raw').format(sql.Identifier(self.schema)))

    def test_timeout_rolls_back_and_next_query_works(self):
        q=QueryTool(self.reader,timeout_ms=1)
        try:
            heavy='SELECT SUM(a.amount*b.amount*c.amount*d.amount*e.amount*f.amount*g.amount*h.amount*i.amount*j.amount) FROM '+', '.join('sales_clean '+x for x in 'abcdefghij')
            with self.assertRaises(psycopg.errors.QueryCanceled):q.execute_sql(self.context,heavy)
            q.timeout_ms=1000
            self.assertEqual(q.execute_sql(self.context,'SELECT COUNT(*) FROM sales_clean')['rows'],[(3,)])
        finally:q.close()



class ContextTests(unittest.TestCase):
    def test_compact_context_does_not_expose_profiles_raw_or_other_business(self):
        import hashlib
        from datamate_baseline.context import ContextLoader
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';business=source/'data/biz_fixture';business.mkdir(parents=True)
            prepared=root/'prepared/biz_fixture';prepared.mkdir(parents=True)
            readme='Synthetic business only';rules='Synthetic rulebook only'
            (business/'README.md').write_text(readme);(source/'rulebook.md').write_text(rules)
            col={'name':'item','original_name':'Mục','role':'text','canonical_role':None,'semantic_role':None,
                'sql_type':'TEXT','description':'Thông tin nguồn','has_data':True,
                'source_profile':{'samples':['DO_NOT_INCLUDE_VALUE']},'semantic':{'status':'unresolved'}}
            catalog={'business_id':'biz_fixture','schema':'synthetic_schema','fingerprint':'synthetic_fp',
                'rulebook_sha256':hashlib.sha256(rules.encode()).hexdigest(),
                'sources':[{'name':'README.md','sha256':hashlib.sha256(readme.encode()).hexdigest()}],
                'tables':{'sale':{'name':'sale','clean_view':'sale_clean','role':'synthetic','rows':1,'duplicates':0,'columns':[col]}}}
            (prepared/'postgres_catalog.json').write_text(json.dumps(catalog))
            loader=ContextLoader(root/'prepared',source);context=loader.load('biz_fixture')
            messages=loader.messages(context,{'question':'Synthetic question','as_of_date':'2026-01-01'})
            self.assertNotIn('DO_NOT_INCLUDE_VALUE',str(messages))
            self.assertIsNone(context['schema'][0]['columns'][0]['role'])
            self.assertNotIn('__raw',context['ddl'])
            with self.assertRaises(ValueError):loader.load('../biz_fixture')
            (business/'README.md').write_text('Changed source')
            with self.assertRaises(ValueError):ContextLoader(root/'prepared',source).load('biz_fixture')



class EvaluationProtocolTests(unittest.TestCase):
    def fixture(self,root):
        import hashlib
        split=root/'split';run=root/'run';split.mkdir();run.mkdir()
        qs=[{'id':str(i),'business_id':'biz_fixture','question':'Synthetic '+str(i),'as_of_date':'2026-01-01'} for i in range(2)]
        files={'source_manifest.json':{'source_fingerprint':'synthetic'},
            'split_manifest.json':{'source_fingerprint':'synthetic','businesses':{'biz_fixture':{'partition':'train','family':'synthetic','industry':'synthetic'}},
                'partitions':{'train':{'business_ids':['biz_fixture'],'question_ids':['0','1'],'questions':2,'answer_types':{'number':2}}}}}
        for name,value in files.items():(split/name).write_text(json.dumps(value))
        (split/'inputs/train').mkdir(parents=True);(split/'evaluator/train').mkdir(parents=True)
        (split/'inputs/train/questions.jsonl').write_text(''.join(json.dumps(q)+'\n' for q in qs))
        (split/'evaluator/train/answers.jsonl').write_text(''.join(json.dumps({'id':str(i),'type':'number','answer':4})+'\n' for i in range(2)))
        names=['source_manifest.json','split_manifest.json','inputs/train/questions.jsonl','evaluator/train/answers.jsonl']
        (split/'artifact_checksums.json').write_text(json.dumps({n:hashlib.sha256((split/n).read_bytes()).hexdigest() for n in names}))
        (run/'run.json').write_text(json.dumps({'status':'complete','question_ids':['0'],'elapsed_ms':100,'startup_ms':20}))
        (run/'questions.jsonl').write_text(json.dumps(qs[0])+'\n')
        (run/'predictions.jsonl').write_text(json.dumps({'id':'0','type':'number','answer':4})+'\n')
        (run/'timings.jsonl').write_text(json.dumps({'id':'0','elapsed_ms':50})+'\n')
        return split,run

    def test_subset_requires_explicit_flag_and_uses_correct_denominator(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            split,run=self.fixture(Path(tmp))
            cmd=[sys.executable,str(ROOT/'scripts/evaluate_baseline_run.py'),'--run-dir',str(run),'--partition','train','--split-root',str(split)]
            result=subprocess.run(cmd,capture_output=True,text=True)
            self.assertEqual(result.returncode,2)
            self.assertIn('subset',result.stderr.lower())
            self.assertEqual(subprocess.run(cmd+['--subset'],capture_output=True).returncode,0)
            report=json.loads((run/'evaluation/report.json').read_text())
            self.assertEqual(report['scope'],'diagnostic_subset')
            self.assertEqual(report['summary']['total'],1)
            self.assertEqual(report['partition_questions'],2)

    def test_changed_question_is_not_scored_under_original_id(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            split,run=self.fixture(Path(tmp))
            q=json.loads((run/'questions.jsonl').read_text());q['question']='Changed prompt'
            (run/'questions.jsonl').write_text(json.dumps(q)+'\n')
            cmd=[sys.executable,str(ROOT/'scripts/evaluate_baseline_run.py'),'--run-dir',str(run),'--partition','train','--split-root',str(split),'--subset']
            self.assertEqual(subprocess.run(cmd,capture_output=True).returncode,2)
            self.assertFalse((run/'evaluation').exists())


if __name__=='__main__':unittest.main()
