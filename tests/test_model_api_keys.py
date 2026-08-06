"""Unit tests for model API key storage (keychain-backed, never in config)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import src.core.config.model_api_keys as model_api_keys
from src.core.config.model_api_keys import (
    delete_model_api_key,
    get_model_api_key,
    model_api_key_set,
    save_model_api_key,
)


class ModelApiKeysTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._file = Path(self._tmp.name) / "model_api_keys.json"
        self._patcher = patch.object(
            model_api_keys, "MODEL_API_KEYS_FILE", self._file
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_save_then_get(self):
        save_model_api_key("demo", "sk-secret")
        self.assertEqual(get_model_api_key("demo"), "sk-secret")
        self.assertTrue(model_api_key_set("demo"))

    def test_missing_returns_empty(self):
        self.assertEqual(get_model_api_key("nope"), "")
        self.assertFalse(model_api_key_set("nope"))

    def test_empty_key_clears_existing(self):
        save_model_api_key("demo", "sk-secret")
        save_model_api_key("demo", "")
        self.assertEqual(get_model_api_key("demo"), "")
        self.assertFalse(model_api_key_set("demo"))

    def test_delete(self):
        save_model_api_key("demo", "sk-secret")
        delete_model_api_key("demo")
        self.assertEqual(get_model_api_key("demo"), "")
        self.assertFalse(model_api_key_set("demo"))

    def test_persisted_outside_config(self):
        save_model_api_key("demo", "sk-secret")
        raw = json.loads(self._file.read_text(encoding="utf-8"))
        self.assertTrue(any("sk-secret" in v for v in raw.values()))


if __name__ == "__main__":
    unittest.main()
