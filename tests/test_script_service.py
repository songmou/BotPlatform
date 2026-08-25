from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.config.loader import ScriptDefinition, ScriptParameter
from src.core.integrations.ilink import Credentials
from src.core.integrations.keychain import KeychainService
from src.core.services.notification import TenantRecipientStore
from src.core.services.script import ScriptRun, ScriptService
from src.core.storage.tenants import IntegrationStore, TenantRegistry


FAKE_SCRIPT = r'''from __future__ import annotations
import json
import base64
import os
import sys
import time
from pathlib import Path

mode = sys.argv[1]
if mode == "slow":
    time.sleep(2)
else:
    time.sleep(0.15)
root = Path(os.environ["ILINKBOT_SCRIPT_DATA_ROOT"])
root.mkdir(parents=True, exist_ok=True)
image = root / "result.png"
image.write_bytes(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
))
target = Path(os.environ["ILINKBOT_SCRIPT_RESULT_FILE"])
temporary = target.with_suffix(target.suffix + ".tmp")
temporary.write_text(json.dumps({
    "status": "success",
    "summary": "done " + mode,
    "artifacts": [str(image)],
}), encoding="utf-8")
temporary.replace(target)
'''


class FakeNotificationService:
    def __init__(self) -> None:
        self.texts = []
        self.images = []

    def send_text_to(self, recipient, message):
        self.texts.append((recipient, message))

    def send_image_to(self, recipient, source, caption=""):
        self.images.append((recipient, source, caption))

    def enqueue_text_to_tenant(self, tenant_id, message, **_kwargs):
        self.texts.append((tenant_id, message))

    def enqueue_image_to_tenant(self, tenant_id, source, caption="", **_kwargs):
        self.images.append((tenant_id, source, caption))


class ScriptServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        scripts = self.root / "scripts"
        scripts.mkdir()
        self.entrypoint = scripts / "fake.py"
        self.entrypoint.write_text(FAKE_SCRIPT, encoding="utf-8")
        self.registry = TenantRegistry(self.root / "data")
        self.tenant = self.registry.resolve("bot", "user@im.wechat")
        self.store = TenantRecipientStore(self.registry)
        self.notifications = FakeNotificationService()

    def service(self, timeout=5, requires_approval=True) -> ScriptService:
        definition = ScriptDefinition(
            id="fake",
            name="测试脚本",
            description="测试",
            entrypoint=str(self.entrypoint),
            timeout_seconds=timeout,
            requires_approval=requires_approval,
            parameters={
                "mode": ScriptParameter(
                    type="string",
                    required=True,
                    choices=["ok", "slow"],
                    positional=True,
                )
            },
            artifact_types=["image"],
        )
        service = ScriptService(
            {"fake": definition},
            Credentials("token", "https://gateway", "bot", "owner"),
            self.store,
            self.root,
            self.registry,
            IntegrationStore(self.registry),
            python_executable=os.sys.executable,
            notification_service=self.notifications,
        )
        self.addCleanup(service.shutdown)
        return service

    def test_script_approval_policy_defaults_closed_and_can_be_disabled(self) -> None:
        protected = self.service()
        automatic = self.service(requires_approval=False)

        self.assertTrue(protected.requires_approval("fake"))
        self.assertTrue(protected.requires_approval("unknown"))
        self.assertTrue(protected.requires_approval(None))
        self.assertTrue(protected.has_approval_required_scripts())
        self.assertFalse(automatic.requires_approval("fake"))
        self.assertFalse(automatic.has_approval_required_scripts())
        self.assertFalse(automatic.list_scripts()[0]["requires_approval"])

    def wait_for(self, service: ScriptService, run_id: str, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = service.get_run(self.tenant, run_id)
            if result["status"] != "running":
                return result
            time.sleep(0.02)
        self.fail("script run did not finish")

    def test_background_success_persists_private_record_and_artifact(self) -> None:
        service = self.service()
        submitted = service.submit(self.tenant, "fake", {"mode": "ok"}, trigger="schedule")
        self.assertEqual(submitted["status"], "running")
        result = self.wait_for(service, submitted["run_id"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["summary"], "done ok")
        self.assertEqual(len(result["artifacts"]), 1)
        with self.registry.database.read() as connection:
            record = connection.execute(
                "SELECT status FROM script_runs WHERE run_id=?", (submitted["run_id"],)
            ).fetchone()
        self.assertEqual(record["status"], "success")
        self.assertFalse(
            (self.registry.tenant_root(self.tenant.tenant_id) / "script_runs").exists()
        )
        self.assertEqual(self.notifications.texts[0][0], self.tenant.tenant_id)

    def test_result_notification_is_queued_by_tenant_with_artifacts(self) -> None:
        service = self.service()
        self.store.update(self.tenant, "context-1")
        submitted = service.submit_for_tenant(self.tenant, "fake", {"mode": "ok"})
        self.store.update(self.tenant, "context-2")
        result = self.wait_for(service, submitted["run_id"])
        deadline = time.monotonic() + 1
        while not self.notifications.texts and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.notifications.texts[0][0], self.tenant.tenant_id)
        self.assertIn("固定脚本结果", self.notifications.texts[0][1])
        self.assertEqual(self.notifications.images[0][0], self.tenant.tenant_id)

    @unittest.skipUnless(os.name == "posix", "POSIX process-group SIGTERM cancellation semantics")
    def test_duplicate_is_skipped_without_queueing(self) -> None:
        service = self.service()
        first = service.submit(self.tenant, "fake", {"mode": "slow"}, trigger="schedule")
        second = service.submit(self.tenant, "fake", {"mode": "ok"}, trigger="schedule")
        self.assertEqual(second["status"], "skipped")
        self.assertIn("已有", second["summary"])
        service.shutdown()
        self.assertEqual(service.get_run(self.tenant, first["run_id"])["status"], "cancelled")

    @unittest.skipUnless(os.name == "posix", "POSIX signal exit codes (-SIGTERM) on timeout")
    def test_timeout_and_parameter_validation(self) -> None:
        service = self.service(timeout=1)
        with self.assertRaisesRegex(ValueError, "仅允许"):
            service.submit(self.tenant, "fake", {"mode": "unknown"}, trigger="schedule")
        submitted = service.submit(self.tenant, "fake", {"mode": "slow"}, trigger="schedule")
        result = self.wait_for(service, submitted["run_id"], timeout=3)
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["exit_code"], -15)

    def test_integration_metadata_is_injected_without_secret(self) -> None:
        integration_store = IntegrationStore(self.registry)
        keychain = KeychainService(storage_path=self.root / "credentials.json")
        reference = keychain.reference(self.tenant.tenant_id, "autogen")
        keychain.set_secret(reference, "never-in-environment")
        integration_store.set(
            self.tenant.tenant_id,
            "autogen",
            {
                "account": "account-value",
                "keychain_service": reference.service,
                "keychain_account": reference.account,
            },
        )
        definition = ScriptDefinition(
            id="autogen_monitor",
            name="AutoGen",
            description="test",
            entrypoint=str(self.entrypoint),
            timeout_seconds=5,
            requires_approval=False,
        )
        service = ScriptService(
            {"autogen_monitor": definition},
            Credentials("token", "https://gateway", "bot", "owner"),
            self.store,
            self.root,
            self.registry,
            integration_store,
            keychain_service=keychain,
            notification_service=self.notifications,
        )
        self.addCleanup(service.shutdown)
        run = ScriptRun(
            run_id="autogen_monitor-20260720T120000-12345678",
            script_id="autogen_monitor",
            script_name="AutoGen",
            trigger="verification",
            parameters={},
            status="running",
            summary="",
            tenant_id=self.tenant.tenant_id,
        )
        environment = service._environment(run, self.root / "result.json")
        self.assertEqual(environment["ILINKBOT_INTEGRATION_ACCOUNT"], "account-value")
        self.assertEqual(environment["ILINKBOT_KEYCHAIN_SERVICE"], reference.service)
        self.assertNotIn("never-in-environment", environment.values())

    def test_autogen_inherits_the_main_default_language_model_credentials(self) -> None:
        integration_store = IntegrationStore(self.registry)
        keychain = KeychainService(storage_path=self.root / "credentials.json")
        reference = keychain.reference(self.tenant.tenant_id, "autogen")
        keychain.set_secret(reference, "site-password")
        integration_store.set(
            self.tenant.tenant_id,
            "autogen",
            {
                "account": "account-value",
                "keychain_service": reference.service,
                "keychain_account": reference.account,
            },
        )
        definition = ScriptDefinition(
            id="autogen_monitor",
            name="AutoGen",
            description="test",
            entrypoint=str(self.entrypoint),
            timeout_seconds=5,
            requires_approval=False,
        )
        project_root = Path(__file__).resolve().parents[1]
        service = ScriptService(
            {"autogen_monitor": definition},
            Credentials("token", "https://gateway", "bot", "owner"),
            self.store,
            project_root,
            self.registry,
            integration_store,
            keychain_service=keychain,
            notification_service=self.notifications,
        )
        self.addCleanup(service.shutdown)
        run = ScriptRun(
            run_id="autogen_monitor-20260720T120000-12345678",
            script_id="autogen_monitor",
            script_name="AutoGen",
            trigger="verification",
            parameters={},
            status="running",
            summary="",
            tenant_id=self.tenant.tenant_id,
        )
        with patch.dict(
            os.environ,
            {"DASHSCOPE_API_KEY": "cloud-key", "MODEL_PROFILE": "qwen_cloud"},
            clear=False,
        ):
            environment = service._environment(run, self.root / "result.json")
        self.assertEqual(environment["AUTOGEN_MODEL_PROFILE"], "qwen_cloud")
        self.assertEqual(environment["DASHSCOPE_API_KEY"], "cloud-key")
        self.assertIn("ILINKBOT_PROJECT_CONFIG", environment)

    def test_integration_script_is_rejected_before_queue_when_credentials_are_missing(self) -> None:
        definition = ScriptDefinition(
            id="ctsehr_check",
            name="CTS EHR",
            description="test",
            entrypoint=str(self.entrypoint),
            timeout_seconds=5,
            requires_approval=False,
        )
        service = ScriptService(
            {"ctsehr_check": definition},
            Credentials("token", "https://gateway", "bot", "owner"),
            self.store,
            self.root,
            self.registry,
            notification_service=self.notifications,
            keychain_service=KeychainService(storage_path=self.root / "credentials.json"),
        )
        self.addCleanup(service.shutdown)
        with self.assertRaisesRegex(ValueError, "/integration setup ctsehr"):
            service.submit(self.tenant, "ctsehr_check", {}, trigger="schedule")

    def test_ctsoa_script_requires_its_own_integration(self) -> None:
        definition = ScriptDefinition(
            id="ctsoa_check",
            name="CTS OA",
            description="test",
            entrypoint=str(self.entrypoint),
            timeout_seconds=5,
            requires_approval=False,
        )
        service = ScriptService(
            {"ctsoa_check": definition},
            Credentials("token", "https://gateway", "bot", "owner"),
            self.store,
            self.root,
            self.registry,
            notification_service=self.notifications,
            keychain_service=KeychainService(storage_path=self.root / "credentials.json"),
        )
        self.addCleanup(service.shutdown)
        with self.assertRaisesRegex(ValueError, "/integration setup ctsoa"):
            service.submit(self.tenant, "ctsoa_check", {}, trigger="manual")


if __name__ == "__main__":
    unittest.main()
