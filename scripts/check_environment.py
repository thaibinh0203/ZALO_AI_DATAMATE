#!/usr/bin/env python3
"""Read-only diagnostics; stdlib works even before installing torch."""
import argparse
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def command(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=20)
        return {"returncode": p.returncode, "stdout": p.stdout.strip(), "stderr": p.stderr.strip()}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": str(e)}


def collect():
    mem = {}
    if Path("/proc/meminfo").exists():
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
                mem[key + "_bytes"] = int(value.split()[0]) * 1024
    versions = {}
    for package in ("torch", "transformers", "accelerate", "huggingface-hub", "sqlglot"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    report = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "cwd": str(Path.cwd()), "os": platform.platform(),
        "wsl": "microsoft" in platform.release().lower(),
        "python": sys.version, "executable": sys.executable,
        "venv": sys.prefix != sys.base_prefix, "versions": versions,
        "memory": mem, "disk": dict(zip(("total", "used", "free"), shutil.disk_usage(Path.cwd()))),
        "temporary_path": tempfile.gettempdir(),
        "temporary_disk": dict(zip(("total", "used", "free"), shutil.disk_usage(tempfile.gettempdir()))),
        "dxg_exists": Path("/dev/dxg").exists(),
        "nvidia_smi": command(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,driver_version", "--format=csv"]),
        "git": command(["git", "rev-parse", "HEAD"]),
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
    }
    try:
        import torch
        cuda = {"torch_version": torch.__version__, "compiled_cuda": torch.version.cuda,
                "available": torch.cuda.is_available(), "device_count": torch.cuda.device_count()}
        cuda["devices"] = [{"name": torch.cuda.get_device_name(i),
                             "vram_bytes": torch.cuda.get_device_properties(i).total_memory}
                            for i in range(cuda["device_count"])]
        if cuda["available"]:
            tensor = torch.ones(1, device="cuda") + 1
            cuda["tensor_check"] = tensor.item() == 2
        report["cuda"] = cuda
    except (ImportError, RuntimeError, OSError) as e:
        report["cuda"] = {"error": f"{type(e).__name__}: {e}"}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    text = json.dumps(collect(), indent=2, ensure_ascii=False, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
