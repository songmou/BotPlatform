"""Opt-in Playwright E2E coverage for the native workflow editor."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import uvicorn

from src.api.app import create_app
from src.core.modeling import CanonicalMessage, ModelResponse, ModelRouter
from src.core.services.auth import AdminAuthService
from src.core.storage.admin_users import AdminRoleStore, AdminSessionStore, AdminUserStore
from src.core.storage.database import Database
from src.core.storage.tenants import TenantContext
from tests.test_web_api import FakeClient, _make_config


class WorkflowE2EClient(FakeClient):
    def complete(self, request):
        prompt = "\n".join(message.content for message in request.messages)
        if "生成有效候选" in prompt:
            proposal = {
                "schema_version": 1,
                "name": "AI 候选流程",
                "description": "确定性浏览器候选",
                "inputs": [], "outputs": [],
                "triggers": [{"id": "manual", "type": "manual", "config": {}}],
                "nodes": [
                    {"id": "start", "type": "start", "name": "开始", "position": {"x": 100, "y": 220}, "config": {}, "error_policy": {"mode": "stop"}},
                    {"id": "end", "type": "end", "name": "结束", "position": {"x": 520, "y": 220}, "config": {"output": {}}, "error_policy": {"mode": "stop"}},
                ],
                "edges": [{"id": "start_end", "source": "start", "source_port": "default", "target": "end", "target_port": "default"}],
                "settings": {"timeout_seconds": 86400, "max_steps": 500},
            }
            return ModelResponse(
                CanonicalMessage("assistant", json.dumps(proposal, ensure_ascii=False)),
                actual_model=self.identity.configured_model,
            )
        return super().complete(request)


@unittest.skipUnless(
    os.environ.get("BOTPLATFORM_RUN_WORKFLOW_E2E") == "1",
    "设置 BOTPLATFORM_RUN_WORKFLOW_E2E=1 运行工作流浏览器测试",
)
class WorkflowEditorE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - environment guard
            raise unittest.SkipTest("Playwright 未安装") from exc

        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        database = Database(root / "botplatform.sqlite3")
        registry = MagicMock()
        registry.database = database
        registry.system_root = root
        registry.tenant_root.side_effect = lambda tenant_id: root / tenant_id
        registry.get.side_effect = lambda tenant_id: TenantContext(
            tenant_id, "organization", "organization:" + tenant_id
        )
        admin_users = AdminUserStore(database)
        admin_roles = AdminRoleStore(database)
        sessions = AdminSessionStore(database, b"workflow-e2e")
        auth = AdminAuthService(admin_users, admin_roles, sessions, root)
        admin_users.create(
            "workflow_admin", "password12345",
            admin_roles.get_by_code("admin").role_id,
        )
        cls.app = create_app(
            _make_config(), ModelRouter.single(WorkflowE2EClient()), registry, MagicMock(),
            admin_auth=auth, admin_user_store=admin_users,
            admin_role_store=admin_roles,
        )
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
                cls.port = int(probe.getsockname()[1])
        except OSError as exc:
            raise unittest.SkipTest("当前环境不允许启动本地工作流 E2E 临时服务") from exc
        cls.server = uvicorn.Server(uvicorn.Config(
            cls.app, host="127.0.0.1", port=cls.port, log_level="warning"
        ))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.time() + 10
        while not cls.server.started and time.time() < deadline:
            if not cls.thread.is_alive():
                break
            time.sleep(0.05)
        if not cls.server.started:
            cls.server.should_exit = True
            cls.thread.join(timeout=2)
            raise unittest.SkipTest("当前环境不允许启动本地工作流 E2E 临时服务")
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment guard
            cls.playwright.stop()
            cls.server.should_exit = True
            cls.thread.join(timeout=5)
            raise unittest.SkipTest("Playwright Chromium 不可用，请运行 playwright install chromium") from exc

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "browser"):
            cls.browser.close()
        if hasattr(cls, "playwright"):
            cls.playwright.stop()
        if hasattr(cls, "server"):
            cls.server.should_exit = True
        if hasattr(cls, "thread"):
            cls.thread.join(timeout=10)
        if hasattr(cls, "temporary"):
            cls.temporary.cleanup()

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 960})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        base = "http://127.0.0.1:{}".format(self.port)
        self.page.goto(base + "/login")
        self.page.get_by_label("用户名").fill("workflow_admin")
        self.page.get_by_label("密码").fill("password12345")
        with self.page.expect_response(
            lambda response: response.url.endswith("/api/auth/login")
        ) as login_response:
            self.page.get_by_role("button", name="登 录").click()
        self.assertEqual(login_response.value.status, 200)
        self.page.wait_for_url(lambda url: not str(url).endswith("/login"))
        self.page.wait_for_load_state("networkidle")
        organization = self.page.evaluate("""async () => {
            const response = await fetch('/api/v2/platform/organizations', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: 'Workflow E2E ' + Date.now()})
            });
            if (!response.ok) throw new Error(await response.text());
            return (await response.json()).organization;
        }""")
        self.base = base
        self.organization_id = organization["organization_id"]
        self.page.goto(base + "/organization/workflows?organization_id=" + self.organization_id)
        self.page.wait_for_load_state("networkidle")

    def tearDown(self):
        self.page.close()

    def _new_workflow(self):
        self.page.get_by_role("button", name="新建工作流").click()
        self.page.get_by_label("工作流 ID").fill("canvas_{}".format(int(time.time() * 1000)))
        self.page.get_by_label("名称", exact=True).fill("画布交互测试")
        self.page.locator("#form-dialog-form").get_by_role("button", name="保存").click()
        self.page.locator("#workflow-editor").wait_for(state="visible")
        self.page.locator(".workflow-node").first.wait_for(state="visible")

    def _drag_port(self, source_selector, target_selector):
        source = self.page.locator(source_selector).bounding_box()
        target = self.page.locator(target_selector).bounding_box()
        self.assertIsNotNone(source)
        self.assertIsNotNone(target)
        self.page.mouse.move(source["x"] + source["width"] / 2, source["y"] + source["height"] / 2)
        self.page.mouse.down()
        self.page.mouse.move(target["x"] + target["width"] / 2, target["y"] + target["height"] / 2, steps=8)
        self.page.mouse.up()

    def test_buttons_canvas_keyboard_and_ai_error_feedback(self):
        self._new_workflow()
        self.assertEqual(self.page.locator(".workflow-node").count(), 2)

        # A concurrent draft update produces a visible 409 and reload recovers.
        external_name = "服务端并发版本"
        self.page.evaluate("""async externalName => {
            const base = '/api/v2/orgs/' + new URLSearchParams(location.search).get('organization_id');
            const listed = await (await fetch(base + '/workflows')).json();
            const id = listed.items[0].workflow_id;
            const current = await (await fetch(base + '/workflows/' + id)).json();
            current.definition.name = externalName;
            const response = await fetch(base + '/workflows/' + id + '/draft', {
                method: 'PUT', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({definition: current.definition, base_revision: current.draft_revision})
            });
            if (!response.ok) throw new Error(await response.text());
        }""", external_name)
        self.page.locator("#workflow-name").fill("本地冲突版本")
        self.page.get_by_text("保存冲突", exact=True).wait_for(timeout=5000)
        self.page.locator("#workflow-reload").click()
        self.page.get_by_role("button", name="确定").click()
        self.page.get_by_text("已重新加载", exact=True).wait_for()
        self.assertEqual(self.page.locator("#workflow-name").input_value(), external_name)

        # AI valid DSL applies only after explicit confirmation.
        self.page.get_by_role("button", name="AI 搭建").click()
        self.page.get_by_label("描述希望生成或修改的流程").fill("生成有效候选")
        self.page.locator("#form-dialog-form").get_by_role("button", name="保存").click()
        self.page.get_by_role("button", name="确定").click()
        self.page.get_by_text("AI 候选草稿已应用", exact=True).wait_for()
        self.assertEqual(self.page.locator("#workflow-name").input_value(), "AI 候选流程")

        # Existing edge is selectable and keyboard-deletable.
        edge = self.page.locator(".workflow-edge-hit")
        edge.evaluate("element => element.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, pointerId: 1}))")
        self.assertEqual(self.page.locator(".workflow-edge.selected").count(), 1)
        self.page.keyboard.press("Delete")
        self.assertEqual(self.page.locator(".workflow-edge-hit").count(), 0)

        # Palette click adds a node; dragging moves it and structured fields render.
        self.page.get_by_role("button", name="文本模板").click()
        template = self.page.locator('[data-node-id="template"]')
        before = template.bounding_box()
        self.page.mouse.move(before["x"] + 70, before["y"] + 20)
        self.page.mouse.down()
        self.page.mouse.move(before["x"] + 150, before["y"] + 100, steps=6)
        self.page.mouse.up()
        after = template.bounding_box()
        self.assertGreater(after["x"], before["x"] + 30)
        self.assertTrue(self.page.locator('[data-config-key="text"]').is_visible())
        self.page.locator('[data-config-key="text"]').fill("你好 {{input.name}}")
        self.page.locator('[data-config-key="text"]').press("Tab")

        # Output-port drag creates start -> template -> end edges.
        self._drag_port('[data-node-id="start"] .workflow-port.out[data-port="default"]', '[data-node-id="template"] .workflow-port.in')
        self._drag_port('[data-node-id="template"] .workflow-port.out[data-port="default"]', '[data-node-id="end"] .workflow-port.in')
        self.assertEqual(self.page.locator(".workflow-edge-hit").count(), 2)
        self.page.locator('[data-node-id="end"]').click()
        self.assertEqual(
            self.page.locator('#workflow-variable-picker option[value="nodes.template.output.text"]').count(),
            1,
        )

        # Toolbar connection mode also creates an edge by clicking a target.
        self.page.locator(".workflow-edge-hit").last.evaluate(
            "element => element.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, pointerId: 2}))"
        )
        self.page.keyboard.press("Delete")
        self.page.locator('[data-node-id="template"]').click()
        self.page.locator("#workflow-connect").click()
        self.page.locator('[data-node-id="end"]').click()
        self.assertEqual(self.page.locator(".workflow-edge-hit").count(), 2)

        # Keyboard copy/paste/delete and undo/redo work after node focus.
        template.click()
        self.page.keyboard.press("ControlOrMeta+c")
        self.page.keyboard.press("ControlOrMeta+v")
        self.assertEqual(self.page.locator(".workflow-node").count(), 4)
        self.page.keyboard.press("Delete")
        self.assertEqual(self.page.locator(".workflow-node").count(), 3)
        self.page.keyboard.press("ControlOrMeta+z")
        self.assertEqual(self.page.locator(".workflow-node").count(), 4)
        self.page.keyboard.press("ControlOrMeta+Shift+z")
        self.assertEqual(self.page.locator(".workflow-node").count(), 3)
        self.page.keyboard.press("ControlOrMeta+z")

        # Empty-canvas drag box selects nodes.
        boxes = [self.page.locator(".workflow-node").nth(index).bounding_box() for index in range(self.page.locator(".workflow-node").count())]
        canvas_box = self.page.locator("#workflow-canvas").bounding_box()
        left = max(canvas_box["x"] + 3, min(box["x"] for box in boxes) - 18)
        top = max(canvas_box["y"] + 3, min(box["y"] for box in boxes) - 18)
        right = min(canvas_box["x"] + canvas_box["width"] - 3, max(box["x"] + box["width"] for box in boxes) + 18)
        bottom = min(canvas_box["y"] + canvas_box["height"] - 3, max(box["y"] + box["height"] for box in boxes) + 18)
        self.page.mouse.move(left, top)
        self.page.mouse.down()
        self.page.mouse.move(right, bottom, steps=8)
        self.page.mouse.up()
        self.assertGreaterEqual(self.page.locator(".workflow-node.multi-selected").count(), 2)

        # Space + primary pointer pans the canvas without moving node data.
        visible_start_before_pan = self.page.locator('[data-node-id="start"]').bounding_box()
        self.page.keyboard.down("Space")
        self.page.mouse.move(canvas_box["x"] + canvas_box["width"] - 20, canvas_box["y"] + canvas_box["height"] - 20)
        self.page.mouse.down()
        self.page.mouse.move(canvas_box["x"] + canvas_box["width"] - 70, canvas_box["y"] + canvas_box["height"] - 60, steps=5)
        self.page.mouse.up()
        self.page.keyboard.up("Space")
        visible_start_after_pan = self.page.locator('[data-node-id="start"]').bounding_box()
        self.assertLess(visible_start_after_pan["x"], visible_start_before_pan["x"] - 20)

        # Modifier multi-select, select-all, arrow movement and zoom controls.
        self.page.locator('[data-node-id="template_copy"]').click()
        self.page.locator('[data-node-id="start"]').click(modifiers=["Shift"])
        self.assertEqual(self.page.locator(".workflow-node.multi-selected").count(), 2)
        self.page.keyboard.press("ControlOrMeta+a")
        self.assertEqual(
            self.page.locator(".workflow-node.multi-selected").count(),
            self.page.locator(".workflow-node").count(),
        )
        start_before = self.page.locator('[data-node-id="start"]').bounding_box()
        self.page.keyboard.press("Shift+ArrowRight")
        start_after = self.page.locator('[data-node-id="start"]').bounding_box()
        self.assertGreater(start_after["x"], start_before["x"])
        zoom_before = self.page.locator("#workflow-zoom-label").inner_text()
        self.page.locator("#workflow-zoom-in").click()
        self.assertNotEqual(self.page.locator("#workflow-zoom-label").inner_text(), zoom_before)

        # Invalid advanced JSON stays visible and cannot overwrite structured config.
        self.page.keyboard.press("Escape")
        self.page.locator('[data-node-id="template_copy"]').click()
        self.page.get_by_role("button", name="高级 JSON").click()
        config = self.page.locator("#workflow-node-config")
        config.fill("{")
        config.press("Tab")
        self.assertTrue(self.page.locator("#workflow-json-error").is_visible())
        self.page.get_by_role("button", name="结构化配置").click()
        self.assertIn("你好", self.page.locator('[data-config-key="text"]').input_value())
        self.page.keyboard.press("Delete")
        self.assertEqual(self.page.locator(".workflow-node").count(), 3)

        # Workflow-level settings are editable without raw API calls.
        self.page.get_by_role("button", name="流程设置").click()
        self.page.locator("#workflow-panel-overlay").wait_for(state="visible")
        self.page.locator("#wf-setting-description").fill("E2E 工作流描述")
        self.page.locator('[data-add-field="inputs"]').click()
        self.page.locator("#wf-inputs tr").last.locator("[data-key]").fill("name")
        self.page.locator("[data-add-trigger]").click()
        api_trigger = self.page.locator("#wf-triggers tr").last
        api_trigger.locator("[data-id]").fill("public_api")
        api_trigger.locator("[data-trigger-type]").select_option("api")
        self.page.locator("[data-add-trigger]").click()
        webhook_trigger = self.page.locator("#wf-triggers tr").last
        webhook_trigger.locator("[data-id]").fill("incoming")
        webhook_trigger.locator("[data-trigger-type]").select_option("webhook")
        self.page.locator("#workflow-panel-save").click()

        # Validate, publish and exercise sensitive lifecycle management.
        self.page.get_by_role("button", name="校验", exact=True).click()
        self.page.get_by_text("工作流校验通过", exact=True).wait_for()
        self.page.get_by_role("button", name="发布", exact=True).click()
        self.page.get_by_text("工作流版本已发布", exact=True).wait_for()

        self.page.get_by_role("button", name="试运行", exact=True).click()
        self.page.get_by_label("输入 JSON").fill('{"name":"Codex"}')
        self.page.locator("#form-dialog-form").get_by_role("button", name="保存").click()
        self.page.locator("#workflow-debug").wait_for(state="visible")
        self.assertIn('"status": "succeeded"', self.page.locator("#workflow-debug-content").inner_text())
        self.page.locator("#workflow-debug-close").click()

        self.page.get_by_role("button", name="管理", exact=True).click()
        self.page.locator("[data-manage-action=new-token]").click()
        self.page.get_by_label("标签").fill("E2E Token")
        self.page.locator("#form-dialog-form").get_by_role("button", name="保存").click()
        self.page.locator("#notice-dialog-value").wait_for(state="visible")
        self.assertTrue(self.page.locator("#notice-dialog-value").input_value().startswith("bpwf_"))
        self.page.locator("#notice-dialog-ok").click()
        self.page.locator("[data-manage-action=revoke-token]:not([disabled])").click()
        self.page.get_by_role("button", name="确定").click()
        self.page.locator("[data-manage-action=revoke-token][disabled]").wait_for()

        self.page.locator("[data-manage-action=webhook-secret]").click()
        self.page.locator("#notice-dialog-value").wait_for(state="visible")
        self.assertTrue(self.page.locator("#notice-dialog-value").input_value().startswith("bpwh_"))
        self.page.locator("#notice-dialog-ok").click()
        self.page.locator("[data-manage-action=webhook-revoke]:not([disabled])").click()
        self.page.get_by_role("button", name="确定").click()
        self.page.locator("[data-manage-action=webhook-revoke][disabled]").wait_for()

        self.page.locator("[data-manage-action=new-credential]").click()
        self.page.get_by_label("凭据编号").fill("e2e_https")
        self.page.get_by_label("标签").fill("E2E HTTPS")
        self.page.get_by_label("密钥或 JSON 请求头").fill('{"Authorization":"Bearer e2e"}')
        self.page.locator("#form-dialog-form").get_by_role("button", name="保存").click()
        self.page.locator('[data-manage-action=delete-credential][data-credential="e2e_https"]').wait_for()
        self.page.locator('[data-manage-action=delete-credential][data-credential="e2e_https"]').click()
        self.page.get_by_role("button", name="确定").click()
        self.page.locator('[data-manage-action=delete-credential][data-credential="e2e_https"]').wait_for(state="detached")

        self.page.locator('[data-manage-action=rollback][data-version="1"]').click()
        self.page.get_by_role("button", name="确定").click()
        self.page.locator('[data-manage-action=rollback][data-version="2"]').wait_for()

        self.page.locator("[data-manage-action=unpublish]").click()
        self.page.get_by_role("button", name="确定").click()
        self.page.locator("[data-manage-action=publish]").wait_for()
        self.page.locator("#workflow-panel-save").click()

        # AI invalid DSL is surfaced as a Chinese operation error, never a missing JS helper.
        self.page.get_by_role("button", name="AI 搭建").click()
        self.page.get_by_label("描述希望生成或修改的流程").fill("生成一个简单问候工作流")
        self.page.locator("#form-dialog-form").get_by_role("button", name="保存").click()
        toast = self.page.locator(".toast-error").last
        toast.wait_for(state="visible", timeout=10000)
        self.assertIn("AI 搭建失败", toast.inner_text())
        self.assertNotIn("showConfirmDialog", toast.inner_text())

        # Returning waits for autosave; archive removes the list item but keeps run history.
        self.page.get_by_role("button", name="返回", exact=True).click()
        self.page.locator("#workflow-editor").wait_for(state="hidden")
        self.page.locator('[data-action="archive"]').click()
        self.page.get_by_role("button", name="确定").click()
        self.page.locator(".workflow-card").wait_for(state="detached")
        self.page.get_by_role("button", name="运行记录", exact=True).click()
        self.page.locator('[data-action="run-detail"]').click()
        self.page.get_by_text("运行详情", exact=True).wait_for()
        self.assertIn('"status": "succeeded"', self.page.locator("#workflow-panel-body").inner_text())
        self.page.locator("#workflow-panel-save").click()
        self.assertFalse(any("showConfirmDialog" in error for error in self.errors), self.errors)


if __name__ == "__main__":
    unittest.main()
