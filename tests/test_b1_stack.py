"""Optional real stack test with RANDOM tiny weights, not Qwen3-4B inference."""
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from datamate_b.model import LocalModel, ModelConfig


@unittest.skipUnless((ROOT / "models/qwen3-4b/tokenizer.json").exists(), "Run metadata-only download for offline tokenizer test")
class OfflineStackTests(unittest.TestCase):
    def test_real_template_and_random_tiny_model_without_network(self):
        import os
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        from transformers import AutoTokenizer, Qwen3Config, Qwen3ForCausalLM
        import torch

        torch.set_num_threads(1)
        source = ROOT / "models/qwen3-4b"
        with tempfile.TemporaryDirectory() as directory, patch.object(
            socket.socket, "connect", side_effect=AssertionError("Network forbidden during offline test")
        ):
            tokenizer = AutoTokenizer.from_pretrained(str(source), local_files_only=True)
            text = tokenizer.apply_chat_template(
                [{"role": "user", "content": "Chào"}], tokenize=False,
                add_generation_prompt=True, enable_thinking=False)
            self.assertTrue(text.endswith("<think>\n\n</think>\n\n"))
            target = Path(directory)
            tokenizer.save_pretrained(target)
            torch.manual_seed(42)
            tiny = Qwen3ForCausalLM(Qwen3Config(
                vocab_size=len(tokenizer), hidden_size=16, intermediate_size=32,
                num_hidden_layers=1, num_attention_heads=2, num_key_value_heads=1,
                head_dim=8, max_position_embeddings=512, tie_word_embeddings=True,
                sliding_window=None))
            tiny.save_pretrained(target, safe_serialization=True)
            (target / "manifest.json").write_text(json.dumps({
                "repo_id": "unit-test/random-tiny-qwen3", "weights_downloaded": True,
                "resolved_revision": "0" * 40,
            }))
            wrapper = LocalModel(ModelConfig(str(target), device="cpu", dtype="float32",
                                             max_new_tokens=2, max_context_tokens=512))
            wrapper.load()
            identity = id(wrapper.model)
            first = wrapper.generate([{"role": "user", "content": "Chào"}])
            wrapper.load()
            second = wrapper.generate([{"role": "user", "content": "Chào"}])
            self.assertEqual(identity, id(wrapper.model))
            self.assertEqual(first["raw_output"], second["raw_output"])
            self.assertGreater(first["prompt_tokens"], 0)
            self.assertLessEqual(first["output_tokens"], 2)
            self.assertGreaterEqual(first["generation_ms"], 0)
            self.assertEqual(wrapper.metadata()["actual_parameters"], tiny.num_parameters())
            # Detect silent prompt truncation before generation.
            with self.assertRaisesRegex(ValueError, "exceeds context"):
                wrapper.generate([{"role": "user", "content": "xin chào " * 1000}])
