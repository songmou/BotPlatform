"""Integration tests for the per-tenant environment variable endpoints.

The router only ever returns *masked* values; the management UI sends new
values via PUT and reads masked values via GET. Permissions require
``tenants.manage`` (the seeded viewer role only has tenants.read).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.core.services.env_resolver import EnvResolver
from src.core.storage.tenants import SettingsStore

from tests._web_api_base import WebApiTestBase


class TenantEnvApiTest(WebApiTestBase):
    def app_kwargs(self):
        settings_store = SettingsStore(self.registry)

        def global_loader():
            return {
                "API_TOKEN": "global-secret-value-1234",
                "FEATURE_FLAG": "on",
            }

        env_resolver = EnvResolver(settings_store, global_loader)
        script_service = MagicMock()
        script_service.definitions = {
            "demo": SimpleNamespace(env_allowlist=("API_TOKEN", "PLUGIN_DEBUG")),
        }
        return {
            "settings_store": settings_store,
            "env_resolver": env_resolver,
            "script_service": script_service,
        }

    # ---- CRUD over masked values ----

    def test_put_then_get_is_masked(self):
        tenant = self._make_tenant()
        put = self.client.put(
            "/api/tenants/{}/env/API_TOKEN".format(tenant.tenant_id),
            json={"value": "org-secret-value-5678"},
        )
        self.assertEqual(put.status_code, 200, put.text)
        self.assertEqual(put.json()["masked"], "or****78")
        self.assertTrue(put.json()["defined"])

        listing = self.client.get(
            "/api/tenants/{}/env".format(tenant.tenant_id)
        )
        self.assertEqual(listing.status_code, 200, listing.text)
        names = {v["name"] for v in listing.json()["variables"]}
        self.assertIn("API_TOKEN", names)
        masked = next(
            v for v in listing.json()["variables"] if v["name"] == "API_TOKEN"
        )
        # Plaintext must never leak back.
        self.assertNotEqual(masked["masked"], "org-secret-value-5678")
        self.assertNotIn("5678", masked["masked"])

    def test_put_requires_string_value(self):
        tenant = self._make_tenant()
        bad = self.client.put(
            "/api/tenants/{}/env/API_TOKEN".format(tenant.tenant_id),
            json={"value": 1234},
        )
        self.assertEqual(bad.status_code, 400)

    def test_put_rejects_invalid_name(self):
        tenant = self._make_tenant()
        bad = self.client.put(
            "/api/tenants/{}/env/path".format(tenant.tenant_id),
            json={"value": "x"},
        )
        self.assertEqual(bad.status_code, 400)

    def test_delete_unknown_is_404(self):
        tenant = self._make_tenant()
        response = self.client.delete(
            "/api/tenants/{}/env/NOPE".format(tenant.tenant_id)
        )
        self.assertEqual(response.status_code, 404)

    def test_delete_then_gone(self):
        tenant = self._make_tenant()
        self.client.put(
            "/api/tenants/{}/env/API_TOKEN".format(tenant.tenant_id),
            json={"value": "org-secret-value-5678"},
        )
        deleted = self.client.delete(
            "/api/tenants/{}/env/API_TOKEN".format(tenant.tenant_id)
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertTrue(deleted.json()["deleted"])
        listing = self.client.get(
            "/api/tenants/{}/env".format(tenant.tenant_id)
        ).json()
        self.assertNotIn(
            "API_TOKEN", {v["name"] for v in listing["variables"]}
        )

    # ---- resolution views ----

    def test_resolve_tenant_shows_org_override(self):
        tenant = self._make_tenant()
        self.client.put(
            "/api/tenants/{}/env/API_TOKEN".format(tenant.tenant_id),
            json={"value": "org-secret-value-5678"},
        )
        response = self.client.get(
            "/api/tenants/{}/env/resolve".format(tenant.tenant_id),
            params={"script_id": "demo"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        rows = {b["name"]: b for b in response.json()["bindings"]}
        self.assertEqual(rows["API_TOKEN"]["source"], "tenant")
        self.assertEqual(rows["API_TOKEN"]["masked"], "or****78")
        # Not declared by the script -> absent from the allowlist view.
        self.assertNotIn("FEATURE_FLAG", rows)

    def test_resolve_global_only(self):
        response = self.client.get(
            "/api/tenants/env/global/resolve", params={"script_id": "demo"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        rows = {b["name"]: b for b in response.json()["bindings"]}
        self.assertEqual(rows["API_TOKEN"]["source"], "global")
        self.assertEqual(rows["API_TOKEN"]["masked"], "gl****34")
        self.assertEqual(rows["PLUGIN_DEBUG"]["source"], "missing")

    def test_resolve_unknown_script_404(self):
        tenant = self._make_tenant()
        response = self.client.get(
            "/api/tenants/{}/env/resolve".format(tenant.tenant_id),
            params={"script_id": "ghost"},
        )
        self.assertEqual(response.status_code, 404)

    # ---- permissions ----

    def test_unknown_tenant_404(self):
        response = self.client.get(
            "/api/tenants/00000000-0000-0000-0000-000000000009/env"
        )
        self.assertEqual(response.status_code, 404)

    def test_viewer_cannot_manage(self):
        tenant = self._make_tenant()
        self.assertEqual(
            self.viewer_client.get(
                "/api/tenants/{}/env".format(tenant.tenant_id)
            ).status_code,
            403,
        )
        self.assertEqual(
            self.viewer_client.put(
                "/api/tenants/{}/env/API_TOKEN".format(tenant.tenant_id),
                json={"value": "x"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.viewer_client.delete(
                "/api/tenants/{}/env/API_TOKEN".format(tenant.tenant_id)
            ).status_code,
            403,
        )
