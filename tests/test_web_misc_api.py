"""Integration tests for /api/auth/me and remaining page routes."""

from __future__ import annotations

from tests._web_api_base import WebApiTestBase


class AuthMeApiTest(WebApiTestBase):
    def test_me_returns_current_admin(self):
        response = self.client.get("/api/auth/me")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["user"]["username"], "root")
        self.assertIn("*", data["permissions"])

    def test_me_for_viewer(self):
        response = self.viewer_client.get("/api/auth/me")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["user"]["username"], "watcher")
        self.assertNotIn("*", data["permissions"])
        self.assertIn("panel.read", data["permissions"])

    def test_me_requires_login(self):
        from fastapi.testclient import TestClient

        anonymous = TestClient(self.app)
        response = anonymous.get("/api/auth/me")
        self.assertEqual(response.status_code, 401)


class RemainingPagesTest(WebApiTestBase):
    PAGES = [
        "/organization/schedules",
        "/platform/plugins",
        "/docs",
        "/platform/knowledge",
        "/platform/drive",
    ]

    def test_pages_render_for_admin(self):
        for page in self.PAGES:
            response = self.client.get(page)
            self.assertEqual(response.status_code, 200, page)
            self.assertIn("text/html", response.headers["content-type"])

    def test_pages_redirect_anonymous_to_login(self):
        from fastapi.testclient import TestClient

        anonymous = TestClient(self.app)
        for page in self.PAGES:
            response = anonymous.get(page, follow_redirects=False)
            self.assertEqual(response.status_code, 302, page)
            self.assertTrue(response.headers["location"].startswith("/login"))

    def test_schedule_page_uses_url_scoped_organization_module(self):
        response = self.client.get("/organization/schedules")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('data-module="schedules"', response.text)
        self.assertIn('id="organization-page-switch"', response.text)
        self.assertNotIn('id="organization-switch"', response.text)
        self.assertIn('data-schedule-tab="schedules"', response.text)
        self.assertIn('data-schedule-tab="runs"', response.text)
        self.assertIn('id="organization-runs-body"', response.text)

    def test_plugins_page_refreshes_runtime_status_after_process_restart(self):
        response = self.client.get("/static/js/plugins.js")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('request("/api/plugins", {cache: "no-store"})', response.text)
        self.assertIn(
            'document.addEventListener("visibilitychange", refreshWhenVisible)',
            response.text,
        )
        self.assertIn(
            "window.setInterval(refreshWhenVisible, 15000)", response.text
        )

    def test_schedule_runs_tab_is_scoped_to_schedules_module(self):
        agents = self.client.get("/organization/agents")
        self.assertEqual(agents.status_code, 200, agents.text)
        self.assertIn('data-module="agents"', agents.text)
        self.assertNotIn('data-schedule-tab="runs"', agents.text)
        self.assertNotIn('id="organization-runs-panel"', agents.text)

    def test_organization_pages_share_page_local_picker(self):
        for page in (
            "/organization/schedules",
            "/organization/knowledge",
            "/organization/drive",
        ):
            response = self.client.get(page)
            self.assertEqual(response.status_code, 200, page)
            self.assertIn('id="organization-page-switch"', response.text, page)
        knowledge = self.client.get("/organization/knowledge")
        self.assertIn('id="knowledge-page" data-resource-mode="organization"', knowledge.text)
        self.assertIn("组织知识", knowledge.text)
        drive = self.client.get("/organization/drive")
        self.assertIn('id="drive-page" data-resource-mode="organization"', drive.text)
        self.assertIn("组织文件", drive.text)
        platform = self.client.get("/platform/scripts")
        self.assertNotIn('id="organization-page-switch"', platform.text)

    def test_retired_page_aliases_return_404(self):
        for path in (
            "/admin", "/app", "/chat", "/models", "/agents", "/tools",
            "/plugins", "/scripts", "/knowledge", "/drive", "/channels",
            "/schedules", "/members", "/analytics", "/audit", "/users",
        ):
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 404, path)
