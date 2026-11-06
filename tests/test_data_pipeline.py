"""Parser/adapter/normalization contracts and optional real PostgreSQL integration."""
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import shutil
import sys
import tempfile
import unittest
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from datamate_data.normalization import column_spec,identifier,normalize_value,parse_date,parse_number
from datamate_data.pipeline import Conversions,prepare_business
from datamate_data.parsers import parse_file


class NormalizationTests(unittest.TestCase):
    def test_readme_columns_are_scoped_to_sheet(self):
        from datamate_data.adapters import readme_column_sections
        sections=readme_column_sections('## Cột trong `export.xlsx`, sheet `Lines`\n- **Tổng tiền**: thành tiền sau giảm\n## Cột trong `export.xlsx`, sheet `Orders`\n- **Tổng tiền**: tổng đơn\n')
        self.assertEqual(sections[('export.xlsx','Lines')]['tongtien'],'thành tiền sau giảm')
        self.assertEqual(sections[('export.xlsx','Orders')]['tongtien'],'tổng đơn')
        self.assertEqual(column_spec('Số lượng nhập','số lượng nhập (đơn vị cơ bản)')['role'],'base_quantity')

    def test_mixed_vnd_formats_and_exact_decimal(self):
        for text,wanted in [('1tr440',1440000),('720.000đ',720000),('38,000',38000),
                            ('1.234,56',Decimal('1234.56')),('(20.000)',-20000),('2,5tr',2500000)]:
            with self.subTest(text=text):self.assertEqual(parse_number(text,currency=True),wanted)
        self.assertEqual(parse_number('0,5'),Decimal('.5'))
        self.assertEqual(parse_number('0.125'),Decimal('.125'))

    def test_ambiguous_quantity_and_invalid_value_not_zero(self):
        for text in ('1,250','1.250','NaN','abc','1.2.3'):
            with self.subTest(text=text),self.assertRaises(ValueError):parse_number(text)
        spec=column_spec('Số lượng')
        value,status,_=normalize_value('unknown',spec)
        self.assertIsNone(value);self.assertTrue(status.startswith('error:'))
        self.assertEqual(normalize_value('0',spec)[0],0)

    def test_dates_need_year_evidence_and_no_host_date(self):
        self.assertEqual(str(parse_date('1/1',year=2025)),'2025-01-01')
        self.assertEqual(str(parse_date('1 thg 6 2025')),'2025-06-01')
        for text in ('1/1','31/02/2025','2025-01-01T01:00:00+07:00'):
            with self.subTest(text=text),self.assertRaises(ValueError):parse_date(text)

    def test_identifiers_padding_and_text_not_numeric(self):
        spec=column_spec('Mã hàng')
        self.assertEqual(normalize_value('0001',spec)[0],'0001')
        self.assertEqual(normalize_value(1,spec,cell={'number_format':'0000'})[0],'0001')
        self.assertIn('unknown',normalize_value(1,spec)[1])
        self.assertLessEqual(len(identifier('Thành tiền'*40)),52)

    def test_unknown_status_not_completed(self):
        spec=column_spec('Trạng thái')
        self.assertEqual(normalize_value('Đang giao',spec)[0],'delivering')
        self.assertIsNone(normalize_value('something-new',spec)[0])

    def test_conversion_store_scope_and_conflicting_factors(self):
        c=Conversions();c.add('SP1','hộp',12,'viên','a',store='A');c.add('SP1','hộp',24,'viên','b',store='B')
        self.assertEqual(c.resolve(['SP1'],'hộp','A'),('12','viên'))
        self.assertEqual(c.resolve(['SP1'],'hộp','B'),('24','viên'))
        c.add('SP1','hộp',15,'viên','c',store='A')
        self.assertIsNone(c.resolve(['SP1'],'hộp','A'));self.assertTrue(c.conflicts)


class PipelineFixtures(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.business=self.root/'demo';self.business.mkdir()
        (self.business/'README.md').write_text('# Synthetic '+self.root.name+'\n- Dữ liệu có đến hết 04/03/2026.\n- Nước: 1 thùng = 24 chai\n',encoding='utf-8')
        self.rulebook=self.root/'rulebook.md';self.rulebook.write_text('Synthetic rules')
        self.calendar=self.root/'calendar.csv';self.calendar.write_text('date\n2026-03-04\n')

    def tearDown(self):self.temp.cleanup()

    def prepare(self,output='prepared'):
        return prepare_business(self.business,self.root/output,rulebook=self.rulebook,calendar=self.calendar)

    def rows(self,catalog):
        table=next(iter(catalog['tables'].values()))
        return [json.loads(l) for l in (self.root/'prepared'/table['data_path']).read_text().splitlines()]

    def test_csv_preserves_source_rows_and_duplicate_orders(self):
        path=self.business/'sales.csv'
        path.write_text('Mã hóa đơn,Ngày bán,Tên hàng,Số lượng,Đơn vị tính,Thành tiền,Trạng thái\n'
            'A,03/03/2026,Nước,2,thùng,1tr440,Hoàn thành\n'
            'A,03/03/2026,Nước,2,thùng,1tr440,Hoàn thành\n'
            'B,03/03/2026,Nước,2,thùng,1tr440,Hoàn thành\n',encoding='utf-8')
        before=path.read_bytes();catalog=self.prepare();rows=self.rows(catalog)
        self.assertEqual(catalog['total_source_records'],3)
        self.assertIsNotNone(rows[1]['duplicate_of']);self.assertIsNone(rows[2]['duplicate_of'])
        self.assertEqual(rows[0]['quantity_base'],'48')
        self.assertEqual(rows[0]['raw'][5],'1tr440')
        self.assertEqual(rows[0]['source']['row'],2);self.assertEqual(path.read_bytes(),before)

    def test_no_order_table_does_not_drop_equal_events(self):
        (self.business/'events.csv').write_text('Ngày bán,Tên hàng,Số lượng,Thành tiền\n03/03/2026,Nước,2,10\n03/03/2026,Nước,2,10\n')
        rows=self.rows(self.prepare());self.assertEqual(len(rows),2)
        self.assertTrue(all(r['duplicate_of'] is None for r in rows))

    def test_base_unit_default_requires_rulebook_evidence(self):
        (self.business/'events.csv').write_text('Tên hàng,Số lượng,Đơn vị tính,Thành tiền\nMón mới,2,ly,10\n')
        rows=self.rows(self.prepare());self.assertIsNone(rows[0]['quantity_base'])
        self.rulebook.write_text('đơn vị không có dòng quy đổi nào là đơn vị cơ bản, hệ số 1')
        cat=self.prepare('with_rule');table=next(iter(cat['tables'].values()))
        row=json.loads((self.root/'with_rule'/table['data_path']).read_text().splitlines()[0])
        self.assertEqual(row['quantity_base'],'2');self.assertEqual(row['unit_base'],'ly')

    def test_typed_numeric_is_not_reparsed_using_source_locale(self):
        from datamate_data.pipeline import role_values
        meta={'columns':[{'role':'quantity','sql_type':'NUMERIC'}]}
        self.assertEqual(role_values(meta,{'typed':['1.125']})['quantity'],Decimal('1.125'))

    def test_fill_only_order_fields_and_group_boundaries(self):
        (self.business/'sales.csv').write_text('Mã hóa đơn,Ngày bán,Tên hàng,Số lượng,Thành tiền,Trạng thái\nA,03/03/2026,Nước,2,10,Đang giao\nA,,Nước,,20,\nB,04/03/2026,Nước,1,30,\n')
        catalog=self.prepare();meta=next(iter(catalog['tables'].values()));rows=self.rows(catalog)
        index={c['role']:i for i,c in enumerate(meta['columns'])}
        self.assertEqual(rows[1]['typed'][index['status']],'delivering')
        self.assertIsNone(rows[1]['typed'][index['quantity']])
        self.assertIsNone(rows[2]['typed'][index['status']])

    def test_ragged_row_quarantined_and_parse_failure_retained(self):
        (self.business/'sales.csv').write_text('Mã hóa đơn,Tên hàng,Số lượng,Thành tiền\nA,Nước,abc,10\nB,Nước,2,20,EXTRA\n')
        rows=self.rows(self.prepare());self.assertIn('so_luong',rows[0]['quality']['errors'])
        self.assertEqual(rows[1]['kind'],'quarantine')
        self.assertEqual(rows[1]['quality']['source_flags'][0]['extra_cells'],['EXTRA'])

    def test_labels_and_symlinks_are_not_sources(self):
        (self.business/'sales.csv').write_text('Tên hàng,Số lượng\nNước,1\n')
        (self.business/'answers.jsonl').write_text('DO NOT READ: not valid JSON')
        catalog=self.prepare();self.assertIn('answers.jsonl',catalog['ignored_files'])
        (self.business/'link.csv').symlink_to(self.business/'sales.csv')
        with self.assertRaises(ValueError):self.prepare('second')

    def test_sqlite_read_only_keeps_declared_schema_and_source(self):
        path=self.business/'pos.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE sales (MaHoaDon TEXT, MaHangHoa TEXT, SL INTEGER, TriGia INTEGER)')
            db.execute("INSERT INTO sales VALUES ('0001','0002',2,50)")
        before=path.read_bytes();catalog=self.prepare();rows=self.rows(catalog)
        self.assertEqual(rows[0]['typed'][:2],['0001','0002'])
        self.assertEqual(path.read_bytes(),before)
        self.assertTrue(rows[0]['source']['sqlite_scan_ordinal'])

    @unittest.skipUnless(importlib.util.find_spec('psycopg'),'requires requirements-data.txt')
    def test_changed_staging_cannot_be_loaded(self):
        from datamate_data.postgres import load_business
        (self.business/'sales.csv').write_text('Tên hàng,Số lượng\nNước,2\n')
        cat=self.prepare();table=next(iter(cat['tables'].values()))
        with (self.root/'prepared'/table['data_path']).open('a') as f:f.write('\n')
        with self.assertRaisesRegex(ValueError,'checksum'):
            load_business('never-connected',self.root/'prepared')

    @unittest.skipUnless(importlib.util.find_spec('openpyxl'),'requires requirements-data.txt')
    def test_excel_title_sparse_cells_year_and_missing_formula_cache(self):
        from openpyxl import Workbook
        wb=Workbook();ws=wb.active;ws.title='T01_2025'
        ws.append(['BÁO CÁO BÁN HÀNG']);ws.append([])
        ws.append(['Mã hóa đơn','Ngày bán','Tên hàng','Số lượng','Đơn giá','Thành tiền'])
        ws.append(['A','1/1','Nước',2,10,'=D4*E4'])
        ws.append(['B','2/1',None,1,20,20])
        path=self.business/'monthly.xlsx';wb.save(path);wb.close()
        catalog=self.prepare();rows=self.rows(catalog);meta=next(iter(catalog['tables'].values()))
        self.assertEqual(meta['source_parts'][0]['header_row'],3)
        self.assertEqual(rows[0]['typed'][1],'2025-01-01')
        self.assertIsNone(rows[0]['typed'][5]);self.assertEqual(rows[0]['quality']['errors']['thanh_tien'],'formula_cache_missing')
        self.assertIsNone(rows[1]['typed'][2]);self.assertEqual(rows[1]['typed'][4],'20')

    @unittest.skipUnless(os.environ.get('DATAMATE_TEST_PG_DSN'),'set isolated test PostgreSQL DSN')
    def test_real_postgres_copy_profiles_and_cache(self):
        from datamate_data.postgres import load_business
        import psycopg
        from psycopg import sql
        (self.business/'sales.csv').write_text('Mã hóa đơn,Tên hàng,Số lượng,Thành tiền\nA,Nước,2,10\nA,Nước,2,10\nB,Nước,1,20\n')
        catalog=self.prepare();dsn=os.environ['DATAMATE_TEST_PG_DSN'];schema=catalog['schema']
        try:
            first=load_business(dsn,self.root/'prepared');self.assertFalse(first['cache_hit'])
            self.assertTrue(load_business(dsn,self.root/'prepared')['cache_hit'])
            shutil.copytree(self.root/'prepared',self.root/'fresh_output',ignore=shutil.ignore_patterns('postgres_catalog.json'))
            self.assertTrue(load_business(dsn,self.root/'fresh_output')['cache_hit'])
            self.assertTrue((self.root/'fresh_output/postgres_catalog.json').exists())
            table=next(iter(first['catalog']['tables'].values()))
            with psycopg.connect(dsn) as conn:
                rows,total=conn.execute(sql.SQL('SELECT count(*),sum(thanh_tien) FROM {}.{}').format(sql.Identifier(schema),sql.Identifier(table['clean_view']))).fetchone()
                self.assertEqual(rows,2);self.assertEqual(total,30)
                raw=conn.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(sql.Identifier(schema),sql.Identifier(table['name']))).fetchone()[0]
                self.assertEqual(raw,3)
        finally:
            # Only the fingerprinted schema created by this synthetic test is removed.
            with psycopg.connect(dsn) as conn:conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))

    @unittest.skipUnless(os.environ.get('DATAMATE_TEST_PG_DSN'),'set isolated test PostgreSQL DSN')
    def test_postgres_relationship_uses_store_and_order_composite_key(self):
        from datamate_data.postgres import load_business
        import psycopg
        from psycopg import sql
        with sqlite3.connect(self.business/'pos.sqlite') as db:
            db.execute('CREATE TABLE orders (IDCuaHang TEXT, MaHoaDon TEXT, TongThanhToan INTEGER)')
            db.execute('CREATE TABLE lines (IDCuaHang TEXT, MaHoaDon TEXT, MaHangHoa TEXT, SL INTEGER, TriGia INTEGER)')
            db.executemany('INSERT INTO orders VALUES (?,?,?)',[('A','001',10),('B','001',20)])
            db.executemany('INSERT INTO lines VALUES (?,?,?,?,?)',[('A','001','P1',1,10),('B','001','P1',2,20)])
        cat=self.prepare();dsn=os.environ['DATAMATE_TEST_PG_DSN']
        try:
            result=load_business(dsn,self.root/'prepared')
            relations=[r for r in result['catalog']['relationships'] if r['column_pairs'][0][0]=='mahoadon']
            self.assertTrue(relations)
            self.assertTrue(all(len(r['column_pairs'])==2 and r['orphan_rows']==0 for r in relations))
        finally:
            with psycopg.connect(dsn) as conn:conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(cat['schema'])))


if __name__=='__main__':unittest.main()
