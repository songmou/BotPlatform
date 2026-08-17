from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from src.core.services.agent import AgentService
from src.core.config.loader import load_project_config
from src.core.plugins.base import PluginContext
from src.core.plugins.todo import TodoPlugin
from src.core.modeling import (
    CanonicalMessage,
    CanonicalToolCall,
    ModelCapabilities,
    ModelError,
    ModelIdentity,
    ModelRouter,
    ModelResponse,
)
from src.core.tooling import ApprovalRequired, FinalAnswer, ToolError, ToolRuntime
from src.core.storage.tenants import TenantRegistry


SOURCE_CONFIG = Path(__file__).resolve().parents[1] / "config"


class FakeToolOllama:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    identity = ModelIdentity("test", "fake", "fake-model")
    capabilities = ModelCapabilities(tools=True, vision=True, reasoning=True)

    def complete(self, request):
        self.calls.append(replace(request, messages=list(request.messages)))
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


class AutoScriptService:
    script_ids = ["todo_manager"]
    definitions = {}

    def __init__(self):
        self.calls = []

    def requires_approval(self, script_id):
        return script_id != "todo_manager"

    def has_approval_required_scripts(self):
        return False

    def list_scripts(self):
        return [
            {
                "id": "todo_manager",
                "name": "待办提醒与管理",
                "requires_approval": False,
                "parameters": {},
            }
        ]

    def submit_for_tenant(self, tenant, script_id, parameters):
        self.calls.append((tenant, script_id, parameters))
        return {
            "run_id": "todo_manager-20260718T120000-12345678",
            "status": "running",
        }

    def get_run(self, tenant, run_id):
        return {
            "run_id": run_id,
            "status": "success",
            "summary": "【待办列表】未完成，共 0 项。",
        }


class IntegrationScriptService:
    script_ids = ["ctsehr_check", "ctsoa_check"]

    def __init__(self, ready=()):
        self.ready = set(ready)
        self.calls = []
        self.definitions = {
            script_id: SimpleNamespace(id=script_id, name=name, description="")
            for script_id, name in (
                ("ctsehr_check", "CTS EHR 考勤查询"),
                ("ctsoa_check", "CTS OA 待办查询"),
            )
        }

    def list_scripts(self):
        return [
            {
                "id": script_id,
                "name": definition.name,
                "requires_approval": False,
                "parameters": {},
            }
            for script_id, definition in self.definitions.items()
        ]

    def requires_approval(self, _script_id):
        return False

    def has_approval_required_scripts(self):
        return False

    def submit_for_tenant(self, tenant, script_id, parameters):
        self.calls.append((tenant, script_id, parameters))
        if script_id not in self.ready:
            integration_id = "ctsehr" if script_id == "ctsehr_check" else "ctsoa"
            raise ValueError(
                "尚未配置 {}，请先使用 /integration setup {}".format(
                    integration_id, integration_id
                )
            )
        return {
            "run_id": "{}-20260814T120000-12345678".format(script_id),
            "status": "running",
        }


class ToolAgentLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.root.mkdir()
        self.config = load_project_config(SOURCE_CONFIG)
        tool_config = replace(
            self.config.tools,
            default_working_directory=str(self.root),
            allowed_roots=[str(self.root)],
            denied_globs=[".env", "**/.env", ".git/**", "**/.git/**"],
        )
        self.runtime = ToolRuntime(
            tool_config,
            "Asia/Shanghai",
            trash_directory=Path(self.temp.name) / "trash",
            sandbox_available=True,
        )

    def service(self, responses):
        ollama = FakeToolOllama(responses)
        service = AgentService(
            ollama,
            self.config.app,
            self.config.agents,
            tool_runtime=self.runtime,
        )
        return service, ollama

    def todo_service(self, responses):
        registry = TenantRegistry(Path(self.temp.name) / "todo-data")
        tenant = registry.resolve("bot", "todo-user")
        todo = TodoPlugin(
            {},
            context=PluginContext(
                self.root,
                registry,
                timezone="Asia/Shanghai",
            ),
        )
        runtime = ToolRuntime(
            self.runtime.base_config,
            "Asia/Shanghai",
            trash_directory=Path(self.temp.name) / "trash",
            sandbox_available=True,
            tenant_registry=registry,
            plugins=[todo],
        )
        ollama = FakeToolOllama(responses)
        service = AgentService(
            ollama,
            self.config.app,
            self.config.agents,
            tool_runtime=runtime,
        )
        return service, ollama, tenant, todo

    def integration_service(self, responses, ready=()):
        scripts = IntegrationScriptService(ready)
        registry = TenantRegistry(Path(self.temp.name) / "integration-data")
        tenant = registry.resolve("bot", "integration-user")
        audits = []
        runtime = ToolRuntime(
            self.runtime.base_config,
            "Asia/Shanghai",
            trash_directory=Path(self.temp.name) / "trash",
            sandbox_available=True,
            script_service=scripts,
            tenant_registry=registry,
            audit_logger=lambda _context, name, *_args: audits.append(name),
        )
        self.runtime = runtime
        service, ollama = self.service(responses)
        return service, ollama, tenant, scripts, audits

    def test_explicit_ehr_query_bypasses_stale_model_history(self) -> None:
        service, ollama, tenant, scripts, audits = self.integration_service(
            [CanonicalMessage("assistant", "仍未配置")],
            ready={"ctsehr_check"},
        )
        service.histories[tenant.tenant_id] = [
            CanonicalMessage("user", "查看打卡"),
            CanonicalMessage("assistant", "尚未配置 ctsehr"),
        ]

        outcome = service.chat(tenant, "查看打卡")

        self.assertEqual(ollama.calls, [])
        self.assertIn("ctsehr_check-20260814T120000-12345678", outcome.text)
        self.assertEqual(
            scripts.calls, [(tenant, "ctsehr_check", {})]
        )
        self.assertEqual(audits, ["list_scripts", "run_script"])

    def test_ehr_query_rechecks_credentials_after_previous_failure(self) -> None:
        service, ollama, tenant, scripts, _audits = self.integration_service([])

        missing = service.chat(tenant, "查看打卡")
        scripts.ready.add("ctsehr_check")
        configured = service.chat(tenant, "查看打卡")

        self.assertEqual(ollama.calls, [])
        self.assertIn("尚未配置 ctsehr", missing.text)
        self.assertIn("已提交", configured.text)
        self.assertEqual(
            [script_id for _tenant, script_id, _parameters in scripts.calls],
            ["ctsehr_check", "ctsehr_check"],
        )

    def test_oa_and_combined_queries_submit_current_scripts(self) -> None:
        service, ollama, tenant, scripts, _audits = self.integration_service(
            [], ready={"ctsehr_check", "ctsoa_check"}
        )

        oa = service.chat(tenant, "请查看 OA 待办")
        combined = service.chat(tenant, "查看打卡和 OA 审批待办")

        self.assertEqual(ollama.calls, [])
        self.assertIn("CTS OA 待办查询已提交", oa.text)
        self.assertIn("CTS EHR 考勤查询已提交", combined.text)
        self.assertIn("CTS OA 待办查询已提交", combined.text)
        self.assertEqual(
            [script_id for _tenant, script_id, _parameters in scripts.calls],
            ["ctsoa_check", "ctsehr_check", "ctsoa_check"],
        )

    def test_non_live_integration_questions_remain_in_model_route(self) -> None:
        service, ollama, tenant, scripts, _audits = self.integration_service(
            [
                CanonicalMessage("assistant", "这是私人待办问题。"),
                CanonicalMessage("assistant", "这是系统区别说明。"),
            ]
        )

        todo = service.chat(tenant, "查看待办")
        explanation = service.chat(tenant, "OA 和 EHR 有什么区别？")

        self.assertEqual(todo.text, "这是私人待办问题。")
        self.assertEqual(explanation.text, "这是系统区别说明。")
        self.assertEqual(len(ollama.calls), 2)
        self.assertEqual(scripts.calls, [])

    def test_safe_tool_result_is_returned_to_model(self) -> None:
        (self.root / "actual.txt").write_text("真实文件", encoding="utf-8")
        service, ollama = self.service(
            [
                tool_call("list_directory", {"path": "."}),
                CanonicalMessage("assistant", "目录里有 actual.txt。"),
            ]
        )
        outcome = service.chat("user", "当前目录有什么文件？")
        self.assertIsInstance(outcome, FinalAnswer)
        self.assertIn("actual.txt", outcome.text)
        second_messages = ollama.calls[1].messages
        self.assertEqual(second_messages[-1].role, "tool")
        self.assertIn("actual.txt", second_messages[-1].content)
        self.assertEqual(second_messages[-1].tool_call_id, "call-1")
        self.assertEqual(len(service.histories["user"]), 2)

    def test_plain_todo_query_bypasses_model_and_ignores_stale_history(self) -> None:
        service, ollama, tenant, todo = self.todo_service(
            [CanonicalMessage("assistant", "旧的四项未完成")]
        )
        todo.execute(
            "todo_manage",
            {"action": "add", "title": "数据库中的唯一待办"},
            tenant,
        )
        service.histories[tenant.tenant_id] = [
            CanonicalMessage("user", "待办？"),
            CanonicalMessage("assistant", "T0004、T0005、T0006 都未完成"),
        ]

        outcome = service.chat(tenant, "待办？")

        self.assertEqual(ollama.calls, [])
        self.assertIn("数据库中的唯一待办", outcome.text)
        self.assertNotIn("T0004", outcome.text)
        self.assertIn("查询时间：", outcome.text)

    def test_plain_todo_queries_route_scopes_without_model(self) -> None:
        service, ollama, tenant, todo = self.todo_service([])
        todo.execute(
            "todo_manage", {"action": "add", "title": "已完成事项"}, tenant
        )
        todo.execute(
            "todo_manage",
            {"action": "complete", "todo_id": "T0001"},
            tenant,
        )

        completed = service.chat(tenant, "已完成的待办？")

        self.assertEqual(ollama.calls, [])
        self.assertIn("近期已完成", completed.text)
        self.assertIn("已完成事项", completed.text)

    def test_todo_tool_result_is_returned_without_model_rewrite(self) -> None:
        service, ollama, tenant, _todo = self.todo_service(
            [
                tool_call(
                    "todo_manage",
                    {"action": "add", "title": "不要让模型改写"},
                ),
                CanonicalMessage("assistant", "错误的二次改写"),
            ]
        )

        outcome = service.chat(tenant, "新增待办：不要让模型改写")

        self.assertEqual(len(ollama.calls), 1)
        self.assertIn("已新增待办", outcome.text)
        self.assertIn("不要让模型改写", outcome.text)
        self.assertNotIn("错误的二次改写", outcome.text)

    def test_multiple_direct_todo_results_are_joined_in_call_order(self) -> None:
        calls = CanonicalMessage(
            "assistant",
            tool_calls=[
                CanonicalToolCall(
                    "call-1", "todo_manage", {"action": "add", "title": "第一项"}
                ),
                CanonicalToolCall(
                    "call-2", "todo_manage", {"action": "add", "title": "第二项"}
                ),
            ],
        )
        service, ollama, tenant, _todo = self.todo_service([calls])

        outcome = service.chat(tenant, "新增第一项和第二项待办")

        self.assertEqual(len(ollama.calls), 1)
        self.assertLess(outcome.text.index("第一项"), outcome.text.index("第二项"))
        self.assertIn("\n\n", outcome.text)

    def test_mixed_tool_batch_still_uses_model_for_final_answer(self) -> None:
        calls = CanonicalMessage(
            "assistant",
            tool_calls=[
                CanonicalToolCall(
                    "call-1", "todo_manage", {"action": "list", "scope": "pending"}
                ),
                CanonicalToolCall("call-2", "get_current_time", {}),
            ],
        )
        service, ollama, tenant, _todo = self.todo_service(
            [calls, CanonicalMessage("assistant", "组合后的最终回答")]
        )

        outcome = service.chat(tenant, "查看待办并告诉我现在几点")

        self.assertEqual(len(ollama.calls), 2)
        self.assertEqual(outcome.text, "组合后的最终回答")

    def test_compound_todo_discussion_is_not_directly_routed(self) -> None:
        service, ollama, tenant, _todo = self.todo_service(
            [CanonicalMessage("assistant", "这是概念说明")]
        )

        outcome = service.chat(tenant, "待办和 Codex 开发任务有什么区别？")

        self.assertEqual(len(ollama.calls), 1)
        self.assertEqual(outcome.text, "这是概念说明")

    def test_todo_script_runs_and_returns_result_without_approval(self) -> None:
        scripts = AutoScriptService()
        registry = TenantRegistry(Path(self.temp.name) / "data")
        tenant = registry.resolve("bot", "user")
        runtime = ToolRuntime(
            self.runtime.config,
            "Asia/Shanghai",
            trash_directory=Path(self.temp.name) / "trash",
            sandbox_available=True,
            script_service=scripts,
            tenant_registry=registry,
        )
        self.runtime = runtime
        run_id = "todo_manager-20260718T120000-12345678"
        service, ollama = self.service(
            [
                tool_call(
                    "run_script",
                    {
                        "script_id": "todo_manager",
                        "parameters": {"action": "list"},
                    },
                ),
                tool_call("get_script_run", {"run_id": run_id}),
                CanonicalMessage("assistant", "【待办列表】未完成，共 0 项。"),
            ]
        )

        outcome = service.chat(tenant, "查看我的待办")

        self.assertIsInstance(outcome, FinalAnswer)
        self.assertEqual(outcome.text, "【待办列表】未完成，共 0 项。")
        self.assertFalse(service.has_pending_approval(tenant))
        self.assertEqual(
            scripts.calls,
            [(tenant, "todo_manager", {"action": "list"})],
        )
        self.assertIn("未完成，共 0 项", ollama.calls[-1].messages[-1].content)
        tools_text = service.tools_text(tenant)
        self.assertIn(
            "自动执行的固定脚本：待办提醒与管理（todo_manager）",
            tools_text,
        )
        self.assertIn("需要确认的固定脚本：无", tools_text)

    def test_tool_audit_uses_actual_model_from_triggering_response(self) -> None:
        logs = []
        self.runtime.audit_logger = lambda *values: logs.append(values)
        service, _ollama = self.service(
            [
                tool_call("get_current_time", {}),
                CanonicalMessage("assistant", "已查询。"),
            ]
        )
        service.chat("user", "现在几点？")
        self.assertEqual(logs[0][0].provider, "fake")
        self.assertEqual(logs[0][0].profile_id, "test")
        self.assertEqual(logs[0][0].model, "fake-model")

    def test_temporary_assistant_extensions_do_not_enter_history(self) -> None:
        first = CanonicalMessage(
            "assistant",
            tool_calls=[CanonicalToolCall("call-1", "get_current_time", {})],
            extensions={"reasoning_content": "private reasoning"},
        )
        service, ollama = self.service(
            [first, CanonicalMessage("assistant", "最终回答")]
        )
        outcome = service.chat("user", "查询时间")
        self.assertEqual(
            ollama.calls[1].messages[-2].extensions["reasoning_content"],
            "private reasoning",
        )
        self.assertTrue(
            all(not message.extensions for message in service.histories["user"])
        )
        self.assertEqual(outcome.thinking, "private reasoning")

    def test_thinking_survives_approval_and_is_aggregated(self) -> None:
        first = CanonicalMessage(
            "assistant",
            tool_calls=[
                CanonicalToolCall(
                    "call-1",
                    "write_text_file",
                    {"path": "reasoned.txt", "content": "ok", "mode": "create"},
                )
            ],
            extensions={"thinking": "第一段思考"},
        )
        final_message = CanonicalMessage(
            "assistant",
            "文件已创建。",
            extensions={"thinking": "第二段思考"},
        )
        service, _ollama = self.service([first, final_message])

        pending = service.chat("user", "创建文件")
        final = service.resolve_approval("user", pending.approval_id, True)

        self.assertEqual(final.thinking, "第一段思考\n\n第二段思考")
        self.assertEqual(final.text, "文件已创建。")

    def test_write_requires_matching_user_approval(self) -> None:
        service, _ollama = self.service(
            [
                tool_call(
                    "write_text_file",
                    {"path": "approved.txt", "content": "ok\n", "mode": "create"},
                ),
                CanonicalMessage("assistant", "文件已经创建。"),
            ]
        )
        outcome = service.chat("user-a", "创建文件")
        self.assertIsInstance(outcome, ApprovalRequired)
        self.assertIn("回复“同意”或“确认”", outcome.text)
        self.assertIn("默认按“不同意”处理", outcome.text)
        self.assertNotIn("批准：/approve", outcome.text)
        self.assertNotIn(outcome.approval_id, outcome.text)
        self.assertFalse((self.root / "approved.txt").exists())
        self.assertTrue(service.has_pending_approval("user-a"))
        self.assertFalse(service.has_pending_approval("user-b"))
        with self.assertRaises(ToolError):
            service.resolve_approval("user-b", outcome.approval_id, True)
        reminder = service.chat("user-a", "普通消息不能批准")
        self.assertEqual(reminder.approval_id, outcome.approval_id)
        final = service.resolve_pending_approval("user-a", True)
        self.assertEqual(final.text, "文件已经创建。")
        self.assertEqual((self.root / "approved.txt").read_text(encoding="utf-8"), "ok\n")
        self.assertFalse(service.has_pending_approval("user-a"))

    def test_denial_does_not_change_file_and_model_can_explain(self) -> None:
        service, ollama = self.service(
            [
                tool_call(
                    "write_text_file",
                    {"path": "denied.txt", "content": "no", "mode": "create"},
                ),
                CanonicalMessage("assistant", "已取消创建文件。"),
            ]
        )
        service.chat("user", "创建文件")
        final = service.resolve_pending_approval("user", False)
        self.assertEqual(final.text, "已取消创建文件。")
        self.assertFalse((self.root / "denied.txt").exists())
        self.assertIn("用户拒绝", ollama.calls[-1].messages[-1].content)

    def test_clear_cancels_pending_approval(self) -> None:
        service, _ollama = self.service(
            [
                tool_call(
                    "write_text_file",
                    {"path": "later.txt", "content": "x", "mode": "create"},
                )
            ]
        )
        pending = service.chat("user", "创建文件")
        service.clear_history("user")
        with self.assertRaises(ToolError):
            service.resolve_approval("user", pending.approval_id, True)

    def test_batch_approval_executes_every_displayed_operation(self) -> None:
        batch_message = CanonicalMessage(
            "assistant",
            tool_calls=[
                CanonicalToolCall(
                    "call-1",
                    "write_text_file",
                    {"path": "one.txt", "content": "1", "mode": "create"},
                ),
                CanonicalToolCall(
                    "call-2",
                    "write_text_file",
                    {"path": "two.txt", "content": "2", "mode": "create"},
                ),
            ],
        )
        service, _ollama = self.service(
            [batch_message, CanonicalMessage("assistant", "两个文件都已创建。")]
        )
        pending = service.chat("user", "创建两个文件")
        self.assertIn("2 项本机操作", pending.text)
        service.resolve_approval("user", pending.approval_id, True)
        self.assertEqual((self.root / "one.txt").read_text(), "1")
        self.assertEqual((self.root / "two.txt").read_text(), "2")

    def test_expired_approval_cannot_execute(self) -> None:
        service, _ollama = self.service(
            [
                tool_call(
                    "write_text_file",
                    {"path": "expired.txt", "content": "x", "mode": "create"},
                )
            ]
        )
        pending = service.chat("user", "创建文件")
        service._pending["user"].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        with self.assertRaisesRegex(ToolError, "过期"):
            service.resolve_approval("user", pending.approval_id, True)
        self.assertFalse((self.root / "expired.txt").exists())

    def test_timeout_atomically_discards_without_model_or_history(self) -> None:
        service, ollama = self.service(
            [
                tool_call(
                    "write_text_file",
                    {"path": "timed-out.txt", "content": "x", "mode": "create"},
                ),
                CanonicalMessage("assistant", "不应生成这条回复。"),
            ]
        )
        pending = service.chat("user", "创建文件")
        model_calls = len(ollama.calls)

        self.assertFalse(
            service.expire_approval(
                "user", "wrong-id", now=pending.expires_at
            )
        )
        self.assertFalse(
            service.expire_approval(
                "user",
                pending.approval_id,
                now=pending.expires_at - timedelta(seconds=1),
            )
        )
        self.assertTrue(
            service.expire_approval(
                "user", pending.approval_id, now=pending.expires_at
            )
        )
        self.assertFalse(
            service.expire_approval(
                "user", pending.approval_id, now=pending.expires_at
            )
        )

        self.assertFalse((self.root / "timed-out.txt").exists())
        self.assertEqual(len(ollama.calls), model_calls)
        self.assertNotIn("user", service.histories)
        with self.assertRaises(ToolError):
            service.resolve_pending_approval("user", True)

    def test_truncated_approval_keeps_chinese_instructions(self) -> None:
        service, _ollama = self.service(
            [
                tool_call(
                    "write_text_file",
                    {"path": "large.txt", "content": "x", "mode": "create"},
                )
            ]
        )
        pending = service.chat("user", "创建文件")
        record = service._pending["user"]
        record.calls[0] = replace(record.calls[0], preview="预览" * 10_000)

        truncated = service._approval_outcome(record)

        self.assertLessEqual(len(truncated.text.encode("utf-8")), 16_384)
        self.assertIn("操作预览已截断", truncated.text)
        self.assertIn("回复“同意”或“确认”", truncated.text)
        self.assertIn("默认按“不同意”处理", truncated.text)
        self.assertNotIn(pending.approval_id, truncated.text)

    def test_approval_and_timeout_race_has_only_one_winner(self) -> None:
        service, _ollama = self.service(
            [
                tool_call(
                    "write_text_file",
                    {"path": "race.txt", "content": "once", "mode": "create"},
                ),
                CanonicalMessage("assistant", "文件已创建。"),
            ]
        )
        pending = service.chat("user", "创建文件")
        barrier = threading.Barrier(3)
        results = {}

        def approve():
            barrier.wait()
            try:
                service.resolve_pending_approval("user", True)
            except ToolError:
                results["approved"] = False
            else:
                results["approved"] = True

        def expire():
            barrier.wait()
            results["expired"] = service.expire_approval(
                "user", pending.approval_id, now=pending.expires_at
            )

        approve_thread = threading.Thread(target=approve)
        expire_thread = threading.Thread(target=expire)
        approve_thread.start()
        expire_thread.start()
        barrier.wait()
        approve_thread.join()
        expire_thread.join()

        self.assertNotEqual(results["approved"], results["expired"])
        self.assertEqual((self.root / "race.txt").exists(), results["approved"])

    def test_unknown_tool_is_returned_as_error_without_execution(self) -> None:
        service, ollama = self.service(
            [
                tool_call("not_allowed", {}),
                CanonicalMessage("assistant", "该工具不可用。"),
            ]
        )
        outcome = service.chat("user", "调用未知工具")
        self.assertEqual(outcome.text, "该工具不可用。")
        self.assertIn("未授权工具", ollama.calls[-1].messages[-1].content)

    def test_scheduled_generation_does_not_receive_tools(self) -> None:
        service, ollama = self.service([])
        result = service.generate("general", "生成提醒")
        self.assertEqual(result, "无工具回答")
        request = ollama.calls[-1]
        self.assertEqual(request.tools, [])
        self.assertFalse(any("默认工作目录" in item.content for item in request.messages))

    def test_failover_after_approved_tool_does_not_repeat_execution(self) -> None:
        local = FakeToolOllama(
            [
                tool_call(
                    "write_text_file",
                    {"path": "once.txt", "content": "once", "mode": "create"},
                ),
                ModelError("local stopped", provider="ollama"),
            ]
        )
        local.identity = ModelIdentity("ollama_local", "ollama", "local")
        flash = FakeToolOllama([CanonicalMessage("assistant", "已由云端确认完成。")])
        flash.identity = ModelIdentity(
            "deepseek_cloud", "deepseek", "deepseek-v4-flash"
        )
        pro = FakeToolOllama([CanonicalMessage("assistant", "unused")])
        pro.identity = ModelIdentity("deepseek_pro", "deepseek", "deepseek-v4-pro")
        router = ModelRouter(
            {
                "ollama_local": local,
                "deepseek_cloud": flash,
                "deepseek_pro": pro,
            },
            primary_profile_id="ollama_local",
            fallback_profile_id="deepseek_cloud",
        )
        service = AgentService(
            router,
            self.config.app,
            self.config.agents,
            tool_runtime=self.runtime,
        )

        pending = service.chat("user", "创建一次文件")
        final = service.resolve_approval("user", pending.approval_id, True)

        self.assertEqual(final.text, "已由云端确认完成。")
        self.assertEqual((self.root / "once.txt").read_text(), "once")
        self.assertEqual(len(local.calls), 2)
        self.assertEqual(len(flash.calls), 1)
        self.assertIn("once.txt", flash.calls[0].messages[-1].content)


if __name__ == "__main__":
    unittest.main()
