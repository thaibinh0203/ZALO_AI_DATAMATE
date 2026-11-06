#!/usr/bin/env python3
"""OFFLINE B1 CLI: schema + rules + question -> validated JSON plan; no SQL execution."""
import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

# Allow invocation by absolute script path from any cwd without installing a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

from datamate_b.contracts import strict_json, validate_request, validate_result
from datamate_b.model import LocalModel, ModelConfig
from datamate_b.prompts import PROMPT_VERSION, build_messages


def run_questions(model, schema, rules, questions, stream, dialect="postgres"):
    failures = 0
    for question in questions:
        started = perf_counter()
        record = {**question, "raw_output": None, "parsed_json": None, "parsed_result": None,
                  "parse_error": None, "contract_error": None,
                  "generation_error": None, "generation_ms": None,
                  "prompt_tokens": None, "output_tokens": None,
                  "reasoning_generated": None, "executed_code": None, "output": None}
        record["messages"] = build_messages(schema, rules, question, dialect)
        record["sql_dialect"] = dialect
        try:
            record.update(model.generate(record["messages"]))
        except Exception as e:
            record["generation_error"] = f"{type(e).__name__}: {e}"
        if record["generation_error"] is None:
            try:
                record["parsed_json"] = strict_json(record["raw_output"])
            except Exception as e:
                record["parse_error"] = f"{type(e).__name__}: {e}"
        if record["generation_error"] is None and record["parse_error"] is None:
            try:
                record["parsed_result"] = validate_result(record["parsed_json"], schema, dialect)
                if record.get("hit_token_limit"):
                    raise ValueError("Output hit token budget; completion not confirmed")
            except Exception as e:
                record["parsed_result"] = None
                record["contract_error"] = f"{type(e).__name__}: {e}"
        record["valid"] = all(record[key] is None for key in ("parse_error", "contract_error", "generation_error"))
        failures += not record["valid"]
        record["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
        record["model"] = model.metadata()
        record["prompt_version"] = PROMPT_VERSION
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--rules", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--question")
    group.add_argument("--questions-file", type=Path)
    parser.add_argument("--as-of-date", help="Required for --question; overrides all JSONL dates if supplied")
    parser.add_argument("--output", type=Path, required=True, help="New JSONL path; refuses to overwrite")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--dtype", choices=["bfloat16", "float16", "float32"], default="bfloat16")
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--max-context-tokens", type=int, default=4096)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--dialect",choices=["postgres","duckdb"],default="postgres")
    args = parser.parse_args()
    schema = args.schema.read_text(encoding="utf-8")
    rules = args.rules.read_text(encoding="utf-8")
    if not schema.strip() or not rules.strip():
        parser.error("Schema and rules must be nonempty")
    if args.question is not None:
        questions = [{"question": args.question, "as_of_date": args.as_of_date}]
    else:
        questions = [strict_json(line) for line in args.questions_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not questions:
        parser.error("No questions")
    for index, q in enumerate(questions):
        if args.as_of_date:
            q["as_of_date"] = args.as_of_date
        validate_request(q)
        # Do not pass any labels/expected answers or other JSONL fields to the model/log.
        questions[index] = {key: q[key] for key in ("id", "question", "as_of_date") if key in q}
    config = ModelConfig(args.model_path, args.device, args.dtype, args.max_new_tokens,
                         args.max_context_tokens, args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    run_path = args.output.with_suffix(args.output.suffix + ".run.json")
    if args.output.exists() or run_path.exists():
        parser.error("Output/run log already exists; choose a new output path")
    report = {"run_id": str(uuid4()), "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "config": asdict(config), "prompt_version": PROMPT_VERSION,
              "sql_dialect": args.dialect,
              "schema_sha256": hashlib.sha256(schema.encode()).hexdigest(),
              "rules_sha256": hashlib.sha256(rules.encode()).hexdigest(),
              "questions_sha256": hashlib.sha256(json.dumps(questions, ensure_ascii=False).encode()).hexdigest(),
              "load_ms": None, "load_error": None, "model": None,
              "question_count": len(questions), "failures": None, "inference_completed": False}
    started = perf_counter()
    try:
        model = LocalModel(config)
        model.load()
        report["load_ms"] = model.load_ms
        report["model"] = model.metadata()
    except Exception as e:
        report["load_error"] = f"{type(e).__name__}: {e}"
        report["load_attempt_ms"] = round((perf_counter() - started) * 1000, 3)
        run_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(report["load_error"], file=sys.stderr)
        return 2
    # Save shared load details before generation so an interrupted run remains inspectable.
    run_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with args.output.open("x", encoding="utf-8") as stream:
        report["failures"] = run_questions(model, schema, rules, questions, stream, args.dialect)
    report["inference_completed"] = True
    report["total_ms"] = round((perf_counter() - started) * 1000, 3)
    run_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "run_log": str(run_path), "failures": report["failures"]}))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
