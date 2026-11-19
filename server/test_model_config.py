"""模型配置读写的单元测试：不依赖网络与真实模型服务。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import model_config


class ModelConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.original = model_config.CONFIG_PATH
        model_config.CONFIG_PATH = Path(self.tmp.name) / "model_config.json"

    def tearDown(self) -> None:
        model_config.CONFIG_PATH = self.original
        self.tmp.cleanup()

    def test_env_defaults_when_file_missing(self) -> None:
        config = model_config.load()
        self.assertEqual(config["source"], "env")
        self.assertTrue(config["base_url"].startswith("http"))
        self.assertIn(config["protocol"], ("anthropic", "openai"))

    def test_save_and_load_roundtrip(self) -> None:
        saved = model_config.save(
            {
                "base_url": "http://127.0.0.1:18787/llmproxy/openai/v1/",
                "protocol": "openai",
                "model": "deepseek-flash",
                "timeout": 30,
            }
        )
        self.assertEqual(saved["base_url"], "http://127.0.0.1:18787/llmproxy/openai/v1")
        self.assertEqual(saved["protocol"], "openai")
        self.assertEqual(saved["timeout"], 30)
        loaded = model_config.load()
        self.assertEqual(loaded["source"], "file")
        self.assertEqual(loaded["model"], "deepseek-flash")

    def test_blank_api_key_keeps_previous(self) -> None:
        model_config.save({"api_key": "sk-first"})
        model_config.save({"api_key": "", "model": "dintal-auto"})
        self.assertEqual(model_config.load()["api_key"], "sk-first")

    def test_public_hides_api_key(self) -> None:
        model_config.save({"api_key": "sk-secret"})
        view = model_config.public()
        self.assertNotIn("api_key", view)
        self.assertTrue(view["has_api_key"])
        self.assertNotIn("sk-secret", json.dumps(view, ensure_ascii=False))

    def test_invalid_url_rejected(self) -> None:
        with self.assertRaises(ValueError):
            model_config.save({"base_url": "ftp://example.com"})

    def test_unknown_protocol_falls_back(self) -> None:
        config = model_config.save({"protocol": "grpc"})
        self.assertEqual(config["protocol"], "anthropic")

    def test_timeout_clamped(self) -> None:
        self.assertEqual(model_config.save({"timeout": 1})["timeout"], 5)
        self.assertEqual(model_config.save({"timeout": 9999})["timeout"], 300)


if __name__ == "__main__":
    unittest.main()
