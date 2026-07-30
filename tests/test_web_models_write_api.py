"""Integration tests for the /api/models write endpoints (create/update/delete/switch)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import src.api.routers.models as models_module

from tests._web_api_base import WebApiTestBase
from tests.test_web_api import FakeClient


class ModelsWriteApiTest(WebApiTestBase):
    def setUp(self):
        super().setUp()
        self._file_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._file_dir.cleanup)
        self.models_file = Path(self._file_dir.name) / "models.json"
        patcher = patch.object(models_module, "MODELS_FILE", self.models_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _model(self, model_id="backup_model", **overrides):
        body = {
            "id": model_id,
            "provider": "test",
            "base_url": "http://127.0.0.1:9000/v1",
            "model": "backup-model",
            "enabled": False,
        }
        body.update(overrides)
        return body

    # ---- create ----

    def test_create_model(self):
        response = self.client.post("/api/models", json=self._model())
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(data["id"], "backup_model")
        self.assertFalse(data["is_primary"])

        self.assertIn("backup_model", self.config.models)
        saved = json.loads(self.models_file.read_text(encoding="utf-8"))
        self.assertIn("backup_model", saved["profiles"])
        # Disabled profile must not register a client.
        self.assertNotIn("backup_model", self.model_router.clients)

    def test_create_invalid_id(self):
        response = self.client.post("/api/models", json=self._model("Bad-ID"))
        self.assertEqual(response.status_code, 400)

    def test_create_duplicate_409(self):
        response = self.client.post("/api/models", json=self._model("test_model"))
        self.assertEqual(response.status_code, 409)

    def test_create_empty_model_name_400(self):
        response = self.client.post("/api/models", json=self._model(model=" "))
        self.assertEqual(response.status_code, 400)

    def test_create_with_pricing(self):
        response = self.client.post(
            "/api/models",
            json=self._model(
                pricing={"input_per_million": "2.5", "output_per_million": "10"}
            ),
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["pricing"]["input_per_million"], "2.5")

    def test_create_pricing_validation(self):
        # Unknown pricing field.
        response = self.client.post(
            "/api/models",
            json=self._model(pricing={"input_per_million": "1", "bogus": "2"}),
        )
        self.assertEqual(response.status_code, 400)
        # Missing mandatory output price.
        response = self.client.post(
            "/api/models", json=self._model(pricing={"input_per_million": "1"})
        )
        self.assertEqual(response.status_code, 400)
        # Negative price.
        response = self.client.post(
            "/api/models",
            json=self._model(
                pricing={"input_per_million": "-1", "output_per_million": "1"}
            ),
        )
        self.assertEqual(response.status_code, 400)

    # ---- update ----

    def test_update_model(self):
        self.client.post("/api/models", json=self._model())
        response = self.client.put(
            "/api/models/backup_model", json={"temperature": 0.2, "max_tokens": 512}
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["temperature"], 0.2)
        self.assertEqual(data["max_tokens"], 512)
        self.assertEqual(self.config.models["backup_model"].max_tokens, 512)

    def test_update_not_found(self):
        response = self.client.put("/api/models/nope", json={"temperature": 0.1})
        self.assertEqual(response.status_code, 404)

    def test_update_rejects_bad_pricing(self):
        self.client.post("/api/models", json=self._model())
        response = self.client.put(
            "/api/models/backup_model",
            json={"pricing": {"input_per_million": "abc", "output_per_million": "1"}},
        )
        self.assertEqual(response.status_code, 400)

    # ---- switch ----

    def test_switch_to_registered_client(self):
        self.model_router.clients["backup_model"] = FakeClient("backup_model")
        response = self.client.put(
            "/api/models/switch", json={"profile_id": "backup_model"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.model_router.primary_profile_id, "backup_model")
        # Fallback moved off the new primary.
        self.assertNotEqual(self.model_router.fallback_profile_id, "backup_model")

    def test_switch_unknown_profile_404(self):
        response = self.client.put(
            "/api/models/switch", json={"profile_id": "ghost"}
        )
        self.assertEqual(response.status_code, 404)

    # ---- delete ----

    def test_delete_model(self):
        self.client.post("/api/models", json=self._model())
        response = self.client.delete("/api/models/backup_model")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("backup_model", self.config.models)
        saved = json.loads(self.models_file.read_text(encoding="utf-8"))
        self.assertNotIn("backup_model", saved["profiles"])

    def test_delete_primary_model_400(self):
        response = self.client.delete("/api/models/test_model")
        self.assertEqual(response.status_code, 400)

    def test_delete_not_found(self):
        response = self.client.delete("/api/models/nope")
        self.assertEqual(response.status_code, 404)

    # ---- auth ----

    def test_requires_login(self):
        from fastapi.testclient import TestClient

        anonymous = TestClient(self.app)
        response = anonymous.post("/api/models", json=self._model())
        self.assertEqual(response.status_code, 401)
