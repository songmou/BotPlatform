from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.core.plugins.base import (
    PluginContext,
    PluginError,
    PluginJobDefinition,
    PluginToolDefinition,
)
from src.core.plugins.catalog import PluginCatalog
from src.core.plugins.manager import PluginManager
from src.core.plugins.manifest import PluginManifestError, load_manifest
from src.core.storage.tenants import TenantRegistry


def write_manifest(root: Path, plugin_id: str = "sample", **overrides):
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.py").write_text("class Plugin: pass\n", encoding="utf-8")
    data = {
        "schema_version": 1,
        "id": plugin_id,
        "name": "示例插件",
        "version": "1.0.0",
        "description": "测试插件",
        "entrypoint": "plugin:Plugin",
        "core_api": "1",
        "tools": {
            "sample_tool": {
                "description": "执行示例",
                "approval": "none",
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
    }
    data.update(overrides)
    path = root / "plugin.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class LifecyclePlugin:
    id = "sample"
    events = []

    def __init__(self, settings, context=None):
        self.context = context
        self.settings = settings
        self.tool_definitions = {
            "sample_tool": PluginToolDefinition(
                "执行示例",
                {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            )
        }
        self.events.append("init")

    def start(self):
        self.events.append("start")

    def is_available(self, tool_name, tenant=None):
        return tool_name == "sample_tool"

    def execute(self, tool_name, arguments, tenant):
        return {"tenant_id": tenant.tenant_id}

    def preview(self, tool_name, arguments, tenant):
        return "示例预览"

    def run_background_job(self, job_id, now=None):
        self.events.append("job:" + job_id)
        return True

    def close_tenant(self, tenant_id):
        self.events.append("tenant:" + tenant_id)

    def close(self):
        self.events.append("close")


class FailingStartPlugin(LifecyclePlugin):
    def start(self):
        raise RuntimeError("启动失败")


class PluginManifestTests(unittest.TestCase):
    def test_external_entrypoint_must_exist_inside_package(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_manifest(
                Path(directory) / "bad",
                entrypoint="missing:Plugin",
            )
            with self.assertRaisesRegex(PluginManifestError, "不存在或越界"):
                load_manifest(path, "external")

    def test_duplicate_ids_and_tools_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = load_manifest(write_manifest(root / "one"), "external")
            second = load_manifest(
                write_manifest(root / "two", plugin_id="other"),
                "external",
            )
            catalog = PluginCatalog([first])
            with self.assertRaisesRegex(PluginManifestError, "工具名称重复"):
                catalog.add(second)

    def test_settings_schema_and_legacy_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_manifest(
                Path(directory) / "settings",
                settings_schema={
                    "type": "object",
                    "properties": {
                        "owners": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string", "minLength": 1},
                        }
                    },
                    "required": ["owners"],
                    "additionalProperties": False,
                },
                settings_aliases={"admins": "owners"},
                discard_unknown_settings=True,
            )
            manifest = load_manifest(path, "external")
            normalized = manifest.normalize_settings(
                {"admins": ["tenant"], "retired": True}
            )
            self.assertEqual(normalized, {"owners": ["tenant"]})
            manifest.validate_settings(normalized)
            with self.assertRaises(PluginManifestError):
                manifest.validate_settings({"owners": []})

    def test_settings_schema_validates_unique_keys_and_references(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_manifest(
                Path(directory) / "references",
                settings_schema={
                    "type": "object",
                    "properties": {
                        "projects": {
                            "type": "array",
                            "x-unique-key": "id",
                            "items": {
                                "type": "object",
                                "properties": {"id": {"type": "string"}},
                                "required": ["id"],
                                "additionalProperties": False,
                            },
                        },
                        "default_project": {"type": "string"},
                    },
                    "required": ["projects", "default_project"],
                    "additionalProperties": False,
                    "x-references": [
                        {
                            "field": "default_project",
                            "array": "projects",
                            "key": "id",
                        }
                    ],
                },
            )
            manifest = load_manifest(path, "external")
            manifest.validate_settings(
                {"projects": [{"id": "one"}], "default_project": "one"}
            )
            with self.assertRaisesRegex(PluginManifestError, "不能重复"):
                manifest.validate_settings(
                    {
                        "projects": [{"id": "one"}, {"id": "one"}],
                        "default_project": "one",
                    }
                )
            with self.assertRaisesRegex(PluginManifestError, "必须引用"):
                manifest.validate_settings(
                    {"projects": [{"id": "one"}], "default_project": "missing"}
                )

    def test_discovery_does_not_import_plugin_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "external" / "quiet"
            write_manifest(package, plugin_id="quiet")
            marker = root / "imported"
            (package / "plugin.py").write_text(
                "from pathlib import Path\n"
                "Path({!r}).write_text('imported')\n"
                "class Plugin: pass\n".format(str(marker)),
                encoding="utf-8",
            )
            catalog = PluginCatalog.discover(
                root, external_root=root / "external"
            )
            self.assertIsNotNone(catalog.get("quiet"))
            self.assertFalse(marker.exists())

    def test_external_entrypoint_supports_relative_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "relative"
            path = write_manifest(package, plugin_id="relative")
            (package / "helper.py").write_text("VALUE = 'ok'\n", encoding="utf-8")
            (package / "plugin.py").write_text(
                "from .helper import VALUE\n"
                "class Plugin:\n"
                "    marker = VALUE\n",
                encoding="utf-8",
            )
            manifest = load_manifest(path, "external")
            plugin_type = PluginManager._load_entrypoint(manifest)
            self.assertEqual(plugin_type.marker, "ok")


class PluginManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.manifest = load_manifest(
            write_manifest(
                root / "sample",
                services=["tenant_storage"],
                background_jobs=[
                    {"id": "tick", "interval_seconds": 10}
                ],
            ),
            "external",
        )
        self.catalog = PluginCatalog([self.manifest])
        self.registry = TenantRegistry(root / "data")
        self.context = PluginContext(
            project_root=root,
            tenant_registry=self.registry,
            notification_service=object(),
            data_root=root / "plugin-data",
        )
        self.configs = {
            "sample": SimpleNamespace(enabled=True, settings={})
        }
        LifecyclePlugin.events = []

    def test_single_instance_routes_lifecycle_and_background_jobs(self):
        with patch.object(
            PluginManager,
            "_load_entrypoint",
            return_value=LifecyclePlugin,
        ):
            manager = PluginManager(
                self.catalog, self.configs, self.context
            )
        plugin = manager.get("sample")
        self.assertIs(plugin, manager.plugins[0])
        self.assertIsNone(plugin.context.notification_service)
        self.assertIsNone(plugin.context.project_root)
        scoped_tenant = self.registry.resolve("bot", "scoped")
        tenant_data = plugin.context.tenant_data_dir(scoped_tenant.tenant_id)
        self.assertEqual(
            tenant_data.name,
            "sample",
        )
        with self.assertRaisesRegex(PluginError, "其他插件"):
            plugin.context.tenant_data_dir("other", scoped_tenant.tenant_id)
        manager.start()
        tenant = self.registry.resolve("bot", "user")
        self.assertEqual(
            manager.execute("sample_tool", {}, tenant)["tenant_id"],
            tenant.tenant_id,
        )
        self.assertEqual(
            manager.background_jobs(),
            [("sample", PluginJobDefinition("tick", 10))],
        )
        self.assertTrue(manager.run_background_job("sample", "tick"))
        manager.close_tenant(tenant.tenant_id)
        manager.close()
        self.assertEqual(
            LifecyclePlugin.events,
            [
                "init",
                "start",
                "job:tick",
                "tenant:" + tenant.tenant_id,
                "close",
            ],
        )

    def test_disabled_and_missing_dependency_plugins_are_not_imported(self):
        disabled = {
            "sample": SimpleNamespace(enabled=False, settings={})
        }
        with patch.object(
            PluginManager, "_load_entrypoint"
        ) as importer:
            manager = PluginManager(
                self.catalog, disabled, self.context
            )
        importer.assert_not_called()
        self.assertEqual(manager.plugins, [])

        missing_manifest = self.manifest.__class__(
            **{
                **self.manifest.__dict__,
                "dependencies": (
                    SimpleNamespace(
                        distribution="never-installed-package",
                        import_name="never_installed_package_xyz",
                        version="",
                    ),
                ),
            }
        )
        with patch.object(
            PluginManager, "_load_entrypoint"
        ) as importer:
            manager = PluginManager(
                PluginCatalog([missing_manifest]),
                self.configs,
                self.context,
            )
        importer.assert_not_called()
        self.assertIn("缺少依赖", manager.errors["sample"])

    def test_builtin_tool_conflict_isolated_before_import(self):
        with patch.object(PluginManager, "_load_entrypoint") as importer:
            manager = PluginManager(
                self.catalog,
                self.configs,
                self.context,
                reserved_tools={"sample_tool"},
            )
        importer.assert_not_called()
        self.assertEqual(manager.plugins, [])
        self.assertIn("内置工具冲突", manager.errors["sample"])

    def test_start_failure_isolated_and_tool_is_unregistered(self):
        with patch.object(
            PluginManager,
            "_load_entrypoint",
            return_value=FailingStartPlugin,
        ):
            manager = PluginManager(
                self.catalog, self.configs, self.context
            )
        manager.start()
        self.assertIsNone(manager.get("sample"))
        self.assertFalse(manager.is_available("sample_tool"))
        self.assertIn("启动失败", manager.errors["sample"])


if __name__ == "__main__":
    unittest.main()
