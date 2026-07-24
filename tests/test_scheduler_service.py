from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.config.loader import ScheduledTask, TaskAction, TaskCondition
from src.core.integrations.ilink import Credentials, ILinkError
from src.core.integrations.images import ImageSourceError
from src.core.plugins.base import PluginContext
from src.core.plugins.todo import TodoPlugin
from src.core.services.notification import TenantRecipientStore
from src.core.services.scheduler import SchedulerService
from src.core.storage.tenants import ScheduleStore, TenantRegistry


class FakeAgentService:
    def __init__(self) -> None:
        self.calls = []

    def generate(self, agent_id, prompt):
        self.calls.append((agent_id, prompt))
        return "AI 定时内容"


class FakeILink:
    def __init__(self, fail=False) -> None:
        self.fail = fail
        self.sent = []
        self.closed = False

    def send_text(self, user_id, context_token, text):
        self.sent.append((user_id, context_token, text))
        if self.fail:
            raise ILinkError("send failed")

    def send_image(self, user_id, context_token, image_bytes, caption=""):
        self.sent.append((user_id, context_token, image_bytes, caption))
        if self.fail:
            raise ILinkError("send failed")

    def close(self):
        self.closed = True


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs = []
        self.started = False
        self.stopped = False

    def add_job(self, function, **kwargs):
        self.jobs.append((function, kwargs))

    def start(self):
        self.started = True

    def shutdown(self, wait=False):
        self.stopped = True


def fixed_task(enabled=True):
    return ScheduledTask(
        id="fixed",
        enabled=enabled,
        cron="*/5 * * * *",
        target="last_active_user",
        action=TaskAction(type="text", content="固定提醒"),
    )


def ai_task(enabled=True):
    return ScheduledTask(
        id="ai",
        enabled=enabled,
        cron="0 9 * * *",
        target="last_active_user",
        action=TaskAction(
            type="agent_prompt", agent_id="general", prompt="生成提醒"
        ),
    )


def inactivity_task(enabled=True):
    return ScheduledTask(
        id="inactive_user_reminder",
        enabled=enabled,
        cron="*/10 * * * *",
        target="last_active_user",
        condition=TaskCondition(
            type="inactivity_once", after_hours=20, before_hours=24
        ),
        action=TaskAction(type="text", content="静默提醒"),
    )


def image_task(source="path", caption="状态报告"):
    return ScheduledTask(
        id="image",
        enabled=True,
        cron="0 10 * * *",
        target="last_active_user",
        action=TaskAction(
            type="image",
            image_path="/tmp/status.png" if source == "path" else None,
            image_url="http://internal/status.png?token=secret"
            if source == "url"
            else None,
            caption=caption,
        ),
    )


class FakeImageLoader:
    def __init__(self, failure=None) -> None:
        self.failure = failure
        self.sources = []

    def load(self, source):
        self.sources.append(source)
        if self.failure:
            raise self.failure
        return b"validated-image"


class FakeScriptService:
    def __init__(self) -> None:
        self.calls = []

    def submit(self, tenant, script_id, parameters, trigger, recipient=None):
        self.calls.append((tenant, script_id, parameters, trigger, recipient))
        return {"run_id": "job-1", "status": "running"}


class FakePlugin:
    def __init__(self, result=None) -> None:
        self.id = "todo"
        self.calls = []
        self.result = result or {"summary": "提醒内容"}

    def execute(self, tool_name, arguments, tenant):
        self.calls.append((tool_name, arguments, tenant))
        return self.result


class FakeMemoryService:
    def __init__(self) -> None:
        self.calls = []

    def recover_dirty(self):
        self.calls.append("recover")
        return {"rebuilt": 1, "failed": 0}

    def run_daily_maintenance(self):
        self.calls.append("daily")
        return {"tenants": 1, "created": 0, "failed": 0}

    def run_weekly_compaction(self):
        self.calls.append("weekly")
        return {"tenants": 1, "failed": 0}


def script_task():
    return ScheduledTask(
        id="script",
        enabled=True,
        cron="0 8 * * *",
        target="last_active_user",
        action=TaskAction(
            type="script",
            script_id="example_check",
            parameters={},
        ),
    )


def plugin_task():
    return ScheduledTask(
        id="plugin",
        enabled=True,
        cron="0 9 * * *",
        target="last_active_user",
        action=TaskAction(
            type="plugin",
            plugin_id="todo",
            tool_name="todo_manage",
            parameters={"action": "remind"},
        ),
    )


class SchedulerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
        self.interaction_time = self.now
        self.registry = TenantRegistry(Path(self.temp.name) / "data")
        self.tenant = self.registry.resolve("bot", "user@im.wechat")
        self.store = TenantRecipientStore(
            self.registry,
            now_provider=lambda: self.interaction_time,
        )
        self.schedules = ScheduleStore(self.registry)
        self.credentials = Credentials("token", "https://gateway", "bot", "owner")
        self.agent = FakeAgentService()
        self.clients = []
        self.logs = []

    def factory(self, _credentials, fail=False):
        client = FakeILink(fail=fail)
        self.clients.append(client)
        return client

    def service(
        self, tasks, factory=None, scheduler=None, image_loader=None, script_service=None,
        plugins=None, memory_service=None,
    ):
        for task in tasks:
            self.schedules.set_enabled(self.tenant.tenant_id, task.id, True)
        return SchedulerService(
            credentials=self.credentials,
            tasks=tasks,
            timezone_name="Asia/Shanghai",
            agent_service=self.agent,
            recipient_store=self.store,
            client_factory=factory or self.factory,
            scheduler=scheduler,
            logger=lambda task_id, status, detail, user: self.logs.append(
                (task_id, status, detail, user)
            ),
            now_provider=lambda: self.now,
            image_loader=image_loader,
            script_service=script_service,
            tenant_registry=self.registry,
            schedule_store=self.schedules,
            plugins=plugins,
            memory_service=memory_service,
        )

    def test_recipient_is_atomic_private_and_reloadable(self) -> None:
        self.store.update(self.tenant, "context")
        recipient = self.store.load(self.tenant.tenant_id)
        self.assertEqual(recipient.user_id, "user@im.wechat")
        self.assertEqual(recipient.context_token, "context")
        self.assertEqual(os.stat(self.registry.database_path).st_mode & 0o777, 0o600)
        self.assertFalse(
            (self.registry.tenant_root(self.tenant.tenant_id) / "recipient.json").exists()
        )

    def test_fixed_and_ai_tasks_send_to_subscribed_tenant(self) -> None:
        self.store.update(self.tenant, "context")
        service = self.service([fixed_task(), ai_task()])

        self.assertTrue(service.run_task(fixed_task()))
        self.assertEqual(self.clients[-1].sent[-1], ("user@im.wechat", "context", "固定提醒"))
        self.assertTrue(self.clients[-1].closed)

        self.assertTrue(service.run_task(ai_task()))
        self.assertEqual(self.agent.calls[-1], ("general", "生成提醒"))
        self.assertEqual(self.clients[-1].sent[-1][2], "AI 定时内容")

    def test_missing_recipient_skips_without_creating_client(self) -> None:
        service = self.service([fixed_task()])
        self.assertFalse(service.run_task(fixed_task()))
        self.assertEqual(self.clients, [])
        self.assertEqual(self.logs[-1][1], "跳过")

    def test_send_failure_is_not_retried(self) -> None:
        self.store.update(self.tenant, "context")
        calls = []

        def failing_factory(credentials):
            calls.append(credentials)
            return self.factory(credentials, fail=True)

        service = self.service([fixed_task()], factory=failing_factory)
        self.assertFalse(service.run_task(fixed_task()))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.logs[-1][1], "失败")

    def test_start_registers_only_enabled_tasks_and_shutdowns(self) -> None:
        scheduler = FakeScheduler()
        service = self.service(
            [fixed_task(enabled=True), ai_task(enabled=False)], scheduler=scheduler
        )
        service.start()
        self.assertTrue(scheduler.started)
        self.assertEqual(len(scheduler.jobs), 1)
        self.assertEqual(scheduler.jobs[0][1]["id"], "fixed")
        self.assertFalse(scheduler.jobs[0][1]["coalesce"])
        self.assertEqual(scheduler.jobs[0][1]["misfire_grace_time"], 1)
        service.shutdown()
        self.assertTrue(scheduler.stopped)

    def test_start_registers_internal_soul_maintenance(self) -> None:
        scheduler = FakeScheduler()
        memory = FakeMemoryService()
        service = self.service(
            [fixed_task(enabled=False)],
            scheduler=scheduler,
            memory_service=memory,
        )
        service.start()
        self.assertEqual(memory.calls, ["recover"])
        self.assertEqual(
            [job[1]["id"] for job in scheduler.jobs],
            ["soul_daily_maintenance", "soul_weekly_compaction"],
        )

    def test_script_schedule_runs_without_recipient_and_submits_parameters(self) -> None:
        scripts = FakeScriptService()
        task = script_task()
        service = self.service([task], script_service=scripts)
        self.assertTrue(service.run_task(task))
        self.assertEqual(
            scripts.calls,
            [(self.tenant, "example_check", {}, "schedule", None)],
        )
        self.assertEqual(self.clients, [])

    def test_plugin_schedule_executes_tool_and_notifies(self) -> None:
        self.store.update(self.tenant, "context")
        plugin = FakePlugin()
        task = plugin_task()
        service = self.service([task], plugins=[plugin])
        self.assertTrue(service.run_task(task))
        self.assertEqual(len(plugin.calls), 1)
        tool_name, arguments, tenant = plugin.calls[0]
        self.assertEqual(tool_name, "todo_manage")
        self.assertEqual(arguments, {"action": "remind"})
        self.assertEqual(tenant, self.tenant)
        self.assertEqual(self.clients[-1].sent[-1], ("user@im.wechat", "context", "提醒内容"))
        self.assertEqual(self.logs[-1][1], "成功")
        self.assertIn("todo", self.logs[-1][2])

    def test_plugin_schedule_without_recipient_skips_notification(self) -> None:
        plugin = FakePlugin()
        task = plugin_task()
        service = self.service([task], plugins=[plugin])
        self.assertTrue(service.run_task(task))
        self.assertEqual(len(plugin.calls), 1)
        self.assertEqual(self.clients, [])

    def test_plugin_schedule_missing_plugin_raises_failure(self) -> None:
        task = plugin_task()
        service = self.service([task], plugins=[])
        self.assertFalse(service.run_task(task))
        self.assertEqual(self.logs[-1][1], "失败")

    def test_due_todo_reminder_is_delivered_once_and_retries_without_recipient(self) -> None:
        plugin = TodoPlugin(
            {}, PluginContext(Path(self.temp.name), self.registry)
        )
        plugin.execute_for_tenant(
            self.tenant.tenant_id, "add", title="到期事项",
            remind_at="2026-07-16T08:01:00+00:00",
            now=datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc),
        )
        service = self.service([fixed_task()], plugins=[plugin])
        self.now = datetime(2026, 7, 16, 8, 1, tzinfo=timezone.utc)
        self.assertFalse(service.run_due_todo_reminders())
        self.assertEqual(self.logs[-1][1], "失败")

        self.store.update(self.tenant, "context")
        self.assertTrue(service.run_due_todo_reminders())
        self.assertEqual(
            self.clients[-1].sent[-1], ("user@im.wechat", "context", "【待办提醒】T0001 到期事项")
        )
        self.assertFalse(service.run_due_todo_reminders())

    def test_one_logical_task_can_register_two_cron_triggers(self) -> None:
        scheduler = FakeScheduler()
        task = replace(
            script_task(),
            cron="50 8 * * *",
            crons=["50 8 * * *", "0 18 * * *"],
        )
        service = self.service([task], scheduler=scheduler, script_service=FakeScriptService())
        service.start()
        self.assertEqual(
            [job[1]["id"] for job in scheduler.jobs],
            ["script#1", "script#2"],
        )
        self.assertEqual(service.enabled_count, 1)
        service.shutdown()

    def test_image_tasks_support_local_path_url_and_caption(self) -> None:
        self.store.update(self.tenant, "context")
        loader = FakeImageLoader()
        service = self.service(
            [image_task("path"), image_task("url", caption=None)],
            image_loader=loader,
        )

        self.assertTrue(service.run_task(image_task("path")))
        self.assertEqual(loader.sources[-1].kind, "path")
        self.assertEqual(
            self.clients[-1].sent[-1],
            ("user@im.wechat", "context", b"validated-image", "状态报告"),
        )
        self.assertEqual(self.logs[-1][2], "[图片] 状态报告")

        self.assertTrue(service.run_task(image_task("url", caption=None)))
        self.assertEqual(loader.sources[-1].kind, "url")
        self.assertEqual(
            self.clients[-1].sent[-1],
            ("user@im.wechat", "context", b"validated-image", ""),
        )
        self.assertEqual(self.logs[-1][2], "[图片]")

    def test_image_source_failure_is_logged_without_creating_wechat_client(self) -> None:
        self.store.update(self.tenant, "context")
        loader = FakeImageLoader(ImageSourceError("读取本地图片失败"))
        service = self.service([image_task()], image_loader=loader)
        self.assertFalse(service.run_task(image_task()))
        self.assertEqual(self.clients, [])
        self.assertEqual(self.logs[-1][1], "失败")
        self.assertNotIn("status.png", self.logs[-1][2])

    def test_inactivity_window_boundaries_and_once_only_claim(self) -> None:
        task = inactivity_task()
        service = self.service([task])

        self.interaction_time = self.now - timedelta(hours=19, minutes=59)
        self.store.update(self.tenant, "context-before")
        self.assertFalse(service.run_task(task))
        self.assertEqual(self.clients, [])

        self.interaction_time = self.now - timedelta(hours=20)
        self.store.update(self.tenant, "context-window")
        self.assertTrue(service.run_task(task))
        self.assertEqual(
            self.clients[-1].sent[-1], ("user@im.wechat", "context-window", "静默提醒")
        )
        client_count = len(self.clients)
        self.assertFalse(service.run_task(task))
        self.assertEqual(len(self.clients), client_count)

        self.interaction_time = self.now - timedelta(hours=24)
        self.store.update(self.tenant, "context-expired")
        self.assertFalse(service.run_task(task))
        self.assertEqual(len(self.clients), client_count)
        self.assertEqual(
            self.store.load(self.tenant.tenant_id).task_attempts[task.id],
            self.store.load(self.tenant.tenant_id).updated_at,
        )

    def test_inactivity_failure_is_claimed_and_not_retried_after_restart(self) -> None:
        task = inactivity_task()
        self.interaction_time = self.now - timedelta(hours=21)
        self.store.update(self.tenant, "context")
        calls = []

        def failing_factory(credentials):
            calls.append(credentials)
            return self.factory(credentials, fail=True)

        self.assertFalse(self.service([task], factory=failing_factory).run_task(task))
        self.assertEqual(len(calls), 1)

        restarted = self.service([task], factory=failing_factory)
        self.assertFalse(restarted.run_task(task))
        self.assertEqual(len(calls), 1)

    def test_new_interaction_resets_inactivity_attempt(self) -> None:
        task = inactivity_task()
        self.interaction_time = self.now - timedelta(hours=20)
        self.store.update(self.tenant, "old-context")
        self.assertTrue(self.service([task]).run_task(task))

        self.interaction_time = self.now
        self.store.update(self.tenant, "new-context")
        self.assertEqual(self.store.load(self.tenant.tenant_id).task_attempts, {})
        self.now += timedelta(hours=20)
        self.assertTrue(self.service([task]).run_task(task))
        self.assertEqual(self.clients[-1].sent[-1][1], "new-context")

    def test_user_change_before_claim_prevents_stale_reminder(self) -> None:
        task = inactivity_task()
        self.interaction_time = self.now - timedelta(hours=21)
        self.store.update(self.tenant, "old-context")
        original_claim = self.store.claim_task_attempt

        def change_user_then_claim(tenant_id, task_id, expected):
            self.interaction_time = self.now
            self.store.update(self.tenant, "new-context")
            return original_claim(tenant_id, task_id, expected)

        self.store.claim_task_attempt = change_user_then_claim
        self.assertFalse(self.service([task]).run_task(task))
        self.assertEqual(self.clients, [])


if __name__ == "__main__":
    unittest.main()
