"""Integration tests for /api/skills and /api/mcp endpoints."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import src.api.routers.mcp as mcp_module
import src.api.routers.skills as skills_module

from tests._web_api_base import WebApiTestBase


class SkillsApiTest(WebApiTestBase):
    def setUp(self):
        super().setUp()
        self._file_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._file_dir.cleanup)
        self.skills_file = Path(self._file_dir.name) / "skills.json"
        patcher = patch.object(skills_module, "SKILLS_FILE", self.skills_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _skill(self, skill_id="greeting", **overrides):
        body = {
            "id": skill_id,
            "name": "问候技能",
            "description": "打招呼",
            "prompt": "请友好地打招呼",
            "enabled": True,
        }
        body.update(overrides)
        return body

    def test_list_empty(self):
        response = self.client.get("/api/skills")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_create_skill_updates_config_and_file(self):
        response = self.client.post("/api/skills", json=self._skill())
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["id"], "greeting")

        # Live config updated in place and JSON persisted.
        self.assertEqual(len(self.config.skills), 1)
        self.assertEqual(self.config.skills[0]["id"], "greeting")
        saved = json.loads(self.skills_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["skills"][0]["id"], "greeting")

    def test_create_invalid_id(self):
        response = self.client.post("/api/skills", json=self._skill("Bad-ID"))
        self.assertEqual(response.status_code, 400)

    def test_create_duplicate_409(self):
        self.client.post("/api/skills", json=self._skill())
        response = self.client.post("/api/skills", json=self._skill())
        self.assertEqual(response.status_code, 409)

    def test_update_skill(self):
        self.client.post("/api/skills", json=self._skill())
        response = self.client.put(
            "/api/skills/greeting", json={"name": "新名字", "enabled": False}
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["name"], "新名字")
        self.assertFalse(data["enabled"])
        self.assertFalse(self.config.skills[0]["enabled"])

    def test_update_not_found(self):
        response = self.client.put("/api/skills/nope", json={"name": "x"})
        self.assertEqual(response.status_code, 404)

    def test_delete_skill(self):
        self.client.post("/api/skills", json=self._skill())
        response = self.client.delete("/api/skills/greeting")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/skills").json(), [])
        self.assertEqual(self.config.skills, [])

    def test_delete_not_found(self):
        response = self.client.delete("/api/skills/nope")
        self.assertEqual(response.status_code, 404)


class McpApiTest(WebApiTestBase):
    def setUp(self):
        super().setUp()
        self._file_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._file_dir.cleanup)
        self.mcp_file = Path(self._file_dir.name) / "mcp_servers.json"
        patcher = patch.object(mcp_module, "MCP_FILE", self.mcp_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _server(self, server_id="local_fs", **overrides):
        body = {
            "id": server_id,
            "name": "本地文件服务",
            "transport": "stdio",
            "command": "mcp-server-fs",
            "args": [],
            "env": {},
            "enabled": True,
        }
        body.update(overrides)
        return body

    def test_list_empty(self):
        response = self.client.get("/api/mcp")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_create_stdio_server(self):
        response = self.client.post("/api/mcp", json=self._server())
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["transport"], "stdio")

        self.assertEqual(len(self.config.mcp_servers), 1)
        saved = json.loads(self.mcp_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["servers"][0]["id"], "local_fs")

    def test_create_invalid_id(self):
        response = self.client.post("/api/mcp", json=self._server("Bad-ID"))
        self.assertEqual(response.status_code, 400)

    def test_create_invalid_transport(self):
        response = self.client.post(
            "/api/mcp", json=self._server(transport="websocket")
        )
        self.assertEqual(response.status_code, 400)

    def test_create_stdio_requires_command(self):
        response = self.client.post(
            "/api/mcp", json=self._server(command=None)
        )
        self.assertEqual(response.status_code, 400)

    def test_create_sse_requires_url(self):
        response = self.client.post(
            "/api/mcp", json=self._server(transport="sse", url=None)
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/mcp",
            json=self._server(transport="sse", url="http://127.0.0.1:9000/sse"),
        )
        self.assertEqual(response.status_code, 201, response.text)

    def test_create_duplicate_409(self):
        self.client.post("/api/mcp", json=self._server())
        response = self.client.post("/api/mcp", json=self._server())
        self.assertEqual(response.status_code, 409)

    def test_update_server(self):
        self.client.post("/api/mcp", json=self._server())
        response = self.client.put(
            "/api/mcp/local_fs", json={"name": "改名", "enabled": False}
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["name"], "改名")
        self.assertFalse(data["enabled"])

    def test_update_rejects_bad_transport(self):
        self.client.post("/api/mcp", json=self._server())
        response = self.client.put(
            "/api/mcp/local_fs", json={"transport": "carrier-pigeon"}
        )
        self.assertEqual(response.status_code, 400)

    def test_update_not_found(self):
        response = self.client.put("/api/mcp/nope", json={"name": "x"})
        self.assertEqual(response.status_code, 404)

    def test_delete_server(self):
        self.client.post("/api/mcp", json=self._server())
        response = self.client.delete("/api/mcp/local_fs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.config.mcp_servers, [])

    def test_delete_not_found(self):
        response = self.client.delete("/api/mcp/nope")
        self.assertEqual(response.status_code, 404)

    def test_list_tools_without_runtime(self):
        self.client.post("/api/mcp", json=self._server())
        response = self.client.get("/api/mcp/local_fs/tools")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertFalse(data["connected"])
        self.assertEqual(data["tools"], [])

    def test_invoke_tool_without_runtime(self):
        self.client.post("/api/mcp", json=self._server())
        response = self.client.post(
            "/api/mcp/local_fs/tools/echo/invoke", json={"arguments": {}}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["ok"])
