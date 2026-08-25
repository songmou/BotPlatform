"""Runtime model routing endpoint: hot rebind plus catalog persistence."""

from __future__ import annotations

from dataclasses import replace

from src.core.modeling import ModelCapabilities
from src.core.services.resources import ScopedResourceStore
from tests._web_api_base import WebApiTestBase
from tests.test_web_api import FakeClient

ROUTING_URL = "/api/v2/platform/model-routing"


class PlatformModelRoutingTest(WebApiTestBase):
    def setUp(self):
        super().setUp()
        base = self.config.models["test_model"]
        # A second chat profile with a live client, so switching the primary
        # binding has somewhere to go.
        self.config.models["second_model"] = replace(
            base, id="second_model", model="second-model"
        )
        self.model_router.clients["second_model"] = FakeClient(
            profile_id="second_model", model="second-model"
        )
        # Enabled in config but never instantiated: exercises the activation
        # rollback path.
        self.config.models["offline_model"] = replace(
            base, id="offline_model", model="offline-model"
        )
        self.config.models["embed_model"] = replace(
            base,
            id="embed_model",
            model="embed-model",
            modality="embedding",
            dimensions=1024,
        )
        self.config.models["vision_model"] = replace(
            base,
            id="vision_model",
            model="vision-model",
            capabilities=ModelCapabilities(tools=True, vision=True, reasoning=False),
        )
        self.model_router.clients["vision_model"] = FakeClient(
            profile_id="vision_model", model="vision-model"
        )
        self.store = self.app.state.resource_store

    def _saved(self) -> dict:
        return self.store.get_public("settings", "runtime")["payload"]

    def test_get_returns_bindings_and_candidates(self):
        response = self.client.get(ROUTING_URL)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["active_model"], "test_model")
        self.assertEqual(body["primary_profile_id"], "test_model")
        chat_ids = {item["id"] for item in body["chat_candidates"]}
        self.assertIn("second_model", chat_ids)
        self.assertEqual(
            [item["id"] for item in body["embedding_candidates"]], ["embed_model"]
        )
        self.assertEqual(
            [item["id"] for item in body["vision_candidates"]], ["vision_model"]
        )

    def test_switch_primary_applies_hot_and_persists(self):
        response = self.client.put(ROUTING_URL, json={"active_model": "second_model"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["restart_required"])
        self.assertEqual(body["routing"]["primary_profile_id"], "second_model")

        self.assertEqual(self.model_router.primary_profile_id, "second_model")
        self.assertEqual(self.config.app.active_model, "second_model")
        self.assertEqual(self._saved()["active_model"], "second_model")
        self.assertEqual(
            self.store.activation("settings", "runtime")["activation_state"], "active"
        )

        # A fresh store on the same database must still see the new binding.
        restarted = ScopedResourceStore(self.app.state.organization_store, self.config)
        rebuilt = restarted.build_project_config(self.config)
        self.assertEqual(rebuilt.app.active_model, "second_model")

    def test_switch_reassigns_fallback_when_it_collides_with_primary(self):
        # The seeded config uses test_model for both primary and fallback.
        self.assertEqual(self._saved()["fallback_model"], "test_model")
        response = self.client.put(ROUTING_URL, json={"active_model": "test_model"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self._saved()["fallback_model"], "second_model")
        self.assertEqual(self.model_router.fallback_profile_id, "second_model")

    def test_unknown_profile_is_rejected_without_touching_runtime(self):
        response = self.client.put(ROUTING_URL, json={"active_model": "ghost"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("不存在", response.json()["detail"])
        self.assertEqual(self.model_router.primary_profile_id, "test_model")
        self.assertEqual(self.config.app.active_model, "test_model")
        self.assertEqual(self._saved()["active_model"], "test_model")

    def test_failed_activation_rolls_back_config_and_router(self):
        response = self.client.put(
            ROUTING_URL, json={"active_model": "offline_model"}
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("offline_model", response.json()["detail"])
        self.assertEqual(self.model_router.primary_profile_id, "test_model")
        self.assertEqual(self.config.app.active_model, "test_model")
        self.assertEqual(self._saved()["active_model"], "test_model")
        self.assertEqual(
            self.store.activation("settings", "runtime")["activation_state"], "failed"
        )

    def test_modality_mismatch_is_rejected(self):
        response = self.client.put(
            ROUTING_URL, json={"active_model": "embed_model"}
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("chat", response.json()["detail"])

    def test_vision_binding_requires_vision_capability(self):
        rejected = self.client.put(ROUTING_URL, json={"vision_model": "second_model"})
        self.assertEqual(rejected.status_code, 400, rejected.text)
        accepted = self.client.put(ROUTING_URL, json={"vision_model": "vision_model"})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(self.model_router.vision_profile_id, "vision_model")
        self.assertEqual(self.config.app.vision_model, "vision_model")

    def test_embedding_binding_defers_to_restart(self):
        response = self.client.put(
            ROUTING_URL, json={"embedding_model": "embed_model"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["restart_required"])
        self.assertEqual(
            response.json()["routing"]["embedding_model"], "embed_model"
        )
        self.assertEqual(
            self.store.activation("settings", "runtime")["activation_state"],
            "restart_required",
        )
        # Not live yet: the active revision and the running config are untouched.
        self.assertEqual(self._saved()["embedding_model"], "")
        self.assertEqual(self.config.app.embedding_model, "")
        self.assertEqual(self.model_router.primary_profile_id, "test_model")

        restarted = ScopedResourceStore(self.app.state.organization_store, self.config)
        self.assertEqual(
            restarted.get_public("settings", "runtime")["payload"]["embedding_model"],
            "embed_model",
        )

    def test_empty_body_is_rejected(self):
        response = self.client.put(ROUTING_URL, json={})
        self.assertEqual(response.status_code, 400, response.text)

    def test_viewer_cannot_change_routing(self):
        response = self.viewer_client.put(
            ROUTING_URL, json={"active_model": "second_model"}
        )
        self.assertEqual(response.status_code, 403, response.text)
