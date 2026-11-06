"""Tooling tests use synthetic outputs, never claim a real model passed B1."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from datamate_b.contracts import parse_result, strict_json, validate_request
from datamate_b.model import LocalModel, ModelConfig
from datamate_b.prompts import build_messages

SCHEMA = (ROOT / "examples/b1_schema.sql").read_text(encoding="utf-8")
RULES = (ROOT / "examples/b1_rules.md").read_text(encoding="utf-8")


def plan(sql="SELECT SUM(amount_vnd) AS revenue FROM sales"):
    return dict(status="sql", answer_type="number", sql=sql, missing_requirements=[])


class ContractTests(unittest.TestCase):
    def test_valid_alternative_queries_and_cte(self):
        for sql in (
            "SELECT SUM(s.amount_vnd) FROM sales AS s WHERE s.status = 'completed'",
            "WITH totals AS (SELECT product_id, SUM(quantity) qty FROM sales GROUP BY product_id) "
            "SELECT p.product_name FROM products p JOIN totals t ON p.product_id=t.product_id "
            "ORDER BY t.qty DESC, p.product_id LIMIT 1",
            "SELECT product_name FROM products UNION ALL SELECT product_name FROM products",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(parse_result(json.dumps(plan(sql)), SCHEMA)["sql"], sql)

    def test_reject_invalid_json(self):
        for raw in ("", '{"status":', '```json\n{}\n```', '{} trailing',
                    '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', '{"x":1,"x":2}', '[]'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_result(raw, SCHEMA)

    def test_reject_invalid_contracts(self):
        for value in (
            {**plan(), "status": "answer"}, {**plan(), "answer_type": "integer"},
            {**plan(), "answer_type": []}, {**plan(), "sql": None},
            {**plan(), "extra": True}, {**plan(), "missing_requirements": [""]},
            dict(status="unanswerable", answer_type="unanswerable", sql=None, missing_requirements=[]),
            dict(status="abstain", answer_type="number", sql=None, missing_requirements=[]),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_result(json.dumps(value), SCHEMA)

    def test_missing_and_abstain(self):
        for status, missing in (("unanswerable", ["Missing expense data"]), ("abstain", [])):
            value = dict(status=status, answer_type=status, sql=None, missing_requirements=missing)
            self.assertEqual(parse_result(json.dumps(value), SCHEMA), value)

    def test_reject_mutations_external_unknown_and_broken_sql(self):
        for sql in (
            "SELECT 1; DROP TABLE sales", "DELETE FROM sales",
            "SELECT * FROM read_csv_auto('/tmp/private.csv')", "SELECT * FROM '/tmp/private.csv'",
            "SELECT * FROM other.sales", "SELECT * FROM invented", "SELECT cogs FROM sales",
            "SELECT SUM( FROM sales", "SELECT CURRENT_DATE FROM sales", "SELECT now()",
            "WITH removed AS (DELETE FROM sales RETURNING *) SELECT * FROM removed",
            "SELECT * INTO new_table FROM sales",
        ):
            with self.subTest(sql=sql), self.assertRaises(Exception):
                parse_result(json.dumps(plan(sql)), SCHEMA)

    def test_requests_and_fixture_time_boundary(self):
        cases = [strict_json(line) for line in (ROOT / "examples/b1_questions.jsonl").read_text().splitlines()]
        self.assertEqual(len(cases), 3)
        for case in cases:
            validate_request(case)
        for invalid in ("2026-02-30", "20260304", "2026-3-4", "tomorrow", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_request({"question": "demo", "as_of_date": invalid})
        # Independent calendar oracle in tests only; no expected dates/SQL in prompts.
        as_of = date.fromisoformat(cases[1]["as_of_date"])
        end = as_of - timedelta(days=as_of.weekday())
        self.assertEqual((end - timedelta(days=7), end), (date(2026, 2, 23), date(2026, 3, 2)))

    def test_prompt_does_not_include_labels_or_oracle(self):
        case = {"question": "Một câu hỏi mới", "as_of_date": "2026-06-10", "expected_sql": "SECRET_LABEL"}
        text = json.dumps(build_messages(SCHEMA, RULES, case), ensure_ascii=False)
        self.assertNotIn("SECRET_LABEL", text)
        self.assertNotIn("2026-02-23", text)
        self.assertIn("Một câu hỏi mới", text)
        self.assertIn("2026-06-10", text)

    def test_project_postgres_dialect_and_explicit_legacy_duckdb(self):
        request={"question":"demo","as_of_date":"2026-06-10"}
        self.assertIn('PostgreSQL',build_messages(SCHEMA,RULES,request)[0]['content'])
        self.assertIn('DuckDB',build_messages(SCHEMA,RULES,request,'duckdb')[0]['content'])
        for dialect in ('postgres','duckdb'):
            self.assertEqual(parse_result(json.dumps(plan()),SCHEMA,dialect)['status'],'sql')
        with self.assertRaises(ValueError):build_messages(SCHEMA,RULES,request,'mysql')


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("smoke_cli", ROOT / "scripts/smoke_sql.py")
        cls.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cli)

    def test_load_once_and_log_success_errors_and_truncation(self):
        class StubModel:
            loads = 0
            calls = 0
            def __init__(self, config):
                pass
            def load(self):
                self.loads += 1
                self.load_ms = 12.0
            def metadata(self):
                return {"test_double": True}
            def generate(self, messages):
                self.calls += 1
                if self.calls == 3:
                    raise RuntimeError("synthetic failure")
                return {"raw_output": json.dumps(plan()) if self.calls == 1 else '{"status":',
                        "prompt_tokens": 20, "output_tokens": 5, "generation_ms": 1.0,
                        "hit_token_limit": False}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.jsonl"
            argv = ["smoke_sql.py", "--model-path", "unused-test-double", "--schema", str(ROOT / "examples/b1_schema.sql"),
                    "--rules", str(ROOT / "examples/b1_rules.md"), "--questions-file", str(ROOT / "examples/b1_questions.jsonl"),
                    "--output", str(output)]
            model = StubModel(None)
            with patch.object(self.cli, "LocalModel", return_value=model), patch.object(sys, "argv", argv):
                self.assertEqual(self.cli.main(), 1)
            self.assertEqual(model.loads, 1)
            self.assertEqual(model.calls, 3)
            records = [strict_json(line) for line in output.read_text().splitlines()]
            self.assertTrue(records[0]["valid"])
            self.assertIsNotNone(records[1]["parse_error"])
            self.assertIsNotNone(records[2]["generation_error"])
            for record in records:
                self.assertIsNone(record["executed_code"])
                self.assertIsNone(record["reasoning_generated"])
            report = strict_json(output.with_suffix(".jsonl.run.json").read_text())
            self.assertEqual(report["load_ms"], 12.0)
            self.assertEqual(report["failures"], 2)

    def test_token_budget_output_is_rejected_even_if_json_parses(self):
        class Stub:
            def generate(self, messages):
                return {"raw_output": json.dumps(plan()), "hit_token_limit": True}
            def metadata(self):
                return {"test_double": True}
        out = io.StringIO()
        request = {"question": "demo", "as_of_date": "2026-06-10"}
        self.assertEqual(self.cli.run_questions(Stub(), SCHEMA, RULES, [request], out), 1)
        self.assertIsNone(strict_json(out.getvalue())["parsed_result"])

    def test_preserve_parsed_json_on_contract_failure(self):
        class Stub:
            def generate(self, messages):
                return {"raw_output": json.dumps(plan("SELECT CURRENT_DATE"))}
            def metadata(self):
                return {"test_double": True}
        out = io.StringIO()
        request = {"question": "demo", "as_of_date": "2026-06-10"}
        self.assertEqual(self.cli.run_questions(Stub(), SCHEMA, RULES, [request], out), 1)
        record = strict_json(out.getvalue())
        self.assertIsNotNone(record["parsed_json"])
        self.assertIsNone(record["parse_error"])
        self.assertIsNotNone(record["contract_error"])
        self.assertIsNone(record["parsed_result"])

    def test_missing_path_fails_before_hf_load_and_writes_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.jsonl"
            result = subprocess.run([sys.executable, str(ROOT / "scripts/smoke_sql.py"),
                "--schema", str(ROOT / "examples/b1_schema.sql"), "--rules", str(ROOT / "examples/b1_rules.md"),
                "--question", "demo", "--as-of-date", "2026-01-01", "--model-path", str(Path(directory) / "missing"),
                "--output", str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())
            self.assertIn("Local model directory not found", output.with_suffix(".jsonl.run.json").read_text())

    def test_model_config_validation_and_metadata_only_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "manifest.json").write_text('{"weights_downloaded":false}')
            with self.assertRaisesRegex(ValueError, "metadata-only"):
                LocalModel(ModelConfig(directory))
            for config in (ModelConfig(directory, device="auto"), ModelConfig(directory, dtype="int8"),
                           ModelConfig(directory, max_new_tokens=5000), ModelConfig(directory, device="cpu")):
                with self.assertRaises(ValueError):
                    LocalModel(config)


if __name__ == "__main__":
    unittest.main()
