"""Real authenticated web boundaries for durable agent tasks."""

from __future__ import annotations

import json
from dataclasses import replace

from fastapi.testclient import TestClient

from src.core.modeling import CanonicalMessage, CanonicalToolCall, ModelResponse
from src.core.tooling import ToolRuntime
from tests._web_api_base import WebApiTestBase


class AgentRunApiTests(WebApiTestBase):
    def setUp(self):
        super().setUp()
        response = self.client.post("/api/chat/conversations")
        self.assertEqual(response.status_code, 201, response.text)
        self.conversation = response.json()
        self.organization_id = self.conversation["organization_id"]
        self.tenant = self.registry.get(self.organization_id)
        self.workspace = self.registry.tenant_root(self.organization_id) / "workspace"
        self.runtime = ToolRuntime(
            replace(
                self.config.tools,
                enabled=True,
                default_working_directory=str(self.workspace),
                allowed_roots=[str(self.workspace)],
            ),
            "Asia/Shanghai",
            tenant_registry=self.registry,
            sandbox_available=True,
        )
        self.app.state.tool_runtime = self.runtime
        self.app.state.agent_service.tool_runtime = self.runtime
        self.runs = self.app.state.agent_service.runs

    def program_write(self):
        preset = replace(self.config.agents["general"], tools=["write_text_file"])
        self.app.state.resource_store.upsert_public(
            "agents", "general", {**vars(preset), "capabilities": [{"name": "文件", "description": "测试文件工具"}]}, 1
        )

        def complete(request):
            if request.context.operation == "completion_check":
                return ModelResponse(
                    CanonicalMessage("assistant", '{"complete":true,"reason":"已核对文件","remaining_steps":[]}')
                )
            if any(message.role == "tool" for message in request.messages):
                return ModelResponse(CanonicalMessage("assistant", "文件已创建"))
            return ModelResponse(
                CanonicalMessage(
                    "assistant",
                    tool_calls=[
                        CanonicalToolCall(
                            "write", "write_text_file", {"path": "done.txt", "content": "一次", "mode": "create"}
                        )
                    ],
                )
            )

        self.fake_client.complete = complete

    def submit(self):
        response = self.client.post(
            "/api/agent-runs", json={"conversation_id": self.conversation["id"], "message": "创建测试文件"}
        )
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["run_id"]

    def test_background_submit_finishes_without_open_stream(self):
        run_id = self.submit()
        self.assertEqual(self.runs.wait(run_id, timeout=5)["status"], "succeeded")
        response = self.client.get("/api/agent-runs/" + run_id)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("state", response.json())
        self.assertTrue(
            self.conversation_store.load_context(self.organization_id, "organization:" + self.conversation["id"])
        )

    def test_scope_and_authentication_boundaries(self):
        run_id = self.submit()
        self.runs.wait(run_id, timeout=5)
        anonymous = TestClient(self.app)
        self.assertEqual(anonymous.get("/api/agent-runs/" + run_id).status_code, 401)
        self.assertEqual(self.viewer_client.get("/api/agent-runs/" + run_id).status_code, 404)
        self.assertEqual(self.viewer_client.post("/api/agent-runs/" + run_id + "/cancel").status_code, 403)
        response = self.viewer_client.get("/api/agent-runs", params={"organization_id": self.organization_id})
        self.assertEqual(response.status_code, 404)

    def test_web_approval_is_run_scoped_single_use_and_resumable(self):
        self.program_write()
        run_id = self.submit()
        run = self.runs.wait(run_id, timeout=5)
        self.assertEqual(run["status"], "waiting_approval")
        self.assertFalse((self.workspace / "done.txt").exists())
        approval_id = run["state"]["approval"]["approval_id"]
        wrong = self.client.post(
            "/api/agent-runs/" + run_id + "/approvals", json={"approval_id": "wrong", "approved": True}
        )
        self.assertEqual(wrong.status_code, 409)
        approved = self.client.post(
            "/api/agent-runs/" + run_id + "/approvals", json={"approval_id": approval_id, "approved": True}
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(self.runs.wait(run_id, timeout=5)["status"], "succeeded")
        self.assertEqual((self.workspace / "done.txt").read_text(), "一次")
        again = self.client.post(
            "/api/agent-runs/" + run_id + "/approvals", json={"approval_id": approval_id, "approved": True}
        )
        self.assertEqual(again.status_code, 409)

    def test_cancel_waiting_approval_never_executes(self):
        self.program_write()
        run_id = self.submit()
        self.assertEqual(self.runs.wait(run_id, timeout=5)["status"], "waiting_approval")
        self.assertEqual(self.client.post("/api/agent-runs/" + run_id + "/cancel").status_code, 200)
        self.assertEqual(self.runs.wait(run_id, timeout=5)["status"], "cancelled")
        self.assertFalse((self.workspace / "done.txt").exists())

    def test_events_reconnect_with_last_event_id(self):
        run_id = self.submit()
        self.runs.wait(run_id, timeout=5)
        events = self.runs.store.events(run_id)
        cursor = events[0]["event_id"]
        response = self.client.get("/api/agent-runs/" + run_id + "/events", headers={"Last-Event-ID": str(cursor)})
        replay = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertTrue(replay)
        self.assertTrue(all(item["event_id"] > cursor for item in replay))
        self.assertTrue(any(item["type"] == "done" for item in replay))

    def test_organization_lists_scheduled_tasks_and_management_page(self):
        self.runs.submit(
            self.tenant, "自动任务", conversation_id="schedule:example", source="schedule", allow_private_context=False
        )
        response = self.client.get("/api/agent-runs", params={"organization_id": self.organization_id})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"][0]["source"], "schedule")
        page = self.client.get("/organization/agent-runs", params={"organization_id": self.organization_id})
        self.assertEqual(page.status_code, 200)
        self.assertIn("执行任务", page.text)

    def workflow_agent(self, test_mode=False):
        from src.core.workflows.definition import empty_definition

        value = empty_definition("智能体接入验证")
        value["nodes"].insert(
            1,
            {
                "id": "agent",
                "type": "agent",
                "name": "执行智能体",
                "position": {"x": 200, "y": 100},
                "config": {"agent_id": "general", "prompt": "创建测试文件"},
                "error_policy": {"mode": "stop"},
            },
        )
        value["nodes"][-1]["config"] = {"output": "{{nodes.agent.output}}"}
        value["edges"] = [
            {"id": "a", "source": "start", "source_port": "default", "target": "agent", "target_port": "default"},
            {"id": "b", "source": "agent", "source_port": "default", "target": "end", "target_port": "default"},
        ]
        service = self.app.state.workflow_service
        flow = service.store.create_workflow(self.organization_id, "agent_test", "智能体测试", 1, value)
        service.store.publish(self.organization_id, flow["workflow_id"], 1)
        service.start()
        self.addCleanup(service.shutdown)
        run = service.enqueue(self.organization_id, flow["workflow_id"], {}, "manual", "test", 1, test_mode=test_mode)
        return service, run

    def test_workflow_waits_for_real_agent_approval_then_continues(self):
        self.program_write()
        service, run = self.workflow_agent()
        waiting = service.run_synchronously(self.organization_id, run["run_id"], timeout=5)
        self.assertEqual(waiting["status"], "waiting", waiting)
        waits = service.store.list_waits(self.organization_id)
        self.assertEqual(len(waits), 1)
        self.assertIn("agent_run_id", waits[0]["payload"])
        self.assertFalse((self.workspace / "done.txt").exists())
        service.store.resolve_wait(self.organization_id, waits[0]["wait_id"], {"status": "approved"}, 1)
        completed = service.run_synchronously(self.organization_id, run["run_id"], timeout=5)
        self.assertEqual(completed["status"], "succeeded", completed)
        self.assertEqual((self.workspace / "done.txt").read_text(), "一次")

    def test_central_approval_also_resumes_parent_workflow(self):
        self.program_write()
        service, run = self.workflow_agent()
        waiting = service.run_synchronously(self.organization_id, run["run_id"], timeout=5)
        self.assertEqual(waiting["status"], "waiting", waiting)
        wait = service.store.list_waits(self.organization_id)[0]
        child_id = wait["payload"]["agent_run_id"]
        child = self.runs.store.get(child_id)
        response = self.client.post(
            "/api/agent-runs/" + child_id + "/approvals",
            json={"approval_id": child["state"]["approval"]["approval_id"], "approved": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = service.run_synchronously(self.organization_id, run["run_id"], timeout=5)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertTrue((self.workspace / "done.txt").exists())

    def test_workflow_approval_is_not_reused_for_next_write(self):
        self.program_write()

        def complete(request):
            if request.context.operation == "completion_check":
                return ModelResponse(
                    CanonicalMessage("assistant", '{"complete":true,"reason":"完成","remaining_steps":[]}')
                )
            count = sum(message.role == "tool" for message in request.messages)
            if count >= 2:
                return ModelResponse(CanonicalMessage("assistant", "两个文件完成"))
            return ModelResponse(
                CanonicalMessage(
                    "assistant",
                    tool_calls=[
                        CanonicalToolCall(
                            str(count),
                            "write_text_file",
                            {"path": str(count) + ".txt", "content": "审批范围", "mode": "create"},
                        )
                    ],
                )
            )

        self.fake_client.complete = complete
        service, run = self.workflow_agent()
        self.assertEqual(service.run_synchronously(self.organization_id, run["run_id"], timeout=5)["status"], "waiting")
        wait = service.store.list_waits(self.organization_id)[0]
        child_id = wait["payload"]["agent_run_id"]
        first = self.runs.store.get(child_id)["state"]["approval"]["approval_id"]
        response = self.client.post(
            "/api/agent-runs/" + child_id + "/approvals", json={"approval_id": first, "approved": True}
        )
        self.assertEqual(response.status_code, 200, response.text)
        child = self.runs.wait(child_id, timeout=5)
        self.assertEqual(child["status"], "waiting_approval")
        self.assertNotEqual(child["state"]["approval"]["approval_id"], first)
        self.assertTrue((self.workspace / "0.txt").exists())
        self.assertFalse((self.workspace / "1.txt").exists())

    def test_workflow_preview_does_not_execute_agent_tools(self):
        self.program_write()
        service, run = self.workflow_agent(test_mode=True)
        result = service.run_synchronously(self.organization_id, run["run_id"], timeout=5)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertFalse((self.workspace / "done.txt").exists())
        self.assertTrue(result["output"]["preview"])

    def test_chat_endpoint_reports_approval_without_executing_write(self):
        self.program_write()
        response = self.client.post(
            "/api/chat", json={"conversation_id": self.conversation["id"], "message": "创建文件"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"type": "approval"', response.text)
        self.assertIn('"status": "waiting_approval"', response.text)
        self.assertFalse((self.workspace / "done.txt").exists())
