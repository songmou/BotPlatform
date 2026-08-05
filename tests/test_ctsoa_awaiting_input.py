"""Tests for CTS OA monitor's awaiting-input behaviour."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.core.jobs.ctsoa import monitor as ctsoa_monitor
from src.core.jobs.ctsoa.monitor import AuthenticationError, execute_monitor, main
from src.core.jobs._common.script_result import AWAITING_INPUT, write_script_result


class FakeClient:
    def __init__(self, config, logger) -> None:
        self.config = config
        self.logger = logger
        self.login_calls = 0
        self.create_calls = 0

    def restore_session(self, path, account):
        return False

    def login_with_challenge(self, account, password, validate_code):
        self.login_calls += 1
        raise AuthenticationError("验证码错误")

    def create_challenge(self, account, challenge_ttl):
        self.create_calls += 1
        return {
            "kind": "challenge",
            "captcha": "/tmp/captcha.png",
            "expires_in_seconds": challenge_ttl,
        }

    def save_session(self, path, account):
        pass

    def close(self):
        pass

    def pending(self, max_items):
        return {
            "pending_count": 0,
            "returned_count": 0,
            "items": [],
            "category_counts": {},
        }


def _config() -> dict:
    return {
        "oa": {
            "base_url": "https://oa.example.com",
            "account": "alice",
            "keychain_service": "ctsoa",
            "timeout_seconds": 30,
            "retries": 3,
            "challenge_ttl_seconds": 300,
        },
        "pending": {"max_items": 5},
        "output": {"results_dir": "results", "logs_dir": "logs"},
    }


class ScriptResultAwaitInputTests(unittest.TestCase):
    def test_write_awaiting_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            os.environ["ILINKBOT_SCRIPT_RESULT_FILE"] = str(path)
            try:
                write_script_result(
                    AWAITING_INPUT,
                    "需要验证码",
                    ["/tmp/captcha.png"],
                    await_input={
                        "param": "validate_code",
                        "ttl_seconds": 300,
                        "hint": "图片中的字符",
                    },
                )
            finally:
                os.environ.pop("ILINKBOT_SCRIPT_RESULT_FILE", None)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], AWAITING_INPUT)
            self.assertEqual(data["await_input"]["param"], "validate_code")
            self.assertEqual(data["await_input"]["ttl_seconds"], 300)

    def test_missing_env_var_is_noop(self) -> None:
        os.environ.pop("ILINKBOT_SCRIPT_RESULT_FILE", None)
        # Should not raise.
        write_script_result(AWAITING_INPUT, "x")


class CtsOaAwaitInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger("test_ctsoa")

    def test_wrong_code_reissues_challenge(self) -> None:
        with mock.patch.object(ctsoa_monitor, "OAClient", FakeClient), mock.patch.object(
            ctsoa_monitor, "load_password", return_value="pw"
        ):
            result = execute_monitor(_config(), self.logger, validate_code="wrong")
        self.assertEqual(result["kind"], "challenge")

    def test_success_returns_pending(self) -> None:
        class OkClient(FakeClient):
            def login_with_challenge(self, account, password, validate_code):
                self.login_calls += 1

        with mock.patch.object(ctsoa_monitor, "OAClient", OkClient), mock.patch.object(
            ctsoa_monitor, "load_password", return_value="pw"
        ):
            result = execute_monitor(_config(), self.logger, validate_code="right")
        self.assertEqual(result["kind"], "pending")

    def test_main_challenge_writes_awaiting_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory) / "data"
            data_root.mkdir()
            result_file = data_root / "result.json"
            cfg = data_root / "config.json"
            cfg.write_text(json.dumps(_config()), encoding="utf-8")
            os.environ["ILINKBOT_SCRIPT_DATA_ROOT"] = str(data_root)
            os.environ["ILINKBOT_SCRIPT_RESULT_FILE"] = str(result_file)
            try:
                with mock.patch.object(
                    ctsoa_monitor, "OAClient", FakeClient
                ), mock.patch.object(
                    ctsoa_monitor, "load_password", return_value="pw"
                ):
                    rc = main(["--config", str(cfg)])
                self.assertEqual(rc, 0)
                data = json.loads(result_file.read_text(encoding="utf-8"))
                self.assertEqual(data["status"], AWAITING_INPUT)
                self.assertEqual(data["await_input"]["param"], "validate_code")
                self.assertEqual(
                    data["await_input"]["ttl_seconds"], 300
                )
            finally:
                os.environ.pop("ILINKBOT_SCRIPT_DATA_ROOT", None)
                os.environ.pop("ILINKBOT_SCRIPT_RESULT_FILE", None)


if __name__ == "__main__":
    unittest.main()
