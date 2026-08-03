"""Versioned platform catalog activation and source-of-truth tests."""

from __future__ import annotations

import tempfile
import unittest
import json
import shutil
from dataclasses import asdict, replace
from pathlib import Path

from src.core.services.resources import ResourceError, ScopedResourceStore
from src.core.storage.admin_users import AdminRoleStore, AdminUserStore
from src.core.storage.organizations import OrganizationStore
from src.core.storage.tenants import TenantRegistry
from tests._web_api_base import WebApiTestBase
from tests.test_web_api import _make_config


class PlatformCatalogStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.registry = TenantRegistry(Path(self._tmp.name))
        self.organizations = OrganizationStore(self.registry)
        roles = AdminRoleStore(self.registry.database)
        users = AdminUserStore(self.registry.database)
        user = users.create(
            "platform-test", "password12345", roles.get_by_code("admin").role_id
        )
        self.organizations.sync_users(users)
        self.user_id = user.user_id
        self.config = _make_config()
        self.store = ScopedResourceStore(self.organizations, self.config)

    def test_publish_and_historical_rollback_move_active_pointer(self):
        first = self.store.save_draft(
            "agents",
            "release_helper",
            {"name": "发布助手", "description": "v1", "tools": []},
            self.user_id,
        )
        activated = self.store.publish(
            "agents", "release_helper", self.user_id, revision=first["revision"]
        )
        self.assertEqual(activated["activation_state"], "active")
        first_revision = activated["active_revision"]

        second = self.store.save_draft(
            "agents",
            "release_helper",
            {"name": "发布助手", "description": "v2", "tools": []},
            self.user_id,
        )
        self.store.publish(
            "agents", "release_helper", self.user_id, revision=second["revision"]
        )
        self.assertEqual(
            self.store.get_public("agents", "release_helper")["payload"][
                "description"
            ],
            "v2",
        )

        rolled_back = self.store.rollback(
            "agents", "release_helper", first_revision, self.user_id
        )
        self.assertEqual(rolled_back["active_revision"], first_revision)
        self.assertEqual(
            self.store.get_public("agents", "release_helper")["payload"][
                "description"
            ],
            "v1",
        )

    def test_hot_activation_failure_keeps_previous_snapshot(self):
        original = self.store.get_public("agents", "general")
        draft = self.store.save_draft(
            "agents",
            "general",
            {
                **original["payload"],
                "description": "不应暴露的失败版本",
            },
            self.user_id,
        )

        def fail_activation(*_args):
            raise RuntimeError("模拟热应用失败")

        self.store.set_activation_handler(fail_activation)
        with self.assertRaisesRegex(ResourceError, "运行时应用失败"):
            self.store.publish(
                "agents", "general", self.user_id, revision=draft["revision"]
            )
        activation = self.store.activation("agents", "general")
        self.assertEqual(activation["activation_state"], "failed")
        self.assertEqual(activation["active_revision"], original["revision"])
        self.assertEqual(
            self.store.get_public("agents", "general")["payload"]["description"],
            original["payload"]["description"],
        )

    def test_restart_required_version_is_hidden_until_next_startup(self):
        draft = self.store.save_draft(
            "plugins",
            "restart_plugin",
            {"id": "restart_plugin", "name": "重启插件", "enabled": True},
            self.user_id,
        )
        activation = self.store.publish(
            "plugins", "restart_plugin", self.user_id, revision=draft["revision"]
        )
        self.assertEqual(activation["activation_state"], "restart_required")
        with self.assertRaises(ResourceError):
            self.store.get_public("plugins", "restart_plugin")

        restarted = ScopedResourceStore(self.organizations, self.config)
        self.assertEqual(
            restarted.activation("plugins", "restart_plugin")["activation_state"],
            "active",
        )
        self.assertEqual(
            restarted.get_public("plugins", "restart_plugin")["payload"]["name"],
            "重启插件",
        )

    def test_bootstrap_files_do_not_override_existing_database_version(self):
        original = self.store.get_public("agents", "general")["payload"]
        changed_agent = replace(
            self.config.agents["general"], description="文件中的后续修改"
        )
        changed_config = replace(
            self.config, agents={"general": changed_agent}
        )
        restarted = ScopedResourceStore(self.organizations, changed_config)
        self.assertEqual(
            restarted.get_public("agents", "general")["payload"]["description"],
            original["description"],
        )

    def test_referenced_platform_model_cannot_be_disabled(self):
        payload = asdict(self.config.models["test_model"])
        payload["enabled"] = False
        draft = self.store.save_draft(
            "models", "test_model", payload, self.user_id
        )
        with self.assertRaisesRegex(ResourceError, "仍被"):
            self.store.publish(
                "models", "test_model", self.user_id, revision=draft["revision"]
            )

    def test_upgrade_snapshot_captures_database_directories_and_config_manifest(self):
        pre_migration = self.registry.database.path.with_name(
            "botplatform.pre-v29.sqlite3"
        )
        shutil.copy2(self.registry.database.path, pre_migration)
        organization_directory = self.registry.users_root / "snapshot-org"
        organization_directory.mkdir()
        (organization_directory / "marker.txt").write_text(
            "snapshot", encoding="utf-8"
        )

        ScopedResourceStore(self.organizations, self.config)
        backup = (
            self.registry.system_root / "platform_catalog_migration_backup_v29"
        )
        self.assertTrue((backup / "botplatform.pre-v29.sqlite3").is_file())
        self.assertTrue(
            (backup / "organizations" / "snapshot-org" / "marker.txt").is_file()
        )
        manifest = json.loads(
            (backup / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["schema_version"], 29)
        self.assertTrue(manifest["config"])
        self.assertTrue(
            all(len(item["sha256"]) == 64 for item in manifest["config"])
        )


class RetiredConfigurationApiTest(WebApiTestBase):
    def test_normal_panel_rejects_legacy_configuration_writes(self):
        self.app.state.allow_legacy_config_writes = False
        response = self.client.post(
            "/api/agents",
            json={"id": "legacy", "name": "旧接口"},
        )
        self.assertEqual(response.status_code, 410, response.text)
        self.assertIn("/api/v2/platform/catalog", response.text)

    def test_old_v2_context_and_generic_resource_routes_are_removed(self):
        self.assertEqual(self.client.get("/api/v2/me/context").status_code, 404)
        self.assertEqual(
            self.client.get("/api/v2/me/active-organization").status_code,
            404,
        )

    def test_platform_catalog_direct_save_and_delete(self):
        payload = {
            "id": "direct_helper",
            "name": "直接保存助手",
            "role": "assistant",
            "description": "不经过草稿流程",
            "system_prompt": "你是一个有帮助的助手。",
            "tools": [],
            "plugin_tools": {},
            "skills": [],
            "mcp_servers": [],
            "enabled": True,
        }
        response = self.client.put(
            "/api/v2/platform/catalog/agents/direct_helper", json=payload
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["payload"]["name"], "直接保存助手")

        listed = self.client.get("/api/v2/platform/catalog/agents")
        self.assertEqual(listed.status_code, 200, listed.text)
        item = next(
            value for value in listed.json()["items"]
            if value["resource_id"] == "direct_helper"
        )
        self.assertEqual(item["payload"]["description"], "不经过草稿流程")
        self.assertNotIn("draft_revision", item)

        deleted = self.client.delete(
            "/api/v2/platform/catalog/agents/direct_helper"
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)

    def test_platform_module_pages_and_tool_submenus_render(self):
        for path, marker in (
            ("/platform/models", "模型管理"),
            ("/platform/agent-templates", "智能体管理"),
            ("/platform/tools", "工具管理"),
            ("/platform/skills", "Skill 技能"),
            ("/platform/mcp", "MCP 服务"),
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn(marker, response.text)
            self.assertIn("系统工具", response.text)
