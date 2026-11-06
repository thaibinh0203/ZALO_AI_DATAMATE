"""One local Qwen3 model instance, reused for all questions."""
import json
import os
import re
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path
from time import perf_counter


@dataclass(frozen=True)
class ModelConfig:
    model_path: str
    device: str = "cuda:0"
    dtype: str = "bfloat16"
    max_new_tokens: int = 512
    max_context_tokens: int = 4096
    seed: int = 42


class LocalModel:
    def __init__(self, config):
        self.config = config
        self.path = Path(config.model_path).expanduser().resolve()
        if not self.path.is_dir():
            raise ValueError(f"Local model directory not found: {self.path}")
        if config.device != "cpu" and not re.fullmatch(r"cuda:\d+", config.device):
            raise ValueError("device must be cpu or cuda:N")
        if config.dtype not in ("bfloat16", "float16", "float32"):
            raise ValueError("Unsupported dtype")
        if config.device == "cpu" and config.dtype != "float32":
            raise ValueError("CPU requires explicit float32; check available RAM before use")
        if not 0 < config.max_new_tokens < config.max_context_tokens <= 32768:
            raise ValueError("Require 0 < max_new_tokens < max_context_tokens <= 32768")
        self.manifest = json.loads((self.path / "manifest.json").read_text(encoding="utf-8"))
        if not self.manifest.get("weights_downloaded"):
            raise ValueError("Manifest is metadata-only; download weights first")
        if not re.fullmatch(r"[0-9a-f]{40}", self.manifest.get("resolved_revision", "")):
            raise ValueError("Manifest requires a fixed commit revision")
        model_config = json.loads((self.path / "config.json").read_text(encoding="utf-8"))
        if model_config.get("model_type") != "qwen3":
            raise ValueError("B1 wrapper currently supports Qwen3 chat template only")
        if not list(self.path.glob("*.safetensors")):
            raise ValueError("Missing local safetensors weights")
        self.model = self.tokenizer = None
        self.load_ms = None

    def load(self):
        if self.model is not None:
            return self
        # Set before importing transformers/hub. Never resolve a repo_id here.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, set_seed

        if self.config.device.startswith("cuda"):
            if not torch.cuda.is_available():
                raise RuntimeError("CUDA unavailable; no automatic CPU fallback")
            index = int(self.config.device.split(":")[1])
            if index >= torch.cuda.device_count():
                raise ValueError("CUDA device index out of range")
            if self.config.dtype == "bfloat16":
                with torch.cuda.device(index):
                    if not torch.cuda.is_bf16_supported():
                        raise ValueError("GPU does not support bfloat16; consider float16")
        set_seed(self.config.seed)
        started = perf_counter()
        self.tokenizer = AutoTokenizer.from_pretrained(
            str(self.path), local_files_only=True, trust_remote_code=False)
        self.model = AutoModelForCausalLM.from_pretrained(
            str(self.path), local_files_only=True, trust_remote_code=False,
            use_safetensors=True, torch_dtype=getattr(torch, self.config.dtype),
            device_map={"": self.config.device}, attn_implementation="sdpa")
        self.model.eval()
        if self.config.device.startswith("cuda"):
            torch.cuda.synchronize(self.model.device)
        self.load_ms = round((perf_counter() - started) * 1000, 3)
        return self

    def metadata(self):
        return {"config": asdict(self.config), "resolved_path": str(self.path),
                "repo_id": self.manifest["repo_id"],
                "revision": self.manifest["resolved_revision"],
                "decoding": {"do_sample": False, "num_beams": 1, "enable_thinking": False},
                "versions": {p: version(p) for p in
                             ("torch", "transformers", "accelerate", "huggingface-hub")},
                "actual_parameters": None if self.model is None else self.model.num_parameters()}

    def generate(self, messages):
        if self.model is None:
            raise RuntimeError("Call load() before generate()")
        import torch
        text = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        inputs = self.tokenizer(text, return_tensors="pt", add_special_tokens=False).to(self.model.device)
        count = inputs.input_ids.shape[-1]
        if count + self.config.max_new_tokens > self.config.max_context_tokens:
            raise ValueError("Prompt + output budget exceeds context; no silent truncation")
        if self.config.device.startswith("cuda"):
            torch.cuda.synchronize(self.model.device)
            torch.cuda.reset_peak_memory_stats(self.model.device)
        started = perf_counter()
        with torch.inference_mode():
            output = self.model.generate(
                **inputs, max_new_tokens=self.config.max_new_tokens,
                do_sample=False, num_beams=1, temperature=None, top_p=None, top_k=None,
                pad_token_id=self.tokenizer.eos_token_id)
        if self.config.device.startswith("cuda"):
            torch.cuda.synchronize(self.model.device)
        elapsed = round((perf_counter() - started) * 1000, 3)
        generated = output[0, count:]
        result = {"raw_output": self.tokenizer.decode(generated, skip_special_tokens=True),
                "prompt_tokens": count, "output_tokens": len(generated),
                "generation_ms": elapsed,
                "hit_token_limit": len(generated) >= self.config.max_new_tokens}
        if self.config.device.startswith("cuda"):
            result["peak_cuda_allocated_bytes"] = torch.cuda.max_memory_allocated(self.model.device)
            result["peak_cuda_reserved_bytes"] = torch.cuda.max_memory_reserved(self.model.device)
        return result
