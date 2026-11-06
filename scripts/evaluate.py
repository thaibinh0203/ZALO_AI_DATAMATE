#!/usr/bin/env python3
"""Score final answer JSONL against a frozen internal split. No model/GPU needed."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from datamate_eval.runner import evaluate, load_partition, read_jsonl, sha256_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split-root", type=Path, default=ROOT / "artifacts/data_split_v1")
    parser.add_argument("--partition", choices=["train", "development", "calibration", "holdout"], default="development")
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--timings", type=Path, help="Optional JSONL: id, elapsed_ms")
    parser.add_argument("--output-dir", required=True, type=Path, help="New evaluator-only report directory")
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error("Output directory already exists; use a new path")
    # Frozen splits remain immutable; reports contain label-derived diagnostics.
    output = args.output_dir.resolve()
    split_root = args.split_root.resolve()
    if output == split_root or split_root in output.parents:
        parser.error("Evaluator reports must be outside the frozen split directory")
    started = perf_counter()
    try:
        questions, references, manifest = load_partition(args.split_root, args.partition)
        source_root = (ROOT / manifest["source_root"]).resolve()
        if output == source_root or source_root in output.parents:
            raise ValueError("Evaluator reports must not be written into source data")
        predictions_hash = sha256_file(args.predictions)
        timings_hash = sha256_file(args.timings) if args.timings else None
        predictions = read_jsonl(args.predictions)
        timings = read_jsonl(args.timings) if args.timings else None
        report, records = evaluate(questions, references, predictions,
                                   business_metadata=manifest["businesses"], timings=timings)
        if predictions_hash != sha256_file(args.predictions) or (
            args.timings and timings_hash != sha256_file(args.timings)
        ):
            raise ValueError("Predictions/timings changed during evaluation; retry after the run completes")
        report.update(partition=args.partition, split_version=manifest["version"],
                      source_fingerprint=manifest["source_fingerprint"],
                      split_manifest_sha256=sha256_file(args.split_root / "split_manifest.json"),
                      predictions_sha256=predictions_hash,
                      evaluator_source_sha256={name: sha256_file(ROOT / name) for name in
                          ("src/datamate_eval/matching.py", "src/datamate_eval/runner.py", "scripts/evaluate.py")},
                      timings_sha256=timings_hash,
                      recorded_at_utc=datetime.now(timezone.utc).isoformat(),
                      evaluation_elapsed_ms=round((perf_counter() - started) * 1000, 3),
                      split_limitations=manifest["limitations"])
    except (ValueError, OSError, KeyError, TypeError, RecursionError) as error:
        print(f"Evaluation input error: {error}", file=sys.stderr)
        return 2
    output.mkdir(parents=True, exist_ok=False)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output / "per_question.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in records), encoding="utf-8")
    print(json.dumps({"partition": args.partition, **report["summary"], "report": str(output / "report.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
