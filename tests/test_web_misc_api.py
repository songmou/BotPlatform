"""Integration tests for /api/bots, /api/auth/me and remaining page routes."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import src.api.routers.bots as bots_module

from tests._web_api_base import WebApiTestBase


class BotsApiTest(WebApiTestBase):
    def setUp(self):
        super().setUp()
        self._file_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._file_dir.cleanup)
        self.credentials_path = Path(self._file_dir.name) / "credentials.json"
        patcher = patch.object(
            bots_module, "CREDENTIALS_PATH", self.credentials_path
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bots_without_credentials(self):
        response = self.client.get("/api/bots")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["channel"], "ilink")
        self.assertFalse(data[0]["connected"])

    def test_bots_with_valid_credentials(self):
        self.credentials_path.write_text(
            json.dumps({"bot_id": "bot123", "user_id": "wxid_abc"}),
            encoding="utf-8",
        )
        response = self.client.get("/api/bots")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()[0]
        self.assertTrue(data["connected"])
        self.assertEqual(data["bot_id"], "bot123")
        self.assertEqual(data["user_id"], "wxid_abc")

    def test_bots_with_corrupted_credentials(self):
        self.credentials_path.write_text("{not-json", encoding="utf-8")
        response = self.client.get("/api/bots")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()[0]["connected"])


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
