"""Integration tests for the external script management endpoints."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from tests._web_api_base import WebApiTestBase


class ScriptsApiUnavailableTest(WebApiTestBase):
    """Without script services every endpoint must answer 503."""

    def test_endpoints_return_503(self):
        self.assertEqual(self.client.get("/api/scripts").status_code, 503)
        self.assertEqual(
            self.client.put(
                "/api/scripts/roots", json={"allowed_roots": []}
            ).status_code,
            503,
        )
        self.assertEqual(
            self.client.get("/api/script-runs", params={"tenant_id": "x"}).status_code,
            503,
        )


class ScriptsApiTest(WebApiTestBase):
    def setUp(self):
        self.script_registry = MagicMock()
        self.script_service = MagicMock()
        self.script_schedules = MagicMock()
        super().setUp()

        self.script_registry.allowed_roots = ["/tmp/scripts"]
        self.script_registry.list_entries.return_value = []
        self.script_service.list_scripts.return_value = [
            {"id": "demo", "name": "演示脚本"}
        ]

    def app_kwargs(self):
        return {
            "script_registry": self.script_registry,
            "script_service": self.script_service,
            "script_schedule_service": self.script_schedules,
        }

    # ---- listing / roots ----

    def test_list_scripts(self):
        response = self.client.get("/api/scripts")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["allowed_roots"], ["/tmp/scripts"])
        self.assertEqual(data["scripts"][0]["id"], "demo")

    def test_update_roots(self):
        self.script_registry.configure_roots.return_value = ["/srv/scripts"]
        response = self.client.put(
            "/api/scripts/roots", json={"allowed_roots": ["/srv/scripts"]}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["allowed_roots"], ["/srv/scripts"])
        self.script_service.reload_external_definitions.assert_called_once()
        self.script_schedules.reload_scheduler.assert_called_once()

    def test_update_roots_rejects_non_list(self):
        response = self.client.put(
            "/api/scripts/roots", json={"allowed_roots": "not-a-list"}
        )
        self.assertEqual(response.status_code, 400)

    def test_update_roots_maps_value_error_to_400(self):
        self.script_registry.configure_roots.side_effect = ValueError("目录不存在")
        response = self.client.put(
            "/api/scripts/roots", json={"allowed_roots": ["/nope"]}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("目录不存在", response.json()["detail"])

    # ---- create / update / delete ----

    def test_create_script(self):
        self.script_registry.create.return_value = SimpleNamespace(id="demo")
        response = self.client.post(
            "/api/scripts", json={"id": "demo", "path": "demo.py"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["id"], "demo")

    def test_create_script_invalid_payload(self):
        self.script_registry.create.side_effect = ValueError("路径不合法")
        response = self.client.post("/api/scripts", json={"id": "bad"})
        self.assertEqual(response.status_code, 400)

    def test_update_script(self):
        response = self.client.put("/api/scripts/demo", json={"name": "改名"})
        self.assertEqual(response.status_code, 200, response.text)
        self.script_registry.update.assert_called_once()

    def test_delete_script_blocked_by_schedule_reference(self):
        self.script_schedules.store.list.return_value = [
            SimpleNamespace(script_id="demo")
        ]
        response = self.client.delete("/api/scripts/demo")
        self.assertEqual(response.status_code, 409)

    def test_delete_script(self):
        self.script_schedules.store.list.return_value = []
        response = self.client.delete("/api/scripts/demo")
        self.assertEqual(response.status_code, 200)
        self.script_registry.delete.assert_called_once_with("demo")

    # ---- runs ----

    def test_run_script_requires_tenant_id_string(self):
        response = self.client.post(
            "/api/scripts/demo/runs", json={"tenant_id": 123}
        )
        self.assertEqual(response.status_code, 400)

    def test_run_script_unknown_tenant(self):
        response = self.client.post(
            "/api/scripts/demo/runs",
            json={"tenant_id": "00000000-0000-0000-0000-000000000009"},
        )
        self.assertEqual(response.status_code, 404)

    def test_run_script_submits(self):
        tenant = self._make_tenant()
        self.script_service.submit.return_value = {"run_id": "r1", "status": "queued"}
        self.script_service.recipient_store.load.return_value = None
        response = self.client.post(
            "/api/scripts/demo/runs",
            json={"tenant_id": tenant.tenant_id, "parameters": {"a": 1}},
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["run_id"], "r1")
        self.script_service.submit.assert_called_once()

    def test_list_runs(self):
        tenant = self._make_tenant()
        self.script_service.list_runs.return_value = [{"run_id": "r1"}]
        response = self.client.get(
            "/api/script-runs", params={"tenant_id": tenant.tenant_id}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()[0]["run_id"], "r1")

    def test_get_run_not_found(self):
        tenant = self._make_tenant()
        self.script_service.get_run.side_effect = ValueError("运行不存在")
        response = self.client.get(
            "/api/script-runs/r404", params={"tenant_id": tenant.tenant_id}
        )
        self.assertEqual(response.status_code, 404)

    def test_cancel_run(self):
        tenant = self._make_tenant()
        self.script_service.cancel_run.return_value = {"run_id": "r1", "status": "cancelled"}
        response = self.client.post(
            "/api/script-runs/r1/cancel", json={"tenant_id": tenant.tenant_id}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "cancelled")

    # ---- tenant script schedules ----

    def test_tenant_script_schedule_crud(self):
        tenant = self._make_tenant()
        self.script_schedules.manage.return_value = {"schedule_id": "s1"}
        response = self.client.post(
            "/api/tenants/{}/script-schedules".format(tenant.tenant_id),
            json={"script_id": "demo", "cron": "0 9 * * *"},
        )
        self.assertEqual(response.status_code, 201, response.text)

        self.script_schedules.list_for_tenant.return_value = [{"schedule_id": "s1"}]
        response = self.client.get(
            "/api/tenants/{}/script-schedules".format(tenant.tenant_id)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["schedule_id"], "s1")

        response = self.client.put(
            "/api/tenants/{}/script-schedules/s1".format(tenant.tenant_id),
            json={"enabled": False},
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.delete(
            "/api/tenants/{}/script-schedules/s1".format(tenant.tenant_id)
        )
        self.assertEqual(response.status_code, 200)

    # ---- permissions ----

    def test_viewer_has_no_script_permissions(self):
        self.assertEqual(self.viewer_client.get("/api/scripts").status_code, 403)
        self.assertEqual(
            self.viewer_client.put(
                "/api/scripts/roots", json={"allowed_roots": []}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.viewer_client.post(
                "/api/scripts/demo/runs", json={"tenant_id": "x"}
            ).status_code,
            403,
        )
