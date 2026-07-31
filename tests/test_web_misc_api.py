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
    PAGES = ["/schedules", "/plugins", "/docs", "/knowledge"]

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

    def test_schedule_page_separates_task_types_into_tabs(self):
        response = self.client.get("/schedules")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('data-schedule-tab="tasks"', response.text)
        self.assertIn('data-schedule-tab="automation"', response.text)
        self.assertIn('data-schedule-pane="tasks"', response.text)
        self.assertIn('data-schedule-pane="automation"', response.text)
        self.assertIn("initScheduleTabs()", response.text)

    def test_tenant_selects_use_shared_control_style(self):
        pages = {
            "/schedules": 'id="script-schedule-tenant" class="tenant-select"',
            "/scripts": 'id="script-run-tenant" class="tenant-select"',
            "/knowledge": 'id="knowledge-tenant" class="tenant-select"',
            "/drive": 'id="drive-tenant" class="drive-select tenant-select"',
            "/static/js/scripts.js": 'id="run-tenant" class="tenant-select"',
        }
        for page, marker in pages.items():
            response = self.client.get(page)
            self.assertEqual(response.status_code, 200, page)
            self.assertIn(marker, response.text, page)
