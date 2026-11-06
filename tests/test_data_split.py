"""Split integrity and source-drift checks without model dependencies."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prepare_data_split", ROOT / "scripts/prepare_data_split.py")
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)


class SplitTests(unittest.TestCase):
    def test_source_snapshot_detects_content_change_with_same_size(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "questions.jsonl"
            file.write_text("abc")
            before = PREPARE.snapshot(root)
            file.write_text("xyz")
            after = PREPARE.snapshot(root)
            self.assertEqual(before[0]["size_bytes"], after[0]["size_bytes"])
            self.assertNotEqual(before[0]["sha256"], after[0]["sha256"])

    def test_rejects_duplicate_business_and_cross_partition_file(self):
        config = json.loads((ROOT / "configs/data_split_v1.json").read_text())
        businesses = [b for f in config["families"] for b in f["businesses"]]
        membership, _ = PREPARE.assignments(config, businesses)
        records = [{"role": "business_data", "path": f"data/{b}/file.csv", "sha256": "same"}
                   for b in ("biz_101", "biz_111")]
        with self.assertRaisesRegex(ValueError, "cross partitions"):
            PREPARE.duplicate_data_files(records, membership)
        config["families"][1]["businesses"].append("biz_101")
        with self.assertRaisesRegex(ValueError, "repeated"):
            PREPARE.assignments(config, businesses)

    def test_materialized_split_is_exhaustive_disjoint_and_label_separated(self):
        output = ROOT / "artifacts/data_split_v1"
        split = json.loads((output / "split_manifest.json").read_text())
        original_q = {q["id"]: q for q in PREPARE.read_jsonl(ROOT / "docs/train/questions.jsonl")}
        original_a = {a["id"]: a for a in PREPARE.read_jsonl(ROOT / "docs/train/answers.jsonl")}
        seen_ids, seen_businesses, seen_families = set(), set(), set()
        for partition, summary in split["partitions"].items():
            questions = PREPARE.read_jsonl(output / "inputs" / partition / "questions.jsonl")
            answers = PREPARE.read_jsonl(output / "evaluator" / partition / "answers.jsonl")
            ids = {q["id"] for q in questions}
            businesses = {q["business_id"] for q in questions}
            families = {split["businesses"][b]["family"] for b in businesses}
            self.assertFalse(seen_ids & ids)
            self.assertFalse(seen_businesses & businesses)
            self.assertFalse(seen_families & families)
            self.assertEqual(ids, {a["id"] for a in answers})
            self.assertEqual(ids, set(summary["question_ids"]))
            for q in questions:
                self.assertEqual(set(q), {"id", "business_id", "question", "as_of_date"})
                self.assertEqual(q, original_q[q["id"]])
            for a in answers:
                self.assertEqual(a, original_a[a["id"]])
            seen_ids |= ids
            seen_businesses |= businesses
            seen_families |= families
        self.assertEqual(seen_ids, set(original_q))
        self.assertEqual(len(seen_businesses), 30)
        self.assertEqual(len(seen_families), 10)


if __name__ == "__main__":
    unittest.main()
