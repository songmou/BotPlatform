from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from src.core.config.loader import ScriptDefinition
from src.core.services.script import ScriptService
from src.core.services.script_registry import ExternalScriptRegistry
from src.core.services.notification import TenantRecipientStore
from src.core.storage.tenants import TenantRegistry


class ExternalScriptRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scripts_root = self.root / "approved"
        self.scripts_root.mkdir()
        self.script = self.scripts_root / "deploy.sh"
        self.script.write_text("#!/bin/sh\nprintf 'ok\\n'\n", encoding="utf-8")
        os.chmod(self.script, 0o700)
        self.registry_path = self.root / "system" / "script_registry.json"
        self.registry = ExternalScriptRegistry(self.registry_path)
        self.registry.configure_roots([str(self.scripts_root)])

    def definition(self):
        return self.registry.create(
            {
                "id": "deploy_demo",
                "name": "发布演示",
                "description": "测试脚本",
                "entrypoint": str(self.script),
                "runtime": "executable",
                "timeout_seconds": 30,
                "parameters": {
                    "dry_run": {
                        "type": "boolean",
                        "flag": "--dry-run",
                    },
                    "count": {
                        "type": "integer",
                        "flag": "--count",
                    },
                },
                "env_allowlist": ["DEPLOY_SSH_KEY"],
                "concurrency_scope": "global",
            }
        )

    def test_registry_is_private_and_boolean_flags_do_not_get_values(self) -> None:
        definition = self.definition()
        self.assertEqual(os.stat(self.registry_path).st_mode & 0o777, 0o600)
        tenant_registry = TenantRegistry(self.root / "data")
        service = ScriptService(
            {"builtin": ScriptDefinition("builtin", "内置", "", __file__, 10)},
            None,
            TenantRecipientStore(tenant_registry),
            self.root,
            tenant_registry,
            external_registry=self.registry,
        )
        self.addCleanup(service.shutdown)
        normalized = service.normalize(
            "deploy_demo", {"dry_run": True, "count": 2}
        )[1]
        self.assertEqual(
            service._argv(definition, normalized),
            [definition.entrypoint, "--dry-run", "--count", "2"],
        )

    def test_hash_change_and_symlink_are_rejected(self) -> None:
        definition = self.definition()
        self.script.write_text("#!/bin/sh\nprintf 'changed\\n'\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "内容已变化"):
            self.registry.verify(definition)

        link = self.scripts_root / "link.sh"
        link.symlink_to(self.script)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.registry.create(
                {
                    "id": "linked",
                    "name": "链接",
                    "entrypoint": str(link),
                    "runtime": "executable",
                }
            )

    def test_only_allowlisted_environment_values_are_loaded(self) -> None:
        definition = self.definition()
        env_path = self.registry.env_path
        env_path.write_text(
            "DEPLOY_SSH_KEY=/safe/key\nOTHER_SECRET=hidden\n", encoding="utf-8"
        )
        os.chmod(env_path, 0o600)
        self.assertEqual(
            self.registry.environment_for(definition),
            {"DEPLOY_SSH_KEY": "/safe/key"},
        )
        os.chmod(env_path, 0o644)
        with self.assertRaisesRegex(ValueError, "权限必须为 0600"):
            self.registry.environment_for(definition)

    def test_invalid_registry_permissions_fail_closed(self) -> None:
        self.definition()
        os.chmod(self.registry_path, 0o644)
        with self.assertRaisesRegex(ValueError, "权限必须为 0600"):
            ExternalScriptRegistry(self.registry_path)

    def test_registry_never_persists_environment_values(self) -> None:
        self.definition()
        payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        serialized = json.dumps(payload)
        self.assertIn("DEPLOY_SSH_KEY", serialized)
        self.assertNotIn("/safe/key", serialized)

    def test_allowlisted_secret_is_redacted_from_summary_and_log(self) -> None:
        self.script.write_text(
            "#!/bin/sh\nprintf 'key=%s\\n' \"$DEPLOY_SSH_KEY\"\n",
            encoding="utf-8",
        )
        os.chmod(self.script, 0o700)
        self.definition()
        self.registry.env_path.write_text(
            "DEPLOY_SSH_KEY=top-secret-value\n", encoding="utf-8"
        )
        os.chmod(self.registry.env_path, 0o600)
        tenant_registry = TenantRegistry(self.root / "data")
        tenant = tenant_registry.resolve("bot", "user")
        service = ScriptService(
            {},
            None,
            TenantRecipientStore(tenant_registry),
            self.root,
            tenant_registry,
            external_registry=self.registry,
        )
        self.addCleanup(service.shutdown)
        submitted = service.submit(
            tenant, "deploy_demo", {}, trigger="verification"
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = service.get_run(tenant, submitted["run_id"])
            if result["status"] != "running":
                break
            time.sleep(0.02)
        else:
            self.fail("script run did not finish")
        self.assertEqual(result["status"], "success")
        self.assertNotIn("top-secret-value", result["summary"])
        self.assertNotIn("top-secret-value", result["log_tail"])
        self.assertIn("***", result["log_tail"])


if __name__ == "__main__":
    unittest.main()
