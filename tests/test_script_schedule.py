from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from src.core.config.loader import ScriptDefinition, ScriptParameter
from src.core.services.notification import TenantRecipientStore
from src.core.services.scheduler import SchedulerService
from src.core.services.script import ScriptService
from src.core.services.script_schedule import ScriptScheduleService
from src.core.storage.tenants import ScheduleStore, TenantRegistry


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs = []

    def add_job(self, function, **kwargs):
        self.jobs.append((function, kwargs))

    def remove_job(self, job_id):
        self.jobs = [
            item for item in self.jobs if item[1].get("id") != job_id
        ]


class ScriptScheduleServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entrypoint = self.root / "job.py"
        self.entrypoint.write_text("print('ok')\n", encoding="utf-8")
        self.registry = TenantRegistry(self.root / "data")
        self.first = self.registry.resolve("bot", "first")
        self.second = self.registry.resolve("bot", "second")
        definition = ScriptDefinition(
            id="deploy_demo",
            name="发布演示",
            description="",
            entrypoint=str(self.entrypoint),
            timeout_seconds=30,
            parameters={
                "dry_run": ScriptParameter(
                    type="boolean", flag="--dry-run"
                )
            },
        )
        self.scripts = ScriptService(
            {"deploy_demo": definition},
            None,
            TenantRecipientStore(self.registry),
            self.root,
            self.registry,
        )
        self.addCleanup(self.scripts.shutdown)
        self.service = ScriptScheduleService(
            self.registry, self.scripts, "Asia/Shanghai"
        )

    def create(self, tenant):
        return self.service.manage(
            tenant,
            {
                "action": "create",
                "schedule_id": "nightly_deploy",
                "script_id": "deploy_demo",
                "parameters": {"dry_run": True},
                "crons": ["0 3 * * *"],
                "enabled": True,
            },
        )

    def test_schedules_are_tenant_isolated_and_pin_script_hash(self) -> None:
        created = self.create(self.first)
        self.assertEqual(created["parameters"], {"dry_run": True})
        self.assertEqual(len(created["authorized_sha256"]), 64)
        self.assertEqual(len(self.service.list_for_tenant(self.first)), 1)
        self.assertEqual(self.service.list_for_tenant(self.second), [])

    def test_mutations_reload_and_reauthorize_on_enable(self) -> None:
        calls = []
        self.service.set_reload_callback(lambda: calls.append("reload"))
        self.create(self.first)
        self.service.manage(
            self.first,
            {"action": "disable", "schedule_id": "nightly_deploy"},
        )
        disabled = self.service.store.get(
            self.first.tenant_id, "nightly_deploy"
        )
        self.assertFalse(disabled.enabled)
        self.service.manage(
            self.first,
            {"action": "enable", "schedule_id": "nightly_deploy"},
        )
        enabled = self.service.store.get(
            self.first.tenant_id, "nightly_deploy"
        )
        self.assertTrue(enabled.enabled)
        self.assertEqual(calls, ["reload", "reload", "reload"])

    def test_preview_explains_unattended_authorization(self) -> None:
        preview = self.service.preview(
            self.first,
            {
                "action": "create",
                "schedule_id": "nightly_deploy",
                "script_id": "deploy_demo",
                "parameters": {"dry_run": True},
                "crons": ["0 3 * * *"],
            },
        )
        self.assertIn("无人值守", preview)
        self.assertIn("Asia/Shanghai", preview)
        self.assertIn("脚本版本", preview)

    def test_invalid_cron_and_unknown_parameters_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "无效的五段 cron"):
            self.service.manage(
                self.first,
                {
                    "action": "create",
                    "schedule_id": "bad",
                    "script_id": "deploy_demo",
                    "parameters": {},
                    "crons": ["invalid"],
                },
            )
        with self.assertRaisesRegex(ValueError, "未知参数"):
            self.service.manage(
                self.first,
                {
                    "action": "create",
                    "schedule_id": "bad",
                    "script_id": "deploy_demo",
                    "parameters": {"host": "example.com"},
                    "crons": ["0 3 * * *"],
                },
            )

    def test_scheduler_startup_pauses_changed_script_version(self) -> None:
        self.create(self.first)
        self.entrypoint.write_text("print('changed')\n", encoding="utf-8")
        fake_scheduler = FakeScheduler()
        SchedulerService(
            tasks=[],
            timezone_name="Asia/Shanghai",
            recipient_store=TenantRecipientStore(self.registry),
            scheduler=fake_scheduler,
            script_service=self.scripts,
            script_schedule_service=self.service,
            tenant_registry=self.registry,
            schedule_store=ScheduleStore(self.registry),
        )._register_script_schedules()
        stored = self.service.store.get(
            self.first.tenant_id, "nightly_deploy"
        )
        self.assertFalse(stored.enabled)
        self.assertIn("授权失效", stored.last_status)
        self.assertEqual(fake_scheduler.jobs, [])

    def test_scheduled_run_records_final_status(self) -> None:
        self.create(self.first)
        scheduler = SchedulerService(
            tasks=[],
            timezone_name="Asia/Shanghai",
            recipient_store=TenantRecipientStore(self.registry),
            scheduler=FakeScheduler(),
            script_service=self.scripts,
            script_schedule_service=self.service,
            tenant_registry=self.registry,
            schedule_store=ScheduleStore(self.registry),
        )
        self.assertTrue(
            scheduler.run_script_schedule(
                self.first.tenant_id, "nightly_deploy"
            )
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            stored = self.service.store.get(
                self.first.tenant_id, "nightly_deploy"
            )
            if stored.last_status != "running":
                break
            time.sleep(0.02)
        else:
            self.fail("scheduled run did not finish")
        self.assertEqual(stored.last_status, "success")
        self.assertTrue(stored.last_run_id)


if __name__ == "__main__":
    unittest.main()
