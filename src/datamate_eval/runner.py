"""JSONL loading, frozen split binding and reports. Standard library only."""
from collections import Counter, defaultdict
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import statistics

from .matching import PROFILE, REFERENCE_TYPES, is_number, score_answer


def strict_json_loads(text):
    def reject_constant(value):
        raise ValueError(f"Non-finite JSON constant: {value}")
    def unique_object(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f"Duplicate JSON key: {key}")
            out[key] = value
        return out
    return json.loads(text, parse_float=Decimal, parse_constant=reject_constant,
                      object_pairs_hook=unique_object)


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def index_records(rows, label):
    out = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            raise ValueError(f"{label}: each record needs a nonempty string id")
        if row["id"] in out:
            raise ValueError(f"{label}: duplicate ID {row['id']}")
        out[row["id"]] = row
    return out


def read_jsonl(path):
    rows = []
    with Path(path).open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                rows.append(strict_json_loads(line))
            except (ValueError, RecursionError) as error:
                raise ValueError(f"{path}:{number}: invalid JSON ({error})") from error
    index_records(rows, str(path))
    return rows


def load_partition(split_root, partition):
    """Check frozen artifact hashes and bind exact questions/labels to a partition.

    Does not mount/serve references to the inference process. Raw business sources
    are separately checked by prepare_data_split.py --verify.
    """
    root = Path(split_root).resolve()
    checksums = strict_json_loads((root / "artifact_checksums.json").read_text(encoding="utf-8"))
    if not isinstance(checksums, dict):
        raise ValueError("Artifact checksum index must be an object")
    required = ["split_manifest.json", "source_manifest.json",
                f"inputs/{partition}/questions.jsonl", f"evaluator/{partition}/answers.jsonl"]
    for relative in required:
        if relative not in checksums:
            raise ValueError(f"Frozen checksum missing: {relative}")
    for relative, expected in checksums.items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Artifact path escapes split root")
        if sha256_file(path) != expected:
            raise ValueError(f"Frozen artifact checksum mismatch: {relative}")
    manifest = strict_json_loads((root / "split_manifest.json").read_text(encoding="utf-8"))
    source = strict_json_loads((root / "source_manifest.json").read_text(encoding="utf-8"))
    if manifest["source_fingerprint"] != source["source_fingerprint"]:
        raise ValueError("Source fingerprint differs between frozen manifests")
    if partition not in manifest["partitions"]:
        raise ValueError(f"Unknown partition: {partition}")
    questions = read_jsonl(root / "inputs" / partition / "questions.jsonl")
    references = read_jsonl(root / "evaluator" / partition / "answers.jsonl")
    spec = manifest["partitions"][partition]
    if len(spec["business_ids"]) != len(set(spec["business_ids"])):
        raise ValueError("Manifest has repeated business IDs")
    ids = [q["id"] for q in questions]
    if len(spec["question_ids"]) != len(set(spec["question_ids"])):
        raise ValueError("Manifest has repeated question IDs")
    if set(ids) != set(spec["question_ids"]) or len(ids) != spec["questions"]:
        raise ValueError("Question file differs from partition manifest")
    if {q["business_id"] for q in questions} != set(spec["business_ids"]):
        raise ValueError("Business membership differs from partition manifest")
    if any(manifest["businesses"][q["business_id"]]["partition"] != partition for q in questions):
        raise ValueError("Business metadata has a different partition")
    if Counter(r["type"] for r in references) != Counter(spec["answer_types"]):
        raise ValueError("Reference type distribution differs from manifest")
    if {r["id"] for r in references} != set(ids):
        raise ValueError("Reference IDs must exactly match questions")
    return questions, references, manifest


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def summarize(records):
    counts = Counter(r["outcome"] for r in records)
    total = len(records)
    correct, wrong, abstain = (counts[k] for k in ("correct", "wrong", "abstain"))
    return {"total": total, "correct": correct, "wrong": wrong, "abstain": abstain,
            "score_sum": correct - wrong, "pa": ratio(correct - wrong, total),
            "answered": correct + wrong, "coverage": ratio(correct + wrong, total),
            "accuracy_all": ratio(correct, total),
            "answered_accuracy": ratio(correct, correct + wrong),
            "format_errors": sum(r["format_error"] is not None for r in records),
            "missing_predictions": sum(r["reason"] == "missing_prediction" for r in records)}


def timing_summary(timings, question_ids):
    if timings is None:
        return None
    indexed = index_records(timings, "timings")
    if set(indexed) - question_ids:
        raise ValueError("Timing IDs outside selected questions")
    samples = []
    for row in indexed.values():
        value = row.get("elapsed_ms")
        if not is_number(value) or value < 0:
            raise ValueError("Timing requires finite nonnegative elapsed_ms")
        try:
            value = float(value)
        except OverflowError as error:
            raise ValueError("Timing outside float range") from error
        if not math.isfinite(value):
            raise ValueError("Timing outside float range")
        samples.append(value)
    samples.sort()
    def percentile(fraction):
        if not samples:
            return None
        offset = (len(samples) - 1) * fraction
        lo = int(offset)
        hi = min(lo + 1, len(samples) - 1)
        return samples[lo] + (samples[hi] - samples[lo]) * (offset - lo)
    return {"records": len(samples), "missing": len(question_ids) - len(samples),
            "mean_ms": statistics.mean(samples) if samples else None,
            "p50_ms": percentile(.5), "p95_ms": percentile(.95),
            "max_ms": max(samples) if samples else None,
            "sum_elapsed_ms": sum(samples),
            "note": "Per-question elapsed values; sum is not cold-run wall time under batching. Shared load not included."}


def evaluate(questions, references, predictions, *, business_metadata=None, timings=None):
    qmap = index_records(questions, "questions")
    rmap = index_records(references, "references")
    pmap = index_records(predictions, "predictions")
    if not qmap:
        raise ValueError("Cannot evaluate an empty question set")
    if set(qmap) != set(rmap):
        raise ValueError("Reference IDs must exactly match question IDs")
    if set(pmap) - set(qmap):
        raise ValueError("Predictions contain IDs outside selected partition")
    records = []
    business_metadata = business_metadata or {}
    for question in questions:
        business = question.get("business_id")
        if not isinstance(business, str) or not business:
            raise ValueError("Question requires business_id for grouping")
        diagnostic = score_answer(rmap[question["id"]], pmap.get(question["id"]))
        # Never copy gold answer/alias values into diagnostics.
        diagnostic.update(id=question["id"], business_id=business,
                          family=business_metadata.get(business, {}).get("family", "unknown"),
                          industry=business_metadata.get(business, {}).get("industry", "unknown"))
        records.append(diagnostic)
    groups = {}
    for key in ("expected_type", "business_id", "family", "industry"):
        buckets = defaultdict(list)
        for record in records:
            buckets[record[key]].append(record)
        if key == "expected_type":
            for kind in REFERENCE_TYPES:
                buckets.setdefault(kind, [])
        groups[key] = {name: summarize(rows) for name, rows in sorted(buckets.items())}
    positives = sum(r["expected_type"] == "unanswerable" for r in records)
    predicted = [r for r in records if r["prediction_type"] == "unanswerable" and not r["format_error"]]
    tp = sum(r["outcome"] == "correct" for r in predicted)
    report = {"profile": PROFILE, "official_scorer": False, "summary": summarize(records),
              "by_type": groups["expected_type"], "by_business": groups["business_id"],
              "by_family": groups["family"], "by_industry": groups["industry"],
              "macro_pa_business": statistics.mean(x["pa"] for x in groups["business_id"].values()),
              "macro_pa_family": statistics.mean(x["pa"] for x in groups["family"].values()),
              "unanswerable": {"support": positives, "predicted": len(predicted), "true_positive": tp,
                               "false_positive": len(predicted) - tp,
                               "precision": ratio(tp, len(predicted)), "recall": ratio(tp, positives)},
              "timing": timing_summary(timings, set(qmap)),
              "policy": {"number": "exact Decimal, no added tolerance/no prediction rounding",
                         "percent": "round both to 2 decimals, ROUND_HALF_UP",
                         "table": "unordered rows, one-to-one multiset; alias on first reference column",
                         "malformed_json_duplicate_unknown_ids": "hard file error; no PA emitted",
                         "invalid_identifiable_answer": "wrong (-1), except non-string unanswerable reason => abstain"},
              "limitations": [
                  "Local approximation; organizers' executable scorer not found in workspace.",
                  "Floating-point noise tolerance is not specified; exact Decimal is conservative.",
                  "Tie rounding, duplicate table-row handling, malformed/duplicate/unknown IDs need official scorer confirmation.",
                  "Missing type support produces null rates, never a fabricated zero accuracy."
              ]}
    return report, records
