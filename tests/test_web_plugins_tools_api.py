"""Integration tests for /api/plugins and /api/tools endpoints."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import src.api.routers.plugins as plugins_module
from src.core.config.loader import PluginConfig

from tests._web_api_base import WebApiTestBase


class PluginsToolsApiTest(WebApiTestBase):
    def setUp(self):
        super().setUp()

        self._file_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._file_dir.cleanup)
        base = Path(self._file_dir.name)
        self.plugins_file = base / "plugins.json"
        self.tool_state_file = base / "tool_state.json"
        for name, value in (
            ("PLUGINS_FILE", self.plugins_file),
            ("TOOL_STATE_FILE", self.tool_state_file),
        ):
            patcher = patch.object(plugins_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    # ---- plugins ----

    def test_list_plugins(self):
        response = self.client.get("/api/plugins")
        self.assertEqual(response.status_code, 200, response.text)
        ids = {item["id"] for item in response.json()}
        self.assertIn("todo", ids)
        # No plugin configs registered -> everything disabled.
        for item in response.json():
            self.assertFalse(item["enabled"])

    def test_get_plugin_detail(self):
        response = self.client.get("/api/plugins/todo")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["id"], "todo")
        self.assertEqual(data["tool_count"], len(data["tools"]))

    def test_get_unknown_plugin_404(self):
        response = self.client.get("/api/plugins/nonexistent")
        self.assertEqual(response.status_code, 404)

    def test_update_unknown_plugin_404(self):
        response = self.client.put(
            "/api/plugins/nonexistent", json={"enabled": True}
        )
        self.assertEqual(response.status_code, 404)

    def test_update_plugin_without_config_404(self):
        response = self.client.put("/api/plugins/todo", json={"enabled": True})
        self.assertEqual(response.status_code, 404)
        self.assertIn("插件配置不存在", response.json()["detail"])

    def test_update_plugin_persists(self):
        self.config.plugins["todo"] = PluginConfig(
            id="todo", enabled=False, settings={}
        )
        response = self.client.put(
            "/api/plugins/todo",
            json={"enabled": True, "settings": {"max_items": 10}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertTrue(data["enabled"])
        self.assertEqual(data["settings"], {"max_items": 10})

        # In-memory config replaced and JSON file written.
        self.assertTrue(self.config.plugins["todo"].enabled)
        saved = json.loads(self.plugins_file.read_text(encoding="utf-8"))
        entry = next(e for e in saved["plugins"] if e["id"] == "todo")
        self.assertTrue(entry["enabled"])
        self.assertEqual(entry["settings"], {"max_items": 10})

    # ---- tools ----

    def test_list_tools_without_runtime(self):
        response = self.client.get("/api/tools")
        self.assertEqual(response.status_code, 200, response.text)
        tools = response.json()
        names = {t["name"] for t in tools}
        self.assertIn("run_command", names)
        categories = list(dict.fromkeys(t["category"] for t in tools))
        self.assertEqual(
            categories,
            ["知识库", "文件系统", "系统信息", "命令执行", "脚本"],
        )
        run_command = next(t for t in tools if t["name"] == "run_command")
        # No tool runtime injected -> unavailable but enabled by default.
        self.assertFalse(run_command["available"])
        self.assertTrue(run_command["enabled"])
        self.assertTrue(run_command["requires_approval"])

    def test_update_tool_state(self):
        response = self.client.patch(
            "/api/tools/run_command",
            json={"enabled": False, "require_approval": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["name"], "run_command")
        self.assertFalse(data["enabled"])
        self.assertTrue(data["require_approval"])
        saved = json.loads(self.tool_state_file.read_text(encoding="utf-8"))
        self.assertFalse(saved["tools"]["run_command"]["enabled"])

    def test_update_unknown_tool_404(self):
        response = self.client.patch(
            "/api/tools/no_such_tool", json={"enabled": False}
        )
        self.assertEqual(response.status_code, 404)

    # ---- tool audit ----

    def test_audit_without_store_returns_empty(self):
        response = self.client.get("/api/tools/audit")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"items": [], "total": 0})

    def test_audit_rejects_invalid_status(self):
        response = self.client.get("/api/tools/audit", params={"status": "unknown"})
        self.assertEqual(response.status_code, 422)

    # ---- auth ----

    def test_requires_login(self):
        from fastapi.testclient import TestClient

        anonymous = TestClient(self.app)
        self.assertEqual(anonymous.get("/api/plugins").status_code, 401)
        self.assertEqual(anonymous.get("/api/tools").status_code, 401)
