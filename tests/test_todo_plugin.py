"""Tests for the TodoPlugin platform plugin interface."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.plugins.base import PluginContext, PluginError
from src.core.plugins.todo import TodoPlugin, TodoError, OperationResult
from src.core.storage.tenants import TenantRegistry


@dataclass
class FakeTenant:
    tenant_id: str


def moment(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


class TodoPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = TenantRegistry(Path(self.temp.name) / "data")
        self.tenant = self.registry.resolve("bot", "plugin-user")
        self.context = PluginContext(
            project_root=Path(self.temp.name),
            tenant_registry=self.registry,
        )
        self.plugin = TodoPlugin({}, context=self.context)

    def test_plugin_id_and_tool_definitions(self) -> None:
        self.assertEqual(self.plugin.id, "todo")
        self.assertIn("todo_manage", self.plugin.tool_definitions)
        definition = self.plugin.tool_definitions["todo_manage"]
        self.assertFalse(definition.requires_approval)
        self.assertTrue(self.plugin.is_available("todo_manage"))
        self.assertFalse(self.plugin.is_available("nonexistent"))

    def test_add_and_list(self) -> None:
        result = self.plugin.execute(
            "todo_manage", {"action": "add", "title": "测试待办"}, self.tenant
        )
        self.assertEqual(result["status"], "success")
        self.assertIn("T0001", result["summary"])
        self.assertIn("测试待办", result["summary"])

        listed = self.plugin.execute(
            "todo_manage", {"action": "list"}, self.tenant
        )
        self.assertEqual(listed["status"], "success")
        self.assertIn("T0001", listed["summary"])

    def test_complete_and_reopen(self) -> None:
        self.plugin.execute(
            "todo_manage", {"action": "add", "title": "完成测试"}, self.tenant
        )
        completed = self.plugin.execute(
            "todo_manage", {"action": "complete", "todo_id": "T0001"}, self.tenant
        )
        self.assertEqual(completed["status"], "success")

        pending = self.plugin.execute(
            "todo_manage", {"action": "list", "scope": "pending"}, self.tenant
        )
        self.assertNotIn("T0001", pending["summary"])

        reopened = self.plugin.execute(
            "todo_manage", {"action": "reopen", "todo_id": "T0001"}, self.tenant
        )
        self.assertEqual(reopened["status"], "success")

    def test_edit(self) -> None:
        self.plugin.execute(
            "todo_manage", {"action": "add", "title": "原始内容"}, self.tenant
        )
        edited = self.plugin.execute(
            "todo_manage",
            {"action": "edit", "todo_id": "T0001", "title": "修改后内容"},
            self.tenant,
        )
        self.assertEqual(edited["status"], "success")
        self.assertIn("修改后内容", edited["summary"])

    def test_remind(self) -> None:
        self.plugin.execute(
            "todo_manage", {"action": "add", "title": "提醒事项"}, self.tenant
        )
        reminded = self.plugin.execute(
            "todo_manage", {"action": "remind"}, self.tenant
        )
        self.assertEqual(reminded["status"], "success")
        self.assertIn("提醒事项", reminded["summary"])

    def test_empty_remind(self) -> None:
        reminded = self.plugin.execute(
            "todo_manage", {"action": "remind"}, self.tenant
        )
        self.assertIn("已清空", reminded["summary"])

    def test_relative_reminder_can_be_set_and_cleared(self) -> None:
        created = self.plugin.execute(
            "todo_manage",
            {"action": "add", "title": "五分钟提醒", "remind_at": "5分钟后"},
            self.tenant,
        )
        self.assertIn("提醒时间", created["summary"])
        listed = self.plugin.execute("todo_manage", {"action": "list"}, self.tenant)
        self.assertIn("提醒：", listed["summary"])
        cleared = self.plugin.execute(
            "todo_manage", {"action": "edit", "todo_id": "T0001", "remind_at": None},
            self.tenant,
        )
        self.assertIn("已清除提醒", cleared["summary"])

    def test_invalid_action_raises_plugin_error(self) -> None:
        with self.assertRaises(PluginError):
            self.plugin.execute(
                "todo_manage", {"action": "delete"}, self.tenant
            )

    def test_unknown_tool_raises_plugin_error(self) -> None:
        with self.assertRaises(PluginError):
            self.plugin.execute(
                "todo_unknown", {"action": "list"}, self.tenant
            )

    def test_todo_error_wrapped_as_plugin_error(self) -> None:
        with self.assertRaises(PluginError) as ctx:
            self.plugin.execute(
                "todo_manage", {"action": "add", "title": "  "}, self.tenant
            )
        self.assertIn("不能为空", str(ctx.exception))

    def test_tenant_isolation(self) -> None:
        other_tenant = self.registry.resolve("bot", "other-user")
        self.plugin.execute(
            "todo_manage", {"action": "add", "title": "用户A事项"}, self.tenant
        )
        self.plugin.execute(
            "todo_manage", {"action": "add", "title": "用户B事项"}, other_tenant
        )
        result_a = self.plugin.execute(
            "todo_manage", {"action": "list"}, self.tenant
        )
        result_b = self.plugin.execute(
            "todo_manage", {"action": "list"}, other_tenant
        )
        self.assertIn("用户A事项", result_a["summary"])
        self.assertNotIn("用户B事项", result_a["summary"])
        self.assertIn("用户B事项", result_b["summary"])
        self.assertNotIn("用户A事项", result_b["summary"])

    def test_execute_for_tenant_direct(self) -> None:
        result = self.plugin.execute_for_tenant(
            self.tenant.tenant_id, "add", title="直接调用"
        )
        self.assertIsInstance(result, OperationResult)
        self.assertEqual(result.status, "success")
        self.assertIn("直接调用", result.summary)

    def test_validate_settings_rejects_unknown(self) -> None:
        with self.assertRaises(ValueError):
            TodoPlugin.validate_settings({"unknown_key": True})

    def test_validate_settings_accepts_empty(self) -> None:
        TodoPlugin.validate_settings({})

    def test_missing_context_raises(self) -> None:
        with self.assertRaises(ValueError):
            TodoPlugin({}, context=None)

    def test_close_tenant_and_close_are_noop(self) -> None:
        self.plugin.close_tenant("any-id")
        self.plugin.close()


if __name__ == "__main__":
    unittest.main()
