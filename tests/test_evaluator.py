"""Conformance and failure tests for the local scorer, independent of model outputs."""
from decimal import Decimal
import json
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from datamate_eval.matching import AnswerFormatError, normalize_text, score_answer, validate_record
from datamate_eval.runner import evaluate, load_partition, read_jsonl, strict_json_loads


def ref(kind, value=None, alias=None, identity="q"):
    return {"id": identity, "type": kind, "answer": value, "alias": alias or []}


def pred(kind, value=None, identity="q"):
    return {"id": identity, "type": kind, "answer": value}


class MatchTests(unittest.TestCase):
    def check(self, kind, expected, actual, outcome="correct", aliases=None):
        result = score_answer(ref(kind, expected, aliases), pred(kind, actual))
        self.assertEqual(result["outcome"], outcome, result)
        return result

    def test_number_exact_without_rounding_or_relative_tolerance(self):
        self.check("number", 123456789, 123456788, "wrong")
        self.check("number", 1, 1.0)
        self.check("number", 51, 50.5, "wrong")
        self.check("number", Decimal("0.3"), Decimal("0.30000000000000004"), "wrong")
        self.check("number", 10**20, 10**20 + 1, "wrong")

    def test_booleans_are_not_numbers(self):
        self.check("number", 1, True, "wrong")
        self.check("boolean", True, 1, "wrong")
        self.check("boolean", False, False)
        self.check("boolean", True, "true", "wrong")

    def test_nonfinite_values_rejected(self):
        for value in (float("nan"), float("inf"), Decimal("NaN")):
            with self.subTest(value=value):
                result = self.check("number", 1, value, "wrong")
                self.assertIsNotNone(result["format_error"])

    def test_numbers_must_be_json_numbers(self):
        for value in ("123", "1.234đ", None, [], {}):
            self.check("number", 123, value, "wrong")

    def test_percent_units_sign_and_two_decimal_rounding(self):
        self.check("percent", Decimal("12.35"), Decimal("12.345"))
        self.check("percent", Decimal("-12.35"), Decimal("-12.345"))
        self.check("percent", Decimal("12.34"), Decimal("12.3449"))
        self.check("percent", 12.4, .124, "wrong")
        self.check("percent", -12.4, 12.4, "wrong")
        self.check("percent", 12.4, "12.4%", "wrong")

    def test_unicode_nfc_punctuation_and_accents(self):
        self.assertEqual(normalize_text("  CÀ PHÊ_sữa!!!\t"), "cà phê sữa")
        self.check("string", "Cà phê sữa", "CA\u0300 PHE\u0302 SU\u031b\u0303A")
        self.check("string", "Cà phê sữa", "ca phe sua", "wrong")
        self.check("string", "SP-01", "sp 01")
        self.check("string", "SP01", "SP_01", "wrong")

    def test_string_alias_is_explicit_not_global_accent_folding(self):
        self.check("string", "Cà phê sữa", "SP001", aliases=["SP001"])
        self.check("string", "Cà phê sữa", "ca phe sua", aliases=["ca phe sua"])
        self.check("string", "Cà phê sữa", ["Cà phê sữa"], "wrong")

    def test_valid_dates_and_months_exact_format(self):
        self.check("date", "2024-02-29", "2024-02-29")
        self.check("date", "2026-02", "2026-02")
        for value in ("2026-02-29", "2026-13", "2026-2", "29/02/2024", "2024-02-29T00:00:00"):
            with self.subTest(value=value):
                self.check("date", "2024-02-29", value, "wrong")
        self.check("date", "2026-02", "2026-02-01", "wrong")

    def test_ordered_list_and_position_aliases(self):
        self.check("list", ["Một", "Hai"], ["A", "B"], aliases=[["A"], ["B"]])
        self.check("list", ["Một", "Hai"], ["B", "A"], "wrong", aliases=[["A"], ["B"]])
        self.check("list", [3, 10], [3, 10])
        self.check("list", [3, 10], [10, 3], "wrong")
        self.check("list", [3], ["3"], "wrong")
        self.check("list", [3], 3, "wrong")
        self.check("list", [3], [True], "wrong")
        self.check("list", [], [])

    def test_wrong_type_is_wrong_even_equal_value(self):
        result = score_answer(ref("string", "2026-01"), pred("date", "2026-01"))
        self.assertEqual(result["reason"], "type_mismatch")
        self.assertEqual(result["score"], -1)

    def test_unanswerable_abstain_and_missing(self):
        self.assertEqual(score_answer(ref("unanswerable"), {"id":"q", "type":"unanswerable"})["score"], 1)
        self.assertEqual(score_answer(ref("number", 0), pred("unanswerable"))["score"], -1)
        for reference in (ref("number", 0), ref("unanswerable")):
            self.assertEqual(score_answer(reference, pred("abstain"))["score"], 0)
            self.assertEqual(score_answer(reference, None)["score"], 0)

    def test_invalid_reason_is_explicit_unreadable_abstain(self):
        result = score_answer(ref("unanswerable"), {"id":"q", "type":"unanswerable", "reason": []})
        self.assertEqual(result["score"], 0)
        self.assertIsNotNone(result["format_error"])

    def test_bad_type_and_bad_reference_not_silently_scored(self):
        for kind in (None, "integer", [], {}, Decimal("1.5")):
            result = score_answer(ref("number", 1), {"id":"q", "type":kind, "answer":1})
            self.assertEqual(result["score"], -1)
            json.dumps(result)  # Invalid type fields must still yield writable diagnostics.
        with self.assertRaises(AnswerFormatError):
            score_answer(ref("number", "wrong-reference"), pred("number", 1))


class TableTests(unittest.TestCase):
    def test_unordered_rows_and_reordered_named_columns(self):
        expected = {"columns":["Tên hàng", "so_luong"], "rows":[["A",2],["B",3]]}
        actual = {"columns":["SO LUONG", "tên hàng"], "rows":[[3,"B"],[2,"A"]]}
        self.assertEqual(score_answer(ref("table", expected), pred("table", actual))["score"], 1)

    def test_column_position_fallback(self):
        expected = {"columns":["name", "amount"], "rows":[["A",2]]}
        actual = {"columns":["customer", "money"], "rows":[["A",2]]}
        self.assertEqual(score_answer(ref("table", expected), pred("table", actual))["score"], 1)

    def test_aliases_attach_to_reference_rows_not_prediction_positions(self):
        expected = {"columns":["name", "amount"], "rows":[["Một",2],["Hai",3]]}
        actual = {"columns":["name", "amount"], "rows":[["B",3],["A",2]]}
        self.assertEqual(score_answer(ref("table", expected, [["A"],["B"]]), pred("table", actual))["score"], 1)

    def test_overlapping_aliases_need_bipartite_not_greedy_matching(self):
        expected = {"columns":["name","amount"], "rows":[["A",1],["B",1]]}
        actual = {"columns":["name","amount"], "rows":[["shared",1],["onlyA",1]]}
        self.assertEqual(score_answer(ref("table", expected, [["shared","onlyA"],["shared"]]), pred("table", actual))["score"], 1)

    def test_duplicate_row_count_local_multiset_policy(self):
        expected = {"columns":["name","amount"], "rows":[["A",1],["A",1]]}
        actual = {"columns":["name","amount"], "rows":[["A",1]]}
        self.assertEqual(score_answer(ref("table", expected), pred("table", actual))["score"], -1)

    def test_reject_ragged_duplicate_columns_and_wrong_cell_types(self):
        expected = {"columns":["name","amount"], "rows":[["A",1]]}
        for actual in (
            {"columns":["name","amount"], "rows":[["A"]]},
            {"columns":["name","NAME"], "rows":[["A",1]]},
            {"columns":["name","amount"], "rows":[["A",None]]},
            {"columns":["name","amount"], "rows":[["A",True]]},
        ):
            with self.subTest(actual=actual):
                self.assertEqual(score_answer(ref("table", expected), pred("table", actual))["score"], -1)
        actual = {"columns":["name","amount"], "rows":[["A","1"]]}
        self.assertEqual(score_answer(ref("table", expected), pred("table", actual))["score"], -1)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.questions = [{"id":str(i),"business_id":"b"+str(i%2)} for i in range(4)]
        self.refs = [ref("number", 10, identity="0"),ref("string", "Đúng", identity="1"),
                     ref("unanswerable", identity="2"),ref("boolean", True, identity="3")]

    def test_pa_coverage_missing_groups_and_unanswerable(self):
        predictions = [pred("number",10,"0"),pred("string","sai","1"),pred("unanswerable",identity="2")]
        report, records = evaluate(self.questions,self.refs,predictions)
        self.assertEqual(report["summary"]["pa"], .25)
        self.assertEqual(report["summary"]["coverage"], .75)
        self.assertEqual(report["summary"]["answered_accuracy"], 2/3)
        self.assertEqual(report["summary"]["missing_predictions"], 1)
        self.assertEqual(report["unanswerable"]["recall"], 1)
        self.assertIsNone(report["by_type"]["table"]["pa"])
        for row in records:
            self.assertNotIn("answer", row)
            self.assertNotIn("alias", row)

    def test_all_missing_has_pa_zero_not_wrong(self):
        report,_ = evaluate(self.questions,self.refs,[])
        self.assertEqual(report["summary"]["pa"], 0)
        self.assertEqual(report["summary"]["coverage"], 0)
        self.assertIsNone(report["summary"]["answered_accuracy"])

    def test_unanswerable_recall_without_support_is_undefined(self):
        report,_=evaluate([{"id":"q","business_id":"b"}],
                          [ref("number",0)],[pred("unanswerable")])
        self.assertEqual(report["unanswerable"]["precision"],0)
        self.assertIsNone(report["unanswerable"]["recall"])
        self.assertEqual(report["summary"]["pa"],-1)

    def test_unknown_duplicate_and_missing_reference_ids_are_hard_errors(self):
        for refs, predictions in ((self.refs,[pred("number",1,"unknown")]),
                                  (self.refs,[pred("number",1,"0")]*2),
                                  (self.refs[:-1],[])):
            with self.assertRaises(ValueError):
                evaluate(self.questions,refs,predictions)

    def test_timing_report_does_not_claim_cold_wall_time(self):
        report,_ = evaluate(self.questions,self.refs,[],timings=[{"id":"0","elapsed_ms":10},{"id":"1","elapsed_ms":20}])
        self.assertEqual(report["timing"]["p50_ms"],15)
        self.assertEqual(report["timing"]["missing"],2)
        with self.assertRaises(ValueError):
            evaluate(self.questions,self.refs,[],timings=[{"id":"0","elapsed_ms":True}])


class FileAndIntegrationTests(unittest.TestCase):
    def test_decimal_preserves_large_integer_and_fraction(self):
        value = strict_json_loads('{"big":100000000000000000001,"small":0.30000000000000004}')
        self.assertEqual(value["big"],10**20+1)
        self.assertEqual(value["small"],Decimal("0.30000000000000004"))

    def test_bad_json_duplicate_keys_and_duplicate_ids_rejected(self):
        for text in ('{"x":NaN}','{"x":1,"x":2}','{"id":'):
            with self.assertRaises(ValueError):
                strict_json_loads(text)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"p.jsonl"
            for text in ('[]\n','{"id":"x"}\n{"id":"x"}\n'):
                path.write_text(text)
                with self.assertRaises(ValueError): read_jsonl(path)

    def test_actual_train_reference_roundtrip_is_a_scorer_test_not_a_model_score(self):
        questions,refs,manifest=load_partition(ROOT/"artifacts/data_split_v1","train")
        predictions=[{k:v for k,v in row.items() if k!="alias"} for row in refs]
        report,_=evaluate(questions,refs,predictions,business_metadata=manifest["businesses"])
        self.assertEqual(report["summary"]["total"],1583)
        self.assertEqual(report["summary"]["pa"],1)
        for row in refs:
            kind,aliases=row["type"],row.get("alias",[])
            if not aliases: continue
            candidate=json.loads(json.dumps(row,default=str))
            if kind=="string": candidate["answer"]=aliases[0]
            elif kind=="list":
                for i,names in enumerate(aliases):
                    if names and isinstance(row["answer"][i],str):candidate["answer"][i]=names[0]
            elif kind=="table":
                for i,names in enumerate(aliases):
                    if names:candidate["answer"]["rows"][i][0]=names[0]
                candidate["answer"]["rows"].reverse()
            else: continue
            self.assertEqual(score_answer(row,candidate)["score"],1)

    def test_cli_all_abstain_does_not_need_gpu_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            predictions=Path(directory)/"empty.jsonl";predictions.write_text("")
            output=Path(directory)/"report"
            cmd=[sys.executable,str(ROOT/"scripts/evaluate.py"),"--partition","development",
                 "--predictions",str(predictions),"--output-dir",str(output)]
            result=subprocess.run(cmd,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            report=json.loads((output/"report.json").read_text())
            self.assertEqual(report["summary"]["total"],708)
            self.assertEqual(report["summary"]["pa"],0)
            self.assertEqual(subprocess.run(cmd,capture_output=True).returncode,2)

    def test_frozen_artifact_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/"split"
            shutil.copytree(ROOT/"artifacts/data_split_v1",target)
            answers=target/"evaluator/train/answers.jsonl"
            answers.write_text(answers.read_text()+"\n")
            with self.assertRaisesRegex(ValueError,"checksum mismatch"):
                load_partition(target,"train")

    def test_cli_invalid_json_emits_no_misleading_score(self):
        with tempfile.TemporaryDirectory() as directory:
            predictions=Path(directory)/"bad.jsonl";predictions.write_text('{"id":')
            output=Path(directory)/"report"
            result=subprocess.run([sys.executable,str(ROOT/"scripts/evaluate.py"),
                "--predictions",str(predictions),"--output-dir",str(output)],capture_output=True,text=True)
            self.assertEqual(result.returncode,2)
            self.assertIn("invalid JSON",result.stderr)
            self.assertFalse(output.exists())

    def test_cli_keeps_frozen_split_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/"split"
            shutil.copytree(ROOT/"artifacts/data_split_v1",target)
            predictions=Path(directory)/"empty.jsonl";predictions.write_text("")
            output=target/"new_report"
            result=subprocess.run([sys.executable,str(ROOT/"scripts/evaluate.py"),
                "--split-root",str(target),"--predictions",str(predictions),
                "--output-dir",str(output)],capture_output=True,text=True)
            self.assertEqual(result.returncode,2)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
