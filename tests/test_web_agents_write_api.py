"""Integration tests for the /api/agents CRUD endpoints."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import src.api.routers.agents as agents_module

from tests._web_api_base import WebApiTestBase


class AgentsWriteApiTest(WebApiTestBase):
    def setUp(self):
        super().setUp()
        self._file_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._file_dir.cleanup)
        self.agents_dir = Path(self._file_dir.name) / "agents"
        patcher = patch.object(agents_module, "AGENTS_DIR", self.agents_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

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

    # ---- read ----

    def test_list_and_get(self):
        response = self.client.get("/api/agents")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["id"], "general")

        response = self.client.get("/api/agents/general")
        self.assertEqual(response.status_code, 200)

        response = self.client.get("/api/agents/nope")
        self.assertEqual(response.status_code, 404)

    # ---- create ----

    def test_create_agent_persists_file(self):
        response = self.client.post("/api/agents", json=self._agent())
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(data["id"], "helper")
        self.assertEqual(data["name"], "小助手")

        self.assertIn("helper", self.config.agents)
        saved = json.loads(
            (self.agents_dir / "helper.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved["id"], "helper")

    def test_create_invalid_id(self):
        response = self.client.post("/api/agents", json=self._agent("Bad-ID"))
        self.assertEqual(response.status_code, 400)

    def test_create_duplicate_409(self):
        response = self.client.post("/api/agents", json=self._agent("general"))
        self.assertEqual(response.status_code, 409)

    def test_create_empty_name_400(self):
        response = self.client.post("/api/agents", json=self._agent(name="  "))
        self.assertEqual(response.status_code, 400)

    def test_create_enabled_defaults_true(self):
        response = self.client.post("/api/agents", json=self._agent())
        self.assertEqual(response.status_code, 201, response.text)
        self.assertTrue(response.json()["enabled"])
        saved = json.loads(
            (self.agents_dir / "helper.json").read_text(encoding="utf-8")
        )
        self.assertTrue(saved["enabled"])

    def test_create_disabled_agent(self):
        response = self.client.post("/api/agents", json=self._agent(enabled=False))
        self.assertEqual(response.status_code, 201, response.text)
        self.assertFalse(response.json()["enabled"])
        self.assertFalse(self.config.agents["helper"].enabled)

    # ---- update ----

    def test_update_agent(self):
        self.client.post("/api/agents", json=self._agent())
        response = self.client.put(
            "/api/agents/helper",
            json={"name": "新助手", "temperature": 0.3, "greeting": "你好呀"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["name"], "新助手")
        self.assertEqual(data["temperature"], 0.3)
        self.assertEqual(data["greeting"], "你好呀")
        self.assertEqual(self.config.agents["helper"].name, "新助手")
        saved = json.loads(
            (self.agents_dir / "helper.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved["name"], "新助手")

    def test_update_not_found(self):
        response = self.client.put("/api/agents/nope", json={"name": "x"})
        self.assertEqual(response.status_code, 404)

    def test_update_enabled_persists(self):
        self.client.post("/api/agents", json=self._agent())
        response = self.client.put("/api/agents/helper", json={"enabled": False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["enabled"])
        self.assertFalse(self.config.agents["helper"].enabled)
        saved = json.loads(
            (self.agents_dir / "helper.json").read_text(encoding="utf-8")
        )
        self.assertFalse(saved["enabled"])

        response = self.client.put("/api/agents/helper", json={"enabled": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.config.agents["helper"].enabled)

    def test_disable_default_agent_400(self):
        response = self.client.put("/api/agents/general", json={"enabled": False})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(self.config.agents["general"].enabled)

    # ---- delete ----

    def test_delete_agent(self):
        self.client.post("/api/agents", json=self._agent())
        response = self.client.delete("/api/agents/helper")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("helper", self.config.agents)
        self.assertFalse((self.agents_dir / "helper.json").exists())

    def test_delete_default_agent_400(self):
        response = self.client.delete("/api/agents/general")
        self.assertEqual(response.status_code, 400)

    def test_delete_not_found(self):
        response = self.client.delete("/api/agents/nope")
        self.assertEqual(response.status_code, 404)

    # ---- auth ----

    def test_requires_login(self):
        from fastapi.testclient import TestClient

        anonymous = TestClient(self.app)
        response = anonymous.post("/api/agents", json=self._agent())
        self.assertEqual(response.status_code, 401)
