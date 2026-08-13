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
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from src.core.config.loader import load_project_config, ScriptDefinition
from src.core.services.agent import AgentService
from src.core.services.agent_tools import build_system_prompt, resolve_tool_names
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
from src.core.services.drive import DriveService
from src.core.services.knowledge import KnowledgeService
from src.core.services.organization_controls import OrganizationControlStore
from src.core.services.organization_schedule_tool import OrganizationScheduleToolService
from src.core.services.resources import ScopedResourceStore
from src.core.storage.organizations import OrganizationStore
from src.core.storage.tenants import TenantContext, TenantRegistry


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

    # ---- 知识库（KnowledgeService + 真实 SQLite）----
    def knowledge_runtime(self):
        registry = TenantRegistry(Path(self.temp.name) / "kb-data")
        tenant = registry.resolve("bot", "kb-user")
        kb = KnowledgeService(registry)
        runtime = ToolRuntime(
            self.tool_config,
            "Asia/Shanghai",
            trash_directory=Path(self.temp.name) / "trash",
            sandbox_available=True,
            tenant_registry=registry,
            knowledge_service=kb,
        )
        return runtime, registry, tenant, kb

    # ---- 网盘（DriveService + 真实文件系统）----
    def drive_runtime(self):
        registry = TenantRegistry(Path(self.temp.name) / "drive-data")
        tenant = registry.resolve("bot", "drive-user")
        public_root = Path(self.temp.name) / "public"
        drive = DriveService(registry, public_root)
        runtime = ToolRuntime(
            self.tool_config,
            "Asia/Shanghai",
            trash_directory=Path(self.temp.name) / "trash",
            sandbox_available=True,
            tenant_registry=registry,
            drive_service=drive,
        )
        return runtime, registry, tenant, drive

    # ---- 定时任务（真实 Organization 存储 + 角色门控）----
    def schedule_service(self):
        registry = TenantRegistry(Path(self.temp.name) / "schedule-data")
        tenant = registry.resolve("bot", "schedule-user")
        org_store = OrganizationStore(registry)
        resource_store = ScopedResourceStore(org_store)
        control = OrganizationControlStore(org_store, resource_store, self.config)
        schedule_tool = OrganizationScheduleToolService(control, org_store, self.config)
        runtime = ToolRuntime(
            self.tool_config,
            "Asia/Shanghai",
            trash_directory=Path(self.temp.name) / "trash",
            sandbox_available=True,
            tenant_registry=registry,
            organization_schedule_service=schedule_tool,
        )
        return runtime, registry, tenant, schedule_tool


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


# --------------------------------------------------------------------------- #
# A2. 内置 Tools 全集（天然真实：真读写临时目录 / 真调 OS / 真子进程）
# --------------------------------------------------------------------------- #


class ToolsInteractionExtraTests(InteractionTestBase):
    def test_readonly_system_tools_feedback(self) -> None:
        # get_system_info / list_allowed_roots / get_disk_usage / list_processes
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("get_system_info", {}),
                    tool_call("list_allowed_roots", {}),
                    tool_call("get_current_time", {}),
                    CanonicalMessage("assistant", "系统信息已获取。"),
                ]
            )
        )
        outcome = service.chat("user", "查看本机信息")
        self.assertIsInstance(outcome, FinalAnswer)
        joined = "\n".join(m.content for m in self.tool_messages(ollama))
        self.assertIn("hostname", joined)
        self.assertNotIn("environ", joined.lower(), "系统信息不应泄露环境变量")
        self.assertIn("default_working_directory", joined)

    def test_disk_and_processes_feedback(self) -> None:
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("get_disk_usage", {"path": "."}),
                    tool_call("list_processes", {"limit": 10}),
                    CanonicalMessage("assistant", "磁盘与进程信息已获取。"),
                ]
            )
        )
        outcome = service.chat("user", "磁盘和进程")
        self.assertIsInstance(outcome, FinalAnswer)
        joined = "\n".join(m.content for m in self.tool_messages(ollama))
        self.assertIn("free", joined)
        # list_processes 在受限环境（如 macOS LaunchAgent 沙箱）会因 /bin/ps
        # 权限不足而返回错误。此时仅强断言磁盘部分，进程部分降级 skip。
        if "processes" not in joined:
            if sys.platform == "darwin":
                self.skipTest("list_processes 需要 /bin/ps 权限，当前环境受限")
            self.fail("list_processes 未返回进程列表：{}".format(joined))

    def test_find_files_and_search_text_feedback(self) -> None:
        (self.root / "secret_notes.txt").write_text("内部密钥 key=ABC123", encoding="utf-8")
        (self.root / "readme.md").write_text("hello world", encoding="utf-8")
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("find_files", {"query": "*.txt"}),
                    tool_call("search_text", {"query": "密钥"}),
                    CanonicalMessage("assistant", "已找到文件与命中行。"),
                ]
            )
        )
        outcome = service.chat("user", "找出 txt 并搜索密钥")
        self.assertIsInstance(outcome, FinalAnswer)
        joined = "\n".join(m.content for m in self.tool_messages(ollama))
        self.assertIn("secret_notes.txt", joined)
        self.assertIn("ABC123", joined)

    def test_get_path_info_feedback(self) -> None:
        (self.root / "note.txt").write_text("12345", encoding="utf-8")
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("get_path_info", {"path": "note.txt"}),
                    CanonicalMessage("assistant", "已获取文件信息。"),
                ]
            )
        )
        outcome = service.chat("user", "note.txt 的信息")
        self.assertIsInstance(outcome, FinalAnswer)
        self.assertIn('"type": "file"', self.tool_messages(ollama)[-1].content)

    def test_create_directory_requires_approval(self) -> None:
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("create_directory", {"path": "sub"}),
                    CanonicalMessage("assistant", "目录已创建。"),
                ]
            )
        )
        pending = service.chat("user", "新建 sub 目录")
        self.assertIsInstance(pending, ApprovalRequired)
        self.assertFalse((self.root / "sub").exists())
        final = service.resolve_pending_approval("user", True)
        self.assertIsInstance(final, FinalAnswer)
        self.assertTrue((self.root / "sub").is_dir())

    def test_replace_text_requires_approval(self) -> None:
        (self.root / "a.txt").write_text("old-content", encoding="utf-8")
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call(
                        "replace_text",
                        {"path": "a.txt", "old_text": "old", "new_text": "new"},
                    ),
                    CanonicalMessage("assistant", "已替换。"),
                ]
            )
        )
        pending = service.chat("user", "把 old 换成 new")
        self.assertIsInstance(pending, ApprovalRequired)
        final = service.resolve_pending_approval("user", True)
        self.assertIsInstance(final, FinalAnswer)
        self.assertEqual(
            (self.root / "a.txt").read_text(encoding="utf-8"), "new-content"
        )

    def test_copy_and_move_and_trash_approval_flows(self) -> None:
        # copy
        (self.root / "src.txt").write_text("copy-me", encoding="utf-8")
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("copy_path", {"source": "src.txt", "destination": "dst.txt"}),
                    CanonicalMessage("assistant", "已复制。"),
                ]
            )
        )
        pending = service.chat("user", "复制 src 到 dst")
        self.assertIsInstance(pending, ApprovalRequired)
        self.assertFalse((self.root / "dst.txt").exists())
        service.resolve_pending_approval("user", True)
        self.assertTrue((self.root / "dst.txt").exists())

        # move
        service2, _ = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("move_path", {"source": "dst.txt", "destination": "moved.txt"}),
                    CanonicalMessage("assistant", "已移动。"),
                ]
            )
        )
        pending2 = service2.chat("user", "移动 dst 到 moved")
        self.assertIsInstance(pending2, ApprovalRequired)
        service2.resolve_pending_approval("user", True)
        self.assertFalse((self.root / "dst.txt").exists())
        self.assertTrue((self.root / "moved.txt").exists())

        # trash
        service3, _ = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("move_to_trash", {"path": "moved.txt"}),
                    CanonicalMessage("assistant", "已移入废纸篓。"),
                ]
            )
        )
        pending3 = service3.chat("user", "删除 moved")
        self.assertIsInstance(pending3, ApprovalRequired)
        service3.resolve_pending_approval("user", True)
        self.assertFalse((self.root / "moved.txt").exists())
        self.assertTrue(list(Path(self.temp.name).glob("trash/*-moved.txt")))

    def test_run_command_python_real_stdout(self) -> None:
        if sys.platform != "darwin" or "python" not in self.tool_config.enabled_command_profiles:
            self.skipTest("run_command 需要 macOS 沙箱且 python profile 已启用")
        # python 档案禁止 -c，只能运行工作目录中的脚本文件
        (self.root / "compute.py").write_text(
            "print('RC:' + str(1 + 1))\n", encoding="utf-8"
        )
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("run_command", {"profile": "python", "args": ["compute.py"]}),
                    CanonicalMessage("assistant", "命令已执行。"),
                ]
            )
        )
        outcome = service.chat("user", "运行 compute.py")
        # 审批策略因运行环境而异：若进入审批流，先同意再取最终结果；
        # 若平台策略直接放行，则已经是最终答复。两条路径都执行了真实子进程。
        if isinstance(outcome, ApprovalRequired):
            final = service.resolve_pending_approval("user", True)
        else:
            final = outcome
        self.assertIsInstance(final, FinalAnswer)
        joined = "\n".join(m.content for m in self.tool_messages(ollama))
        # 受限 macOS 环境（如 CI / 已处于沙箱中）sandbox-exec 无法应用策略，
        # 此时命令无法真正执行，降级 skip 而非误报失败。
        if "sandbox_apply" in joined or "Operation not permitted" in joined:
            self.skipTest("run_command 需要可用的 macOS sandbox-exec，当前环境无法应用沙箱策略")
        self.assertIn("RC:2", joined)

    def test_multi_tool_round_then_synthesis(self) -> None:
        (self.root / "actual.txt").write_text("真实文件", encoding="utf-8")
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("list_directory", {"path": "."}),
                    tool_call("get_current_time", {}),
                    CanonicalMessage("assistant", "目录已列出，当前时间已查询。"),
                ]
            )
        )
        outcome = service.chat("user", "看目录并报时间")
        self.assertIsInstance(outcome, FinalAnswer)
        msgs = self.tool_messages(ollama)
        self.assertTrue(any("actual.txt" in m.content for m in msgs))
        self.assertTrue(any("iso" in m.content for m in msgs))

    def test_disabled_tool_returns_disabled_message(self) -> None:
        runtime = ToolRuntime(
            self.tool_config,
            "Asia/Shanghai",
            trash_directory=Path(self.temp.name) / "trash",
            sandbox_available=True,
            tool_states={"read_text_file": {"enabled": False}},
        )
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("read_text_file", {"path": "note.txt"}),
                    CanonicalMessage("assistant", "该工具不可用。"),
                ]
            ),
            runtime=runtime,
        )
        outcome = service.chat("user", "读 note.txt")
        self.assertEqual(outcome.text, "该工具不可用。")
        self.assertIn("工具已被禁用", self.tool_messages(ollama)[-1].content)


# --------------------------------------------------------------------------- #
# C. 真实 MCP（本地 stdio server，真实协议握手）
# --------------------------------------------------------------------------- #

MCP_SERVER_PATH = Path(__file__).resolve().parent / "_mcp_stdio_server.py"


class McpRealInteractionTests(InteractionTestBase):
    def _start_manager(self) -> McpClientManager:
        manager = McpClientManager()
        manager.start()
        self.addCleanup(manager.close)
        manager.connect_server(
            {
                "id": "local_echo",
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(MCP_SERVER_PATH)],
            }
        )
        for _ in range(40):
            if manager.tool_names("local_echo"):
                break
            time.sleep(0.25)
        self.assertTrue(
            manager.tool_names("local_echo"), "本地 stdio MCP 未暴露工具"
        )
        return manager

    def test_resolve_tool_names_expands_local_mcp(self) -> None:
        manager = self._start_manager()
        runtime = ToolRuntime(
            self.tool_config,
            "Asia/Shanghai",
            trash_directory=Path(self.temp.name) / "trash",
            sandbox_available=True,
            mcp_manager=manager,
        )
        general = self.config.agents[self.config.app.default_agent]
        agent = replace(general, id="mcp_real", mcp_servers=["local_echo"])
        names = resolve_tool_names(agent, runtime)
        self.assertTrue(any(n.startswith("local_echo__") for n in names))

    def test_local_mcp_echo_via_chat(self) -> None:
        manager = self._start_manager()
        runtime = ToolRuntime(
            self.tool_config,
            "Asia/Shanghai",
            trash_directory=Path(self.temp.name) / "trash",
            sandbox_available=True,
            mcp_manager=manager,
        )
        general = self.config.agents[self.config.app.default_agent]
        agent = replace(general, id="mcp_real", mcp_servers=["local_echo"])
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("local_echo__echo", {"text": "hello"}),
                    CanonicalMessage("assistant", "已回显。"),
                ]
            ),
            runtime=runtime,
            agents={**self.config.agents, "mcp_real": agent},
        )
        outcome = service.chat("user", "回显 hello", agent_id="mcp_real")
        self.assertIsInstance(outcome, FinalAnswer)
        self.assertTrue(
            any("echo:hello" in m.content for m in self.tool_messages(ollama))
        )

    def test_local_mcp_add_via_chat(self) -> None:
        manager = self._start_manager()
        runtime = ToolRuntime(
            self.tool_config,
            "Asia/Shanghai",
            trash_directory=Path(self.temp.name) / "trash",
            sandbox_available=True,
            mcp_manager=manager,
        )
        general = self.config.agents[self.config.app.default_agent]
        agent = replace(general, id="mcp_real", mcp_servers=["local_echo"])
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("local_echo__add", {"a": 2, "b": 3}),
                    CanonicalMessage("assistant", "已相加。"),
                ]
            ),
            runtime=runtime,
            agents={**self.config.agents, "mcp_real": agent},
        )
        outcome = service.chat("user", "算 2+3", agent_id="mcp_real")
        self.assertIsInstance(outcome, FinalAnswer)
        self.assertTrue(
            any("5" in m.content for m in self.tool_messages(ollama))
        )

    def test_local_mcp_boom_error_feedback(self) -> None:
        manager = self._start_manager()
        with self.assertRaises(RuntimeError):
            manager.call_tool("local_echo__boom", {})


# --------------------------------------------------------------------------- #
# G. 知识库 Knowledge（真实 KnowledgeService + SQLite）
# --------------------------------------------------------------------------- #

KNOWLEDGE_TOOLS = [
    "knowledge_add_text",
    "knowledge_index_file",
    "knowledge_search",
    "knowledge_list",
    "knowledge_delete",
]


class KnowledgeInteractionTests(InteractionTestBase):
    def _kb_agent(self):
        general = self.config.agents[self.config.app.default_agent]
        return replace(
            general,
            id="kb_agent",
            tools=list(dict.fromkeys(list(general.tools) + KNOWLEDGE_TOOLS)),
        )

    def test_add_requires_approval_then_real_insert(self) -> None:
        runtime, _registry, tenant, kb = self.knowledge_runtime()
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call(
                        "knowledge_add_text",
                        {"name": "机密笔记", "content": "内部密码是 PROJECT-SECRET-XYZ"},
                    ),
                    CanonicalMessage("assistant", "已记录。"),
                ]
            ),
            runtime=runtime,
            agents={**self.config.agents, "kb_agent": self._kb_agent()},
        )
        pending = service.chat(tenant, "记住这段机密", agent_id="kb_agent")
        self.assertIsInstance(pending, ApprovalRequired)
        self.assertIn("同意", pending.text)
        final = service.resolve_pending_approval(tenant, True)
        self.assertIsInstance(final, FinalAnswer)
        sources = kb.list(tenant.tenant_id)
        self.assertTrue(any(s["name"] == "机密笔记" for s in sources))

    def test_search_and_list_via_chat(self) -> None:
        runtime, _registry, tenant, kb = self.knowledge_runtime()
        kb.add_text(tenant.tenant_id, "机密笔记", "内部密码是 PROJECT-SECRET-XYZ")
        # list
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("knowledge_list", {}),
                    CanonicalMessage("assistant", "已列出。"),
                ]
            ),
            runtime=runtime,
            agents={**self.config.agents, "kb_agent": self._kb_agent()},
        )
        outcome = service.chat(tenant, "列出知识", agent_id="kb_agent")
        self.assertIsInstance(outcome, FinalAnswer)
        self.assertTrue(
            any("机密笔记" in m.content for m in self.tool_messages(ollama))
        )
        # search
        service2, ollama2 = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("knowledge_search", {"query": "PROJECT-SECRET-XYZ"}),
                    CanonicalMessage("assistant", "已检索。"),
                ]
            ),
            runtime=runtime,
            agents={**self.config.agents, "kb_agent": self._kb_agent()},
        )
        outcome2 = service2.chat(tenant, "搜索机密", agent_id="kb_agent")
        self.assertIsInstance(outcome2, FinalAnswer)
        self.assertTrue(
            any(
                "PROJECT-SECRET-XYZ" in m.content
                for m in self.tool_messages(ollama2)
            )
        )

    def test_delete_requires_approval(self) -> None:
        runtime, _registry, tenant, kb = self.knowledge_runtime()
        added = kb.add_text(tenant.tenant_id, "待删笔记", "临时内容")
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("knowledge_delete", {"source_id": added["source_id"]}),
                    CanonicalMessage("assistant", "已删除。"),
                ]
            ),
            runtime=runtime,
            agents={**self.config.agents, "kb_agent": self._kb_agent()},
        )
        pending = service.chat(tenant, "删除待删笔记", agent_id="kb_agent")
        self.assertIsInstance(pending, ApprovalRequired)
        final = service.resolve_pending_approval(tenant, True)
        self.assertIsInstance(final, FinalAnswer)
        self.assertFalse(
            any(s["source_id"] == added["source_id"] for s in kb.list(tenant.tenant_id))
        )


# --------------------------------------------------------------------------- #
# H. 网盘 Drive（真实 DriveService + 文件系统）
# --------------------------------------------------------------------------- #

DRIVE_TOOLS = [
    "drive_list_files",
    "drive_read_file",
    "drive_save_file",
    "drive_delete_file",
]


class DriveInteractionTests(InteractionTestBase):
    def _drive_agent(self):
        general = self.config.agents[self.config.app.default_agent]
        return replace(
            general,
            id="drive_agent",
            tools=list(dict.fromkeys(list(general.tools) + DRIVE_TOOLS)),
        )

    def test_save_list_read_roundtrip(self) -> None:
        runtime, _registry, tenant, _drive = self.drive_runtime()
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call(
                        "drive_save_file",
                        {"path": "notes.txt", "content": "网盘内容123"},
                    ),
                    tool_call("drive_list_files", {"scope": "tenant"}),
                    tool_call("drive_read_file", {"scope": "tenant", "path": "notes.txt"}),
                    CanonicalMessage("assistant", "已保存、列出并读取。"),
                ]
            ),
            runtime=runtime,
            agents={**self.config.agents, "drive_agent": self._drive_agent()},
        )
        outcome = service.chat(tenant, "存并读 notes.txt", agent_id="drive_agent")
        self.assertIsInstance(outcome, FinalAnswer)
        joined = "\n".join(m.content for m in self.tool_messages(ollama))
        self.assertIn("notes.txt", joined)
        self.assertIn("网盘内容123", joined)

    def test_delete_requires_approval(self) -> None:
        runtime, _registry, tenant, _drive = self.drive_runtime()
        # 先保存（非审批）
        save_service, _ = self.agent_service(
            FakeToolOllama(
                [
                    tool_call(
                        "drive_save_file",
                        {"path": "to_delete.txt", "content": "bye"},
                    ),
                    CanonicalMessage("assistant", "已保存。"),
                ]
            ),
            runtime=runtime,
            agents={**self.config.agents, "drive_agent": self._drive_agent()},
        )
        save_service.chat(tenant, "保存 to_delete.txt", agent_id="drive_agent")
        # 再删除（审批）
        del_service, _ = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("drive_delete_file", {"path": "to_delete.txt"}),
                    CanonicalMessage("assistant", "已删除。"),
                ]
            ),
            runtime=runtime,
            agents={**self.config.agents, "drive_agent": self._drive_agent()},
        )
        pending = del_service.chat(tenant, "删除 to_delete.txt", agent_id="drive_agent")
        self.assertIsInstance(pending, ApprovalRequired)
        final = del_service.resolve_pending_approval(tenant, True)
        self.assertIsInstance(final, FinalAnswer)


# --------------------------------------------------------------------------- #
# I. 定时任务 Schedule（真实 Organization 存储 + 角色门控）
# --------------------------------------------------------------------------- #


class ScheduleInteractionTests(InteractionTestBase):
    def _sched_agent(self):
        general = self.config.agents[self.config.app.default_agent]
        return replace(
            general,
            id="sched_agent",
            tools=list(
                dict.fromkeys(
                    list(general.tools)
                    + ["list_script_schedules", "manage_script_schedule"]
                )
            ),
        )

    def test_list_script_schedules_via_chat(self) -> None:
        runtime, _registry, tenant, _control = self.schedule_service()
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("list_script_schedules", {}),
                    CanonicalMessage("assistant", "已列出定时任务。"),
                ]
            ),
            runtime=runtime,
            agents={**self.config.agents, "sched_agent": self._sched_agent()},
        )
        outcome = service.chat(tenant, "列出定时任务", agent_id="sched_agent")
        self.assertIsInstance(outcome, FinalAnswer)
        self.assertTrue(
            any("schedules" in m.content for m in self.tool_messages(ollama))
        )

    def _seed_user(self, registry, user_id: int, username: str) -> None:
        """Seed the full FK chain behind a user (admin_roles -> admin_users -> users)."""
        ts = datetime.now(timezone.utc).isoformat()
        with registry.database.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO admin_roles(role_id, code, name) "
                "VALUES (1, 'tenant_user', 'Tenant User')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO admin_users("
                "user_id, username, password_hash, role_id, created_at) "
                "VALUES (?, ?, '', 1, ?)",
                (user_id, username, ts),
            )
            conn.execute(
                "INSERT OR IGNORE INTO users(user_id, display_name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, username, ts, ts),
            )

    def _seed_org(self, registry, org_id: str, owner_uid: int) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        self._seed_user(registry, owner_uid, "owner-user")
        with registry.database.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tenants(tenant_id, bot_id, user_id, created_at) "
                "VALUES (?, 'organization', ?, ?)",
                (org_id, "organization:{}".format(org_id), ts),
            )
            conn.execute(
                "INSERT OR IGNORE INTO organizations("
                "organization_id, name, status, legacy, created_at, updated_at) "
                "VALUES (?, ?, 'active', 0, ?, ?)",
                (org_id, "集成测试组织", ts, ts),
            )
            conn.execute(
                "INSERT OR IGNORE INTO organization_memberships("
                "membership_id, organization_id, user_id, role, status, "
                "created_at, updated_at) VALUES (?, ?, ?, 'owner', 'active', ?, ?)",
                (str(uuid.uuid4()), org_id, owner_uid, ts, ts),
            )

    def _seed_membership(
        self, registry, org_id: str, user_id: int, role: str
    ) -> None:
        self._seed_user(registry, user_id, "member-user")
        ts = datetime.now(timezone.utc).isoformat()
        with registry.database.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO organization_memberships("
                "membership_id, organization_id, user_id, role, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?)",
                (str(uuid.uuid4()), org_id, user_id, role, ts, ts),
            )

    def test_manage_schedule_create_and_role_gate(self) -> None:
        _runtime, registry, _tenant, schedule_tool = self.schedule_service()
        script_id = None
        for sid, definition in self.config.scripts.items():
            if getattr(definition, "enabled", False) and not getattr(
                definition, "requires_approval", True
            ):
                script_id = sid
                break
        if script_id is None:
            self.skipTest("没有可用于无人值守定时任务的平台脚本")

        # 直接写表建立组织 + 所有者（active）成员关系，并补齐 users/admin_users 外键链
        owner_uid = 91001
        org_id = str(uuid.uuid4())
        self._seed_org(registry, org_id, owner_uid)
        owner_tenant = TenantContext(
            tenant_id=org_id,
            bot_id="bot",
            user_id=str(owner_uid),
            member_user_id=owner_uid,
        )
        result = schedule_tool.manage(
            owner_tenant,
            {
                "action": "create",
                "schedule_id": "sched-x",
                "script_id": script_id,
                "crons": ["0 9 * * *"],
            },
        )
        self.assertEqual(result["action"], "create")
        created_id = result["schedule"]["schedule_id"]
        self.assertTrue(created_id, "创建应返回生成的 schedule_id")
        listed = schedule_tool.list_for_tenant(owner_tenant)
        self.assertTrue(
            any(s["schedule_id"] == created_id for s in listed)
        )

        # 非所有者（普通成员）应被拒绝
        member_uid = 91002
        self._seed_membership(registry, org_id, member_uid, "member")
        member_tenant = TenantContext(
            tenant_id=org_id,
            bot_id="bot",
            user_id=str(member_uid),
            member_user_id=member_uid,
        )
        with self.assertRaises(ValueError):
            schedule_tool.manage(
                member_tenant,
                {
                    "action": "create",
                    "schedule_id": "sched-y",
                    "script_id": script_id,
                    "crons": ["0 9 * * *"],
                },
            )


if __name__ == "__main__":
    unittest.main()
