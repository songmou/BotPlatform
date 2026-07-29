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
        from pathlib import Path
        from src.core.services.auth import AdminAuthService
        from src.core.storage.admin_users import (
            AdminRoleStore,
            AdminSessionStore,
            AdminUserStore,
        )
        from src.core.storage.database import Database

        self.config = _make_config()
        self.fake_client = FakeClient()
        self.model_router = ModelRouter.single(self.fake_client)

        self.mock_registry = MagicMock()
        self.mock_registry.resolve.return_value = MagicMock(tenant_id="00000000-0000-0000-0000-000000000001")

        self.mock_store = MagicMock()
        self.mock_store.load_context.return_value = []

        self._db_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._db_dir.cleanup)
        database = Database(Path(self._db_dir.name) / "botplatform.sqlite3")
        self.admin_users = AdminUserStore(database)
        self.admin_roles = AdminRoleStore(database)
        self.admin_sessions = AdminSessionStore(database, b"test-secret")
        self.admin_auth = AdminAuthService(
            self.admin_users,
            self.admin_roles,
            self.admin_sessions,
            Path(self._db_dir.name),
        )
        admin_role = self.admin_roles.get_by_code("admin")
        self.admin_users.create("admin", "password12345", admin_role.role_id)

        self.app = create_app(
            self.config, self.model_router, self.mock_registry, self.mock_store,
            admin_auth=self.admin_auth,
            admin_user_store=self.admin_users,
            admin_role_store=self.admin_roles,
        )
        self.client = TestClient(self.app)
        response = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "password12345"},
        )
        assert response.status_code == 200, response.text

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
        return {}

    def _create_conversation(self):
        response = self.client.post("/api/chat/conversations", params=self._auth_params())
        self.assertEqual(response.status_code, 201)
        return response.json()["id"]

    def test_health_no_auth(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_status_requires_auth(self):
        from fastapi.testclient import TestClient as _TestClient

        anonymous = _TestClient(self.app)
        response = anonymous.get("/api/status")
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

    def test_login_sets_session_cookie(self):
        from fastapi.testclient import TestClient as _TestClient

        client = _TestClient(self.app)
        response = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "password12345"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("admin_session", response.cookies)
        self.assertEqual(response.json()["user"]["username"], "admin")

    def test_unauthenticated_page_redirects_to_login(self):
        from fastapi.testclient import TestClient as _TestClient

        anonymous = _TestClient(self.app)
        response = anonymous.get("/models", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["location"].startswith("/login"))

    def test_logout_invalidates_session(self):
        response = self.client.post("/api/auth/logout")
        self.assertEqual(response.status_code, 200)
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 401)


class AgentWriteApiTest(unittest.TestCase):
    """Write-path tests for /api/agents (create/update)."""

    def setUp(self):
        import src.api.routers.agents as agents_module
        from pathlib import Path

        # Reuse the exact same app/auth fixture as WebApiTest.
        WebApiTest.setUp(self)

        # Isolate agent JSON persistence so tests never touch config/agents/.
        self._agents_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._agents_dir.cleanup)
        agents_patcher = patch.object(
            agents_module, "AGENTS_DIR", Path(self._agents_dir.name)
        )
        agents_patcher.start()
        self.addCleanup(agents_patcher.stop)

    _auth_params = WebApiTest._auth_params

    def test_create_agent_and_read_back(self):
        body = {
            "id": "coder",
            "name": "编码助手",
            "role": "coder",
            "description": "帮助编写代码",
            "system_prompt": "你是编码助手。",
            "capabilities": [{"name": "写代码", "description": "编写 Python 代码"}],
            "tools": ["read_file"],
        }
        response = self.client.post("/api/agents", params=self._auth_params(), json=body)
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(data["id"], "coder")
        self.assertEqual(data["name"], "编码助手")
        self.assertEqual(data["role"], "coder")
        self.assertEqual(data["system_prompt"], "你是编码助手。")
        self.assertEqual(
            data["capabilities"],
            [{"name": "写代码", "description": "编写 Python 代码"}],
        )
        self.assertEqual(data["tools"], ["read_file"])

        # Read back through GET to confirm in-memory registration.
        response = self.client.get("/api/agents/coder", params=self._auth_params())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "编码助手")

    def test_update_agent_capabilities_non_empty(self):
        response = self.client.put(
            "/api/agents/general",
            params=self._auth_params(),
            json={"capabilities": [{"name": "翻译", "description": "中英互译"}]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["capabilities"],
            [{"name": "翻译", "description": "中英互译"}],
        )

    def test_update_agent_empty_capabilities_keeps_existing(self):
        # Current implementation contract: `if body.capabilities:` treats an
        # empty list as falsy, so [] is ignored and capabilities keep old values.
        response = self.client.put(
            "/api/agents/general",
            params=self._auth_params(),
            json={"capabilities": []},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["capabilities"],
            [{"name": "对话", "description": "通用对话"}],
        )

    def test_update_agent_omitted_capabilities_keeps_existing(self):
        response = self.client.put(
            "/api/agents/general",
            params=self._auth_params(),
            json={"name": "新名字"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["name"], "新名字")
        self.assertEqual(
            data["capabilities"],
            [{"name": "对话", "description": "通用对话"}],
        )


class McpApiTest(unittest.TestCase):
    """CRUD tests for /api/mcp with the config file redirected to a temp path."""

    def setUp(self):
        import src.api.routers.mcp as mcp_module
        import src.core.config.mcp_headers as mcp_headers_module
        from pathlib import Path

        # Reuse the exact same app/auth fixture as WebApiTest. tool_runtime is
        # None in this fixture, so _sync() skips the runtime manager reload.
        WebApiTest.setUp(self)

        # Redirect MCP_FILE to a temp file so config/mcp_servers.json is untouched.
        self._mcp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._mcp_dir.cleanup)
        self.mcp_file = Path(self._mcp_dir.name) / "mcp_servers.json"
        mcp_patcher = patch.object(mcp_module, "MCP_FILE", self.mcp_file)
        mcp_patcher.start()
        self.addCleanup(mcp_patcher.stop)

        self.mcp_headers_file = Path(self._mcp_dir.name) / "mcp_headers.json"
        headers_patcher = patch.object(
            mcp_headers_module, "MCP_HEADERS_FILE", self.mcp_headers_file
        )
        headers_patcher.start()
        self.addCleanup(headers_patcher.stop)

    _auth_params = WebApiTest._auth_params

    def _create_stdio_server(self, server_id="local_fs"):
        return self.client.post(
            "/api/mcp",
            params=self._auth_params(),
            json={
                "id": server_id,
                "name": "本地文件",
                "transport": "stdio",
                "command": "python",
                "args": ["-m", "some_server"],
            },
        )

    def test_list_servers_initially_empty(self):
        response = self.client.get("/api/mcp", params=self._auth_params())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_create_stdio_server(self):
        response = self._create_stdio_server()
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(data["id"], "local_fs")
        self.assertEqual(data["transport"], "stdio")
        self.assertEqual(data["command"], "python")
        self.assertEqual(data["args"], ["-m", "some_server"])
        self.assertTrue(data["enabled"])

        response = self.client.get("/api/mcp", params=self._auth_params())
        self.assertEqual(len(response.json()), 1)

    def test_create_streamablehttp_server(self):
        response = self.client.post(
            "/api/mcp",
            params=self._auth_params(),
            json={
                "id": "remote_api",
                "name": "远程服务",
                "transport": "streamablehttp",
                "url": "https://example.com/mcp",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(data["transport"], "streamablehttp")
        self.assertEqual(data["url"], "https://example.com/mcp")

    def test_create_streamablehttp_without_url_rejected(self):
        response = self.client.post(
            "/api/mcp",
            params=self._auth_params(),
            json={"id": "remote_api", "name": "远程服务", "transport": "streamablehttp"},
        )
        self.assertEqual(response.status_code, 400)

    def test_update_partial_preserves_unsubmitted_keys(self):
        import json as json_module

        # Seed the file with a custom key that no schema field covers;
        # update_server mutates the dict in place, so it must survive.
        self.mcp_file.write_text(
            json_module.dumps(
                {
                    "servers": [
                        {
                            "id": "local_fs",
                            "name": "本地文件",
                            "transport": "stdio",
                            "command": "python",
                            "args": [],
                            "env": {},
                            "headers_env": "MY_HEADERS",
                            "enabled": True,
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        response = self.client.put(
            "/api/mcp/local_fs",
            params=self._auth_params(),
            json={"name": "新名字"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["name"], "新名字")

        saved = json_module.loads(self.mcp_file.read_text(encoding="utf-8"))["servers"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["name"], "新名字")
        self.assertEqual(saved[0]["headers_env"], "MY_HEADERS")
        self.assertEqual(saved[0]["command"], "python")

    def test_update_missing_server_404(self):
        response = self.client.put(
            "/api/mcp/nonexistent",
            params=self._auth_params(),
            json={"name": "x"},
        )
        self.assertEqual(response.status_code, 404)

    def test_delete_server_then_404(self):
        response = self._create_stdio_server()
        self.assertEqual(response.status_code, 201, response.text)

        response = self.client.delete("/api/mcp/local_fs", params=self._auth_params())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

        response = self.client.delete("/api/mcp/local_fs", params=self._auth_params())
        self.assertEqual(response.status_code, 404)

        response = self.client.get("/api/mcp", params=self._auth_params())
        self.assertEqual(response.json(), [])

    def _create_http_server_with_headers(self):
        return self.client.post(
            "/api/mcp",
            params=self._auth_params(),
            json={
                "id": "remote_api",
                "name": "远程服务",
                "transport": "streamablehttp",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer secret-token"},
            },
        )

    def test_headers_stored_outside_config(self):
        import json as json_module

        response = self._create_http_server_with_headers()
        self.assertEqual(response.status_code, 201, response.text)

        saved = json_module.loads(self.mcp_file.read_text(encoding="utf-8"))["servers"]
        self.assertEqual(saved[0]["headers"], {})
        self.assertNotIn("secret-token", self.mcp_file.read_text(encoding="utf-8"))

        self.assertIn(
            "secret-token", self.mcp_headers_file.read_text(encoding="utf-8")
        )

        response = self.client.get("/api/mcp", params=self._auth_params())
        self.assertEqual(
            response.json()[0]["headers"],
            {"Authorization": "Bearer secret-token"},
        )

    def test_update_headers_replaces_secret(self):
        self._create_http_server_with_headers()
        response = self.client.put(
            "/api/mcp/remote_api",
            params=self._auth_params(),
            json={"headers": {"Authorization": "Bearer new-token"}},
        )
        self.assertEqual(response.status_code, 200, response.text)

        content = self.mcp_headers_file.read_text(encoding="utf-8")
        self.assertIn("new-token", content)
        self.assertNotIn("secret-token", content)

        response = self.client.get("/api/mcp", params=self._auth_params())
        self.assertEqual(
            response.json()[0]["headers"], {"Authorization": "Bearer new-token"}
        )

    def test_update_without_headers_preserves_secret(self):
        self._create_http_server_with_headers()
        response = self.client.put(
            "/api/mcp/remote_api",
            params=self._auth_params(),
            json={"name": "新名字"},
        )
        self.assertEqual(response.status_code, 200, response.text)

        response = self.client.get("/api/mcp", params=self._auth_params())
        self.assertEqual(
            response.json()[0]["headers"],
            {"Authorization": "Bearer secret-token"},
        )

    def test_delete_server_removes_secret(self):
        from src.core.config.mcp_headers import load_headers

        self._create_http_server_with_headers()
        response = self.client.delete(
            "/api/mcp/remote_api", params=self._auth_params()
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(load_headers("remote_api"), {})


if __name__ == "__main__":
    unittest.main()
