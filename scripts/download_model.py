#!/usr/bin/env python3
"""ONLINE preparation only. Inference does not import this script."""
import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-id", default="Qwen/Qwen3-4B")
    parser.add_argument("--revision", required=True, help="Commit SHA; branch/tag allowed only with --resolve-revision")
    parser.add_argument("--resolve-revision", action="store_true", help="Explicit online branch/tag to SHA resolution")
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--metadata-only", action="store_true", help="Do not download weights; manifest will say not runnable")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision) and not args.resolve_revision:
        parser.error("Use a 40-character commit SHA or explicitly --resolve-revision")
    target = args.model_path.expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        old_path = target / "manifest.json"
        if not old_path.exists():
            parser.error("Nonempty target has no manifest; choose a clean directory")
        old = json.loads(old_path.read_text(encoding="utf-8"))
    else:
        old = None
    from huggingface_hub import HfApi, snapshot_download
    info = HfApi().model_info(args.repo_id, revision=args.revision, files_metadata=True)
    revision = info.sha
    if not re.fullmatch(r"[0-9a-f]{40}", revision or ""):
        raise ValueError("Hub did not return a commit SHA")
    if old and (old["repo_id"] != args.repo_id or old["resolved_revision"] != revision):
        parser.error("Target belongs to another model/revision; choose a different directory")
    target.mkdir(parents=True, exist_ok=True)
    # Invalidate old readiness before any partial preparation (safe to resume same SHA).
    manifest = {
        "repo_id": args.repo_id, "requested_revision": args.revision, "resolved_revision": revision,
        "source_url": f"https://huggingface.co/{args.repo_id}/tree/{revision}",
        "license": (info.card_data.to_dict() if info.card_data else {}).get("license", "unknown; verify"),
        "original_release_date": "2025-04-29" if args.repo_id in {"Qwen/Qwen3-4B", "Qwen/Qwen3-8B"} else "unknown; verify",
        "release_source": "https://qwenlm.github.io/blog/qwen3/" if args.repo_id.startswith("Qwen/Qwen3-") else None,
        "local_path": str(target), "weights_downloaded": False,
        "dtype": "unknown until config downloaded", "quantization": "unknown until config downloaded",
        "parameter_count": {"value": None, "method": "unknown; read safetensors index total_parameters or model.num_parameters() after load; do not infer from rounded name"},
        "prepared_at_utc": datetime.now(timezone.utc).isoformat(), "versions": {}, "files": [],
    }
    if info.safetensors is not None:
        manifest["parameter_count"] = {
            "value": info.safetensors.total,
            "method": "Hugging Face safetensors metadata for the resolved commit (all stored parameters)",
            "by_dtype": info.safetensors.parameters,
        }
    for name in ("torch", "transformers", "accelerate", "huggingface-hub"):
        try:
            manifest["versions"][name] = version(name)
        except PackageNotFoundError:
            manifest["versions"][name] = None
    manifest_path = target / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    patterns = ["*.json", "*.jinja", "*.txt", "*.model", "README.md", "LICENSE*", "NOTICE*"]
    if not args.metadata_only:
        patterns.append("*.safetensors")
    snapshot_download(repo_id=args.repo_id, revision=revision, local_dir=target, allow_patterns=patterns)
    config = json.loads((target / "config.json").read_text(encoding="utf-8"))
    manifest["dtype"] = config.get("torch_dtype", "unknown; verify")
    manifest["quantization"] = config.get("quantization_config", "none")
    index = target / "model.safetensors.index.json"
    if index.exists():
        metadata = json.loads(index.read_text(encoding="utf-8")).get("metadata", {})
        manifest["weight_bytes_from_index"] = metadata.get("total_size")
        if metadata.get("total_parameters"):
            manifest["parameter_count"] = {"value": metadata["total_parameters"], "method": "safetensors index metadata"}
    required = {f.rfilename for f in info.siblings if f.rfilename.endswith(".safetensors")}
    if not args.metadata_only and (not required or any(not (target / name).is_file() for name in required)):
        raise ValueError("Incomplete safetensors snapshot")
    manifest["weights_downloaded"] = not args.metadata_only
    for path in sorted(target.rglob("*")):
        if path.is_file() and ".cache" not in path.parts and path != manifest_path:
            manifest["files"].append({"path": path.relative_to(target).as_posix(),
                                      "bytes": path.stat().st_size, "sha256": checksum(path)})
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(manifest_path), "revision": revision,
                      "weights_downloaded": manifest["weights_downloaded"]}))


if __name__ == "__main__":
    main()
