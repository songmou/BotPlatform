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
    PAGES = ["/schedules", "/plugins", "/docs", "/platform/knowledge", "/platform/drive"]

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
        response = self.client.get("/schedules")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('data-module="schedules"', response.text)
        self.assertIn('id="organization-page-switch"', response.text)
        self.assertNotIn('id="organization-switch"', response.text)

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
        platform = self.client.get("/scripts")
        self.assertNotIn('id="organization-page-switch"', platform.text)

    def test_legacy_content_urls_are_role_aware(self):
        knowledge = self.client.get("/knowledge", follow_redirects=False)
        self.assertEqual(knowledge.status_code, 308)
        self.assertEqual(knowledge.headers["location"], "/platform/knowledge")
        drive = self.client.get("/drive", follow_redirects=False)
        self.assertEqual(drive.status_code, 308)
        self.assertEqual(drive.headers["location"], "/platform/drive")
