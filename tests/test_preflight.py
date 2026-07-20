from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.infrastructure.diagnostics import check_configuration


SOURCE_CONFIG = Path(__file__).resolve().parents[1] / "config"
SOURCE_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class PreflightTests(unittest.TestCase):
    def copy_project_config(self, directory: str) -> Path:
        config = Path(directory) / "config"
        shutil.copytree(SOURCE_CONFIG, config)
        shutil.copytree(SOURCE_SCRIPTS, Path(directory) / "scripts")
        return config

    def test_missing_required_key_is_aggregated_and_optional_features_warn(self):
        with patch(
            "src.infrastructure.diagnostics._ollama_models",
            return_value=["gemma4:e4b"],
        ):
            report = check_configuration(SOURCE_CONFIG, environment={})
        self.assertFalse(report.ok)
        self.assertEqual(
            sum("DEEPSEEK_API_KEY" in item.message for item in report.errors), 1
        )
        warning_text = "\n".join(item.message for item in report.warnings)
        self.assertIn("全文检索", warning_text)

    def test_key_is_not_sent_to_provider_and_optional_warnings_do_not_fail(self):
        with patch(
            "src.infrastructure.diagnostics._ollama_models",
            return_value=["gemma4:e4b"],
        ):
            report = check_configuration(
                SOURCE_CONFIG, environment={"DEEPSEEK_API_KEY": "test-key"}
            )
        self.assertTrue(report.ok)

    def test_enabled_ollama_placeholder_fails_then_configured_model_is_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.copy_project_config(directory)
            path = config / "models.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["profiles"]["ollama_local"]["enabled"] = True
            data["profiles"]["ollama_local"]["model"] = "YOUR_OLLAMA_MODEL"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            report = check_configuration(
                config, environment={"DEEPSEEK_API_KEY": "test-key"}
            )
            self.assertFalse(report.ok)
            self.assertTrue(any("占位模型名" in item.message for item in report.errors))

            data["profiles"]["ollama_local"]["model"] = "qwen-test"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with patch("src.infrastructure.diagnostics._ollama_models", return_value=["qwen-test"]):
                report = check_configuration(
                    config, environment={"DEEPSEEK_API_KEY": "test-key"}
                )
            self.assertTrue(report.ok)
            self.assertTrue(any("Ollama 档案 ollama_local 可用" in item.message for item in report.ready))


if __name__ == "__main__":
    unittest.main()
