#!/usr/bin/env python3
"""Freeze official train sources and materialize a reviewed business-level split.

Standard library only. No source mutations, inference, downloads or public-test reads.
Labels are written only under evaluator/, separate from inference question files.
"""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import posixpath
import sqlite3
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
PARTITIONS = ("train", "development", "calibration", "holdout")


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_jsonl(path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate IDs: {path}")
    return rows


def snapshot(source):
    records = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Source symlink not supported: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        if ":Zone.Identifier" in path.name:
            role = "excluded_os_metadata"
        elif path.name == ".Rhistory":
            role = "excluded_session_history"
        elif path.name == "answers.jsonl":
            role = "evaluator_labels"
        elif path.name == "questions.jsonl":
            role = "questions"
        elif path.name.startswith("rulebook"):
            role = "rulebook"
        elif path.name == "calendar.csv":
            role = "calendar"
        elif path.name.startswith("README"):
            role = "business_readme"
        elif path.suffix.lower() in {".csv", ".xlsx", ".sqlite"}:
            role = "business_data"
        else:
            raise ValueError(f"Unreviewed source file: {relative}")
        records.append({"path": relative, "size_bytes": path.stat().st_size,
                        "sha256": digest(path), "role": role})
    return records


def inspect_xlsx(path):
    # Inspect sheet metadata and first three rows of each sheet; never load all cells.
    with zipfile.ZipFile(path) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            with archive.open("xl/sharedStrings.xml") as stream:
                for _, node in ET.iterparse(stream, events=("end",)):
                    if node.tag == f"{{{NS['m']}}}si":
                        shared.append("".join(t.text or "" for t in node.findall(".//m:t", NS)))
                        node.clear()
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {r.attrib["Id"]: r.attrib["Target"] for r in rels}
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheets = []
        for sheet in workbook.find("m:sheets", NS):
            rid = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
            target = targets[rid]
            member = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            preview, dimension = [], None
            with archive.open(member) as stream:
                for _, node in ET.iterparse(stream, events=("end",)):
                    if node.tag == f"{{{NS['m']}}}dimension":
                        dimension = node.get("ref")
                    if node.tag != f"{{{NS['m']}}}row":
                        continue
                    cells = {}
                    for cell in node:
                        value = cell.findtext("m:v", default="", namespaces=NS)
                        if cell.get("t") == "s" and value:
                            value = shared[int(value)]
                        elif cell.get("t") == "inlineStr":
                            value = "".join(t.text or "" for t in cell.findall(".//m:t", NS))
                        if value:
                            cells[cell.get("r")] = value
                    preview.append(cells)
                    node.clear()
                    if len(preview) >= 3:
                        break
            sheets.append({"name": sheet.get("name"), "state": sheet.get("state", "visible"),
                           "declared_dimension": dimension, "leading_rows": preview})
        return {"sheets": sheets}


def inspect_business(path):
    readme = (path / "README.md").read_text(encoding="utf-8")
    files = []
    for file in sorted(path.iterdir()):
        item = {"path": file.name, "format": file.suffix.lower()}
        if file.suffix == ".xlsx":
            item.update(inspect_xlsx(file))
        elif file.suffix == ".csv":
            with file.open(encoding="utf-8-sig", newline="") as stream:
                item["header"] = next(csv.reader(stream))
        elif file.suffix == ".sqlite":
            with sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True) as db:
                item["schema"] = [{"name": n, "sql": s} for n, s in db.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type IN ('table','view') ORDER BY name")]
        else:
            continue
        files.append(item)
    return {"readme_introduction": readme.split("## File")[0].strip(), "files": files}


def assignments(config, businesses):
    membership, families, industries = {}, {}, {}
    for family in config["families"]:
        if family["id"] in families:
            raise ValueError("Duplicate family ID")
        if family["partition"] not in PARTITIONS:
            raise ValueError("Unknown partition")
        families[family["id"]] = family
        for business in family["businesses"]:
            if business in membership:
                raise ValueError(f"Business repeated between families: {business}")
            membership[business] = (family["partition"], family["id"])
    for industry, members in config["industries"].items():
        for business in members:
            if business in industries:
                raise ValueError("Duplicate industry membership")
            industries[business] = industry
    if set(membership) != set(businesses) or set(industries) != set(businesses):
        raise ValueError("Config must cover source businesses exactly")
    actual = Counter(p for p, _ in membership.values())
    if dict(actual) != config["target_business_counts"]:
        raise ValueError(f"Business counts differ from target: {actual}")
    return membership, industries


def duplicate_data_files(records, membership):
    groups = {}
    for record in records:
        if record["role"] == "business_data":
            groups.setdefault(record["sha256"], []).append(record["path"])
    duplicates = [paths for paths in groups.values() if len(paths) > 1]
    for paths in duplicates:
        if len({membership[Path(p).parts[1]][0] for p in paths}) > 1:
            raise ValueError(f"Identical business data files cross partitions: {paths}")
    return duplicates


def archive_check(archive_path, source, records):
    by_path = {r["path"]: r for r in records}
    matched = []
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            member_name = info.filename
            # Some supplied ZIP names contain UTF-8 bytes without the UTF-8 flag;
            # zipfile decodes those as CP437. Recover only when UTF-8 is valid.
            if not info.flag_bits & 0x800:
                try:
                    member_name = member_name.encode("cp437").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            parts = Path(member_name).parts
            if not parts or parts[0] != source.name or ".." in parts:
                raise ValueError(f"Unexpected archive member: {info.filename}")
            relative = Path(*parts[1:]).as_posix()
            if relative not in by_path:
                raise ValueError(f"Archive member missing in extracted source: {relative}")
            h = hashlib.sha256()
            with archive.open(info) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(block)
            if h.hexdigest() != by_path[relative]["sha256"]:
                raise ValueError(f"Archive/source content mismatch: {relative}")
            matched.append(relative)
    return {"path": str(archive_path.relative_to(ROOT)), "size_bytes": archive_path.stat().st_size,
            "sha256": digest(archive_path), "matched_members": len(matched),
            "extracted_extra_files": sorted(set(by_path) - set(matched)), "all_archive_members_match": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/data_split_v1")
    parser.add_argument("--verify", action="store_true", help="Verify existing artifacts and current source without writing")
    args = parser.parse_args()
    source = ROOT / "docs/train"
    config_path = ROOT / "configs/data_split_v1.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    output = args.output.resolve()
    if output == source or source in output.parents or output in source.parents:
        raise ValueError("Output must be separate from source")
    records = snapshot(source)
    if args.verify:
        manifest = json.loads((output / "source_manifest.json").read_text(encoding="utf-8"))
        if records != manifest["files"] or digest(config_path) != manifest["split_config_sha256"]:
            raise ValueError("Source or split config changed")
        if digest(Path(__file__)) != manifest["script_sha256"]:
            raise ValueError("Preparation script changed")
        archive = manifest["archive"]
        if digest(ROOT / archive["path"]) != archive["sha256"]:
            raise ValueError("Original archive changed")
        index = json.loads((output / "artifact_checksums.json").read_text(encoding="utf-8"))
        for relative, expected in index.items():
            if digest(output / relative) != expected:
                raise ValueError(f"Artifact changed: {relative}")
        print("Verified: source, archive, config, script and all generated artifacts unchanged.")
        return
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite split: {output}; use --verify or a new --output")
    questions = read_jsonl(source / "questions.jsonl")
    answers = read_jsonl(source / "answers.jsonl")
    labels = {a["id"]: a for a in answers}
    if set(labels) != {q["id"] for q in questions}:
        raise ValueError("Question/answer IDs differ")
    businesses = sorted(p.name for p in (source / "data").iterdir() if p.is_dir())
    if {q["business_id"] for q in questions} != set(businesses):
        raise ValueError("Question and business-folder coverage differ")
    membership, industries = assignments(config, businesses)
    duplicates = duplicate_data_files(records, membership)
    archive = archive_check(ROOT / "docs/train.zip", source, records)
    audit = {b: inspect_business(source / "data" / b) for b in businesses}
    split = {"version": config["version"], "source_fingerprint": canonical_hash(records),
             "source_root": "docs/train", "partitions": {}, "businesses": {},
             "families": config["families"], "limitations": config["limitations"],
             "duplicate_business_data_files": duplicates,
             "validation": {"businesses_disjoint": True, "families_disjoint": True,
                            "question_ids_unique": True, "answer_ids_match": True,
                            "identical_data_files_do_not_cross_partitions": True}}
    for b in businesses:
        rows = [q for q in questions if q["business_id"] == b]
        split["businesses"][b] = {
            "partition": membership[b][0], "family": membership[b][1], "industry": industries[b],
            "questions": len(rows), "answer_types": dict(sorted(Counter(labels[q["id"]]["type"] for q in rows).items())),
            "formats": dict(sorted(Counter(f["format"] for f in audit[b]["files"]).items()))}
    for partition in PARTITIONS:
        members = [b for b in businesses if membership[b][0] == partition]
        rows = [q for q in questions if q["business_id"] in members]
        split["partitions"][partition] = {
            "business_ids": members, "question_ids": [q["id"] for q in rows], "questions": len(rows),
            "industries": dict(sorted(Counter(industries[b] for b in members).items())),
            "answer_types": dict(sorted(Counter(labels[q["id"]]["type"] for q in rows).items()))}
    if snapshot(source) != records:
        raise ValueError("Source changed during preparation; refusing to publish split")
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"version": "source-manifest-v1", "source_root": "docs/train",
                "source_fingerprint": canonical_hash(records), "files": records, "archive": archive,
                "split_config_sha256": digest(config_path), "script_sha256": digest(Path(__file__)),
                "rulebook_version": next(r["sha256"] for r in records if r["path"] == "rulebook.md"),
                "calendar_version": next(r["sha256"] for r in records if r["path"] == "calendar.csv"),
                "version_semantics": "Content-addressed SHA-256; no upstream release identifier assumed."}
    write_json(output / "source_manifest.json", manifest)
    write_json(output / "split_manifest.json", split)
    write_json(output / "layout_audit.json", {"scope": "All train businesses; CSV headers, SQLite schema, all XLSX sheets with leading three rows. Not a full cell-quality audit.", "businesses": audit})
    for partition in PARTITIONS:
        ids = set(split["partitions"][partition]["question_ids"])
        # Inference inputs use an explicit allowlist, never copy evaluator fields.
        qrows = [{k: q[k] for k in ("id", "business_id", "question", "as_of_date")}
                 for q in questions if q["id"] in ids]
        arows = [a for a in answers if a["id"] in ids]
        for path, rows in ((output / "inputs" / partition / "questions.jsonl", qrows),
                           (output / "evaluator" / partition / "answers.jsonl", arows)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    write_json(output / "artifact_checksums.json", {p.relative_to(output).as_posix(): digest(p)
               for p in sorted(output.rglob("*")) if p.is_file()})
    for partition, summary in split["partitions"].items():
        print(partition, len(summary["business_ids"]), summary["questions"], summary["industries"], summary["answer_types"])
    print(f"Created {output}; source unchanged; all {archive['matched_members']} archive members match.")


if __name__ == "__main__":
    main()
