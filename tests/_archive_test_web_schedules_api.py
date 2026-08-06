"""Integration tests for the /api/schedules management endpoints."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import src.api.routers.schedules as schedules_module

from tests._web_api_base import WebApiTestBase


class SchedulesApiTest(WebApiTestBase):
    def setUp(self):
        self.scheduler = MagicMock()
        super().setUp()

        self._file_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._file_dir.cleanup)
        self.schedules_file = Path(self._file_dir.name) / "schedules.json"
        patcher = patch.object(schedules_module, "SCHEDULES_FILE", self.schedules_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def app_kwargs(self):
        return {"scheduler": self.scheduler}

    def _text_task(self, task_id="morning_ping", **overrides):
        body = {
            "id": task_id,
            "cron": "0 9 * * *",
            "target": "last_active_user",
            "action": {"type": "text", "content": "早上好"},
        }
        body.update(overrides)
        return body

    # ---- list / detail ----

    def test_list_empty(self):
        response = self.client.get("/api/schedules")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_detail_not_found(self):
        response = self.client.get("/api/schedules/nonexistent")
        self.assertEqual(response.status_code, 404)

    # ---- create ----

    def test_create_text_task_persists_and_reloads(self):
        response = self.client.post("/api/schedules", json=self._text_task())
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(data["id"], "morning_ping")
        self.assertEqual(data["action"]["type"], "text")

        # In-memory config and JSON file both updated.
        self.assertEqual(len(self.config.schedules), 1)
        saved = json.loads(self.schedules_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["tasks"][0]["id"], "morning_ping")
        self.scheduler.reload_tasks.assert_called()

        response = self.client.get("/api/schedules/morning_ping")
        self.assertEqual(response.status_code, 200)

    def test_create_invalid_id(self):
        response = self.client.post("/api/schedules", json=self._text_task("Bad-ID"))
        self.assertEqual(response.status_code, 400)

    def test_create_duplicate_id(self):
        self.client.post("/api/schedules", json=self._text_task())
        response = self.client.post("/api/schedules", json=self._text_task())
        self.assertEqual(response.status_code, 409)

    def test_create_requires_cron_or_crons(self):
        body = self._text_task()
        body.pop("cron")
        response = self.client.post("/api/schedules", json=body)
        self.assertEqual(response.status_code, 400)

    def test_create_rejects_malformed_cron(self):
        response = self.client.post(
            "/api/schedules", json=self._text_task(cron="0 9 * *")
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/schedules", json=self._text_task(cron="0 9 * * MON")
        )
        self.assertEqual(response.status_code, 400)

    def test_create_rejects_bad_actions(self):
        response = self.client.post(
            "/api/schedules", json=self._text_task(action={"type": "unknown"})
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/schedules", json=self._text_task(action={"type": "text"})
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/schedules",
            json=self._text_task(action={"type": "agent_prompt", "agent_id": "general"}),
        )
        self.assertEqual(response.status_code, 400)

    def test_create_rejects_unknown_script_reference(self):
        response = self.client.post(
            "/api/schedules",
            json=self._text_task(action={"type": "script", "script_id": "missing"}),
        )
        self.assertEqual(response.status_code, 400)

    def test_create_with_crons_and_condition(self):
        body = self._text_task()
        body.pop("cron")
        body["crons"] = ["0 9 * * *", "0 18 * * *"]
        body["condition"] = {"type": "inactivity_once", "after_hours": 2}
        response = self.client.post("/api/schedules", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(data["crons"], ["0 9 * * *", "0 18 * * *"])
        self.assertEqual(data["condition"]["after_hours"], 2)

    # ---- update ----

    def test_update_task(self):
        self.client.post("/api/schedules", json=self._text_task())
        response = self.client.put(
            "/api/schedules/morning_ping",
            json={"enabled": False, "cron": "30 8 * * *"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertFalse(data["enabled"])
        self.assertEqual(data["cron"], "30 8 * * *")
        saved = json.loads(self.schedules_file.read_text(encoding="utf-8"))
        self.assertFalse(saved["tasks"][0]["enabled"])

    def test_update_not_found(self):
        response = self.client.put("/api/schedules/nope", json={"enabled": False})
        self.assertEqual(response.status_code, 404)

    def test_update_rejects_bad_cron(self):
        self.client.post("/api/schedules", json=self._text_task())
        response = self.client.put(
            "/api/schedules/morning_ping", json={"cron": "not a cron"}
        )
        self.assertEqual(response.status_code, 400)

    # ---- delete ----

    def test_delete_task(self):
        self.client.post("/api/schedules", json=self._text_task())
        response = self.client.delete("/api/schedules/morning_ping")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/schedules").json(), [])
        saved = json.loads(self.schedules_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["tasks"], [])

    def test_delete_not_found(self):
        response = self.client.delete("/api/schedules/nope")
        self.assertEqual(response.status_code, 404)

    # ---- auth ----

    def test_requires_login(self):
        from fastapi.testclient import TestClient

        anonymous = TestClient(self.app)
        response = anonymous.get("/api/schedules")
        self.assertEqual(response.status_code, 401)
