from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.core.modeling import (
    CanonicalMessage,
    ModelCapabilities,
    ModelIdentity,
    ModelRequest,
    ModelResponse,
    ModelRouter,
)


class FakeClient:
    def __init__(self, profile_id="test_model", provider="test", model="test-model"):
        self.identity = ModelIdentity(profile_id, provider, model)
        self.capabilities = ModelCapabilities(tools=True, vision=False, reasoning=False)
        self.closed = False

    def ensure_ready(self):
        pass

    def complete(self, request):
        return ModelResponse(
            CanonicalMessage("assistant", "你好！我是测试模型。"),
            actual_model=self.identity.configured_model,
        )

    def complete_stream(self, request):
        yield "你好！"
        yield "我是测试模型。"

    def close(self):
        self.closed = True


def _make_config():
    from src.core.config.loader import (
        AgentPreset,
        AppConfig,
        Capability,
        EmbeddingProfile,
        ModelProfile,
        PluginConfig,
        ProjectConfig,
        ToolConfig,
    )

    app = AppConfig(
        default_agent="general",
        timezone="Asia/Shanghai",
        history_rounds=10,
        image_prompt="",
        active_model="test_model",
        fallback_model="test_model",
        local_model="",
        flash_model="",
        pro_model="",
        vision_model="",
        fallback_cooldown_seconds=60,
    )
    model_profile = ModelProfile(
        id="test_model",
        enabled=True,
        type="openai_compatible",
        provider="test",
        base_url="http://localhost:11434",
        model="test-model",
        temperature=0.7,
        max_tokens=2048,
        timeout_seconds=30,
        capabilities=ModelCapabilities(tools=True, vision=False, reasoning=False),
    )
    agent = AgentPreset(
        id="general",
        name="通用助手",
        role="assistant",
        description="通用对话助手",
        system_prompt="你是一个有帮助的助手。",
        capabilities=[Capability(name="对话", description="通用对话")],
        tools=[],
    )
    tools = ToolConfig(
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
    embedding = EmbeddingProfile(
        id="none", enabled=False, base_url="", model="", dimensions=0, timeout_seconds=5
    )
    return ProjectConfig(
        app=app,
        models={"test_model": model_profile},
        tools=tools,
        plugins={},
        agents={"general": agent},
        scripts={},
        schedules=[],
        embedding=embedding,
    )


class WebApiTest(unittest.TestCase):
    def setUp(self):
        from src.api.app import create_app
        import src.api.routers.chat as chat_module

        self.config = _make_config()
        self.fake_client = FakeClient()
        self.model_router = ModelRouter.single(self.fake_client)

        self.mock_registry = MagicMock()
        self.mock_registry.resolve.return_value = MagicMock(tenant_id="00000000-0000-0000-0000-000000000001")

        self.mock_store = MagicMock()
        self.mock_store.load_context.return_value = []

        self.app = create_app(
            self.config, self.model_router, self.mock_registry, self.mock_store
        )
        self.app.state.web_token = "test-token"
        self.client = TestClient(self.app)

        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        os.remove(self._tmp.name)
        self._patcher = patch.object(
            chat_module, "CONVERSATIONS_FILE", type(chat_module.CONVERSATIONS_FILE)(self._tmp.name)
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(lambda: os.path.exists(self._tmp.name) and os.remove(self._tmp.name))

    def _auth_params(self):
        return {"token": "test-token"}

    def _create_conversation(self):
        response = self.client.post("/api/chat/conversations", params=self._auth_params())
        self.assertEqual(response.status_code, 201)
        return response.json()["id"]

    def test_health_no_auth(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_status_requires_auth(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 401)

    def test_status_with_auth(self):
        response = self.client.get("/api/status", params=self._auth_params())
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["model_ready"])
        self.assertEqual(data["active_model"], "test_model")

    def test_models_list(self):
        response = self.client.get("/api/models", params=self._auth_params())
        self.assertEqual(response.status_code, 200)
        models = response.json()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["id"], "test_model")
        self.assertTrue(models[0]["is_primary"])

    def test_model_status(self):
        response = self.client.get("/api/models/status", params=self._auth_params())
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["primary_profile_id"], "test_model")
        self.assertFalse(data["cooling_down"])

    def test_model_not_found(self):
        response = self.client.get("/api/models/nonexistent", params=self._auth_params())
        self.assertEqual(response.status_code, 404)

    def test_agents_list(self):
        response = self.client.get("/api/agents", params=self._auth_params())
        self.assertEqual(response.status_code, 200)
        agents = response.json()
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["id"], "general")

    def test_agent_active(self):
        response = self.client.get("/api/agents/active", params=self._auth_params())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "general")

    def test_agent_not_found(self):
        response = self.client.get("/api/agents/nonexistent", params=self._auth_params())
        self.assertEqual(response.status_code, 404)

    def test_chat_stream(self):
        conv_id = self._create_conversation()
        response = self.client.post(
            "/api/chat",
            params=self._auth_params(),
            json={"message": "你好", "conversation_id": conv_id},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        body = response.text
        self.assertIn('"type": "token"', body)
        self.assertIn('"type": "done"', body)
        self.mock_store.save_context.assert_called_once()

    def test_chat_requires_conversation(self):
        response = self.client.post(
            "/api/chat",
            params=self._auth_params(),
            json={"message": "你好"},
        )
        self.assertEqual(response.status_code, 400)

    def test_chat_history(self):
        conv_id = self._create_conversation()
        self.mock_store.load_context.return_value = [
            CanonicalMessage("user", "你好"),
            CanonicalMessage("assistant", "你好！"),
        ]
        response = self.client.get(
            "/api/chat/history",
            params=dict(self._auth_params(), conversation_id=conv_id),
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["messages"]), 2)

    def test_conversation_list_and_delete(self):
        conv_id = self._create_conversation()
        response = self.client.get("/api/chat/conversations", params=self._auth_params())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

        response = self.client.delete(
            "/api/chat/conversations/" + conv_id, params=self._auth_params()
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.get("/api/chat/conversations", params=self._auth_params())
        self.assertEqual(len(response.json()), 0)

    def test_page_chat(self):
        response = self.client.get("/", params=self._auth_params())
        self.assertEqual(response.status_code, 200)
        self.assertIn("chat-messages", response.text)

    def test_page_models(self):
        response = self.client.get("/models", params=self._auth_params())
        self.assertEqual(response.status_code, 200)
        self.assertIn("model-list", response.text)

    def test_page_agents(self):
        response = self.client.get("/agents", params=self._auth_params())
        self.assertEqual(response.status_code, 200)
        self.assertIn("agent-list", response.text)

    def test_cookie_set_on_token_param(self):
        response = self.client.get("/api/status", params=self._auth_params())
        self.assertIn("web_token", response.cookies)


if __name__ == "__main__":
    unittest.main()
