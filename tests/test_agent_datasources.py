"""Tests for the agent ↔ datasource binding feature.

Covers the full chain introduced for the "数据源" tab:

* A. Platform agent CRUD round-trips the ``datasources`` field and rejects
     unknown / disabled bindings.
* B. ``resolve_tool_names`` appends the read-only db_* tools for any agent
     that has at least one datasource bound.
* C. ``ToolRuntime`` enforces the binding at execution time (fail-closed).
* D. ``DataSourceService.prompt_block`` degrades gracefully on missing /
     disabled datasources instead of raising on the chat hot path.
* E. Organization-side editor options expose ``datasources`` while hiding
     db_* from the built-in tool picker, and org agents persist bindings.

The datasource runtime stack depends on ``sqlglot`` (only installed via
``requirements-db.txt``, which CI does not install), so every test here
exercises paths that do NOT require a live database driver or sqlglot.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import src.api.routers.agents as agents_module
from src.api.routers.v2 import organization_agent_editor_options
from src.core.config.loader import AgentPreset, Capability, ToolConfig
from src.core.datasource.service import DataSourceService
from src.core.services.agent_tools import resolve_tool_names
from src.core.tooling.definitions import (
    DATASOURCE_READONLY_TOOLS,
    DATASOURCE_TOOLS,
)
from src.core.tooling.runtime import ToolRuntime
from src.core.tooling.runtime import ToolError

from tests._web_api_base import WebApiTestBase


ENABLED_DS = {
    "id": "crm_db",
    "name": "CRM 数据库",
    "engine": "mysql",
    "enabled": True,
}
DISABLED_DS = {
    "id": "legacy_db",
    "name": "遗留库",
    "engine": "mysql",
    "enabled": False,
}


# ---------------------------------------------------------------------------
# A. Platform agent CRUD round-trip
# ---------------------------------------------------------------------------


class PlatformAgentDatasourceApiTest(WebApiTestBase):
    def setUp(self):
        super().setUp()
        # ProjectConfig is frozen; mutate the existing list in place rather
        # than reassigning the attribute.
        self.config.datasources.clear()
        self.config.datasources.extend([dict(ENABLED_DS), dict(DISABLED_DS)])

        self._file_dir = self._mk_agent_dir()
        patcher = patch.object(agents_module, "AGENTS_DIR", self._file_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _mk_agent_dir(self):
        import tempfile
        from pathlib import Path

        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Path(d.name) / "agents"

    def _agent(self, agent_id="helper", **overrides):
        body = {
            "id": agent_id,
            "name": "小助手",
            "role": "assistant",
            "description": "测试用智能体",
            "system_prompt": "你是一个乐于助人的助手。",
        }
        body.update(overrides)
        return body

    def test_create_agent_persists_datasources(self):
        response = self.client.post(
            "/api/agents", json=self._agent(datasources=["crm_db"])
        )
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(data["datasources"], ["crm_db"])

        # In-memory config (memory map) and on-disk file both carry the binding.
        self.assertEqual(self.config.agents["helper"].datasources, ["crm_db"])
        saved = (self._file_dir / "helper.json").read_text(encoding="utf-8")
        self.assertIn("crm_db", saved)

    def test_create_agent_rejects_unknown_datasource(self):
        response = self.client.post(
            "/api/agents", json=self._agent(datasources=["ghost_db"])
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("ghost_db", response.json()["detail"])

    def test_create_agent_rejects_disabled_datasource(self):
        response = self.client.post(
            "/api/agents", json=self._agent(datasources=["legacy_db"])
        )
        self.assertEqual(response.status_code, 400, response.text)
        # The disabled-datasource error surfaces the datasource name, not id.
        self.assertIn("遗留库", response.json()["detail"])
        self.assertIn("停用", response.json()["detail"])

    def test_create_agent_does_not_auto_enable_db_tools(self):
        response = self.client.post(
            "/api/agents",
            json=self._agent(tools=[], datasources=["crm_db"]),
        )
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        # db_* must never appear in the explicit tool list nor be auto-granted
        # at the storage layer; the "数据源" tab is the only entry point.
        self.assertEqual(data["tools"], [])
        for db_tool in DATASOURCE_TOOLS:
            self.assertNotIn(db_tool, data["tools"])

    def test_get_agent_returns_datasources(self):
        self.client.post("/api/agents", json=self._agent(datasources=["crm_db"]))
        response = self.client.get("/api/agents/helper")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["datasources"], ["crm_db"])

    def test_update_agent_changes_datasources(self):
        self.client.post("/api/agents", json=self._agent(datasources=["crm_db"]))
        response = self.client.put(
            "/api/agents/helper", json={"datasources": []}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["datasources"], [])
        self.assertEqual(self.config.agents["helper"].datasources, [])

    def test_update_agent_rejects_disabled_datasource(self):
        self.client.post("/api/agents", json=self._agent(datasources=["crm_db"]))
        response = self.client.put(
            "/api/agents/helper", json={"datasources": ["legacy_db"]}
        )
        self.assertEqual(response.status_code, 400, response.text)


# ---------------------------------------------------------------------------
# B. resolve_tool_names appends read-only db_* tools
# ---------------------------------------------------------------------------


def _agent_preset(**overrides):
    preset = AgentPreset(
        id="a",
        name="a",
        role="assistant",
        description="",
        system_prompt="",
        capabilities=[Capability(name="通用对话", description="")],
        tools=[],
    )
    for key, value in overrides.items():
        object.__setattr__(preset, key, value)
    return preset


class ResolveToolNamesTest(unittest.TestCase):
    def test_binding_appends_readonly_tools(self):
        agent = _agent_preset(datasources=["crm_db"])
        names = resolve_tool_names(agent, None)
        for tool in DATASOURCE_READONLY_TOOLS:
            self.assertIn(tool, names)
        self.assertNotIn("db_execute", names)

    def test_no_binding_no_db_tools(self):
        agent = _agent_preset(datasources=[])
        names = resolve_tool_names(agent, None)
        for tool in DATASOURCE_TOOLS:
            self.assertNotIn(tool, names)

    def test_existing_tools_preserved_without_duplicates(self):
        agent = _agent_preset(tools=["list_directory"], datasources=["crm_db"])
        names = resolve_tool_names(agent, None)
        self.assertIn("list_directory", names)
        self.assertIn("db_query", names)
        self.assertEqual(
            [n for n in names if n == "db_query"], ["db_query"]
        )


# ---------------------------------------------------------------------------
# C. ToolRuntime enforces the binding (fail-closed)
# ---------------------------------------------------------------------------


def _tool_runtime():
    config = ToolConfig(
        enabled=False,
        default_working_directory=".",
        allowed_roots=[],
        denied_globs=[],
        approval_ttl_seconds=60,
        max_tool_rounds=5,
        max_total_tool_calls=20,
        max_read_bytes=1024,
        max_write_bytes=1024,
        max_directory_entries=100,
        max_search_results=50,
        max_command_output_bytes=4096,
        default_command_timeout_seconds=30,
        max_command_timeout_seconds=60,
        enabled_command_profiles=[],
    )
    return ToolRuntime(config, "Asia/Shanghai")


class ToolRuntimeBindingTest(unittest.TestCase):
    def test_bound_agent_can_access_its_datasource(self):
        runtime = _tool_runtime()
        runtime.bind_agent_datasources(["sales"])
        self.assertEqual(runtime._require_datasource("sales"), "sales")

    def test_bound_agent_cannot_access_other_datasource(self):
        runtime = _tool_runtime()
        runtime.bind_agent_datasources(["sales"])
        with self.assertRaises(ToolError):
            runtime._require_datasource("hr")

    def test_none_binding_is_deny_all(self):
        runtime = _tool_runtime()
        runtime.bind_agent_datasources(None)
        self.assertIsNone(runtime.bound_datasources)
        with self.assertRaises(ToolError):
            runtime._require_datasource("sales")

    def test_empty_binding_is_deny_all(self):
        runtime = _tool_runtime()
        runtime.bind_agent_datasources([])
        with self.assertRaises(ToolError):
            runtime._require_datasource("sales")


# ---------------------------------------------------------------------------
# D. prompt_block failure tolerance (no sqlglot / no live driver)
# ---------------------------------------------------------------------------


class PromptBlockToleranceTest(unittest.TestCase):
    def test_empty_input_returns_empty(self):
        service = DataSourceService()
        self.assertEqual(service.prompt_block([]), "")

    def test_unknown_datasource_skipped(self):
        service = DataSourceService()
        service._configs = {}
        self.assertEqual(service.prompt_block(["ghost"]), "")

    def test_disabled_datasource_skipped(self):
        service = DataSourceService()
        service._configs = {
            "legacy_db": {"id": "legacy_db", "engine": "mysql", "enabled": False}
        }
        self.assertEqual(service.prompt_block(["legacy_db"]), "")


# ---------------------------------------------------------------------------
# E. Organization-side editor options + persistence
# ---------------------------------------------------------------------------


class OrganizationAgentDatasourceTest(WebApiTestBase):
    def setUp(self):
        super().setUp()
        self.config.datasources.clear()
        self.config.datasources.extend([dict(ENABLED_DS), dict(DISABLED_DS)])
        self.org_id, self.owner = self._create_owner("ds-binding")

    def _create_owner(self, suffix):
        created = self.client.post(
            "/api/v2/platform/organizations", json={"name": "组织 " + suffix}
        )
        self.assertEqual(created.status_code, 201, created.text)
        org_id = created.json()["organization"]["organization_id"]
        payload = created.json()
        from fastapi.testclient import TestClient

        anonymous = TestClient(self.app)
        accepted = anonymous.post(
            "/api/v2/invitations/accept",
            json={
                "token": payload["owner_invitation_token"],
                "username": "owner_" + suffix,
                "password": "password-" + suffix,
            },
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        login = anonymous.post(
            "/api/auth/login",
            json={
                "username": "owner_" + suffix,
                "password": "password-" + suffix,
            },
        )
        self.assertEqual(login.status_code, 200, login.text)
        return org_id, anonymous

    def test_editor_options_expose_datasources_and_hide_db_tools(self):
        response = self.owner.get(
            "/api/v2/orgs/{}/agent-editor-options".format(self.org_id)
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("datasources", body)
        ds_ids = [item["id"] for item in body["datasources"]]
        self.assertIn("crm_db", ds_ids)
        # Disabled datasources must not be offered for binding.
        self.assertNotIn("legacy_db", ds_ids)
        # db_* tools must never appear in the built-in tool picker.
        builtin_names = [item["name"] for item in body["builtin_tools"]]
        for db_tool in DATASOURCE_TOOLS:
            self.assertNotIn(db_tool, builtin_names)

    def test_create_org_agent_with_datasource(self):
        created = self.owner.put(
            "/api/v2/orgs/{}/agents/org_helper".format(self.org_id),
            json={
                "payload": {
                    "name": "组织助手",
                    "system_prompt": "组织提示词",
                    "tools": [],
                    "plugin_tools": {},
                    "skills": [],
                    "mcp_servers": [],
                    "datasources": ["crm_db"],
                }
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        # The persisted payload carries the binding (safe fields keep it).
        self.assertEqual(
            created.json()["payload"].get("datasources"), ["crm_db"]
        )

        listed = self.owner.get(
            "/api/v2/orgs/{}/agents".format(self.org_id)
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        by_id = {
            item["resource_id"]: item for item in listed.json()["items"]
        }
        self.assertIn("org_helper", by_id)
        self.assertEqual(
            by_id["org_helper"]["payload"].get("datasources"), ["crm_db"]
        )

    def test_create_org_agent_rejects_unknown_datasource(self):
        created = self.owner.put(
            "/api/v2/orgs/{}/agents/org_bad".format(self.org_id),
            json={
                "payload": {
                    "name": "坏绑定",
                    "system_prompt": "x",
                    "tools": [],
                    "plugin_tools": {},
                    "skills": [],
                    "mcp_servers": [],
                    "datasources": ["ghost_db"],
                }
            },
        )
        # Unknown datasource → ResourceError → 404 (message contains "不存在").
        self.assertEqual(created.status_code, 404, created.text)
        self.assertIn("ghost_db", created.json()["detail"])


if __name__ == "__main__":
    unittest.main()
