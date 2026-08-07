"""Tests for the generic "script awaiting input" registry."""
from __future__ import annotations

import time
import unittest

from src.core.services.script_input import PendingScriptInput, ScriptInputRegistry


class ScriptInputRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ScriptInputRegistry()

    def _await(self, **overrides: object) -> dict:
        data = {
            "param": "validate_code",
            "ttl_seconds": 300,
            "prompt": "请输入验证码",
            "hint": "4-6 位字符",
        }
        data.update(overrides)
        return data

    def test_register_and_peek(self) -> None:
        pending = self.registry.register(
            "t1", "run1", "ctsoa_check", "CTS OA 待办", self._await()
        )
        self.assertIsInstance(pending, PendingScriptInput)
        self.assertEqual(pending.param, "validate_code")
        self.assertAlmostEqual(pending.expires_at, time.time() + 300, delta=5)
        self.assertEqual(self.registry.peek("t1").run_id, "run1")

    def test_invalid_param_returns_none(self) -> None:
        self.assertIsNone(
            self.registry.register("t1", "r", "s", "n", {"ttl_seconds": 10})
        )
        self.assertIsNone(self.registry.peek("t1"))

    def test_consume_removes_entry(self) -> None:
        self.registry.register("t1", "r", "s", "n", self._await())
        pending = self.registry.consume("t1")
        self.assertIsNotNone(pending)
        self.assertIsNone(self.registry.peek("t1"))
        self.assertIsNone(self.registry.consume("t1"))

    def test_clear_removes_entry(self) -> None:
        self.registry.register("t1", "r", "s", "n", self._await())
        self.registry.clear("t1")
        self.assertIsNone(self.registry.peek("t1"))

    def test_expiry(self) -> None:
        now = 1000.0
        self.registry.register(
            "t1", "r", "s", "n", self._await(ttl_seconds=300), now=now
        )
        self.assertIsNotNone(self.registry.peek("t1", now=now))
        self.assertIsNone(self.registry.peek("t1", now=now + 301))
        self.assertIsNone(self.registry.consume("t1", now=now + 301))

    def test_zero_or_negative_ttl_defaults_to_safe_value(self) -> None:
        pending = self.registry.register(
            "t1", "r", "s", "n", self._await(ttl_seconds=0), now=1000.0
        )
        self.assertGreater(pending.expires_at, 1000.0)
        pending_neg = self.registry.register(
            "t1", "r", "s", "n", self._await(ttl_seconds=-5), now=1000.0
        )
        self.assertGreater(pending_neg.expires_at, 1000.0)

    def test_non_numeric_ttl_defaults_to_safe_value(self) -> None:
        pending = self.registry.register(
            "t1", "r", "s", "n", self._await(ttl_seconds="oops"), now=1000.0
        )
        self.assertGreater(pending.expires_at, 1000.0)

    def test_session_key_isolation(self) -> None:
        self.registry.register(
            "t1", "r", "s", "n", self._await(), session_key="direct"
        )
        self.assertIsNone(self.registry.peek("t1", session_key="organization"))


if __name__ == "__main__":
    unittest.main()
