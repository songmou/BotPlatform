"""Tests for the ScriptService wiring of the awaiting-input primitive."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.core.config.loader import ScriptDefinition, ScriptParameter
from src.core.services.notification import TenantRecipientStore
from src.core.services.script import ScriptRun, ScriptService
from src.core.services.script_input import PendingScriptInput
from src.core.storage.tenants import TenantRegistry


class FakeNotification:
    def __init__(self) -> None:
        self.texts: list = []

    def enqueue_text_to_tenant(self, tenant_id: str, message: str, **kwargs: object) -> None:
        self.texts.append(message)

    def enqueue_image_to_tenant(self, tenant_id: str, image: object, **kwargs: object) -> None:
        pass


class ScriptPendingInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entrypoint = self.root / "job.py"
        self.entrypoint.write_text("print('ok')\n", encoding="utf-8")
        self.registry = TenantRegistry(self.root / "data")
        self.tenant = self.registry.resolve("bot", "member")
        self.definition = ScriptDefinition(
            id="ctsoa_check",
            name="CTS OA 待办",
            description="",
            entrypoint=str(self.entrypoint),
            timeout_seconds=30,
            parameters={"validate_code": ScriptParameter(type="string", flag="--validate-code")},
            artifact_types={"image"},
        )
        self.scripts = ScriptService(
            {"ctsoa_check": self.definition},
            None,
            TenantRecipientStore(self.registry),
            self.root,
            self.registry,
        )
        self.addCleanup(self.scripts.shutdown)

    def _run_result(self, payload: dict) -> ScriptRun:
        run = ScriptRun(
            run_id="ctsoa_check-20260805T000000-aaaaaaaa",
            script_id="ctsoa_check",
            script_name="CTS OA 待办",
            trigger="model",
            parameters={},
            status="running",
            summary="",
            tenant_id=self.tenant.tenant_id,
        )
        result_path = self.root / "child.json"
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        self.scripts._apply_child_result(run, self.definition, result_path, 0, "", "")
        return run

    def test_awaiting_input_registers_pending_and_sets_status(self) -> None:
        run = self._run_result(
            {
                "status": "awaiting_input",
                "summary": "需要验证码",
                "await_input": {"param": "validate_code", "ttl_seconds": 300},
            }
        )
        self.assertEqual(run.status, "awaiting_input")
        pending = self.scripts.peek_pending_input(self.tenant)
        self.assertIsInstance(pending, PendingScriptInput)
        self.assertEqual(pending.param, "validate_code")
        self.assertEqual(pending.script_id, "ctsoa_check")
        self.assertEqual(pending.tenant_id, self.tenant.tenant_id)

    def test_normal_result_does_not_register_pending(self) -> None:
        run = self._run_result({"status": "success", "summary": "ok"})
        self.assertEqual(run.status, "success")
        self.assertIsNone(self.scripts.peek_pending_input(self.tenant))

    def test_invalid_await_input_is_ignored(self) -> None:
        self._run_result(
            {
                "status": "awaiting_input",
                "summary": "x",
                "await_input": {"ttl_seconds": 300},  # missing param
            }
        )
        self.assertIsNone(self.scripts.peek_pending_input(self.tenant))

    def test_resume_pending_input_submits_with_param(self) -> None:
        self.scripts.input_registry.register(
            self.tenant.tenant_id,
            "run1",
            "ctsoa_check",
            "CTS OA 待办",
            {"param": "validate_code", "ttl_seconds": 300},
        )
        captured: list = []

        def fake_submit(tenant, script_id, parameters):
            captured.append((script_id, parameters))
            return {"run_id": "x", "status": "running"}

        self.scripts.submit_for_tenant = fake_submit
        pending = self.scripts.peek_pending_input(self.tenant)
        self.scripts.resume_pending_input(self.tenant, pending, "6841")
        self.assertEqual(captured, [("ctsoa_check", {"validate_code": "6841"})])
        self.assertIsNone(self.scripts.peek_pending_input(self.tenant))

    def test_resume_failure_keeps_pending_input(self) -> None:
        self.scripts.input_registry.register(
            self.tenant.tenant_id,
            "run1",
            "ctsoa_check",
            "CTS OA 待办",
            {"param": "validate_code", "ttl_seconds": 300},
        )

        def fail_submit(_tenant, _script_id, _parameters):
            raise ValueError("temporary failure")

        self.scripts.submit_for_tenant = fail_submit
        pending = self.scripts.peek_pending_input(self.tenant)
        with self.assertRaisesRegex(ValueError, "temporary failure"):
            self.scripts.resume_pending_input(self.tenant, pending, "6841")
        self.assertEqual(
            self.scripts.peek_pending_input(self.tenant).run_id, "run1"
        )

    def test_skipped_resume_keeps_pending_input(self) -> None:
        pending = self.scripts.input_registry.register(
            self.tenant.tenant_id,
            "run1",
            "ctsoa_check",
            "CTS OA 待办",
            {"param": "validate_code", "ttl_seconds": 300},
        )
        self.scripts.submit_for_tenant = lambda *_args: {
            "run_id": "skipped-run",
            "status": "skipped",
            "summary": "已有同一脚本正在运行",
        }

        with self.assertRaisesRegex(ValueError, "已有同一脚本正在运行"):
            self.scripts.resume_pending_input(self.tenant, pending, "6841")
        self.assertEqual(
            self.scripts.peek_pending_input(self.tenant).run_id, "run1"
        )

    def test_resume_marks_original_run_with_successor(self) -> None:
        original = ScriptRun(
            run_id="ctsoa_check-20260805T000000-aaaaaaaa",
            script_id="ctsoa_check",
            script_name="CTS OA 待办",
            trigger="model",
            parameters={},
            status="awaiting_input",
            summary="需要验证码",
            tenant_id=self.tenant.tenant_id,
        )
        self.scripts._persist(original)
        pending = self.scripts.input_registry.register(
            self.tenant.tenant_id,
            original.run_id,
            "ctsoa_check",
            "CTS OA 待办",
            {"param": "validate_code", "ttl_seconds": 300},
        )
        successor = "ctsoa_check-20260805T000001-bbbbbbbb"
        self.scripts.submit_for_tenant = lambda *_args: {
            "run_id": successor,
            "status": "running",
        }

        self.scripts.resume_pending_input(self.tenant, pending, "6841")

        updated = self.scripts.get_run(self.tenant, original.run_id)
        self.assertEqual(updated["status"], "success")
        self.assertIn(successor, updated["summary"])

    def test_notify_shows_awaiting_input_label(self) -> None:
        self.scripts.notification_service = FakeNotification()
        run = ScriptRun(
            run_id="ctsoa_check-20260805T000001-bbbbbbbb",
            script_id="ctsoa_check",
            script_name="CTS OA 待办",
            trigger="model",
            parameters={},
            status="awaiting_input",
            summary="需要验证码",
            tenant_id=self.tenant.tenant_id,
        )
        self.scripts._notify(run)
        self.assertTrue(
            any("待输入" in text for text in self.scripts.notification_service.texts)
        )


if __name__ == "__main__":
    unittest.main()
