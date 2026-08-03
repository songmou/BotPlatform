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
        self.app_file = Path(self._file_dir.name) / "app.json"
        self.app_file.write_text(
            json.dumps(
                {
                    "active_model": "test_model",
                    "fallback_model": "test_model",
                    "local_model": "",
                    "flash_model": "",
                    "pro_model": "",
                    "vision_model": "",
                    "embedding_model": "",
                    "rerank_model": "",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        app_patcher = patch.object(models_module, "APP_FILE", self.app_file)
        app_patcher.start()
        self.addCleanup(app_patcher.stop)

    def _model(self, model_id="backup_model", **overrides):
        body = {
            "id": model_id,
            "provider": "test",
            "base_url": "http://127.0.0.1:9000/v1",
            "model": "backup-model",
            "api_key_env": "BACKUP_MODEL_KEY",
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

    def _embedding_model(self, model_id="backup_embedding", **overrides):
        body = self._model(
            model_id,
            modality="embedding",
            model="bge-m3",
            dimensions=1024,
        )
        body.update(overrides)
        return body

    def _rerank_model(self, model_id="backup_rerank", **overrides):
        body = self._model(
            model_id,
            modality="rerank",
            model="bge-reranker",
        )
        body.update(overrides)
        return body

    # ---- modality create ----

    def test_create_embedding_model(self):
        response = self.client.post("/api/models", json=self._embedding_model())
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(data["modality"], "embedding")
        self.assertEqual(data["dimensions"], 1024)
        self.assertTrue(data["restart_required"])
        self.assertEqual(
            self.config.models["backup_embedding"].modality, "embedding"
        )
        # Embedding profiles never become chat router clients.
        self.assertNotIn("backup_embedding", self.model_router.clients)
        saved = json.loads(self.models_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["profiles"]["backup_embedding"]["modality"], "embedding")

    def test_create_rerank_model(self):
        response = self.client.post("/api/models", json=self._rerank_model())
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(data["modality"], "rerank")
        self.assertTrue(data["restart_required"])
        self.assertNotIn("backup_rerank", self.model_router.clients)

    def test_create_embedding_missing_dimensions_400(self):
        body = self._embedding_model()
        body.pop("dimensions")
        response = self.client.post("/api/models", json=body)
        self.assertEqual(response.status_code, 400, response.text)

    # ---- roles ----

    def test_roles_get_returns_bindings_and_candidates(self):
        response = self.client.get("/api/models/roles")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["active_model"], "test_model")
        self.assertEqual(data["embedding_model"], "")
        chat_ids = [item["id"] for item in data["chat_candidates"]]
        self.assertIn("test_model", chat_ids)
        self.assertEqual(data["embedding_candidates"], [])

    def test_roles_put_updates_vision_binding_at_runtime(self):
        with patch.object(
            models_module,
            "create_model_client",
            lambda profile, logger=None: FakeClient(profile.id),
        ):
            self.client.post(
                "/api/models",
                json=self._model(
                    "vision_chat",
                    enabled=True,
                    capabilities={"tools": True, "vision": True, "reasoning": False},
                ),
            )
        response = self.client.put(
            "/api/models/roles", json={"vision_model": "vision_chat"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["restart_required"])
        self.assertEqual(self.model_router.vision_profile_id, "vision_chat")
        saved = json.loads(self.app_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["vision_model"], "vision_chat")

    def test_roles_put_embedding_requires_restart(self):
        self.client.post("/api/models", json=self._embedding_model())
        response = self.client.put(
            "/api/models/roles", json={"embedding_model": "backup_embedding"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["restart_required"])
        self.assertEqual(self.config.app.embedding_model, "backup_embedding")

    def test_roles_put_rejects_wrong_modality(self):
        response = self.client.put(
            "/api/models/roles", json={"embedding_model": "test_model"}
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_delete_bound_model_protected(self):
        self.client.post("/api/models", json=self._embedding_model())
        self.client.put(
            "/api/models/roles", json={"embedding_model": "backup_embedding"}
        )
        response = self.client.delete("/api/models/backup_embedding")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("backup_embedding", self.config.models)

    # ---- auth ----

    def test_requires_login(self):
        from fastapi.testclient import TestClient

        anonymous = TestClient(self.app)
        response = anonymous.post("/api/models", json=self._model())
        self.assertEqual(response.status_code, 401)
