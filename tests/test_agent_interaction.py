"""智能体交互用例测试套件。

覆盖智能体与系统中各模块（内置 Tools、插件 Plugins、MCP、Skill、脚本 Scripts、
数据库 DataSources）的交互：验证智能体能够正确地调用这些工具并拿到预期反馈
（tool_result 回灌、审批流、系统提示词注入、子进程结果等）。

约定：
- 复用 ``test_tool_agent_loop`` 的 FakeToolOllama / tool_call 模式，用脚本化响应驱动
  真实 AgentService 工具循环，断言工具结果回灌到模型。
- 除 MCP 真实调用（依赖外网，不可达时优雅 skip）外，全部离线、无密钥依赖。
- 用标准库 unittest 运行：``python -m unittest tests.test_agent_interaction -v``。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from src.core.config.loader import load_project_config, ScriptDefinition
from src.core.services.agent import AgentService
from src.core.services.agent_tools import build_system_prompt
from src.core.datasource.gateway import compile_readonly, DataSourceError
from src.core.integrations.ilink import Credentials
from src.core.modeling import (
    CanonicalMessage,
    CanonicalToolCall,
    ModelCapabilities,
    ModelIdentity,
    ModelResponse,
)
from src.core.plugins.base import PluginContext
from src.core.plugins.catalog import PluginCatalog
from src.core.plugins.todo import TodoPlugin
from src.core.services.notification import TenantRecipientStore
from src.core.services.script import ScriptService
from src.core.storage.tenants import IntegrationStore, TenantRegistry
from src.core.tooling import ApprovalRequired, FinalAnswer, ToolRuntime
from src.core.tooling.mcp_client import McpClientManager


SOURCE_CONFIG = Path(__file__).resolve().parents[1] / "config"
REPO_ROOT = SOURCE_CONFIG.parent


class FakeToolOllama:
    """假模型：按预设顺序吐出响应（工具调用或纯文本），并把每次请求记录下来。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    identity = ModelIdentity("test", "fake", "fake-model")
    capabilities = ModelCapabilities(tools=True, vision=True, reasoning=True)

    def complete(self, request):
        self.calls.append(request)
        if not self.responses:
            return ModelResponse(
                CanonicalMessage("assistant", "无工具回答"),
                actual_model="fake-model",
            )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return ModelResponse(response, actual_model=self.identity.configured_model)


def tool_call(name, arguments):
    return CanonicalMessage(
        "assistant",
        tool_calls=[CanonicalToolCall("call-1", name, arguments)],
    )


class InteractionTestBase(unittest.TestCase):
    """共享脚手架：加载真实 config，准备临时工作目录与基础 ToolRuntime。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.root.mkdir()
        self.config = load_project_config(SOURCE_CONFIG)
        self.tool_config = replace(
            self.config.tools,
            default_working_directory=str(self.root),
            allowed_roots=[str(self.root)],
            denied_globs=[".env", "**/.env", ".git/**", "**/.git/**"],
        )
        self.basic_runtime = ToolRuntime(
            self.tool_config,
            "Asia/Shanghai",
            trash_directory=Path(self.temp.name) / "trash",
            sandbox_available=True,
        )

    def agent_service(self, ollama, runtime=None, agents=None, skills=None):
        runtime = runtime or self.basic_runtime
        agents = agents if agents is not None else self.config.agents
        service = AgentService(
            ollama,
            self.config.app,
            agents,
            tool_runtime=runtime,
            skills=skills,
        )
        return service, ollama

    def tool_messages(self, ollama):
        """Collect every tool-role message recorded across model calls."""
        msgs = []
        for call in ollama.calls:
            for message in call.messages:
                if message.role == "tool":
                    msgs.append(message)
        return msgs

    # ---- 插件（todo）复用的运行时装配（来自 test_tool_agent_loop）----

    def todo_runtime(self):
        registry = TenantRegistry(Path(self.temp.name) / "todo-data")
        tenant = registry.resolve("bot", "todo-user")
        todo = TodoPlugin(
            {},
            context=PluginContext(self.root, registry, timezone="Asia/Shanghai"),
        )
        runtime = ToolRuntime(
            self.tool_config,
            "Asia/Shanghai",
            trash_directory=Path(self.temp.name) / "trash",
            sandbox_available=True,
            tenant_registry=registry,
            plugins=[todo],
        )
        return runtime, registry, tenant, todo


# --------------------------------------------------------------------------- #
# A. 内置 Tools 交互
# --------------------------------------------------------------------------- #


class ToolsInteractionTests(InteractionTestBase):
    def test_get_current_time_feedback(self) -> None:
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("get_current_time", {}),
                    CanonicalMessage("assistant", "已查询当前时间。"),
                ]
            )
        )
        outcome = service.chat("user", "现在几点？")
        self.assertIsInstance(outcome, FinalAnswer)
        tool_msg = self.tool_messages(ollama)[-1]
        self.assertEqual(tool_msg.role, "tool")
        self.assertIn("iso", tool_msg.content)
        self.assertEqual(tool_msg.tool_call_id, "call-1")

    def test_list_directory_feedback(self) -> None:
        (self.root / "actual.txt").write_text("真实文件", encoding="utf-8")
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("list_directory", {"path": "."}),
                    CanonicalMessage("assistant", "目录里有 actual.txt。"),
                ]
            )
        )
        outcome = service.chat("user", "当前目录有什么文件？")
        self.assertIsInstance(outcome, FinalAnswer)
        self.assertIn("actual.txt", outcome.text)
        self.assertIn("actual.txt", self.tool_messages(ollama)[-1].content)

    def test_read_text_file_feedback(self) -> None:
        (self.root / "note.txt").write_text("文件内容123", encoding="utf-8")
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("read_text_file", {"path": "note.txt"}),
                    CanonicalMessage("assistant", "已读取文件内容。"),
                ]
            )
        )
        outcome = service.chat("user", "读取 note.txt")
        self.assertIsInstance(outcome, FinalAnswer)
        self.assertIn("文件内容123", self.tool_messages(ollama)[-1].content)

    def test_write_requires_approval_then_feedback(self) -> None:
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call(
                        "write_text_file",
                        {"path": "approved.txt", "content": "ok\n", "mode": "create"},
                    ),
                    CanonicalMessage("assistant", "文件已经创建。"),
                ]
            )
        )
        pending = service.chat("user", "创建文件")
        self.assertIsInstance(pending, ApprovalRequired)
        self.assertIn("同意", pending.text)
        self.assertFalse((self.root / "approved.txt").exists())
        final = service.resolve_pending_approval("user", True)
        self.assertEqual(final.text, "文件已经创建。")
        self.assertEqual(
            (self.root / "approved.txt").read_text(encoding="utf-8"), "ok\n"
        )

    def test_unknown_tool_error_feedback(self) -> None:
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("not_allowed", {}),
                    CanonicalMessage("assistant", "该工具不可用。"),
                ]
            )
        )
        outcome = service.chat("user", "调用未知工具")
        self.assertEqual(outcome.text, "该工具不可用。")
        self.assertIn("未授权工具", self.tool_messages(ollama)[-1].content)


# --------------------------------------------------------------------------- #
# B. 插件 Plugins 交互
# --------------------------------------------------------------------------- #


class PluginInteractionTests(InteractionTestBase):
    def test_todo_plugin_tool_direct_response(self) -> None:
        runtime, _registry, tenant, _todo = self.todo_runtime()
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("todo_manage", {"action": "add", "title": "不要让模型改写"}),
                    CanonicalMessage("assistant", "错误的二次改写"),
                ]
            ),
            runtime=runtime,
        )
        outcome = service.chat(tenant, "新增待办：不要让模型改写")
        # direct_response：直达答复，不经过模型二次改写
        self.assertEqual(len(ollama.calls), 1)
        self.assertIn("已新增待办", outcome.text)
        self.assertIn("不要让模型改写", outcome.text)
        self.assertNotIn("错误的二次改写", outcome.text)

    def test_plugin_catalog_discovers_bundled_plugins(self) -> None:
        catalog = PluginCatalog.discover(REPO_ROOT)
        self.assertIn("todo", catalog.manifests)
        self.assertIn("web_research", catalog.manifests)
        web_tools = catalog.manifests["web_research"].tools
        self.assertTrue(any(name.startswith("web_") for name in web_tools))
        # 插件工具被声明，智能体即可通过 ToolRuntime.schemas 暴露给模型
        todo_name = catalog.manifests["todo"].id
        self.assertEqual(todo_name, "todo")


# --------------------------------------------------------------------------- #
# C. MCP 交互（离线命名空间分发 + 真实调用 mcp_list）
# --------------------------------------------------------------------------- #


class FakeMcpManager:
    """离线仿真 McpClientManager：模拟 mcp_list 暴露 mcp_list__ping。"""

    def __init__(self, server_id="mcp_list", tool="ping"):
        self._server = server_id
        self._ns = "{}__{}".format(server_id, tool)
        self.calls = []

    def server_ids(self):
        return [self._server]

    def tool_names(self, server_id=None):
        if server_id is None:
            return [self._ns]
        return [self._ns] if server_id == self._server else []

    def has_tool(self, name):
        return name == self._ns

    def is_available(self, name):
        return name == self._ns

    def tool_schema(self, name):
        if name != self._ns:
            return None
        return {"description": "离线 ping 测试", "parameters": {"type": "object", "properties": {}}}

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return "pong:{}".format(name)


class McpInteractionTests(InteractionTestBase):
    def test_offline_namespace_dispatch_via_chat(self) -> None:
        fake = FakeMcpManager()
        runtime = ToolRuntime(
            self.tool_config,
            "Asia/Shanghai",
            trash_directory=Path(self.temp.name) / "trash",
            sandbox_available=True,
            mcp_manager=fake,
        )
        general = self.config.agents[self.config.app.default_agent]
        mcp_agent = replace(general, id="mcp_agent", mcp_servers=["mcp_list"])
        agents = {**self.config.agents, "mcp_agent": mcp_agent}
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("mcp_list__ping", {}),
                    CanonicalMessage("assistant", "已收到 MCP 返回。"),
                ]
            ),
            runtime=runtime,
            agents=agents,
        )
        outcome = service.chat("user", "ping 一下 mcp", agent_id="mcp_agent")
        self.assertIsInstance(outcome, FinalAnswer)
        tool_msg = self.tool_messages(ollama)[-1]
        self.assertEqual(tool_msg.role, "tool")
        self.assertIn("pong", tool_msg.content)
        self.assertEqual(fake.calls, [("mcp_list__ping", {})])

    def test_real_mcp_connection_and_invoke(self) -> None:
        manager = McpClientManager()
        manager.start()
        self.addCleanup(manager.close)
        try:
            manager.reload(self.config.mcp_servers)
        except Exception:  # noqa: BLE001 - 连接失败不应让测试崩溃
            pass

        connected = False
        for _ in range(16):  # 最多 ~8s
            if "mcp_list" in manager.server_ids():
                connected = True
                break
            time.sleep(0.5)

        if not connected:
            self.skipTest("MCP 服务器 mcp_list 不可达（离线环境），跳过真实调用")

        namespaced = manager.tool_names("mcp_list")
        self.assertTrue(namespaced, "已连接的 MCP 服务器应暴露工具")
        target = namespaced[0]
        try:
            result = manager.call_tool(target, {})
        except Exception as exc:  # 拿到错误反馈也算“有反馈”
            self.fail("MCP 工具调用异常：{}".format(exc))
        self.assertIsNotNone(result, "MCP 工具调用应返回结果/反馈")


# --------------------------------------------------------------------------- #
# D. Skill 交互
# --------------------------------------------------------------------------- #


class SkillInteractionTests(InteractionTestBase):
    def test_enabled_skill_injected_into_prompt(self) -> None:
        general = self.config.agents[self.config.app.default_agent]
        agent = replace(general, id="skill_agent", skills=["ops_script_automation"])
        prompt = build_system_prompt(agent, self.config.skills, self.basic_runtime)
        self.assertIn("# Skill: 运维脚本自动化", prompt)

    def test_disabled_skill_not_injected(self) -> None:
        general = self.config.agents[self.config.app.default_agent]
        agent = replace(general, id="skill_agent2", skills=["structured_output"])
        prompt = build_system_prompt(agent, self.config.skills, self.basic_runtime)
        self.assertNotIn("# Skill: structured_output", prompt)

    def test_skill_reaches_model_context_via_chat(self) -> None:
        general = self.config.agents[self.config.app.default_agent]
        skill_agent = replace(general, id="skill_agent", skills=["ops_script_automation"])
        agents = {**self.config.agents, "skill_agent": skill_agent}
        service, ollama = self.agent_service(
            FakeToolOllama([CanonicalMessage("assistant", "已按技能规范回答。")]),
            agents=agents,
            skills=self.config.skills,
        )
        outcome = service.chat("user", "帮我处理运维脚本", agent_id="skill_agent")
        self.assertIsInstance(outcome, FinalAnswer)
        system_text = "\n".join(
            m.content for m in ollama.calls[0].messages if m.role == "system"
        )
        self.assertIn("# Skill: 运维脚本自动化", system_text)


# --------------------------------------------------------------------------- #
# E. 脚本 Scripts 交互
# --------------------------------------------------------------------------- #


FAKE_SCRIPT = r'''from __future__ import annotations
import json
import os
import time
from pathlib import Path

time.sleep(0.1)
root = Path(os.environ["ILINKBOT_SCRIPT_DATA_ROOT"])
root.mkdir(parents=True, exist_ok=True)
target = Path(os.environ["ILINKBOT_SCRIPT_RESULT_FILE"])
temporary = target.with_suffix(target.suffix + ".tmp")
temporary.write_text(json.dumps({
    "status": "success",
    "summary": "脚本执行成功",
}), encoding="utf-8")
temporary.replace(target)
'''


class ScriptInteractionTests(InteractionTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.scripts_dir = Path(self.temp.name) / "scripts"
        self.scripts_dir.mkdir()
        self.entrypoint = self.scripts_dir / "fake.py"
        self.entrypoint.write_text(FAKE_SCRIPT, encoding="utf-8")
        self.registry = TenantRegistry(Path(self.temp.name) / "data")
        self.tenant = self.registry.resolve("bot", "script-user")
        self.store = TenantRecipientStore(self.registry)

    def make_script_service(self, requires_approval=True) -> ScriptService:
        definition = ScriptDefinition(
            id="fake",
            name="测试脚本",
            description="交互测试用脚本",
            entrypoint=str(self.entrypoint),
            timeout_seconds=10,
            requires_approval=requires_approval,
            parameters={},
            artifact_types=[],
        )
        service = ScriptService(
            {"fake": definition},
            Credentials("token", "https://gateway", "bot", "owner"),
            self.store,
            Path(self.temp.name),
            self.registry,
            IntegrationStore(self.registry),
            python_executable=sys.executable,
        )
        self.addCleanup(service.shutdown)
        return service

    def make_runtime(self, script_service) -> ToolRuntime:
        return ToolRuntime(
            self.tool_config,
            "Asia/Shanghai",
            trash_directory=Path(self.temp.name) / "trash",
            sandbox_available=True,
            tenant_registry=self.registry,
            script_service=script_service,
        )

    def wait_for(self, service, run_id, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = service.get_run(self.tenant, run_id)
            if result["status"] != "running":
                return result
            time.sleep(0.02)
        self.fail("脚本执行未在超时内完成")

    def _run_id_from_calls(self, ollama):
        for call in ollama.calls:
            for msg in call.messages:
                if msg.role == "tool" and "run_id" in msg.content:
                    return json.loads(msg.content)["data"]["run_id"]
        return None

    def test_run_script_triggers_real_subprocess_and_feedback(self) -> None:
        script_service = self.make_script_service(requires_approval=False)
        runtime = self.make_runtime(script_service)
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("run_script", {"script_id": "fake", "parameters": {}}),
                    tool_call("get_script_run", {"run_id": "PLACEHOLDER"}),
                    CanonicalMessage("assistant", "脚本结果已返回。"),
                ]
            ),
            runtime=runtime,
        )
        outcome = service.chat(self.tenant, "运行测试脚本")
        self.assertIsInstance(outcome, FinalAnswer)
        run_id = self._run_id_from_calls(ollama)
        self.assertIsNotNone(run_id, "run_script 应返回 run_id")
        result = self.wait_for(script_service, run_id)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["summary"], "脚本执行成功")

    def test_list_scripts_tool_feedback(self) -> None:
        script_service = self.make_script_service(requires_approval=False)
        runtime = self.make_runtime(script_service)
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("list_scripts", {}),
                    CanonicalMessage("assistant", "已列出脚本。"),
                ]
            ),
            runtime=runtime,
        )
        outcome = service.chat(self.tenant, "有哪些脚本？")
        self.assertIsInstance(outcome, FinalAnswer)
        self.assertIn("测试脚本", self.tool_messages(ollama)[-1].content)

    def test_script_requires_approval_flow(self) -> None:
        script_service = self.make_script_service(requires_approval=True)
        runtime = self.make_runtime(script_service)
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("run_script", {"script_id": "fake", "parameters": {}}),
                    CanonicalMessage("assistant", "脚本已执行。"),
                ]
            ),
            runtime=runtime,
        )
        pending = service.chat(self.tenant, "运行测试脚本")
        self.assertIsInstance(pending, ApprovalRequired)
        self.assertIn("同意", pending.text)
        final = service.resolve_pending_approval(self.tenant, True)
        self.assertIsInstance(final, FinalAnswer)
        run_id = self._run_id_from_calls(ollama)
        self.assertIsNotNone(run_id)
        result = self.wait_for(script_service, run_id)
        self.assertEqual(result["status"], "success")


# --------------------------------------------------------------------------- #
# F. 数据库 DataSources 交互（只读校验，纯函数）
# --------------------------------------------------------------------------- #


class DataSourceInteractionTests(InteractionTestBase):
    def test_readonly_select_passes(self) -> None:
        safe_sql, used_tables, limit = compile_readonly(
            "SELECT id FROM users",
            dialect="postgres",
            allowed_tables={"public.users"},
            default_schema="public",
        )
        self.assertIn("public.users", used_tables)
        self.assertTrue(limit > 0)

    def test_insert_rejected(self) -> None:
        with self.assertRaises(DataSourceError):
            compile_readonly(
                "INSERT INTO users VALUES (1)",
                dialect="postgres",
                allowed_tables={"public.users"},
                default_schema="public",
            )

    def test_unauthorized_table_rejected(self) -> None:
        with self.assertRaises(DataSourceError):
            compile_readonly(
                "SELECT id FROM secret",
                dialect="postgres",
                allowed_tables={"public.users"},
                default_schema="public",
            )


if __name__ == "__main__":
    unittest.main()
