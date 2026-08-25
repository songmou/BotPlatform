from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.core.plugins import PluginContext
from src.core.plugins.base import PluginError
from src.core.plugins.codex_tasks import (
    CodexTaskStore,
    CodexTasksPlugin,
)
from src.core.storage.tenants import TenantRegistry


def wait_for(predicate, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("等待 Codex 插件状态超时")


class FakeHandle:
    def __init__(self, response: str, block: bool = False) -> None:
        self.response = response
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
            final_response=self.response,
            error=None,
        )

    def interrupt(self):
        self.interrupted = True
        self.release.set()


class FakeThread:
    def __init__(self, task_id: str, response: str, block: bool = False) -> None:
        self.id = task_id
        self.name = ""
        self.instructions = []
        self.handle = FakeHandle(response, block)

    def set_name(self, name: str) -> None:
        self.name = name

    def turn(self, instruction: str, **kwargs):
        self.instructions.append((instruction, kwargs))
        return self.handle


class FakeSession:
    def __init__(self, owner: "FakeSessionFactory") -> None:
        self.owner = owner
        self.closed = False

    def thread_start(self, *, cwd: str):
        self.owner.counter += 1
        task_id = "task-{}".format(self.owner.counter)
        thread = FakeThread(
            task_id,
            self.owner.responses.pop(0) if self.owner.responses else "完成",
            self.owner.block,
        )
        self.owner.threads[task_id] = thread
        self.owner.start_paths.append(cwd)
        return thread

    def thread_resume(self, task_id: str, *, cwd: str):
        thread = FakeThread(
            task_id,
            self.owner.responses.pop(0) if self.owner.responses else "继续完成",
            self.owner.block,
        )
        self.owner.threads[task_id] = thread
        self.owner.resume_paths.append(cwd)
        return thread

    def close(self):
        self.closed = True


class FakeSessionFactory:
    def __init__(self, responses=None, block: bool = False) -> None:
        self.counter = 0
        self.responses = list(responses or [])
        self.block = block
        self.threads = {}
        self.start_paths = []
        self.resume_paths = []

    def __call__(self):
        return FakeSession(self)


class CodexTasksPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.registry = TenantRegistry(self.root / "data")
        self.tenant_a = self.registry.resolve("bot", "alice")
        self.tenant_b = self.registry.resolve("bot", "bob")
        self.context = PluginContext(
            project_root=self.root,
            tenant_registry=self.registry,
            data_root=self.root / "plugin-data",
        )

    def plugin(self, factory: FakeSessionFactory) -> CodexTasksPlugin:
        plugin = CodexTasksPlugin(
            {
                "allowed_tenant_ids": [self.tenant_a.tenant_id],
                "projects": [{"id": "app", "path": str(self.project)}],
                "default_project": "app",
                "max_concurrent_tasks": 1,
            },
            context=self.context,
            client_factory=factory,
        )
        self.addCleanup(plugin.close)
        return plugin

    def test_five_tools_and_outer_approval_policy(self) -> None:
        definitions = CodexTasksPlugin.TOOL_DEFINITIONS
        self.assertEqual(
            set(definitions),
            {
                "codex_list_tasks",
                "codex_get_task",
                "codex_create_task",
                "codex_continue_task",
                "codex_cancel_task",
            },
        )
        for name in (
            "codex_create_task",
            "codex_continue_task",
            "codex_cancel_task",
        ):
            self.assertEqual(definitions[name].approval_policy, "required")
        self.assertEqual(definitions["codex_list_tasks"].approval_policy, "none")
        self.assertEqual(definitions["codex_get_task"].approval_policy, "none")

    def test_create_list_get_and_continue_are_tenant_scoped(self) -> None:
        factory = FakeSessionFactory(["首次结果", "继续结果"])
        plugin = self.plugin(factory)
        created = plugin.execute(
            "codex_create_task",
            {"title": "修复问题", "instruction": "执行修改"},
            self.tenant_a,
        )
        task_id = created["task_id"]
        completed = wait_for(
            lambda: (
                item
                if (item := plugin.execute(
                    "codex_get_task", {"task_id": task_id}, self.tenant_a
                ))["status"] == "completed"
                else None
            )
        )
        self.assertEqual(completed["result"], "首次结果")
        self.assertEqual(
            plugin.execute("codex_list_tasks", {}, self.tenant_a)["tasks"][0][
                "task_id"
            ],
            task_id,
        )
        continued = plugin.execute(
            "codex_continue_task",
            {"task_id": task_id, "instruction": "再处理一次"},
            self.tenant_a,
        )
        self.assertIn(continued["status"], {"queued", "running"})
        wait_for(
            lambda: plugin.execute(
                "codex_get_task", {"task_id": task_id}, self.tenant_a
            )["status"]
            == "completed"
        )
        with self.assertRaises(PluginError):
            plugin.execute(
                "codex_get_task", {"task_id": task_id}, self.tenant_b
            )

    def test_project_whitelist_and_permissions_are_enforced(self) -> None:
        plugin = self.plugin(FakeSessionFactory())
        with self.assertRaisesRegex(PluginError, "未知或未开放"):
            plugin.execute(
                "codex_create_task",
                {
                    "title": "越界",
                    "instruction": "执行",
                    "project_id": "other",
                },
                self.tenant_a,
            )
        with self.assertRaisesRegex(PluginError, "无权访问"):
            plugin.execute("codex_list_tasks", {}, self.tenant_b)

    def test_cancel_interrupts_running_task(self) -> None:
        factory = FakeSessionFactory(block=True)
        plugin = self.plugin(factory)
        task = plugin.execute(
            "codex_create_task",
            {"title": "长任务", "instruction": "等待"},
            self.tenant_a,
        )
        handle = factory.threads[task["task_id"]].handle
        self.assertTrue(handle.running.wait(2))
        plugin.execute(
            "codex_cancel_task", {"task_id": task["task_id"]}, self.tenant_a
        )
        interrupted = wait_for(
            lambda: (
                item
                if (item := plugin.execute(
                    "codex_get_task",
                    {"task_id": task["task_id"]},
                    self.tenant_a,
                ))["status"]
                == "interrupted"
                else None
            )
        )
        self.assertEqual(interrupted["status"], "interrupted")

    def test_concurrency_limit_releases_after_task_finishes(self) -> None:
        factory = FakeSessionFactory(block=True)
        plugin = self.plugin(factory)
        first = plugin.execute(
            "codex_create_task",
            {"title": "第一个任务", "instruction": "等待"},
            self.tenant_a,
        )
        first_handle = factory.threads[first["task_id"]].handle
        self.assertTrue(first_handle.running.wait(2))
        with self.assertRaisesRegex(PluginError, "并发上限"):
            plugin.execute(
                "codex_create_task",
                {"title": "第二个任务", "instruction": "执行"},
                self.tenant_a,
            )
        first_handle.release.set()
        wait_for(
            lambda: plugin.execute(
                "codex_get_task",
                {"task_id": first["task_id"]},
                self.tenant_a,
            )["status"]
            == "completed"
        )
        second = plugin.execute(
            "codex_create_task",
            {"title": "第二个任务", "instruction": "执行"},
            self.tenant_a,
        )
        factory.threads[second["task_id"]].handle.release.set()
        wait_for(
            lambda: plugin.execute(
                "codex_get_task",
                {"task_id": second["task_id"]},
                self.tenant_a,
            )["status"]
            == "completed"
        )

    def test_internal_input_or_approval_fails_closed(self) -> None:
        factory = FakeSessionFactory(block=True)
        plugin = self.plugin(factory)
        task = plugin.execute(
            "codex_create_task",
            {"title": "需交互", "instruction": "请求用户输入"},
            self.tenant_a,
        )
        handle = factory.threads[task["task_id"]].handle
        self.assertTrue(handle.running.wait(2))
        response = plugin.service._handle_server_request(
            "item/tool/requestUserInput", {"threadId": task["task_id"]}
        )
        self.assertEqual(response, {"answers": {}})
        failed = wait_for(
            lambda: (
                item
                if (item := plugin.execute(
                    "codex_get_task",
                    {"task_id": task["task_id"]},
                    self.tenant_a,
                ))["status"]
                == "failed"
                else None
            )
        )
        self.assertIn("安全拒绝", failed["error"])

    def test_store_reconciles_only_once_per_service_and_on_restart(self) -> None:
        path = self.root / "tasks.sqlite3"
        store = CodexTaskStore(path)
        store.create("task-x", "app", "测试")
        store.mark_running("task-x")
        self.assertEqual(store.get("task-x")["status"], "running")
        restarted = CodexTaskStore(path)
        self.assertEqual(restarted.get("task-x")["status"], "interrupted")

    def test_availability_reflects_optional_dependency(self) -> None:
        plugin = self.plugin(FakeSessionFactory())
        with patch(
            "src.core.plugins.codex_tasks.importlib.util.find_spec",
            return_value=object(),
        ):
            self.assertTrue(
                plugin.is_available("codex_list_tasks", self.tenant_a)
            )
            self.assertFalse(
                plugin.is_available("codex_list_tasks", self.tenant_b)
            )
        with patch(
            "src.core.plugins.codex_tasks.importlib.util.find_spec",
            return_value=None,
        ):
            self.assertFalse(
                plugin.is_available("codex_list_tasks", self.tenant_a)
            )


if __name__ == "__main__":
    unittest.main()
