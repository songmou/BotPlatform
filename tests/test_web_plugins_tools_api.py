"""Integration tests for /api/plugins and /api/tools endpoints."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import src.api.routers.plugins as plugins_module
from src.core.config.loader import PluginConfig
from src.core.paths import PROJECT_ROOT
from src.core.plugins.catalog import PluginCatalog

from tests._web_api_base import WebApiTestBase


class PluginsToolsApiTest(WebApiTestBase):
    def setUp(self):
        super().setUp()

        self._file_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._file_dir.cleanup)
        base = Path(self._file_dir.name)
        self.plugins_file = base / "plugins.json"
        self.tool_state_file = base / "tool_state.json"
        self.packages_dir = base / "packages"
        self.trash_dir = base / "trash"
        for name, value in (
            ("PLUGINS_FILE", self.plugins_file),
            ("TOOL_STATE_FILE", self.tool_state_file),
            ("PLUGIN_PACKAGES_DIR", self.packages_dir),
            ("PLUGIN_TRASH_DIR", self.trash_dir),
        ):
            patcher = patch.object(plugins_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        catalog = lambda: PluginCatalog.discover(  # noqa: E731
            PROJECT_ROOT, external_root=self.packages_dir
        )
        for name in ("default_catalog", "refresh_catalog"):
            patcher = patch.object(plugins_module, name, side_effect=catalog)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _package(self, root: Path, version: str = "1.0.0") -> Path:
        root.mkdir(parents=True)
        (root / "plugin.py").write_text(
            "class LocalPlugin:\n"
            "    id = 'local_example'\n",
            encoding="utf-8",
        )
        (root / "plugin.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": "local_example",
                    "name": "本地示例",
                    "version": version,
                    "description": "用于测试本地安装",
                    "entrypoint": "plugin:LocalPlugin",
                    "core_api": "1",
                    "tools": {
                        "local_example_run": {
                            "description": "执行本地示例",
                            "parameters": {
                                "type": "object",
                                "properties": {},
                                "additionalProperties": False,
                            },
                        }
                    },
                    "settings_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return root

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
        # env_allowlist is always present, even when the manifest declares none.
        self.assertIn("env_allowlist", data)
        self.assertIsInstance(data["env_allowlist"], list)

    def test_plugin_out_carries_declared_allowlist(self):
        # A locally installed package manifest declaring env_allowlist must
        # surface that list on the detail endpoint.
        source = self._package(Path(self._file_dir.name) / "with_env")
        manifest_path = source / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["env_allowlist"] = ["API_TOKEN", "PLUGIN_DEBUG"]
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        installed = self.client.post(
            "/api/plugins/install", json={"source_path": str(source)}
        )
        self.assertEqual(installed.status_code, 201, installed.text)
        detail = self.client.get("/api/plugins/local_example")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(
            detail.json()["env_allowlist"], ["API_TOKEN", "PLUGIN_DEBUG"]
        )

    def test_get_unknown_plugin_404(self):
        response = self.client.get("/api/plugins/nonexistent")
        self.assertEqual(response.status_code, 404)

    def test_update_unknown_plugin_404(self):
        response = self.client.put(
            "/api/plugins/nonexistent", json={"enabled": True}
        )
        self.assertEqual(response.status_code, 404)

    def test_update_plugin_without_config_creates_entry(self):
        response = self.client.put("/api/plugins/todo", json={"enabled": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["enabled"])
        self.assertTrue(response.json()["restart_required"])
        saved = json.loads(self.plugins_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["plugins"][0]["id"], "todo")

    def test_update_plugin_persists(self):
        self.config.plugins["todo"] = PluginConfig(
            id="todo", enabled=False, settings={}
        )
        response = self.client.put(
            "/api/plugins/todo",
            json={"enabled": True, "settings": {}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertTrue(data["enabled"])
        self.assertEqual(data["settings"], {})
        self.assertTrue(data["restart_required"])

        # Runtime config is intentionally unchanged until a full restart.
        self.assertFalse(self.config.plugins["todo"].enabled)
        saved = json.loads(self.plugins_file.read_text(encoding="utf-8"))
        entry = next(e for e in saved["plugins"] if e["id"] == "todo")
        self.assertTrue(entry["enabled"])
        self.assertEqual(entry["settings"], {})

    def test_install_update_and_remove_local_package(self):
        source = self._package(
            Path(self._file_dir.name) / "source-v1"
        )
        installed = self.client.post(
            "/api/plugins/install",
            json={"source_path": str(source)},
        )
        self.assertEqual(installed.status_code, 201, installed.text)
        self.assertEqual(installed.json()["source"], "external")
        self.assertTrue(installed.json()["restart_required"])

        update = self._package(
            Path(self._file_dir.name) / "source-v2", version="2.0.0"
        )
        updated = self.client.put(
            "/api/plugins/local_example/package",
            json={"source_path": str(update)},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["version"], "2.0.0")

        removed = self.client.delete("/api/plugins/local_example")
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertTrue(removed.json()["data_preserved"])
        self.assertFalse((self.packages_dir / "local_example").exists())

    def test_install_rejects_tool_name_conflict(self):
        source = self._package(Path(self._file_dir.name) / "conflict")
        manifest_path = source / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["tools"] = {
            "todo_manage": {
                "description": "冲突工具",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            }
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        response = self.client.post(
            "/api/plugins/install",
            json={"source_path": str(source)},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse((self.packages_dir / "local_example").exists())

    def test_settings_update_does_not_import_plugin_code(self):
        source = self._package(Path(self._file_dir.name) / "no-import")
        marker = Path(self._file_dir.name) / "imported"
        (source / "plugin.py").write_text(
            "from pathlib import Path\n"
            "Path({!r}).write_text('yes')\n"
            "class LocalPlugin:\n"
            "    id = 'local_example'\n".format(str(marker)),
            encoding="utf-8",
        )
        installed = self.client.post(
            "/api/plugins/install",
            json={"source_path": str(source)},
        )
        self.assertEqual(installed.status_code, 201, installed.text)
        updated = self.client.put(
            "/api/plugins/local_example",
            json={"enabled": False, "settings": {}},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertFalse(marker.exists())

    def test_plugin_manage_permission_is_required(self):
        self.assertEqual(self.viewer_client.get("/api/plugins").status_code, 200)
        response = self.viewer_client.put(
            "/api/plugins/todo",
            json={"enabled": True},
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_clear_plugin_data_requires_exact_confirmation(self):
        tenant = self._make_tenant()
        target = (
            self.registry.tenant_root(tenant.tenant_id)
            / "plugins"
            / "retired_plugin"
        )
        target.mkdir(parents=True)
        bad = self.client.request(
            "DELETE",
            "/api/plugins/retired_plugin/data",
            json={"confirmation": "wrong"},
        )
        self.assertEqual(bad.status_code, 400)
        cleared = self.client.request(
            "DELETE",
            "/api/plugins/retired_plugin/data",
            json={"confirmation": "retired_plugin"},
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertFalse(target.exists())

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
            ["知识库", "文件系统", "系统信息", "命令执行", "Git", "脚本"],
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
