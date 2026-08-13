"""End-to-end FastAPI tests for workflow authoring and public triggers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.core.modeling import ModelRouter
from src.core.services.auth import AdminAuthService
from src.core.storage.admin_users import AdminRoleStore, AdminSessionStore, AdminUserStore
from src.core.storage.database import Database
from src.core.storage.tenants import TenantContext
from src.core.workflows.definition import empty_definition
from tests.test_web_api import FakeClient, _make_config


class WorkflowApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        database = Database(Path(self.temporary.name) / "botplatform.sqlite3")
        registry = MagicMock()
        registry.database = database
        registry.system_root = Path(self.temporary.name)
        registry.tenant_root.side_effect = lambda tenant_id: Path(self.temporary.name) / tenant_id
        registry.get.side_effect = lambda tenant_id: TenantContext(tenant_id, "organization", "organization:" + tenant_id)
        admin_users = AdminUserStore(database)
        admin_roles = AdminRoleStore(database)
        sessions = AdminSessionStore(database, b"workflow-tests")
        auth = AdminAuthService(admin_users, admin_roles, sessions, Path(self.temporary.name))
        admin_users.create("admin", "password12345", admin_roles.get_by_code("admin").role_id)
        admin_users.create("viewer", "password12345", admin_roles.get_by_code("viewer").role_id)
        app = create_app(
            _make_config(),
            ModelRouter.single(FakeClient()),
            registry,
            MagicMock(),
            admin_auth=auth,
            admin_user_store=admin_users,
            admin_role_store=admin_roles,
        )
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()
        self.addCleanup(self.client_context.__exit__, None, None, None)
        response = self.client.post("/api/auth/login", json={"username": "admin", "password": "password12345"})
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post("/api/v2/platform/organizations", json={"name": "Workflow Org"})
        self.assertEqual(response.status_code, 201, response.text)
        self.organization_id = response.json()["organization"]["organization_id"]

    def test_lifecycle_secrets_and_archive_contract(self):
        definition = empty_definition("生命周期测试")
        definition["triggers"].extend([
            {"id": "public_api", "type": "api", "config": {}},
            {"id": "incoming", "type": "webhook", "config": {}},
        ])
        created = self.client.post(
            "/api/v2/orgs/{}/workflows".format(self.organization_id),
            json={"id": "lifecycle", "name": "生命周期测试", "definition": definition},
        )
        self.assertEqual(created.status_code, 201, created.text)
        workflow_id = created.json()["workflow_id"]
        first = self.client.post(
            "/api/v2/orgs/{}/workflows/{}/publish".format(self.organization_id, workflow_id)
        )
        self.assertEqual(first.status_code, 200, first.text)

        updated = dict(definition)
        updated["description"] = "第二版"
        saved = self.client.put(
            "/api/v2/orgs/{}/workflows/{}/draft".format(self.organization_id, workflow_id),
            json={"definition": updated, "base_revision": first.json()["draft_revision"]},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        second = self.client.post(
            "/api/v2/orgs/{}/workflows/{}/publish".format(self.organization_id, workflow_id)
        )
        self.assertEqual(second.status_code, 200, second.text)
        rolled_back = self.client.post(
            "/api/v2/orgs/{}/workflows/{}/rollback".format(self.organization_id, workflow_id),
            json={"version": 1},
        )
        self.assertEqual(rolled_back.status_code, 200, rolled_back.text)
        self.assertEqual(rolled_back.json()["published_version"], 3)
        detail = self.client.get(
            "/api/v2/orgs/{}/workflows/{}".format(self.organization_id, workflow_id)
        ).json()
        self.assertEqual([item["version"] for item in detail["versions"]], [3, 2, 1])

        token_response = self.client.post(
            "/api/v2/orgs/{}/workflows/{}/access-tokens".format(self.organization_id, workflow_id),
            json={"label": "生命周期令牌"},
        )
        self.assertEqual(token_response.status_code, 201, token_response.text)
        token_data = token_response.json()
        listed_tokens = self.client.get(
            "/api/v2/orgs/{}/workflows/{}/access-tokens".format(self.organization_id, workflow_id)
        ).json()["items"]
        self.assertNotIn("token", listed_tokens[0])
        revoked = self.client.delete(
            "/api/v2/orgs/{}/workflows/{}/access-tokens/{}".format(
                self.organization_id, workflow_id, token_data["token_id"]
            )
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        denied = self.client.post(
            "/api/workflows/v1/{}/run".format(workflow_id),
            headers={"Authorization": "Bearer " + token_data["token"]},
            json={"inputs": {}},
        )
        self.assertEqual(denied.status_code, 401, denied.text)

        trigger = next(item for item in detail["trigger_bindings"] if item["trigger_key"] == "incoming")
        webhook_response = self.client.post(
            "/api/v2/orgs/{}/workflows/{}/webhook-triggers/{}/secret".format(
                self.organization_id, workflow_id, trigger["trigger_id"]
            )
        )
        self.assertEqual(webhook_response.status_code, 201, webhook_response.text)
        webhook_token = webhook_response.json()["token"]
        webhook_revoked = self.client.delete(
            "/api/v2/orgs/{}/workflows/{}/webhook-triggers/{}/secret".format(
                self.organization_id, workflow_id, trigger["trigger_id"]
            )
        )
        self.assertEqual(webhook_revoked.status_code, 200, webhook_revoked.text)
        denied_hook = self.client.post(
            "/api/workflows/v1/hooks/{}".format(trigger["trigger_id"]),
            headers={"Authorization": "Bearer " + webhook_token},
            json={"value": 1},
        )
        self.assertEqual(denied_hook.status_code, 401, denied_hook.text)

        credential = self.client.put(
            "/api/v2/orgs/{}/workflow-http-credentials/e2e_https".format(self.organization_id),
            json={"label": "E2E HTTPS", "secret": '{"Authorization":"Bearer secret"}'},
        )
        self.assertEqual(credential.status_code, 200, credential.text)
        self.assertNotIn("secret", credential.json())
        credentials = self.client.get(
            "/api/v2/orgs/{}/credentials".format(self.organization_id)
        ).json()["items"]
        self.assertEqual([item["credential_id"] for item in credentials], ["e2e_https"])
        deleted = self.client.delete(
            "/api/v2/orgs/{}/credentials/e2e_https".format(self.organization_id)
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)

        disabled = self.client.post(
            "/api/v2/orgs/{}/workflows/{}/unpublish".format(self.organization_id, workflow_id)
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertEqual(disabled.json()["status"], "disabled")
        archived = self.client.post(
            "/api/v2/orgs/{}/workflows/{}/archive".format(self.organization_id, workflow_id)
        )
        self.assertEqual(archived.status_code, 200, archived.text)
        self.assertEqual(archived.json()["status"], "archived")
        listed = self.client.get(
            "/api/v2/orgs/{}/workflows".format(self.organization_id)
        ).json()["items"]
        self.assertFalse(any(item["workflow_id"] == workflow_id for item in listed))
        retained = self.client.get(
            "/api/v2/orgs/{}/workflows/{}".format(self.organization_id, workflow_id)
        )
        self.assertEqual(retained.status_code, 200, retained.text)
        self.assertEqual(retained.json()["status"], "archived")

    def test_platform_viewer_can_read_but_cannot_write_templates(self):
        definition = empty_definition("只读模板")
        created = self.client.put(
            "/api/v2/platform/workflow-templates/readonly_template/draft",
            json={"definition": definition},
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.client.post("/api/auth/logout")
        login = self.client.post(
            "/api/auth/login", json={"username": "viewer", "password": "password12345"}
        )
        self.assertEqual(login.status_code, 200, login.text)
        readable = self.client.get("/api/v2/platform/workflow-templates")
        self.assertEqual(readable.status_code, 200, readable.text)
        blocked = self.client.put(
            "/api/v2/platform/workflow-templates/readonly_template/draft",
            json={"definition": definition},
        )
        self.assertEqual(blocked.status_code, 403, blocked.text)

    def test_wait_listing_and_resolution_follow_assignee_scope(self):
        invitation = self.client.post(
            "/api/v2/orgs/{}/invitations".format(self.organization_id),
            json={"role": "member"},
        )
        self.assertEqual(invitation.status_code, 201, invitation.text)
        member = TestClient(self.client_context.app)
        self.addCleanup(member.close)
        accepted = member.post(
            "/api/v2/invitations/accept",
            json={
                "token": invitation.json()["invitation_token"],
                "username": "workflow_member",
                "password": "password12345",
            },
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        login = member.post(
            "/api/auth/login",
            json={"username": "workflow_member", "password": "password12345"},
        )
        self.assertEqual(login.status_code, 200, login.text)

        definition = empty_definition("管理员审批")
        definition["nodes"] = [
            definition["nodes"][0],
            {
                "id": "gate", "type": "approval", "name": "管理员审批",
                "position": {"x": 250, "y": 160},
                "config": {
                    "title": "仅管理员可处理", "ttl_seconds": 3600,
                    "assignees": {"roles": ["admin"]},
                },
                "error_policy": {"mode": "stop"},
            },
            {"id": "yes", "type": "end", "name": "通过", "position": {"x": 500, "y": 80}, "config": {"output": {"approved": True}}, "error_policy": {"mode": "stop"}},
            {"id": "no", "type": "end", "name": "拒绝", "position": {"x": 500, "y": 240}, "config": {"output": {"approved": False}}, "error_policy": {"mode": "stop"}},
        ]
        definition["edges"] = [
            {"id": "a", "source": "start", "source_port": "default", "target": "gate", "target_port": "default"},
            {"id": "b", "source": "gate", "source_port": "approved", "target": "yes", "target_port": "default"},
            {"id": "c", "source": "gate", "source_port": "rejected", "target": "no", "target_port": "default"},
        ]
        created = self.client.post(
            "/api/v2/orgs/{}/workflows".format(self.organization_id),
            json={"id": "admin_wait", "name": "管理员审批", "definition": definition},
        )
        self.assertEqual(created.status_code, 201, created.text)
        workflow_id = created.json()["workflow_id"]
        published = self.client.post(
            "/api/v2/orgs/{}/workflows/{}/publish".format(self.organization_id, workflow_id)
        )
        self.assertEqual(published.status_code, 200, published.text)
        started = self.client.post(
            "/api/v2/orgs/{}/workflows/{}/runs".format(self.organization_id, workflow_id),
            json={"inputs": {}},
        )
        self.assertEqual(started.status_code, 202, started.text)
        wait_id = ""
        for _ in range(100):
            waits = self.client.get(
                "/api/v2/orgs/{}/workflow-waits".format(self.organization_id)
            ).json()["items"]
            if waits:
                wait_id = waits[0]["wait_id"]
                break
            import time
            time.sleep(0.02)
        self.assertTrue(wait_id)
        member_waits = member.get(
            "/api/v2/orgs/{}/workflow-waits".format(self.organization_id)
        )
        self.assertEqual(member_waits.status_code, 200, member_waits.text)
        self.assertEqual(member_waits.json()["items"], [])
        denied = member.post(
            "/api/v2/orgs/{}/workflow-waits/{}/resolve".format(self.organization_id, wait_id),
            json={"status": "approved", "comment": "越权"},
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        resolved = self.client.post(
            "/api/v2/orgs/{}/workflow-waits/{}/resolve".format(self.organization_id, wait_id),
            json={"status": "approved", "comment": "允许"},
        )
        self.assertEqual(resolved.status_code, 200, resolved.text)

    def test_delay_and_attention_waits_cannot_use_generic_resolution(self):
        service = self.client_context.app.state.workflow_service
        workflow = service.store.create_workflow(
            self.organization_id, "wait_guards", "待办保护", 1, empty_definition("待办保护")
        )
        service.store.publish(self.organization_id, workflow["workflow_id"], 1)
        run = service.store.enqueue_run(
            self.organization_id, workflow["workflow_id"], {}, "manual", "guard", 1
        )
        for wait_type in ("delay", "attention"):
            wait = service.store.create_wait(
                run, wait_type, wait_type, {}, {"roles": ["admin"]}, None
            )
            response = self.client.post(
                "/api/v2/orgs/{}/workflow-waits/{}/resolve".format(
                    self.organization_id, wait["wait_id"]
                ),
                json={"status": "resolved"},
            )
            self.assertEqual(response.status_code, 400, response.text)

    def test_human_input_wait_validates_declared_field_types(self):
        service = self.client_context.app.state.workflow_service
        workflow = service.store.create_workflow(
            self.organization_id, "typed_wait", "类型待办", 1, empty_definition("类型待办")
        )
        service.store.publish(self.organization_id, workflow["workflow_id"], 1)
        run = service.store.enqueue_run(
            self.organization_id, workflow["workflow_id"], {}, "manual", "typed", 1
        )
        fields = [
            {"key": "count", "label": "数量", "type": "integer", "required": True},
            {"key": "enabled", "label": "启用", "type": "boolean", "required": True},
            {"key": "metadata", "label": "元数据", "type": "object", "required": False},
        ]
        wait = service.store.create_wait(
            run, "collect", "input", {"title": "补充信息", "fields": fields},
            {"roles": ["admin"]}, None,
        )
        url = "/api/v2/orgs/{}/workflow-waits/{}/resolve".format(
            self.organization_id, wait["wait_id"]
        )
        missing = self.client.post(
            url, json={"status": "resolved", "response": {"enabled": True}}
        )
        self.assertEqual(missing.status_code, 400, missing.text)
        self.assertIn("数量", missing.json()["detail"])
        wrong_type = self.client.post(
            url,
            json={"status": "resolved", "response": {"count": "2", "enabled": True}},
        )
        self.assertEqual(wrong_type.status_code, 400, wrong_type.text)
        self.assertIn("integer", wrong_type.json()["detail"])
        resolved = self.client.post(
            url,
            json={
                "status": "resolved",
                "response": {"count": 2, "enabled": True, "metadata": {"source": "web"}},
            },
        )
        self.assertEqual(resolved.status_code, 200, resolved.text)
        self.assertEqual(resolved.json()["response"]["response"]["count"], 2)

    def test_organization_authoring_publish_run_and_public_token(self):
        definition = empty_definition("公开测试")
        definition["triggers"].append({"id": "public_api", "type": "api", "config": {}})
        response = self.client.post(
            "/api/v2/orgs/{}/workflows".format(self.organization_id),
            json={"id": "public_flow", "name": "公开测试", "definition": definition},
        )
        self.assertEqual(response.status_code, 201, response.text)
        workflow_id = response.json()["workflow_id"]
        response = self.client.post(
            "/api/v2/orgs/{}/workflows/{}/publish".format(self.organization_id, workflow_id)
        )
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post(
            "/api/v2/orgs/{}/workflows/{}/test".format(self.organization_id, workflow_id),
            json={"inputs": {"authorization": "Bearer hidden"}, "wait": True},
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "succeeded", response.text)
        self.assertEqual(response.json()["input"]["authorization"], "[已脱敏]")
        token_response = self.client.post(
            "/api/v2/orgs/{}/workflows/{}/access-tokens".format(self.organization_id, workflow_id),
            json={"label": "test"},
        )
        self.assertEqual(token_response.status_code, 201, token_response.text)
        token = token_response.json()["token"]
        invalid_timeout = self.client.post(
            "/api/workflows/v1/{}/run".format(workflow_id),
            headers={"Authorization": "Bearer " + token},
            json={"inputs": {}, "wait": True, "timeout": "invalid"},
        )
        self.assertEqual(invalid_timeout.status_code, 400, invalid_timeout.text)
        self.assertIsInstance(invalid_timeout.json()["detail"], str)
        public = self.client.post(
            "/api/workflows/v1/{}/run".format(workflow_id),
            headers={"Authorization": "Bearer " + token, "Idempotency-Key": "api-test"},
            json={"inputs": {}, "wait": True},
        )
        self.assertEqual(public.status_code, 202, public.text)
        self.assertEqual(public.json()["status"], "succeeded")
        self.assertNotIn("state", public.json())
        self.assertNotIn("node_runs", public.json())
        self.assertNotIn("input", public.json())
        status = self.client.get(
            "/api/workflows/v1/runs/{}".format(public.json()["run_id"]),
            headers={"Authorization": "Bearer " + token},
        )
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["workflow_id"], workflow_id)
        self.assertNotIn("state", status.json())
        page = self.client.get(
            "/organization/workflows?organization_id={}".format(self.organization_id)
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn("workflow-canvas", page.text)

    def test_platform_template_draft_publish_and_copy(self):
        child = empty_definition("平台子流程")
        response = self.client.put(
            "/api/v2/platform/workflow-templates/child_template/draft",
            json={"definition": child},
        )
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post(
            "/api/v2/platform/workflow-templates/child_template/publish"
        )
        self.assertEqual(response.status_code, 200, response.text)
        definition = empty_definition("平台模板")
        definition["nodes"].insert(
            1,
            {
                "id": "child",
                "type": "subworkflow",
                "name": "调用子流程",
                "position": {"x": 250, "y": 160},
                "config": {"workflow_id": "child_template", "inputs": {}},
                "error_policy": {"mode": "stop"},
            },
        )
        definition["edges"] = [
            {"id": "a", "source": "start", "source_port": "default", "target": "child", "target_port": "default"},
            {"id": "b", "source": "child", "source_port": "default", "target": "end", "target_port": "default"},
        ]
        response = self.client.put(
            "/api/v2/platform/workflow-templates/report_template/draft",
            json={"definition": definition},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "draft")
        response = self.client.post(
            "/api/v2/platform/workflow-templates/report_template/publish"
        )
        self.assertEqual(response.status_code, 200, response.text)
        copied = self.client.post(
            "/api/v2/orgs/{}/workflow-templates/report_template/copy".format(self.organization_id),
            json={"id": "org_report", "name": "组织报表"},
        )
        self.assertEqual(copied.status_code, 201, copied.text)
        self.assertEqual(copied.json()["template_resource_id"], "report_template")
        self.assertEqual(copied.json()["definition"]["name"], "组织报表")
        child_workflow_id = copied.json()["definition"]["nodes"][1]["config"]["workflow_id"]
        workflows = self.client.get(
            "/api/v2/orgs/{}/workflows".format(self.organization_id)
        )
        self.assertEqual(workflows.status_code, 200, workflows.text)
        copied_child = next(
            item for item in workflows.json()["items"] if item["workflow_id"] == child_workflow_id
        )
        self.assertEqual(copied_child["status"], "published")
        root_publish = self.client.post(
            "/api/v2/orgs/{}/workflows/{}/publish".format(
                self.organization_id, copied.json()["workflow_id"]
            )
        )
        self.assertEqual(root_publish.status_code, 200, root_publish.text)

    def test_platform_template_allows_incomplete_draft_but_rejects_publish(self):
        definition = empty_definition("编辑中的平台模板")
        definition["nodes"].insert(
            -1,
            {
                "id": "pending",
                "type": "template",
                "name": "待连线节点",
                "position": {"x": 360, "y": 320},
                "config": {"text": ""},
                "error_policy": {"mode": "stop"},
            },
        )
        saved = self.client.put(
            "/api/v2/platform/workflow-templates/incomplete_template/draft",
            json={"definition": definition},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["status"], "draft")
        self.assertEqual(len(saved.json()["payload"]["nodes"]), 3)

        listed = self.client.get("/api/v2/platform/workflow-templates")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()["items"][0]["payload"]["nodes"]), 3)

        published = self.client.post(
            "/api/v2/platform/workflow-templates/incomplete_template/publish"
        )
        self.assertEqual(published.status_code, 400, published.text)
        self.assertIn("没有连接到后续节点", published.json()["detail"])

    def test_catalog_contract_and_platform_validation_endpoint(self):
        catalog = self.client.get("/api/v2/workflow-node-catalog")
        self.assertEqual(catalog.status_code, 200, catalog.text)
        items = {item["type"]: item for item in catalog.json()["items"]}
        self.assertEqual(len(items), 23)
        self.assertIn("config_fields", items["http"])
        self.assertIn("output_ports", items["condition"])
        self.assertTrue(items["notification"]["side_effect"])

        options = self.client.get(
            "/api/v2/orgs/{}/workflow-editor-options".format(self.organization_id)
        )
        self.assertEqual(options.status_code, 200, options.text)
        for key in ("agents", "workflows", "credentials", "tools", "models"):
            self.assertIn(key, options.json())

        definition = empty_definition("平台校验")
        saved = self.client.put(
            "/api/v2/platform/workflow-templates/validate_template/draft",
            json={"definition": definition},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        valid = self.client.post(
            "/api/v2/platform/workflow-templates/validate_template/validate",
            json={"definition": definition},
        )
        self.assertEqual(valid.status_code, 200, valid.text)
        self.assertTrue(valid.json()["valid"])

        invalid = empty_definition("平台校验失败")
        invalid["nodes"].insert(-1, {
            "id": "request", "type": "http", "name": "请求",
            "position": {"x": 300, "y": 160},
            "config": {"method": "GET", "url": "http://127.0.0.1"},
            "error_policy": {"mode": "stop"},
        })
        invalid["edges"] = [
            {"id": "a", "source": "start", "source_port": "default", "target": "request", "target_port": "default"},
            {"id": "b", "source": "request", "source_port": "default", "target": "end", "target_port": "default"},
        ]
        rejected = self.client.post(
            "/api/v2/platform/workflow-templates/validate_template/validate",
            json={"definition": invalid},
        )
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertIn("HTTPS", rejected.json()["detail"])


if __name__ == "__main__":
    unittest.main()
