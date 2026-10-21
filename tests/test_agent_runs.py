"""Behavioral coverage for continuous, durable and permission-scoped agents."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from src.core.config.loader import (
    load_project_config, validate_agent_execution_options, ConfigError, ScriptDefinition, ScriptParameter,
)
from src.core.modeling import CanonicalMessage, CanonicalToolCall, ModelCapabilities, ModelIdentity, ModelResponse
from src.core.services.agent import AgentService
from src.core.services.agent_runs import AgentRunService
from src.core.services.react import compact_messages, encode_message, new_state
from src.core.services.script import ScriptRun, ScriptService
from src.core.services.notification import TenantRecipientStore
from src.core.storage.agent_runs import AgentRunError, AgentRunStore, TERMINAL, WAITING
from src.core.storage.tenants import ConversationStore, TenantRegistry
from src.core.tooling import ToolRuntime


class ProgramModel:
    identity = ModelIdentity("test", "test", "program")
    capabilities = ModelCapabilities(tools=True, vision=True)

    def __init__(self, program=None):
        self.requests = []
        self.program = program or (lambda request: CanonicalMessage("assistant", "已完成"))
        self.verdict = {"complete": True, "reason": "已核对实际结果", "remaining_steps": []}

    def complete(self, request):
        self.requests.append(request)
        if request.context.operation == "completion_check":
            value = CanonicalMessage("assistant", json.dumps(self.verdict, ensure_ascii=False))
        else:
            value = self.program(request)
        return ModelResponse(value, actual_model="program")


def call(name, arguments, call_id="call"):
    return CanonicalMessage("assistant", tool_calls=[CanonicalToolCall(call_id, name, arguments)])


class DurableAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = TenantRegistry(Path(self.temp.name) / "data")
        self.tenant = self.registry.resolve("test", "person")
        self.config = load_project_config(Path(__file__).resolve().parents[1] / "config")
        self.preset = replace(
            self.config.agents["general"],
            plugin_tools={},
            mcp_servers=[],
            skills=[],
            tools=["list_directory", "read_text_file", "write_text_file", "get_current_time"],
        )
        workspace = (
            self.registry.workspace(self.tenant.tenant_id)
            if hasattr(self.registry, "workspace")
            else self.registry.tenant_root(self.tenant.tenant_id) / "workspace"
        )
        self.workspace = workspace
        self.runtime = ToolRuntime(
            replace(
                self.config.tools,
                allowed_roots=[str(workspace)],
                default_working_directory=str(workspace),
                max_tool_rounds=1,
                max_total_tool_calls=1,
            ),
            "Asia/Shanghai",
            tenant_registry=self.registry,
            sandbox_available=True,
        )
        self.model = ProgramModel()
        self.conversations = ConversationStore(self.registry, 12)
        self.owner = AgentService(
            self.model,
            self.config.app,
            {"general": self.preset},
            tool_runtime=self.runtime,
            conversation_store=self.conversations,
        )
        self.runs = AgentRunService(self.owner, self.registry)
        # A deterministic scheduler uses the real claim/lease/checkpoint path.
        self.runs.start = lambda: None
        self.owner.runs = self.runs
        self.addCleanup(self.runs.shutdown)

    def submit(self, question="完成任务", **kwargs):
        return self.runs.submit(self.tenant, question, **kwargs)

    def step(self):
        run = self.runs.store.claim("test-worker")
        if run:
            self.runs.advance(run, "test-worker")
        return run

    def drive(self, run_id, limit=200):
        for _ in range(limit):
            run = self.runs.store.get(run_id)
            if run["status"] in TERMINAL or run["status"] in WAITING:
                return run
            self.step()
        self.fail("执行未达到稳定状态")

    def write_program(self, request):
        if any(item.role == "tool" for item in request.messages):
            return CanonicalMessage("assistant", "文件已创建")
        return call("write_text_file", {"path": "result.txt", "content": "once", "mode": "create"})

    def test_exceeds_old_round_and_call_limits_and_verifies_completion(self):
        def program(request):
            count = sum(item.role == "tool" for item in request.messages)
            if count == 16:
                return CanonicalMessage("assistant", "十六个文件检查完成")
            return call("read_text_file", {"path": "file{}.txt".format(count)}, str(count))

        for index in range(16):
            (self.workspace / "file{}.txt".format(index)).write_text(str(index))
        self.model.program = program
        result = self.drive(self.submit()["run_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["state"]["total_calls"], 16)
        self.assertGreater(result["state"]["rounds"], 10)
        self.assertTrue(result["state"]["verification"]["complete"])
        self.assertEqual(len(self.conversations.load_context(self.tenant.tenant_id)), 2)

    def test_unadvertised_boundaries_reject_hallucinated_tool(self):
        executed = Mock(wraps=self.runtime.execute)
        self.runtime.execute = executed
        self.model.program = lambda request: (
            CanonicalMessage("assistant", "工具不可用")
            if any(m.role == "tool" for m in request.messages)
            else call("run_command", {"profile": "python", "args": ["--version"]})
        )
        result = self.drive(self.submit()["run_id"])
        self.assertEqual(result["status"], "failed")
        executed.assert_not_called()
        self.assertIn("未授权工具", result["state"]["failures"]["run_command"])

    def test_approval_is_durable_single_use_and_executes_once(self):
        self.model.program = self.write_program
        run = self.drive(self.submit()["run_id"])
        self.assertEqual(run["status"], "waiting_approval")
        self.assertFalse((self.workspace / "result.txt").exists())
        approval_id = run["state"]["approval"]["approval_id"]
        # A fresh service instance resolves the persisted approval.
        restored = AgentRunService(self.owner, self.registry)
        restored.resolve_approval(run["run_id"], self.tenant.tenant_id, approval_id, True)
        with self.assertRaises(AgentRunError):
            restored.resolve_approval(run["run_id"], self.tenant.tenant_id, approval_id, True)
        result = self.drive(run["run_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual((self.workspace / "result.txt").read_text(), "once")
        with self.registry.database.read() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM agent_run_steps").fetchone()[0], 1)

    def test_revocation_after_approval_prevents_execution(self):
        self.model.program = self.write_program
        run = self.drive(self.submit()["run_id"])
        approval_id = run["state"]["approval"]["approval_id"]
        self.owner.agents["general"] = replace(self.preset, tools=[])
        self.runs.resolve_approval(run["run_id"], self.tenant.tenant_id, approval_id, True)
        result = self.drive(run["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertFalse((self.workspace / "result.txt").exists())

    def test_denial_is_not_recorded_as_success(self):
        self.model.program = self.write_program
        run = self.drive(self.submit()["run_id"])
        self.runs.resolve_approval(run["run_id"], self.tenant.tenant_id, run["state"]["approval"]["approval_id"], False)
        self.assertEqual(self.drive(run["run_id"])["status"], "failed")
        self.assertFalse((self.workspace / "result.txt").exists())

    def test_expiry_preserves_task_for_new_approval(self):
        self.model.program = self.write_program
        run = self.drive(self.submit()["run_id"])
        approval_id = run["state"]["approval"]["approval_id"]
        with self.registry.database.transaction() as con:
            con.execute("UPDATE agent_run_approvals SET expires_at=0 WHERE approval_id=?", (approval_id,))
        self.runs.store.expire_approvals()
        paused = self.runs.store.get(run["run_id"])
        self.assertEqual(paused["status"], "paused")
        self.runs.resume(run["run_id"], self.tenant.tenant_id)
        again = self.drive(run["run_id"])
        self.assertEqual(again["status"], "waiting_approval")
        self.assertNotEqual(again["state"]["approval"]["approval_id"], approval_id)

    def test_exact_parameter_preauthorization_and_mandatory_approval(self):
        self.owner.agents["general"] = replace(
            self.preset, automatic_tools={"write_text_file": [{"path": "result.txt"}]}
        )
        self.model.program = self.write_program
        result = self.drive(self.submit()["run_id"])
        self.assertEqual(result["status"], "succeeded")
        (self.workspace / "result.txt").unlink()
        self.runtime.reload_tool_states({"write_text_file": {"enabled": True, "require_approval": True}})
        result = self.drive(self.submit()["run_id"])
        self.assertEqual(result["status"], "waiting_approval")

    def test_parameter_scope_does_not_authorize_another_file(self):
        self.owner.agents["general"] = replace(
            self.preset, automatic_tools={"write_text_file": [{"path": "other.txt"}]}
        )
        self.model.program = self.write_program
        self.assertEqual(self.drive(self.submit()["run_id"])["status"], "waiting_approval")

    def test_cancel_queued_task_prevents_model_calls(self):
        run = self.submit()
        self.runs.store.cancel(run["run_id"], self.tenant.tenant_id)
        self.assertEqual(self.drive(run["run_id"])["status"], "cancelled")
        self.assertFalse(self.model.requests)

    def test_cancel_during_tool_retains_result_without_next_model_call(self):
        self.owner.agents["general"] = replace(self.preset, automatic_tools={"write_text_file": [{}]})
        self.model.program = self.write_program
        run = self.submit()
        self.step()  # checkpoint the model's intended action
        execute = self.runtime.execute

        def cancel_during_call(*args, **kwargs):
            result = execute(*args, **kwargs)
            self.runs.store.cancel(run["run_id"], self.tenant.tenant_id)
            return result

        self.runtime.execute = cancel_during_call
        self.step()
        self.assertEqual(self.runs.store.get(run["run_id"])["status"], "cancelled")
        self.assertEqual(len(self.model.requests), 1)
        with self.registry.database.read() as con:
            self.assertEqual(con.execute("SELECT status FROM agent_run_steps").fetchone()[0], "done")

    def test_completed_journal_replays_result_without_repeating_write(self):
        self.owner.agents["general"] = replace(self.preset, automatic_tools={"write_text_file": [{}]})
        self.model.program = self.write_program
        run = self.submit()
        self.step()
        key = "1:0"
        raw = self.runs.store.get(run["run_id"])["state"]["queue"][0]
        self.runs.store.journal(run["run_id"], key, raw)
        result = self.runtime.execute(raw["name"], raw["arguments"])
        self.runs.store.journal_result(run["run_id"], key, result.payload())
        self.runtime.execute = Mock(side_effect=AssertionError("不能重放写操作"))
        self.assertEqual(self.drive(run["run_id"])["status"], "succeeded")
        self.runtime.execute.assert_not_called()

    def test_unknown_write_pauses_until_admin_reconciles(self):
        self.owner.agents["general"] = replace(self.preset, automatic_tools={"write_text_file": [{}]})
        self.model.program = self.write_program
        run = self.submit()
        self.step()
        raw = self.runs.store.get(run["run_id"])["state"]["queue"][0]
        self.runs.store.journal(run["run_id"], "1:0", raw)
        result = self.drive(run["run_id"])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["state"]["reason_code"], "execution_unknown")
        with self.assertRaises(AgentRunError):
            self.runs.resume(run["run_id"], self.tenant.tenant_id)
        self.runs.store.reconcile(
            run["run_id"], self.tenant.tenant_id, "1:0", {"ok": True, "data": {"path": "result.txt"}}
        )
        self.runtime.execute = Mock(side_effect=AssertionError("不能重复执行"))
        self.assertEqual(self.drive(run["run_id"])["status"], "succeeded")
        self.runtime.execute.assert_not_called()

    def test_lease_ownership_and_restart_recovery(self):
        run = self.submit()
        first = self.runs.store.claim("worker-a")
        self.assertEqual(first["run_id"], run["run_id"])
        self.assertIsNone(self.runs.store.claim("worker-b"))
        with self.registry.database.transaction() as con:
            con.execute("UPDATE agent_runs SET lease_until=0 WHERE run_id=?", (run["run_id"],))
        second = AgentRunStore(self.registry.database).claim("worker-b")
        with self.assertRaises(AgentRunError):
            self.runs.store.checkpoint(run["run_id"], "worker-a", first["state"], "queued")
        self.runs.advance(second, "worker-b")
        self.assertEqual(self.drive(run["run_id"])["status"], "succeeded")

    def test_independent_model_steps_can_be_claimed_in_parallel(self):
        self.submit(conversation_id="first")
        self.submit(conversation_id="second")
        first = self.runs.store.claim("worker-a")
        second = self.runs.store.claim("worker-b")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_cancelled_parent_cannot_spawn_new_children(self):
        parent = self.submit()
        self.runs.store.cancel(parent["run_id"], self.tenant.tenant_id)
        with self.assertRaises(AgentRunError):
            self.submit(parent_run_id=parent["run_id"])

    def test_same_conversation_rejects_concurrent_root_runs(self):
        self.submit()
        with self.assertRaises(AgentRunError):
            self.submit()
        self.assertIsNotNone(self.submit(conversation_id="another"))

    def test_event_replay_uses_monotonic_cursor(self):
        run = self.submit()
        self.drive(run["run_id"])
        events = self.runs.store.events(run["run_id"])
        cursor = events[1]["event_id"]
        replay = self.runs.store.events(run["run_id"], cursor)
        self.assertEqual(replay, events[2:])
        self.assertTrue(any(event["type"] == "done" for event in replay))

    def test_scope_check_rejects_other_tenant(self):
        run = self.submit()
        other = self.registry.resolve("test", "other")
        with self.assertRaises(AgentRunError):
            self.runs.store.get(run["run_id"], other.tenant_id)
        with self.assertRaises(AgentRunError):
            self.runs.store.cancel(run["run_id"], other.tenant_id)

    def test_missing_input_resumes_same_run_and_preserves_call_pairing(self):
        def program(request):
            if any(m.role == "user" and m.content == "蓝色" for m in request.messages):
                return CanonicalMessage("assistant", "已按蓝色完成")
            return call("agent__request_input", {"question": "请选择颜色"})

        self.model.program = program
        run = self.drive(self.submit()["run_id"])
        self.assertEqual(run["status"], "waiting_input")
        self.runs.resume(run["run_id"], self.tenant.tenant_id, "蓝色")
        result = self.drive(run["run_id"])
        self.assertEqual(result["status"], "succeeded")
        resumed_request = [
            request for request in self.model.requests if request.context.operation != "completion_check"
        ][-1]
        roles = [m.role for m in resumed_request.messages]
        self.assertEqual(roles[-3:], ["assistant", "tool", "user"])

    def script_challenge(self):
        scripts = ScriptService(
            {"example": ScriptDefinition(
                id="example", name="示例脚本", description="", entrypoint="unused.py", timeout_seconds=30,
                parameters={"code": ScriptParameter(type="string", flag="--code")},
            )}, None, TenantRecipientStore(self.registry), Path(self.temp.name), self.registry,
        )
        self.addCleanup(scripts.shutdown)
        self.runtime.script_service = scripts
        self.owner.agents["general"] = replace(self.preset, tools=["run_script", "get_script_run"])
        original = ScriptRun(
            run_id="example-20261021T000000-aaaaaaaa", script_id="example", script_name="示例脚本",
            trigger="model", parameters={}, status="awaiting_input", summary="请输入验证码",
            tenant_id=self.tenant.tenant_id,
        )
        scripts._persist(original)
        scripts.register_pending_input(original, {"param": "code"})
        successor = ScriptRun(
            run_id="example-20261021T000001-bbbbbbbb", script_id="example", script_name="示例脚本",
            trigger="model", parameters={"code": "secret-captcha"}, status="success", summary="查询完成",
            tenant_id=self.tenant.tenant_id,
        )
        scripts._persist(successor)
        submitted = []
        scripts.submit_for_tenant = lambda *_args: (
            submitted.append(_args) or {"run_id": successor.run_id, "status": "running", "parameters": _args[2]}
        )

        def program(request):
            content = " ".join(m.content or "" for m in request.messages)
            if successor.run_id not in content:
                return call("get_script_run", {"run_id": original.run_id})
            if "查询完成" not in content:
                return call("get_script_run", {"run_id": successor.run_id})
            return CanonicalMessage("assistant", "已验证查询结果")

        self.model.program = program
        return scripts, original, successor, submitted

    def test_script_input_resumes_same_agent_without_sending_input_to_model(self):
        scripts, original, successor, submitted = self.script_challenge()
        run = self.drive(self.submit()["run_id"])
        self.assertEqual(run["status"], "waiting_input")
        self.runs.resume(run["run_id"], self.tenant.tenant_id, "secret-captcha")
        result = self.drive(run["run_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0][2], {"code": "secret-captcha"})
        self.assertEqual(result["state"]["pending_operations"], {})
        self.assertNotIn("secret-captcha", json.dumps(result["state"], ensure_ascii=False))
        self.assertNotIn("code", scripts.get_run(self.tenant, successor.run_id)["parameters"])

    def test_channel_script_reply_wakes_persisted_agent(self):
        scripts, original, successor, submitted = self.script_challenge()
        run = self.drive(self.submit()["run_id"])
        pending = scripts.peek_pending_input(self.tenant)
        scripts.resume_pending_input(self.tenant, pending, "secret-captcha")
        self.runs.sync_script_inputs()
        self.assertEqual(self.drive(run["run_id"])["status"], "succeeded")
        self.assertEqual(len(submitted), 1)

    def test_stopping_agent_cancels_its_pending_script_input(self):
        scripts, original, successor, submitted = self.script_challenge()
        run = self.drive(self.submit()["run_id"])
        self.runs.cancel(run["run_id"], self.tenant.tenant_id)
        self.assertIsNone(scripts.peek_pending_input(self.tenant))
        self.assertEqual(submitted, [])

    def test_cancelled_script_input_pauses_original_agent(self):
        scripts, original, successor, submitted = self.script_challenge()
        run = self.drive(self.submit()["run_id"])
        scripts.clear_pending_input(self.tenant)
        self.runs.sync_script_inputs()
        result = self.runs.store.get(run["run_id"])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["state"]["reason_code"], "script_input_unavailable")
        self.assertEqual(submitted, [])

    def test_revoked_script_grant_blocks_input_submission(self):
        scripts, original, successor, submitted = self.script_challenge()
        run = self.drive(self.submit()["run_id"])
        self.owner.agents["general"] = replace(self.preset, tools=[])
        with self.assertRaisesRegex(AgentRunError, "脚本执行权限"):
            self.runs.resume(run["run_id"], self.tenant.tenant_id, "secret-captcha")
        self.assertEqual(submitted, [])

    def test_repetition_replans_then_pauses_without_a_round_cap(self):
        self.model.program = lambda request: call("list_directory", {"path": "."})
        result = self.drive(self.submit()["run_id"])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["state"]["reason_code"], "no_progress")
        self.assertTrue(any("调整计划" in m.content for request in self.model.requests for m in request.messages))

    def test_failed_verification_cannot_report_success(self):
        self.model.verdict = {"complete": False, "reason": "尚缺少产物", "remaining_steps": ["创建产物"]}
        self.model.program = lambda request: (
            CanonicalMessage("assistant", "已完成")
            if any(m.role == "tool" for m in request.messages)
            else call("list_directory", {"path": "."})
        )
        result = self.drive(self.submit()["run_id"])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["state"]["reason_code"], "incomplete")

    def test_published_history_is_idempotent(self):
        run = self.drive(self.submit()["run_id"])
        self.runs._publish(run, "succeeded")
        with self.registry.database.read() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM conversation_events").fetchone()[0], 0)
        self.assertEqual(len(self.conversations.load_context(self.tenant.tenant_id)), 2)

    def test_context_compaction_preserves_tool_call_pairs_and_goal(self):
        state = new_state([CanonicalMessage("system", "规则"), CanonicalMessage("user", "原始任务")], self.preset)
        for index in range(20):
            state["messages"].extend(
                [
                    encode_message(call("list_directory", {"path": str(index)}, str(index))),
                    encode_message(CanonicalMessage("tool", "x" * 1500, tool_call_id=str(index))),
                ]
            )
        messages = compact_messages(state, 8192, 2048)
        self.assertTrue(any(item.content == "原始任务" for item in messages))
        calls = [call.call_id for item in messages for call in item.tool_calls]
        results = [item.tool_call_id for item in messages if item.role == "tool"]
        self.assertEqual(calls, results)
        self.assertLess(len(json.dumps([encode_message(item) for item in messages]).encode()), 8192)

    def test_background_execution_does_not_need_event_subscriber(self):
        service = AgentRunService(self.owner, self.registry, max_workers=1)
        self.addCleanup(service.shutdown)
        run = service.submit(self.tenant, "无需连接也完成")
        result = service.wait(run["run_id"], timeout=5)
        self.assertEqual(result["status"], "succeeded")

    def test_multi_agent_dependencies_and_parent_verification(self):
        import uuid

        analytics = Mock()
        analytics.start_run.side_effect = lambda **kwargs: uuid.uuid4().hex
        self.owner.model_analytics_store = analytics
        self.owner.agents["general"] = replace(self.preset, temperature=0.23, max_tokens=1234)
        self.owner.agents["coder"] = replace(self.preset, id="coder", name="编程助手")

        def program(request):
            if request.context.operation == "planner":
                return CanonicalMessage(
                    "assistant",
                    json.dumps(
                        [
                            {
                                "task_id": "a",
                                "agent_id": "general",
                                "subtask": "查资料",
                                "depends_on": [],
                                "completion_criteria": ["取得资料"],
                            },
                            {
                                "task_id": "b",
                                "agent_id": "coder",
                                "subtask": "使用前序资料",
                                "depends_on": ["a"],
                                "completion_criteria": ["完成分析"],
                            },
                        ]
                    ),
                )
            if request.context.operation == "summary":
                return CanonicalMessage("assistant", "协作已完成")
            return CanonicalMessage("assistant", "前序资料结果")

        self.model.program = program
        result = self.drive(self.submit(agent_ids=["general", "coder"])["run_id"])
        self.assertEqual(result["status"], "succeeded")
        children = self.runs.store.children(result["run_id"])
        self.assertEqual(len(children), 2)
        self.assertIn("前序资料结果", children[1]["state"]["question"])
        self.assertTrue(result["state"]["verification"]["complete"])
        summary = [request for request in self.model.requests if request.context.operation == "summary"][0]
        self.assertEqual(summary.context.run_id, result["state"]["analytics_run_id"])
        self.assertEqual(summary.generation.temperature, 0.23)
        self.assertEqual(summary.generation.max_tokens, 1234)
        self.assertEqual(analytics.finish_run.call_count, 3)

    def test_coordinator_rejects_cycles_and_unselected_agents(self):
        for tasks in (
            [{"task_id": "a", "agent_id": "general", "subtask": "x", "depends_on": ["a"], "completion_criteria": []}],
            [{"task_id": "a", "agent_id": "other", "subtask": "x", "depends_on": [], "completion_criteria": []}],
        ):
            with self.assertRaises(ValueError):
                AgentRunService._validate_tasks(tasks, ["general"])

    def test_more_than_two_completion_checks_continue_while_progress_is_made(self):
        for index in range(3):
            (self.workspace / (str(index) + ".txt")).write_text(str(index))

        def program(request):
            count = sum(message.role == "tool" for message in request.messages)
            if request.messages[-1].role == "tool":
                return CanonicalMessage("assistant", "检查完成")
            return call("read_text_file", {"path": str(count) + ".txt"}, str(count))

        self.model.program = program
        complete = self.model.complete

        def verify(request):
            if request.context.operation == "completion_check":
                self.model.verdict = {
                    "complete": sum(m.role == "tool" for m in request.messages) >= 3,
                    "reason": "需要检查三个文件",
                    "remaining_steps": [],
                }
            return complete(request)

        self.model.complete = verify
        result = self.drive(self.submit()["run_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["state"]["total_calls"], 3)

    def test_paused_model_step_receives_user_clarification(self):
        run = self.submit()
        claimed = self.runs.store.claim("test-worker")
        state = claimed["state"]
        state.update(reason="需要调整任务", reason_code="execution_blocked")
        self.runs.store.checkpoint(run["run_id"], "test-worker", state, "paused")
        self.runs.resume(run["run_id"], self.tenant.tenant_id, "新增完成条件")
        self.drive(run["run_id"])
        self.assertTrue(any(m.content == "新增完成条件" for request in self.model.requests for m in request.messages))

    def test_permission_lookup_failure_does_not_fall_back_to_platform_grants(self):
        self.owner.resource_store = Mock()
        self.owner.resource_store.effective_agent_presets.side_effect = RuntimeError("不可用")
        with self.assertRaisesRegex(AgentRunError, "无法读取组织智能体授权"):
            self.submit()
        self.assertFalse(self.model.requests)

    def test_finished_run_notification_is_recovered_and_deduplicated(self):
        run = self.drive(self.submit(notify=True)["run_id"])
        self.assertEqual(self.runs.store.pending_notifications()[0]["run_id"], run["run_id"])
        notices = Mock()
        restored = AgentRunService(self.owner, self.registry, notification_service=notices)
        restored._notify(run, run["status"])
        restored._notify(run, run["status"])
        self.assertEqual(notices.enqueue_text_to_tenant.call_count, 1)
        self.assertFalse(self.runs.store.pending_notifications())

    def test_preview_validation_error_is_returned_for_automatic_repair(self):
        self.model.program = lambda request: (
            CanonicalMessage("assistant", "该路径不可用")
            if any(m.role == "tool" for m in request.messages)
            else call("write_text_file", {"path": "../../outside.txt", "content": "x", "mode": "create"})
        )
        result = self.drive(self.submit()["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("超出允许目录", result["state"]["failures"]["write_text_file"])
        self.assertFalse(result["state"].get("approval"))

    def test_suspended_organization_cannot_run_a_system_schedule(self):
        self.runs.organizations = Mock()
        self.runs.organizations.get.return_value = {"status": "suspended"}
        with self.assertRaisesRegex(AgentRunError, "组织已停用"):
            self.submit(source="schedule", allow_private_context=False)
        self.assertFalse(self.model.requests)

    def test_execution_options_validate_capacity_and_grants(self):
        for value in (
            {"context_window_tokens": 512},
            {"max_tokens": 65536},
            {"automatic_tools": {"run_command": "all"}},
        ):
            with self.assertRaises(ConfigError):
                validate_agent_execution_options(value)


if __name__ == "__main__":
    unittest.main()
