from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from src.plugins import PluginContext
from src.plugins.base import PluginError
from src.plugins.codex_tasks import (
    CodexTaskStore,
    CodexTasksPlugin,
)
from src.storage.tenants import TenantRegistry


def wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


class FakeNotificationService:
    def __init__(self):
        self.messages = []

    def send_text_to_tenant(self, tenant_id, message):
        self.messages.append((tenant_id, message))


class FlakyNotificationService(FakeNotificationService):
    def __init__(self):
        super().__init__()
        self.failures = 1

    def send_text_to_tenant(self, tenant_id, message):
        if self.failures:
            self.failures -= 1
            raise OSError("temporary delivery failure")
        super().send_text_to_tenant(tenant_id, message)


class FakeHandle:
    def __init__(self, final_response="done", block=False):
        self.final_response = final_response
        self.block = block
        self.running = threading.Event()
        self.release = threading.Event()
        self.interrupted = False

    def run(self):
        self.running.set()
        if self.block:
            self.release.wait(3)
        return SimpleNamespace(
            status="interrupted" if self.interrupted else "completed",
            final_response=self.final_response,
            error=None,
        )

    def interrupt(self):
        self.interrupted = True
        self.release.set()


class FakeThread:
    def __init__(self, thread_id, final_response="done", block=False):
        self.id = thread_id
        self.name = ""
        self.handle = FakeHandle(final_response, block=block)
        self.instructions = []
        self.status_type = "idle"
        self.active_flags = []

    def set_name(self, name):
        self.name = name

    def turn(self, instruction, **_kwargs):
        self.instructions.append(instruction)
        return self.handle

    def read(self, include_turns=False):
        return {
            "thread": {
                "id": self.id,
                "turns": [
                    {
                        "items": [
                            {"type": "agentMessage", "text": "existing result"}
                        ]
                    }
                ] if include_turns else [],
            }
        }


class FakeCodex:
    def __init__(self, *, block=False):
        self.block = block
        self.threads = {}
        self.counter = 0
        self.closed = False
        self.last_thread_list_kwargs = None
        self.external = FakeThread("external-thread", final_response="external")
        self.threads[self.external.id] = self.external

    def thread_start(self, **_kwargs):
        self.counter += 1
        thread = FakeThread(
            "thread-{}".format(self.counter),
            final_response="implemented",
            block=self.block,
        )
        self.threads[thread.id] = thread
        return thread

    def thread_resume(self, thread_id, **_kwargs):
        return self.threads[thread_id]

    def thread_list(self, **kwargs):
        self.last_thread_list_kwargs = kwargs
        data = []
        for thread in self.threads.values():
            data.append(
                SimpleNamespace(
                    id=thread.id,
                    name=thread.name or "Existing task",
                    preview="preview",
                    status=SimpleNamespace(
                        type=thread.status_type,
                        active_flags=list(thread.active_flags),
                    ),
                    created_at=100,
                    updated_at=200,
                )
            )
        return SimpleNamespace(data=data, next_cursor=None)

    def close(self):
        self.closed = True


class CodexTasksPluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = TenantRegistry(self.root / "data")
        self.admin = self.registry.resolve("bot", "admin")
        self.other = self.registry.resolve("bot", "other")
        self.notifications = FakeNotificationService()

    def settings(self):
        return {
            "admin_tenant_ids": [self.admin.tenant_id],
            "projects": [{"id": "botplatform", "path": "."}],
            "default_project": "botplatform",
            "max_concurrent_tasks": 1,
            "notify_on_completion": True,
            "monitor_external_tasks": False,
            "notify_events": [
                "waiting_approval",
                "waiting_input",
                "completed",
                "failed",
                "interrupted",
            ],
        }

    def plugin(self, fake=None):
        client = fake or FakeCodex()
        plugin = CodexTasksPlugin(
            self.settings(),
            context=PluginContext(
                project_root=self.root,
                tenant_registry=self.registry,
                notification_service=self.notifications,
            ),
            client_factory=lambda: client,
        )
        self.addCleanup(plugin.close)
        return plugin, client

    def test_settings_and_tool_approval_contract(self):
        plugin, _client = self.plugin()
        self.assertTrue(plugin.is_available("codex_list_tasks"))
        self.assertFalse(
            plugin.tool_definitions["codex_list_tasks"].requires_approval
        )
        for name in (
            "codex_create_task",
            "codex_continue_task",
            "codex_cancel_task",
        ):
            self.assertTrue(plugin.tool_definitions[name].requires_approval)

        invalid = self.settings()
        invalid["default_project"] = "missing"
        with self.assertRaisesRegex(ValueError, "default_project"):
            CodexTasksPlugin.validate_settings(invalid)

        invalid = self.settings()
        invalid["admin_tenant_ids"] = [self.admin.tenant_id, self.other.tenant_id]
        invalid["monitor_external_tasks"] = True
        with self.assertRaisesRegex(ValueError, "monitor_tenant_id"):
            CodexTasksPlugin.validate_settings(invalid)

    def test_non_admin_is_denied_and_empty_allowlist_disables_tools(self):
        plugin, _client = self.plugin()
        with self.assertRaisesRegex(PluginError, "无权"):
            plugin.execute("codex_list_tasks", {}, self.other)

        settings = self.settings()
        settings["admin_tenant_ids"] = []
        disabled = CodexTasksPlugin(
            settings,
            context=PluginContext(self.root, self.registry, self.notifications),
            client_factory=lambda: FakeCodex(),
        )
        self.addCleanup(disabled.close)
        self.assertFalse(disabled.is_available("codex_list_tasks"))

    def test_create_runs_in_background_persists_and_notifies_once(self):
        plugin, client = self.plugin()
        created = plugin.execute(
            "codex_create_task",
            {"title": "Implement feature", "instruction": "Make the change"},
            self.admin,
        )
        self.assertEqual(created["task_id"], "thread-1")
        task = wait_for(
            lambda: plugin.service.store.get("thread-1")
            if (
                plugin.service.store.get("thread-1")["status"] == "completed"
                and plugin.service.store.get("thread-1")["notification_status"] == "sent"
            )
            else None
        )
        self.assertEqual(task["result_excerpt"], "implemented")
        self.assertEqual(task["notification_status"], "sent")
        self.assertEqual(client.threads["thread-1"].instructions, ["Make the change"])
        self.assertEqual(len(self.notifications.messages), 1)

        plugin.service._notify("thread-1")
        self.assertEqual(len(self.notifications.messages), 1)

    def test_full_lifecycle_notifies_queued_running_and_terminal_once(self):
        settings = self.settings()
        settings["notify_events"] = [
            "queued",
            "running",
            "waiting_approval",
            "waiting_input",
            "completed",
            "failed",
            "interrupted",
        ]
        plugin = CodexTasksPlugin(
            settings,
            context=PluginContext(
                project_root=self.root,
                tenant_registry=self.registry,
                notification_service=self.notifications,
            ),
            client_factory=lambda: FakeCodex(),
        )
        self.addCleanup(plugin.close)
        created = plugin.execute(
            "codex_create_task",
            {"title": "Lifecycle", "instruction": "Complete"},
            self.admin,
        )
        wait_for(
            lambda: plugin.service.store.get(created["task_id"])["status"]
            == "completed"
        )
        wait_for(lambda: len(self.notifications.messages) == 3)
        messages = [message for _, message in self.notifications.messages]
        self.assertIn("已排队", messages[0])
        self.assertIn("开始执行", messages[1])
        self.assertIn("已完成", messages[2])
        plugin.service._notify(created["task_id"])
        self.assertEqual(len(self.notifications.messages), 3)

    def test_active_list_and_cancel_interrupt_running_turn(self):
        plugin, client = self.plugin(FakeCodex(block=True))
        created = plugin.execute(
            "codex_create_task",
            {"title": "Long task", "instruction": "Keep working"},
            self.admin,
        )
        thread_id = created["task_id"]
        wait_for(lambda: client.threads[thread_id].handle.running.is_set())

        active = plugin.execute(
            "codex_list_tasks", {"status": "active", "limit": 10}, self.admin
        )
        self.assertEqual(active["tasks"][0]["task_id"], thread_id)
        cancelled = plugin.execute(
            "codex_cancel_task", {"task_id": thread_id}, self.admin
        )
        self.assertIn(cancelled["status"], {"running", "interrupted"})
        task = wait_for(
            lambda: plugin.service.store.get(thread_id)
            if plugin.service.store.get(thread_id)["status"] == "interrupted"
            else None
        )
        self.assertEqual(task["status"], "interrupted")

    def test_external_history_can_be_read_and_adopted_on_continue(self):
        plugin, client = self.plugin()
        listed = plugin.execute(
            "codex_list_tasks", {"status": "all", "limit": 10}, self.admin
        )
        self.assertTrue(client.last_thread_list_kwargs["use_state_db_only"])
        self.assertIn("external-thread", [item["task_id"] for item in listed["tasks"]])
        completed = plugin.execute(
            "codex_list_tasks", {"status": "completed", "limit": 10}, self.admin
        )
        self.assertIn("external-thread", [item["task_id"] for item in completed["tasks"]])
        detail = plugin.execute(
            "codex_get_task", {"task_id": "external-thread"}, self.admin
        )
        self.assertEqual(detail["result"], "existing result")

        continued = plugin.execute(
            "codex_continue_task",
            {"task_id": "external-thread", "instruction": "Continue it"},
            self.admin,
        )
        self.assertEqual(continued["task_id"], "external-thread")
        wait_for(
            lambda: plugin.service.store.get("external-thread")
            if plugin.service.store.get("external-thread")["status"] == "completed"
            else None
        )
        self.assertEqual(client.external.instructions, ["Continue it"])

    def test_restart_reconciles_running_rows(self):
        store = CodexTaskStore(self.registry)
        store.create(
            "stale-thread",
            self.admin.tenant_id,
            "botplatform",
            "Stale",
            notify=False,
        )
        store.mark_running("stale-thread")
        plugin, _client = self.plugin()
        task = plugin.service.store.get("stale-thread")
        self.assertEqual(task["status"], "interrupted")
        self.assertIn("重启", task["error"])

    def test_command_approval_is_notified_resolved_once_and_resumes(self):
        plugin, client = self.plugin(FakeCodex(block=True))
        created = plugin.execute(
            "codex_create_task",
            {"title": "Protected task", "instruction": "Run protected command"},
            self.admin,
        )
        thread_id = created["task_id"]
        wait_for(lambda: client.threads[thread_id].handle.running.is_set())
        result = {}

        def request():
            result["response"] = plugin.service._handle_server_request(
                "item/commandExecution/requestApproval",
                {
                    "threadId": thread_id,
                    "turnId": "turn-1",
                    "itemId": "item-1",
                    "command": "python -m unittest",
                    "reason": "运行测试",
                },
            )

        worker = threading.Thread(target=request)
        worker.start()
        interaction = wait_for(
            lambda: plugin.service.store.pending_interaction(thread_id)
        )
        code = interaction["interaction_id"]
        self.assertEqual(
            plugin.service.store.get(thread_id)["phase"], "waiting_approval"
        )
        detail = plugin.execute(
            "codex_get_task", {"task_id": thread_id}, self.admin
        )
        self.assertEqual(detail["origin"], "botplatform")
        self.assertEqual(detail["phase"], "waiting_approval")
        self.assertIn("python -m unittest", detail["pending_request"]["summary"])
        notification = wait_for(
            lambda: self.notifications.messages[-1][1]
            if self.notifications.messages
            else None
        )
        self.assertIn("/codex approve {}".format(code), notification)

        reply = plugin.resolve_wechat_command(
            self.admin, "/codex approve {}".format(code)
        )
        self.assertIn("已批准", reply)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result["response"], {"decision": "accept"})
        self.assertEqual(plugin.service.store.get(thread_id)["phase"], "running")
        with self.assertRaisesRegex(PluginError, "已经处理"):
            plugin.resolve_wechat_command(
                self.admin, "/codex approve {}".format(code)
            )

        client.threads[thread_id].handle.release.set()
        wait_for(
            lambda: plugin.service.store.get(thread_id)["status"] == "completed"
        )

    def test_duplicate_approval_callbacks_share_one_waiter_and_notification(self):
        plugin, client = self.plugin(FakeCodex(block=True))
        created = plugin.execute(
            "codex_create_task",
            {"title": "Duplicate approval", "instruction": "Run command"},
            self.admin,
        )
        thread_id = created["task_id"]
        wait_for(lambda: client.threads[thread_id].handle.running.is_set())
        payload = {
            "threadId": thread_id,
            "turnId": "turn-duplicate",
            "itemId": "item-duplicate",
            "command": "python -m unittest",
        }
        responses = []

        def request():
            responses.append(
                plugin.service._handle_server_request(
                    "item/commandExecution/requestApproval", payload
                )
            )

        workers = [threading.Thread(target=request) for _ in range(2)]
        for worker in workers:
            worker.start()
        interaction = wait_for(
            lambda: plugin.service.store.pending_interaction(thread_id)
        )
        code = interaction["interaction_id"]
        wait_for(lambda: len(self.notifications.messages) == 1)
        plugin.resolve_wechat_command(
            self.admin, "/codex approve {}".format(code)
        )
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(responses, [{"decision": "accept"}] * 2)
        self.assertEqual(len(self.notifications.messages), 1)
        client.threads[thread_id].handle.release.set()

    def test_two_tasks_wait_for_independent_approvals(self):
        settings = self.settings()
        settings["max_concurrent_tasks"] = 2
        clients = []

        def factory():
            client = FakeCodex(block=True)
            client.counter = len(clients) * 100
            clients.append(client)
            return client

        plugin = CodexTasksPlugin(
            settings,
            context=PluginContext(
                project_root=self.root,
                tenant_registry=self.registry,
                notification_service=self.notifications,
            ),
            client_factory=factory,
        )
        self.addCleanup(plugin.close)
        first = plugin.execute(
            "codex_create_task",
            {"title": "First", "instruction": "First command"},
            self.admin,
        )
        second = plugin.execute(
            "codex_create_task",
            {"title": "Second", "instruction": "Second command"},
            self.admin,
        )
        self.assertEqual(len(clients), 2)
        self.assertIsNot(clients[0], clients[1])
        wait_for(lambda: clients[0].threads[first["task_id"]].handle.running.is_set())
        wait_for(lambda: clients[1].threads[second["task_id"]].handle.running.is_set())
        responses = {}

        def request(task_id, suffix):
            responses[task_id] = plugin.service._handle_server_request(
                "item/commandExecution/requestApproval",
                {
                    "threadId": task_id,
                    "turnId": "turn-{}".format(suffix),
                    "itemId": "item-{}".format(suffix),
                    "command": "command-{}".format(suffix),
                },
            )

        workers = [
            threading.Thread(target=request, args=(first["task_id"], "first")),
            threading.Thread(target=request, args=(second["task_id"], "second")),
        ]
        for worker in workers:
            worker.start()
        first_pending = wait_for(
            lambda: plugin.service.store.pending_interaction(first["task_id"])
        )
        second_pending = wait_for(
            lambda: plugin.service.store.pending_interaction(second["task_id"])
        )
        plugin.resolve_wechat_command(
            self.admin,
            "/codex approve {}".format(first_pending["interaction_id"]),
        )
        plugin.resolve_wechat_command(
            self.admin,
            "/codex deny {}".format(second_pending["interaction_id"]),
        )
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(responses[first["task_id"]], {"decision": "accept"})
        self.assertEqual(responses[second["task_id"]], {"decision": "decline"})
        clients[0].threads[first["task_id"]].handle.release.set()
        clients[1].threads[second["task_id"]].handle.release.set()

    def test_interaction_tenant_expiry_and_input_timeout_are_fail_closed(self):
        plugin, client = self.plugin(FakeCodex(block=True))
        plugin.service.config = replace(
            plugin.service.config, interaction_ttl_seconds=0.05
        )
        created = plugin.execute(
            "codex_create_task",
            {"title": "Timeout", "instruction": "Ask required question"},
            self.admin,
        )
        thread_id = created["task_id"]
        wait_for(lambda: client.threads[thread_id].handle.running.is_set())
        result = {}

        def request():
            result["response"] = plugin.service._handle_server_request(
                "item/tool/requestUserInput",
                {
                    "threadId": thread_id,
                    "turnId": "turn-timeout",
                    "itemId": "item-timeout",
                    "questions": [
                        {"id": "required", "question": "Required answer"}
                    ],
                },
            )

        worker = threading.Thread(target=request)
        worker.start()
        pending = wait_for(
            lambda: plugin.service.store.pending_interaction(thread_id)
        )
        with self.assertRaisesRegex(PluginError, "不存在或不属于"):
            plugin.service.resolve_interaction(
                self.other.tenant_id,
                pending["interaction_id"],
                "answer",
                "must-not-apply",
            )
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result["response"], {"answers": {}})
        interaction = plugin.service.store.get_interaction(
            pending["interaction_id"]
        )
        self.assertEqual(interaction["status"], "expired")
        wait_for(
            lambda: plugin.service.store.get(thread_id)["status"] == "interrupted"
        )

    def test_user_input_answer_supports_multiple_questions(self):
        plugin, client = self.plugin(FakeCodex(block=True))
        created = plugin.execute(
            "codex_create_task",
            {"title": "Question task", "instruction": "Ask questions"},
            self.admin,
        )
        thread_id = created["task_id"]
        wait_for(lambda: client.threads[thread_id].handle.running.is_set())
        result = {}

        def request():
            result["response"] = plugin.service._handle_server_request(
                "item/tool/requestUserInput",
                {
                    "threadId": thread_id,
                    "turnId": "turn-2",
                    "itemId": "item-2",
                    "questions": [
                        {
                            "id": "framework",
                            "header": "框架",
                            "question": "选择框架",
                            "options": [
                                {"label": "FastAPI", "description": "Python"},
                                {"label": "Flask", "description": "Python"},
                            ],
                        },
                        {
                            "id": "name",
                            "header": "名称",
                            "question": "输入名称",
                            "isOther": True,
                        },
                    ],
                },
            )

        worker = threading.Thread(target=request)
        worker.start()
        interaction = wait_for(
            lambda: plugin.service.store.pending_interaction(thread_id)
        )
        code = interaction["interaction_id"]
        reply = plugin.resolve_wechat_command(
            self.admin,
            "/codex answer {} 1=1;2=demo".format(code),
        )
        self.assertIn("已提交答案", reply)
        worker.join(2)
        self.assertEqual(
            result["response"],
            {
                "answers": {
                    "framework": {"answers": ["FastAPI"]},
                    "name": {"answers": ["demo"]},
                }
            },
        )
        client.threads[thread_id].handle.release.set()

    def test_secret_user_input_is_delivered_but_not_persisted(self):
        plugin, client = self.plugin(FakeCodex(block=True))
        created = plugin.execute(
            "codex_create_task",
            {"title": "Secret question", "instruction": "Ask for token"},
            self.admin,
        )
        thread_id = created["task_id"]
        wait_for(lambda: client.threads[thread_id].handle.running.is_set())
        result = {}

        def request():
            result["response"] = plugin.service._handle_server_request(
                "item/tool/requestUserInput",
                {
                    "threadId": thread_id,
                    "turnId": "turn-secret",
                    "itemId": "item-secret",
                    "questions": [
                        {
                            "id": "token",
                            "header": "令牌",
                            "question": "输入一次性令牌",
                            "isSecret": True,
                        }
                    ],
                },
            )

        worker = threading.Thread(target=request)
        worker.start()
        interaction = wait_for(
            lambda: plugin.service.store.pending_interaction(thread_id)
        )
        code = interaction["interaction_id"]
        plugin.resolve_wechat_command(
            self.admin, "/codex answer {} highly-secret".format(code)
        )
        worker.join(2)
        self.assertEqual(
            result["response"],
            {"answers": {"token": {"answers": ["highly-secret"]}}},
        )
        stored = plugin.service.store.get_interaction(code)
        self.assertEqual(stored["status"], "answered")
        self.assertIsNone(stored["response_json"])
        client.threads[thread_id].handle.release.set()

    def test_external_waiting_transition_notifies_but_cannot_be_resolved(self):
        fake = FakeCodex()
        settings = self.settings()
        settings["monitor_external_tasks"] = True
        settings["external_poll_interval_seconds"] = 300
        plugin = CodexTasksPlugin(
            settings,
            context=PluginContext(
                project_root=self.root,
                tenant_registry=self.registry,
                notification_service=self.notifications,
            ),
            client_factory=lambda: fake,
        )
        self.addCleanup(plugin.close)
        wait_for(lambda: plugin.service._external_baseline_done)
        fake.external.status_type = "active"
        fake.external.active_flags = ["waitingOnApproval"]
        plugin.service._reconcile_external_tasks()
        wait_for(
            lambda: any("请回原 Codex 端处理" in message for _, message in self.notifications.messages)
        )
        task = plugin.service.store.get("external-thread")
        self.assertEqual(task["origin"], "external")
        self.assertEqual(task["phase"], "waiting_approval")

    def test_new_external_task_notifies_running_then_completed(self):
        fake = FakeCodex()
        settings = self.settings()
        settings["monitor_external_tasks"] = True
        settings["external_poll_interval_seconds"] = 300
        settings["notify_events"] = ["running", "completed"]
        plugin = CodexTasksPlugin(
            settings,
            context=PluginContext(
                project_root=self.root,
                tenant_registry=self.registry,
                notification_service=self.notifications,
            ),
            client_factory=lambda: fake,
        )
        self.addCleanup(plugin.close)
        wait_for(lambda: plugin.service._external_baseline_done)
        new_thread = FakeThread("external-new")
        new_thread.status_type = "active"
        fake.threads[new_thread.id] = new_thread
        plugin.service._reconcile_external_tasks()
        wait_for(lambda: len(self.notifications.messages) == 1)
        self.assertIn("开始执行", self.notifications.messages[0][1])
        new_thread.status_type = "idle"
        plugin.service._reconcile_external_tasks()
        wait_for(lambda: len(self.notifications.messages) == 2)
        self.assertIn("已完成", self.notifications.messages[1][1])

    def test_terminal_notification_retries_and_updates_legacy_status(self):
        flaky = FlakyNotificationService()
        plugin = CodexTasksPlugin(
            self.settings(),
            context=PluginContext(
                project_root=self.root,
                tenant_registry=self.registry,
                notification_service=flaky,
            ),
            client_factory=lambda: FakeCodex(),
        )
        self.addCleanup(plugin.close)
        created = plugin.execute(
            "codex_create_task",
            {"title": "Retry task", "instruction": "Complete"},
            self.admin,
        )
        thread_id = created["task_id"]
        wait_for(lambda: plugin.service.store.get(thread_id)["status"] == "completed")
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE codex_task_events SET next_attempt_at=? "
                "WHERE thread_id=? AND delivery_status='retry'",
                ("2000-01-01T00:00:00+00:00", thread_id),
            )
        due = plugin.service.store.due_events()
        if due:
            plugin.service._deliver_event(due[0])
        wait_for(
            lambda: plugin.service.store.get(thread_id)["notification_status"]
            == "sent"
        )
        self.assertEqual(len(flaky.messages), 1)

    def test_pinned_protocol_response_shapes_are_fail_closed(self):
        service = self.plugin()[0].service
        payload = {"permissions": {"network": {"enabled": True}}}
        self.assertEqual(
            service._request_response(
                "item/commandExecution/requestApproval", {}, "approve"
            ),
            {"decision": "accept"},
        )
        self.assertEqual(
            service._request_response(
                "item/fileChange/requestApproval", {}, "deny"
            ),
            {"decision": "decline"},
        )
        self.assertEqual(
            service._request_response(
                "item/permissions/requestApproval", payload, "deny"
            ),
            {"permissions": {}, "scope": "turn"},
        )
        self.assertEqual(
            service._safe_unknown_response("item/tool/requestUserInput"),
            {"answers": {}},
        )


if __name__ == "__main__":
    unittest.main()
