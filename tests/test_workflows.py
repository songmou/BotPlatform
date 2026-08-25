"""Workflow DSL, persistence, migration and durable runtime tests."""

from __future__ import annotations

import tempfile
import time
import unittest
import sqlite3
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core.storage.database import Database, DatabaseError
from src.core.workflows.definition import (
    NODE_CATALOG,
    WorkflowValidationError,
    empty_definition,
    validate_declared_output,
    validate_definition,
)
from src.core.workflows.runtime import WorkflowService
from src.core.workflows.store import WorkflowError, WorkflowStore


ORG_ID = "10000000-0000-0000-0000-000000000001"


def definition_with_template():
    value = empty_definition("测试工作流")
    value["inputs"] = [
        {"key": "text", "label": "文本", "type": "string", "required": True}
    ]
    value["nodes"].insert(
        1,
        {
            "id": "render",
            "type": "template",
            "name": "渲染",
            "position": {"x": 300, "y": 160},
            "config": {"text": "结果：{{input.text}}"},
            "error_policy": {"mode": "stop"},
        },
    )
    value["nodes"][-1]["config"] = {"output": "{{nodes.render.output}}"}
    value["edges"] = [
        {"id": "a", "source": "start", "source_port": "default", "target": "render", "target_port": "default"},
        {"id": "b", "source": "render", "source_port": "default", "target": "end", "target_port": "default"},
    ]
    return value


def definition_with_api_trigger():
    value = definition_with_template()
    value["triggers"].append({"id": "api", "type": "api", "config": {}})
    return value


class WorkflowFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "botplatform.sqlite3")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO admin_users(username,password_hash,role_id,created_at) "
                "VALUES ('owner','x',1,'2026-08-11T00:00:00Z')"
            )
            user_id = int(connection.execute("SELECT user_id FROM admin_users WHERE username='owner'").fetchone()[0])
            connection.execute(
                "INSERT INTO users(user_id,display_name,created_at,updated_at) "
                "VALUES (?, 'Owner','2026-08-11T00:00:00Z','2026-08-11T00:00:00Z')",
                (user_id,),
            )
            connection.execute(
                "INSERT INTO tenants(tenant_id,bot_id,user_id,created_at) "
                "VALUES (?, 'organization', ?, '2026-08-11T00:00:00Z')",
                (ORG_ID, "organization:" + ORG_ID),
            )
            connection.execute(
                "INSERT INTO organizations(organization_id,name,created_at,updated_at) "
                "VALUES (?, '测试组织','2026-08-11T00:00:00Z','2026-08-11T00:00:00Z')",
                (ORG_ID,),
            )
            connection.execute(
                "INSERT INTO organization_memberships(membership_id,organization_id,user_id,role,"
                "created_at,updated_at) "
                "VALUES ('membership', ?, ?, 'owner','2026-08-11T00:00:00Z','2026-08-11T00:00:00Z')",
                (ORG_ID, user_id),
            )
        self.user_id = user_id
        self.organizations = MagicMock()
        self.organizations.database = self.database
        self.organizations.get.return_value = {"organization_id": ORG_ID}
        self.store = WorkflowStore(self.organizations)


class WorkflowDefinitionTests(unittest.TestCase):
    def test_normalizes_valid_definition_and_safe_reference(self):
        result = validate_definition(definition_with_template())
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["nodes"][1]["config"]["text"], "结果：{{input.text}}")

    def test_rejects_cycles_unknown_variables_and_secrets(self):
        value = definition_with_template()
        value["edges"].append(
            {"id": "cycle", "source": "end", "source_port": "default", "target": "start", "target_port": "default"}
        )
        with self.assertRaises(WorkflowValidationError):
            validate_definition(value)
        value = definition_with_template()
        value["nodes"][1]["config"]["text"] = "{{input.missing}}"
        with self.assertRaisesRegex(WorkflowValidationError, "不存在的输入"):
            validate_definition(value)
        value = definition_with_template()
        value["nodes"][1]["config"]["api_key"] = "secret"
        with self.assertRaisesRegex(WorkflowValidationError, "不得保存密钥"):
            validate_definition(value)
        value = definition_with_template()
        value["edges"][0]["source_port"] = "unknown"
        with self.assertRaisesRegex(WorkflowValidationError, "无效输出端口"):
            validate_definition(value)

    def test_node_catalog_exposes_complete_editor_contract(self):
        expected = {
            "start", "end", "set_variable", "template", "field_map", "merge", "delay",
            "llm", "extract", "classifier", "agent", "knowledge", "condition", "switch",
            "for_each", "subworkflow", "tool", "script", "datasource", "http",
            "notification", "approval", "human_input",
        }
        self.assertEqual(set(NODE_CATALOG), expected)
        for node_type, item in NODE_CATALOG.items():
            with self.subTest(node_type=node_type):
                self.assertIn("config_fields", item)
                self.assertIn("inputs", item)
                self.assertIn("outputs", item)
                self.assertIn("output_ports", item)
                self.assertIn("side_effect", item)
                self.assertIn("supports_retry", item)
        http_timeout = next(
            field for field in NODE_CATALOG["http"]["config_fields"]
            if field["key"] == "timeout_seconds"
        )
        self.assertEqual(http_timeout["constraints"], {"min": 1, "max": 120})

    def test_node_specific_config_validation(self):
        def flow(node_type, config):
            value = empty_definition("节点校验")
            value["nodes"].insert(1, {
                "id": "subject", "type": node_type, "name": "测试节点",
                "position": {"x": 250, "y": 160}, "config": config,
                "error_policy": {"mode": "stop"},
            })
            value["edges"] = [
                {"id": "a", "source": "start", "source_port": "default", "target": "subject", "target_port": "default"},
                {
                    "id": "b", "source": "subject",
                    "source_port": "true" if node_type == "condition" else "default",
                    "target": "end", "target_port": "default",
                },
            ]
            return value

        invalid = [
            ("template", {"text": ""}, "模板文本"),
            ("set_variable", {"values": []}, "变量映射"),
            ("field_map", {"mapping": []}, "字段映射"),
            ("merge", {"values": []}, "合并对象"),
            ("condition", {"left": "x", "operator": "invalid"}, "操作符"),
            ("switch", {"value": "x", "cases": [{"key": "a", "value": 1}, {"key": "a", "value": 2}]}, "分支键不能重复"),
            ("switch", {"value": "x", "cases": [{"key": "a", "value": 1}, {"key": "b", "value": 1}]}, "匹配值不能重复"),
            ("switch", {"value": "x", "cases": [{"key": "a"}]}, "缺少匹配值"),
            ("knowledge", {"query": "x", "category_ids": {}}, "知识库分类必须是数组"),
            ("tool", {"tool_name": "demo", "arguments": []}, "工具参数必须是对象"),
            ("script", {"script_id": "demo", "parameters": []}, "脚本参数必须是对象"),
            ("subworkflow", {"workflow_id": "child", "inputs": []}, "输入映射必须是对象"),
            ("datasource", {"datasource_id": "db", "sql": "DELETE FROM x"}, "只允许只读"),
            (
                "datasource",
                {
                    "datasource_id": "db",
                    "sql": "WITH changed AS (DELETE FROM x RETURNING *) SELECT * FROM changed",
                },
                "只允许只读",
            ),
            ("http", {"method": "GET", "url": "http://example.com"}, "HTTPS"),
            ("http", {"method": "GET", "url": "https://user:secret@example.com"}, "HTTPS"),
            ("for_each", {"workflow_id": "child", "items": list(range(101))}, "100 项"),
        ]
        for node_type, config, message in invalid:
            with self.subTest(node_type=node_type):
                with self.assertRaisesRegex(WorkflowValidationError, message):
                    validate_definition(flow(node_type, config))

        condition = flow("condition", {"left": "x", "operator": "equals", "right": "x"})
        with self.assertRaisesRegex(WorkflowValidationError, "false"):
            validate_definition(condition)
        self.assertEqual(validate_definition(condition, allow_incomplete=True)["name"], "节点校验")

    def test_field_defaults_and_schedule_cron_are_validated_before_publish(self):
        value = empty_definition("默认值类型")
        value["inputs"] = [{
            "key": "count", "label": "数量", "type": "integer", "default": "1",
        }]
        with self.assertRaisesRegex(WorkflowValidationError, "默认值类型必须为 integer"):
            validate_definition(value)

        value = empty_definition("错误定时")
        value["triggers"] = [{
            "id": "nightly", "type": "schedule", "config": {"cron": "61 * * * *"},
        }]
        with self.assertRaisesRegex(WorkflowValidationError, "有效的五段 cron"):
            validate_definition(value)

        value["triggers"][0]["config"]["cron"] = "0 2 * * 1-5"
        self.assertEqual(validate_definition(value)["triggers"][0]["id"], "nightly")

    def test_declared_output_contract_preserves_extra_fields(self):
        fields = [{"key": "answer", "label": "答案", "type": "string", "required": True}]
        self.assertEqual(validate_declared_output(fields, {"answer": "ok", "extra": 1})["extra"], 1)
        with self.assertRaisesRegex(WorkflowValidationError, "缺少必填输出"):
            validate_declared_output(fields, {})
        with self.assertRaisesRegex(WorkflowValidationError, "类型必须"):
            validate_declared_output(fields, {"answer": 1})

    def test_invalid_numeric_metadata_is_reported_as_chinese_validation_error(self):
        value = empty_definition("错误数字")
        value["nodes"][0]["position"]["x"] = "invalid"
        with self.assertRaisesRegex(WorkflowValidationError, "位置或重试次数必须是数字"):
            validate_definition(value)
        value = empty_definition("错误重试")
        value["nodes"][0]["error_policy"] = {"mode": "retry", "max_retries": 0}
        with self.assertRaisesRegex(WorkflowValidationError, "至少重试 1 次"):
            validate_definition(value)
        value = empty_definition("错误设置")
        value["settings"]["max_steps"] = 501
        with self.assertRaisesRegex(WorkflowValidationError, "最大步骤"):
            validate_definition(value)


class WorkflowStoreTests(WorkflowFixture):
    def test_draft_publish_rollback_and_idempotent_run(self):
        workflow = self.store.create_workflow(
            ORG_ID, "daily_report", "测试工作流", self.user_id, definition_with_template()
        )
        published = self.store.publish(ORG_ID, workflow["workflow_id"], self.user_id)
        self.assertEqual(published["published_version"], 1)
        updated = definition_with_template()
        updated["name"] = "第二版"
        draft = self.store.save_draft(
            ORG_ID,
            workflow["workflow_id"],
            updated,
            published["draft_revision"],
            self.user_id,
        )
        self.assertEqual(draft["published_version"], 1)
        with self.assertRaisesRegex(WorkflowError, "其他成员更新"):
            self.store.save_draft(
                ORG_ID, workflow["workflow_id"], updated, published["draft_revision"], self.user_id
            )
        first = self.store.enqueue_run(
            ORG_ID,
            workflow["workflow_id"],
            {"text": "hello"},
            "api",
            "token",
            self.user_id,
            idempotency_key="same",
        )
        second = self.store.enqueue_run(
            ORG_ID,
            workflow["workflow_id"],
            {"text": "changed"},
            "api",
            "token",
            self.user_id,
            idempotency_key="same",
        )
        self.assertEqual(first["run_id"], second["run_id"])

    def test_incomplete_graph_can_autosave_but_cannot_publish(self):
        workflow = self.store.create_workflow(
            ORG_ID, "incomplete_draft", "未完成草稿", self.user_id, definition_with_template()
        )
        incomplete = definition_with_template()
        incomplete["nodes"].insert(
            -1,
            {
                "id": "pending", "type": "template", "name": "待连线节点",
                "position": {"x": 360, "y": 320}, "config": {"text": "编辑中"},
                "error_policy": {"mode": "stop"},
            },
        )
        saved = self.store.save_draft(
            ORG_ID, workflow["workflow_id"], incomplete, workflow["draft_revision"], self.user_id
        )
        self.assertEqual(len(saved["definition"]["nodes"]), 4)
        with self.assertRaisesRegex(WorkflowValidationError, "没有连接到后续节点"):
            self.store.publish(ORG_ID, workflow["workflow_id"], self.user_id)

    def test_publish_rejects_cross_workflow_recursive_dependency(self):
        flow_a = self.store.create_workflow(
            ORG_ID, "recursive_a", "流程 A", self.user_id, empty_definition("流程 A")
        )
        self.store.publish(ORG_ID, flow_a["workflow_id"], self.user_id)

        definition_b = empty_definition("流程 B")
        definition_b["nodes"].insert(-1, {
            "id": "call_a", "type": "subworkflow", "name": "调用 A",
            "position": {"x": 260, "y": 160},
            "config": {"workflow_id": flow_a["workflow_id"], "inputs": {}},
            "error_policy": {"mode": "stop"},
        })
        definition_b["edges"] = [
            {"id": "ba", "source": "start", "source_port": "default", "target": "call_a", "target_port": "default"},
            {"id": "bb", "source": "call_a", "source_port": "default", "target": "end", "target_port": "default"},
        ]
        flow_b = self.store.create_workflow(
            ORG_ID, "recursive_b", "流程 B", self.user_id, definition_b
        )
        self.store.publish(ORG_ID, flow_b["workflow_id"], self.user_id)

        definition_a = empty_definition("流程 A")
        definition_a["nodes"].insert(-1, {
            "id": "call_b", "type": "subworkflow", "name": "调用 B",
            "position": {"x": 260, "y": 160},
            "config": {"workflow_id": flow_b["workflow_id"], "inputs": {}},
            "error_policy": {"mode": "stop"},
        })
        definition_a["edges"] = [
            {"id": "aa", "source": "start", "source_port": "default", "target": "call_b", "target_port": "default"},
            {"id": "ab", "source": "call_b", "source_port": "default", "target": "end", "target_port": "default"},
        ]
        saved = self.store.save_draft(
            ORG_ID, flow_a["workflow_id"], definition_a,
            self.store.get_workflow(ORG_ID, flow_a["workflow_id"])["draft_revision"],
            self.user_id,
        )
        with self.assertRaisesRegex(WorkflowError, "递归调用"):
            self.store.publish(ORG_ID, saved["workflow_id"], self.user_id)

    def test_access_token_is_hashed_and_revocable(self):
        workflow = self.store.create_workflow(
            ORG_ID, "public_flow", "公开流程", self.user_id, definition_with_api_trigger()
        )
        self.store.publish(ORG_ID, workflow["workflow_id"], self.user_id)
        token = self.store.issue_access_token(ORG_ID, workflow["workflow_id"], "API", self.user_id)
        self.assertTrue(token["token"].startswith("bpwf_"))
        self.assertIsNotNone(self.store.authenticate_token(workflow["workflow_id"], token["token"]))
        with self.database.read() as connection:
            stored = str(
                connection.execute(
                    "SELECT token_hash FROM workflow_access_tokens WHERE token_id=?",
                    (token["token_id"],),
                ).fetchone()[0]
            )
        self.assertNotEqual(stored, token["token"])
        self.store.revoke_access_token(ORG_ID, workflow["workflow_id"], token["token_id"])
        self.assertIsNone(self.store.authenticate_token(workflow["workflow_id"], token["token"]))

    def test_input_types_and_per_organization_concurrency_limit(self):
        workflow = self.store.create_workflow(
            ORG_ID, "typed_flow", "类型流程", self.user_id, definition_with_template()
        )
        self.store.publish(ORG_ID, workflow["workflow_id"], self.user_id)
        with self.assertRaisesRegex(WorkflowError, "类型必须"):
            self.store.enqueue_run(
                ORG_ID, workflow["workflow_id"], {"text": 123}, "manual", "web", self.user_id
            )
        for index in range(3):
            self.store.enqueue_run(
                ORG_ID, workflow["workflow_id"], {"text": str(index)}, "manual", str(index), self.user_id
            )
        self.assertIsNotNone(self.store.claim_run("worker-1", 600))
        self.assertIsNotNone(self.store.claim_run("worker-2", 600))
        self.assertIsNone(self.store.claim_run("worker-3", 600))

    def test_input_size_limit_and_cancel_persist_terminal_state(self):
        workflow = self.store.create_workflow(
            ORG_ID, "input_limits", "输入限制", self.user_id, definition_with_template()
        )
        self.store.publish(ORG_ID, workflow["workflow_id"], self.user_id)
        with self.assertRaisesRegex(WorkflowError, "1 MiB"):
            self.store.enqueue_run(
                ORG_ID, workflow["workflow_id"], {"text": "x" * (1024 * 1024)},
                "manual", "large", self.user_id,
            )
        queued = self.store.enqueue_run(
            ORG_ID, workflow["workflow_id"], {"text": "ok"},
            "manual", "cancel", self.user_id,
        )
        canceled = self.store.cancel_run(ORG_ID, queued["run_id"])
        self.assertEqual(canceled["status"], "canceled")
        with self.assertRaisesRegex(WorkflowError, "已结束"):
            self.store.cancel_run(ORG_ID, queued["run_id"])


class WorkflowNodeContractTests(WorkflowFixture):
    def setUp(self):
        super().setUp()
        self.model = MagicMock()
        self.agent = MagicMock()
        self.knowledge = MagicMock()
        self.scripts = MagicMock()
        self.datasource = MagicMock()
        self.notifications = MagicMock()
        self.service = WorkflowService(
            self.organizations,
            model_router=self.model,
            registry=MagicMock(),
            tool_runtime=MagicMock(),
            agent_service=self.agent,
            knowledge_service=self.knowledge,
            script_service=self.scripts,
            datasource_service=self.datasource,
            notification_service=self.notifications,
        )
        self.run = {
            "run_id": "contract-run", "workflow_id": "contract-flow",
            "organization_id": ORG_ID, "initiated_by": self.user_id,
            "input": {"text": "hello"}, "test_mode": False,
            "allow_side_effects": True,
        }

    @staticmethod
    def node(node_type, node_id="subject"):
        return {"id": node_id, "type": node_type, "name": node_type}

    def execute(self, node_type, config, **kwargs):
        return self.service.execute_node_for_test(
            self.run, self.node(node_type), config, **kwargs
        )

    def test_pure_and_branch_node_outputs(self):
        cases = [
            ("start", {}, {"text": "hello"}, "default"),
            ("end", {"output": {"done": True}}, {"done": True}, "default"),
            ("set_variable", {"values": {"x": 1}}, {"x": 1}, "default"),
            ("template", {"text": "hello"}, {"text": "hello"}, "default"),
            ("field_map", {"mapping": {"x": 1}}, {"x": 1}, "default"),
            ("merge", {"values": {"a": 1}}, {"a": 1}, "default"),
            ("condition", {"left": 2, "operator": "gt", "right": 1}, {"matched": True}, "true"),
            ("switch", {"value": "a", "cases": [{"key": "alpha", "value": "a"}]}, {"value": "a"}, "case:alpha"),
        ]
        for node_type, config, output, port in cases:
            with self.subTest(node_type=node_type):
                result = self.execute(node_type, config)
                self.assertEqual(result.output, output)
                self.assertEqual(result.port, port)

    def test_ai_and_resource_node_outputs_use_production_dispatcher(self):
        with patch.object(self.service, "_llm", return_value={"text": "model"}):
            for node_type in ("llm", "extract", "classifier"):
                self.assertEqual(self.execute(node_type, {"prompt": "x"}).output, {"text": "model"})
        self.agent.generate.return_value = "agent"
        self.assertEqual(self.execute("agent", {"agent_id": "general", "prompt": "x"}).output, {"text": "agent"})
        self.knowledge.search.return_value = [{"content": "hit"}]
        self.assertEqual(self.execute("knowledge", {"query": "x", "limit": 2}).output["items"][0]["content"], "hit")
        with (
            patch.object(self.service, "_execute_tool", return_value={"data": 3}),
            patch.object(self.service, "_safety_required", return_value=False),
        ):
            self.assertEqual(self.execute("tool", {"tool_name": "get_current_time"}).output, {"data": 3})
        self.scripts.requires_approval.return_value = False
        self.scripts.submit.return_value = {"run_id": "script-run", "status": "queued"}
        with patch.object(self.service, "_tenant", return_value=MagicMock()):
            self.assertEqual(
                self.execute("script", {"script_id": "demo", "parameters": {}}).output["run_id"],
                "script-run",
            )
        self.datasource.query.return_value = {"rows": [[1]], "row_count": 1}
        self.assertEqual(self.execute("datasource", {"datasource_id": "db", "sql": "SELECT 1"}).output["row_count"], 1)
        with (
            patch.object(self.service, "_http", return_value={"status_code": 200, "body": {"ok": True}}),
            patch.object(self.service, "_safety_required", return_value=False),
        ):
            self.assertEqual(
                self.execute("http", {"method": "GET", "url": "https://example.com"}).output["status_code"],
                200,
            )
        self.notifications.enqueue_text_to_tenant.return_value = SimpleNamespace(
            notification_ids=["n1"], status="queued"
        )
        with patch.object(self.service, "_safety_required", return_value=False):
            self.assertEqual(self.execute("notification", {"message": "hello"}).output["notification_ids"], ["n1"])

    def test_side_effect_preview_and_failure_boundaries(self):
        preview_run = {**self.run, "test_mode": True, "allow_side_effects": False}
        for node_type, config in (
            ("tool", {"tool_name": "write"}),
            ("script", {"script_id": "script"}),
            ("http", {"method": "POST", "url": "https://example.com"}),
            ("notification", {"message": "hello"}),
        ):
            with self.subTest(node_type=node_type), patch.object(
                self.service, "_safety_required", return_value=False
            ):
                outcome = self.service.execute_node_for_test(
                    preview_run, self.node(node_type), config
                )
                self.assertTrue(outcome.output["preview"])
        with self.assertRaisesRegex(WorkflowError, "固定到已发布版本"):
            self.execute("subworkflow", {"workflow_id": "missing", "inputs": {}})
        with self.assertRaisesRegex(WorkflowError, "100 项"):
            self.execute(
                "for_each",
                {"workflow_id": "child", "items": list(range(101))},
                dependencies={"child": 1},
            )
        with self.assertRaisesRegex(WorkflowError, "深度不能超过 5"):
            self.execute(
                "subworkflow",
                {"workflow_id": "child", "inputs": {}},
                dependencies={"child": 1},
                state={"nodes": {}, "depth": 5},
            )
        with self.assertRaisesRegex(WorkflowError, "不支持的条件操作符"):
            self.execute("condition", {"left": 1, "operator": "bad", "right": 1})

    def test_wait_and_nested_workflow_outputs(self):
        with patch.object(self.service.store, "pending_wait_for_node", return_value=None), patch.object(
            self.service, "_create_wait", return_value={"wait_id": "wait-1", "status": "pending"}
        ):
            self.assertEqual(self.execute("delay", {"seconds": 1}).wait["wait_id"], "wait-1")
            self.assertEqual(
                self.execute("approval", {"title": "approve", "ttl_seconds": 60}).wait["wait_id"],
                "wait-1",
            )
            self.assertEqual(
                self.execute("human_input", {"title": "input", "ttl_seconds": 60, "fields": []}).wait["wait_id"],
                "wait-1",
            )

        resolved_wait = {
            "status": "resolved",
            "response": {
                "status": "resolved",
                "response": {"count": 2, "enabled": True},
            },
        }
        with patch.object(
            self.service.store, "pending_wait_for_node", return_value=resolved_wait
        ):
            resumed = self.execute(
                "human_input", {"title": "input", "ttl_seconds": 60, "fields": []}
            )
        self.assertEqual(resumed.output, {"count": 2, "enabled": True})

        self.service.store.enqueue_run = MagicMock(
            side_effect=[{"run_id": "child-1"}, {"run_id": "child-2"}, {"run_id": "child-3"}]
        )
        self.service.store.start_specific_run = MagicMock(
            side_effect=lambda _org, run_id, _owner: {**self.run, "run_id": run_id, "state": {}}
        )
        self.service.store.get_run = MagicMock(side_effect=[
            {"status": "succeeded", "output": {"answer": 1}},
            {"status": "succeeded", "output": "a"},
            {"status": "succeeded", "output": "b"},
        ])
        with patch.object(self.service, "_execute_run"):
            child = self.execute("subworkflow", {"workflow_id": "child", "inputs": {}}, dependencies={"child": 1})
            each = self.execute("for_each", {"workflow_id": "child", "items": ["a", "b"]}, dependencies={"child": 1})
        self.assertEqual(child.output, {"answer": 1})
        self.assertEqual(each.output, {"items": ["a", "b"]})

    def test_uncertain_external_write_requires_admin_attention(self):
        definition = empty_definition("外部写入")
        definition["nodes"].insert(
            1,
            {
                "id": "send",
                "type": "http",
                "name": "发送请求",
                "position": {"x": 250, "y": 160},
                "config": {"method": "POST", "url": "https://example.com/events"},
                "error_policy": {"mode": "stop"},
            },
        )
        definition["edges"] = [
            {"id": "a", "source": "start", "source_port": "default", "target": "send", "target_port": "default"},
            {"id": "b", "source": "send", "source_port": "default", "target": "end", "target_port": "default"},
        ]
        workflow = self.store.create_workflow(
            ORG_ID, "external_write", "外部写入", self.user_id, definition
        )
        self.store.publish(ORG_ID, workflow["workflow_id"], self.user_id)
        queued = self.store.enqueue_run(
            ORG_ID, workflow["workflow_id"], {}, "manual", "web", self.user_id
        )
        claimed = self.store.claim_run("dead-worker", 600)
        node = validate_definition(definition)["nodes"][1]
        first_node_run = self.store.begin_node(
            claimed, node, {"authorization": "Bearer private", "body": "payload"}, 1
        )
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE workflow_runs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE run_id=?",
                (queued["run_id"],),
            )
        self.assertIsNone(self.store.claim_run("recovery-worker", 600))
        attention = self.store.get_run(ORG_ID, queued["run_id"])
        self.assertEqual(attention["status"], "needs_attention")
        self.assertEqual(attention["node_runs"][0]["input"]["authorization"], "[已脱敏]")
        retried = self.store.resolve_attention(
            ORG_ID, queued["run_id"], "retry", self.user_id, "确认可重试"
        )
        self.assertEqual(retried["status"], "queued")
        claimed_again = self.store.claim_run("recovery-worker", 600)
        second_node_run = self.store.begin_node(claimed_again, node, {}, 2)
        with self.database.read() as connection:
            keys = [
                row[0]
                for row in connection.execute(
                    "SELECT operation_key FROM workflow_node_runs WHERE node_run_id IN (?, ?) ORDER BY attempt",
                    (first_node_run, second_node_run),
                ).fetchall()
            ]
        self.assertEqual(keys, [queued["run_id"] + ":send"] * 2)

    def test_webhook_secret_and_idempotency_are_trigger_scoped(self):
        definition = definition_with_template()
        definition["triggers"].append({"id": "incoming", "type": "webhook", "config": {}})
        workflow = self.store.create_workflow(
            ORG_ID, "webhook_flow", "Webhook 流程", self.user_id, definition
        )
        self.store.publish(ORG_ID, workflow["workflow_id"], self.user_id)
        binding = next(
            item for item in self.store.list_trigger_bindings(ORG_ID, workflow["workflow_id"])
            if item["trigger_type"] == "webhook"
        )
        issued = self.store.issue_webhook_secret(
            ORG_ID, workflow["workflow_id"], binding["trigger_id"]
        )
        authenticated = self.store.authenticate_webhook(binding["trigger_id"], issued["token"])
        self.assertIsNotNone(authenticated)
        first = self.store.enqueue_run(
            ORG_ID, workflow["workflow_id"], {"text": "first"}, "webhook",
            binding["trigger_id"], None, idempotency_key="event-1",
        )
        second = self.store.enqueue_run(
            ORG_ID, workflow["workflow_id"], {"text": "second"}, "webhook",
            binding["trigger_id"], None, idempotency_key="event-1",
        )
        self.assertEqual(first["run_id"], second["run_id"])
        self.store.revoke_webhook_secret(ORG_ID, workflow["workflow_id"], binding["trigger_id"])
        self.assertIsNone(self.store.authenticate_webhook(binding["trigger_id"], issued["token"]))


class WorkflowRuntimeTests(WorkflowFixture):
    @staticmethod
    def _agent_policy_definition(mode, output):
        definition = empty_definition("错误策略")
        definition["nodes"].insert(1, {
            "id": "unstable", "type": "agent", "name": "不稳定智能体",
            "position": {"x": 250, "y": 160},
            "config": {"agent_id": "unstable", "prompt": "执行"},
            "error_policy": {
                "mode": mode,
                "max_retries": 1 if mode == "retry" else 0,
            },
        })
        definition["nodes"][-1]["config"] = {"output": output}
        definition["edges"] = [
            {"id": "a", "source": "start", "source_port": "default", "target": "unstable", "target_port": "default"},
            {
                "id": "b", "source": "unstable",
                "source_port": "error" if mode == "error_branch" else "default",
                "target": "end", "target_port": "default",
            },
        ]
        return definition

    def test_worker_executes_published_workflow_and_records_nodes(self):
        workflow = self.store.create_workflow(
            ORG_ID, "runtime", "运行测试", self.user_id, definition_with_template()
        )
        self.store.publish(ORG_ID, workflow["workflow_id"], self.user_id)
        service = WorkflowService(self.organizations, max_workers=1)
        service.start()
        self.addCleanup(service.shutdown)
        run = service.enqueue(
            ORG_ID,
            workflow["workflow_id"],
            {"text": "你好"},
            "manual",
            "web",
            self.user_id,
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            result = self.store.get_run(ORG_ID, run["run_id"])
            if result["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["output"], {"text": "结果：你好"})
        self.assertEqual([item["node_id"] for item in result["node_runs"]], ["start", "render", "end"])

    def test_draft_test_run_resolves_published_subworkflow_version(self):
        child = self.store.create_workflow(
            ORG_ID, "test_child", "测试子流程", self.user_id, empty_definition("测试子流程")
        )
        published_child = self.store.publish(
            ORG_ID, child["workflow_id"], self.user_id
        )
        parent_definition = empty_definition("草稿父流程")
        parent_definition["nodes"].insert(-1, {
            "id": "child", "type": "subworkflow", "name": "调用子流程",
            "position": {"x": 280, "y": 160},
            "config": {"workflow_id": child["workflow_id"], "inputs": {}},
            "error_policy": {"mode": "stop"},
        })
        parent_definition["edges"] = [
            {"id": "a", "source": "start", "source_port": "default", "target": "child", "target_port": "default"},
            {"id": "b", "source": "child", "source_port": "default", "target": "end", "target_port": "default"},
        ]
        parent = self.store.create_workflow(
            ORG_ID, "test_parent", "草稿父流程", self.user_id, parent_definition
        )
        run = self.store.enqueue_run(
            ORG_ID, parent["workflow_id"], {}, "test", "web", self.user_id,
            test_mode=True,
        )
        changed_parent = empty_definition("后来修改的草稿")
        self.store.save_draft(
            ORG_ID, parent["workflow_id"], changed_parent,
            parent["draft_revision"], self.user_id,
        )
        service = WorkflowService(self.organizations)
        definition, dependencies = service._definition_and_dependencies(run)
        self.assertEqual(definition["name"], "草稿父流程")
        self.assertEqual(definition["nodes"][1]["type"], "subworkflow")
        self.assertEqual(
            dependencies[child["workflow_id"]], published_child["published_version"]
        )

    def test_retry_continue_and_error_branch_use_real_scheduler_state(self):
        agent = MagicMock()
        service = WorkflowService(self.organizations, agent_service=agent)

        retry_flow = self.store.create_workflow(
            ORG_ID, "retry_policy", "重试策略", self.user_id,
            self._agent_policy_definition("retry", {"retried": True}),
        )
        self.store.publish(ORG_ID, retry_flow["workflow_id"], self.user_id)
        agent.generate.side_effect = [RuntimeError("暂时失败"), "恢复成功"]
        retry_run = self.store.enqueue_run(
            ORG_ID, retry_flow["workflow_id"], {}, "manual", "retry", self.user_id
        )
        claimed = self.store.claim_run("retry-worker", 600)
        service._execute_run(claimed, "retry-worker")
        self.assertEqual(self.store.get_run(ORG_ID, retry_run["run_id"])["status"], "queued")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE workflow_runs SET wake_at='2000-01-01T00:00:00+00:00' WHERE run_id=?",
                (retry_run["run_id"],),
            )
        claimed = self.store.claim_run("retry-worker", 600)
        service._execute_run(claimed, "retry-worker")
        retried = self.store.get_run(ORG_ID, retry_run["run_id"])
        self.assertEqual(retried["status"], "succeeded", retried)
        self.assertEqual(
            [item["attempt"] for item in retried["node_runs"] if item["node_id"] == "unstable"],
            [1, 2],
        )

        for index, mode in enumerate(("continue", "error_branch"), start=1):
            with self.subTest(mode=mode):
                definition = self._agent_policy_definition(mode, {"mode": mode})
                workflow = self.store.create_workflow(
                    ORG_ID, "policy_{}".format(index), "{}策略".format(mode),
                    self.user_id, definition,
                )
                self.store.publish(ORG_ID, workflow["workflow_id"], self.user_id)
                agent.generate.side_effect = RuntimeError("确定失败")
                run = self.store.enqueue_run(
                    ORG_ID, workflow["workflow_id"], {}, "manual", mode, self.user_id
                )
                owner = mode + "-worker"
                claimed = self.store.claim_run(owner, 600)
                service._execute_run(claimed, owner)
                result = self.store.get_run(ORG_ID, run["run_id"])
                self.assertEqual(result["status"], "succeeded", result)
                self.assertEqual(result["output"], {"mode": mode})
                failed = next(item for item in result["node_runs"] if item["node_id"] == "unstable")
                self.assertEqual(failed["status"], "failed")

    def test_resource_validation_names_the_node_context(self):
        resources = MagicMock()
        resources.get_effective.side_effect = ValueError("not found")
        service = WorkflowService(self.organizations, resources)
        definition = empty_definition("资源校验")
        definition["nodes"].insert(-1, {
            "id": "agent", "type": "agent", "name": "客服智能体",
            "position": {"x": 300, "y": 160},
            "config": {"agent_id": "missing", "prompt": "你好"},
            "error_policy": {"mode": "stop"},
        })
        definition["edges"] = [
            {"id": "a", "source": "start", "source_port": "default", "target": "agent", "target_port": "default"},
            {"id": "b", "source": "agent", "source_port": "default", "target": "end", "target_port": "default"},
        ]
        with self.assertRaisesRegex(WorkflowError, "客服智能体.*不存在或无权访问"):
            service.validate_resources(ORG_ID, definition)

    def test_resource_validation_requires_model_and_preflights_datasource_sql(self):
        model_definition = empty_definition("模型校验")
        model_definition["nodes"].insert(-1, {
            "id": "writer", "type": "llm", "name": "撰写答案",
            "position": {"x": 300, "y": 160},
            "config": {"prompt": "你好"}, "error_policy": {"mode": "stop"},
        })
        model_definition["edges"] = [
            {"id": "a", "source": "start", "source_port": "default", "target": "writer", "target_port": "default"},
            {"id": "b", "source": "writer", "source_port": "default", "target": "end", "target_port": "default"},
        ]
        with self.assertRaisesRegex(WorkflowError, "撰写答案.*模型服务不可用"):
            WorkflowService(self.organizations).validate_resources(ORG_ID, model_definition)

        datasource = MagicMock()
        datasource.get_config.return_value = {"id": "sales", "enabled": True}
        datasource.validate_readonly_query.side_effect = ValueError("表 sales.secret 未获授权")
        data_definition = empty_definition("数据校验")
        data_definition["nodes"].insert(-1, {
            "id": "query", "type": "datasource", "name": "查询销售数据",
            "position": {"x": 300, "y": 160},
            "config": {"datasource_id": "sales", "sql": "SELECT * FROM secret", "limit": 10},
            "error_policy": {"mode": "stop"},
        })
        data_definition["edges"] = [
            {"id": "a", "source": "start", "source_port": "default", "target": "query", "target_port": "default"},
            {"id": "b", "source": "query", "source_port": "default", "target": "end", "target_port": "default"},
        ]
        with self.assertRaisesRegex(WorkflowError, "查询销售数据.*不存在或无权访问"):
            WorkflowService(
                self.organizations, datasource_service=datasource
            ).validate_resources(ORG_ID, data_definition)
        datasource.validate_readonly_query.assert_called_once_with(
            "sales", "SELECT * FROM secret", limit=10
        )

    def test_ai_invalid_json_is_a_safe_chinese_workflow_error(self):
        session = MagicMock()
        session.complete.return_value = SimpleNamespace(
            message=SimpleNamespace(content="not-json")
        )
        router = MagicMock()
        router.session.return_value = session
        service = WorkflowService(self.organizations, model_router=router)
        with self.assertRaisesRegex(WorkflowError, "未返回有效 JSON"):
            service.design_suggestion(
                ORG_ID, "生成问候工作流", empty_definition("AI"), self.user_id
            )

    def test_model_adapter_failures_are_safe_chinese_workflow_errors(self):
        router = MagicMock()
        router.session.side_effect = RuntimeError("provider secret=private")
        service = WorkflowService(self.organizations, model_router=router)
        run = {
            "run_id": "model-failure", "workflow_id": "flow",
            "organization_id": ORG_ID, "initiated_by": self.user_id,
        }
        with self.assertRaisesRegex(WorkflowError, "模型节点调用失败") as raised:
            service.execute_node_for_test(
                run, {"id": "llm", "type": "llm", "name": "模型"},
                {"prompt": "hello"},
            )
        self.assertNotIn("private", str(raised.exception))

    def test_approval_wait_resumes_after_worker_restart(self):
        definition = empty_definition("审批恢复")
        definition["nodes"] = [
            definition["nodes"][0],
            {
                "id": "gate", "type": "approval", "name": "审批", "position": {"x": 250, "y": 160},
                "config": {"title": "请审批", "ttl_seconds": 3600}, "error_policy": {"mode": "stop"},
            },
            {
                "id": "approved_end", "type": "end", "name": "通过",
                "position": {"x": 500, "y": 100}, "config": {"output": {"approved": True}},
                "error_policy": {"mode": "stop"},
            },
            {
                "id": "rejected_end", "type": "end", "name": "拒绝",
                "position": {"x": 500, "y": 240}, "config": {"output": {"approved": False}},
                "error_policy": {"mode": "stop"},
            },
        ]
        definition["edges"] = [
            {"id": "a", "source": "start", "source_port": "default", "target": "gate", "target_port": "default"},
            {
                "id": "b", "source": "gate", "source_port": "approved",
                "target": "approved_end", "target_port": "default",
            },
            {
                "id": "c", "source": "gate", "source_port": "rejected",
                "target": "rejected_end", "target_port": "default",
            },
        ]
        workflow = self.store.create_workflow(
            ORG_ID, "approval_restart", "审批恢复", self.user_id, definition
        )
        self.store.publish(ORG_ID, workflow["workflow_id"], self.user_id)
        first_service = WorkflowService(self.organizations, max_workers=1)
        first_service.start()
        run = first_service.enqueue(
            ORG_ID, workflow["workflow_id"], {}, "manual", "web", self.user_id
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            waiting_run = self.store.get_run(ORG_ID, run["run_id"])
            if waiting_run["status"] == "waiting":
                break
            time.sleep(0.05)
        first_service.shutdown()
        self.assertEqual(waiting_run["status"], "waiting")
        wait = self.store.list_waits(ORG_ID)[0]
        self.store.resolve_wait(ORG_ID, wait["wait_id"], {"status": "approved", "comment": "同意"}, self.user_id)
        resumed_service = WorkflowService(self.organizations, max_workers=1)
        resumed_service.start()
        self.addCleanup(resumed_service.shutdown)
        deadline = time.time() + 5
        while time.time() < deadline:
            result = self.store.get_run(ORG_ID, run["run_id"])
            if result["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["output"], {"approved": True})


class WorkflowMigrationTests(unittest.TestCase):
    def test_v1_database_is_backed_up_and_upgraded_without_data_loss(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "botplatform.sqlite3"
            database = Database(path)
            workflow_tables = [
                "workflow_waits", "workflow_events", "workflow_node_runs", "workflow_runs",
                "workflow_access_tokens", "workflow_trigger_bindings",
                "organization_workflow_versions", "organization_workflows",
            ]
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT INTO tenants(tenant_id,bot_id,user_id,created_at) VALUES ('preserved','bot','user','now')"
                )
                for table in workflow_tables:
                    connection.execute("DROP TABLE " + table)
                connection.execute("UPDATE schema_metadata SET format_version=1 WHERE singleton=1")
            upgraded = Database(path)
            with upgraded.read() as connection:
                self.assertEqual(connection.execute("SELECT format_version FROM schema_metadata").fetchone()[0], 3)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM tenants WHERE tenant_id='preserved'").fetchone()[0],
                    1,
                )
                self.assertIsNotNone(
                    connection.execute("SELECT name FROM sqlite_master WHERE name='workflow_runs'").fetchone()
                )
            self.assertEqual(len(list((Path(temporary) / "backups").glob("*-v1-*.sqlite3"))), 1)

    def test_failed_v2_migration_rolls_back_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "botplatform.sqlite3"
            database = Database(path)
            workflow_tables = [
                "workflow_waits", "workflow_events", "workflow_node_runs", "workflow_runs",
                "workflow_access_tokens", "workflow_trigger_bindings",
                "organization_workflow_versions", "organization_workflows",
            ]
            with database.transaction(immediate=True) as connection:
                for table in workflow_tables:
                    connection.execute("DROP TABLE " + table)
                connection.execute("UPDATE schema_metadata SET format_version=1 WHERE singleton=1")
            with patch(
                "src.core.storage.database.WORKFLOW_SCHEMA_V2",
                "CREATE TABLE migration_probe(value TEXT);\nCREATE TABLE invalid(",
            ):
                with self.assertRaisesRegex(DatabaseError, "备份位于"):
                    Database(path)
            connection = sqlite3.connect(str(path))
            try:
                self.assertEqual(
                    connection.execute("SELECT format_version FROM schema_metadata").fetchone()[0],
                    1,
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE name='migration_probe'"
                    ).fetchone()
                )
            finally:
                connection.close()
            self.assertEqual(len(list((Path(temporary) / "backups").glob("*-v1-*.sqlite3"))), 1)


if __name__ == "__main__":
    unittest.main()
