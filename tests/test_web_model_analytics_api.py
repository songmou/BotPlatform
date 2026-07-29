"""Integration tests for the model analytics and budget endpoints."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

from tests._web_api_base import WebApiTestBase


class ModelAnalyticsUnavailableTest(WebApiTestBase):
    """Without an analytics store the endpoints must answer 503."""

    def test_endpoints_return_503(self):
        self.assertEqual(
            self.client.get("/api/model-analytics/overview").status_code, 503
        )
        self.assertEqual(self.client.get("/api/model-budgets").status_code, 503)


class ModelAnalyticsApiTest(WebApiTestBase):
    def setUp(self):
        self.store = MagicMock()
        self.store.currency = "CNY"
        super().setUp()

        self.store.overview.return_value = {"total_runs": 0}
        self.store.timeseries.return_value = []
        self.store.breakdown.return_value = []
        self.store.list_runs.return_value = []
        self.store.list_budgets.return_value = []
        self.store.list_alerts.return_value = []

    def app_kwargs(self):
        return {"model_analytics_store": self.store}

    # ---- query endpoints ----

    def test_overview(self):
        response = self.client.get("/api/model-analytics/overview")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"total_runs": 0})
        self.store.overview.assert_called_once()

    def test_overview_passes_filters(self):
        response = self.client.get(
            "/api/model-analytics/overview",
            params={"tenant_id": "t1", "source": "web", "status": "success"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        kwargs = self.store.overview.call_args.kwargs
        self.assertEqual(kwargs["tenant_id"], "t1")
        self.assertEqual(kwargs["source"], "web")
        self.assertEqual(kwargs["status"], "success")

    def test_invalid_time_range(self):
        response = self.client.get(
            "/api/model-analytics/overview",
            params={"from": "2024-05-02T00:00:00", "to": "2024-05-01T00:00:00"},
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.get(
            "/api/model-analytics/overview",
            params={"from": "2020-01-01T00:00:00", "to": "2024-01-01T00:00:00"},
        )
        self.assertEqual(response.status_code, 400)

    def test_invalid_source_and_status(self):
        response = self.client.get(
            "/api/model-analytics/overview", params={"source": "carrier-pigeon"}
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.get(
            "/api/model-analytics/overview", params={"status": "exploded"}
        )
        self.assertEqual(response.status_code, 400)

    def test_timeseries_bucket_validation(self):
        response = self.client.get(
            "/api/model-analytics/timeseries", params={"bucket": "day"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["currency"], "CNY")
        response = self.client.get(
            "/api/model-analytics/timeseries", params={"bucket": "week"}
        )
        self.assertEqual(response.status_code, 422)

    def test_breakdown_dimension_validation(self):
        response = self.client.get(
            "/api/model-analytics/breakdown", params={"dimension": "profile"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.get(
            "/api/model-analytics/breakdown", params={"dimension": "moon-phase"}
        )
        self.assertEqual(response.status_code, 422)

    def test_runs_and_detail(self):
        self.store.list_runs.return_value = [{"run_id": "r1"}]
        response = self.client.get("/api/model-analytics/runs")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"][0]["run_id"], "r1")

        self.store.run_detail.return_value = {"run_id": "r1"}
        response = self.client.get("/api/model-analytics/runs/r1")
        self.assertEqual(response.status_code, 200)

        self.store.run_detail.return_value = None
        response = self.client.get("/api/model-analytics/runs/r404")
        self.assertEqual(response.status_code, 404)

    def test_export_csv(self):
        self.store.list_runs.return_value = [
            {"run_id": "r1", "tenant_id": "t1", "status": "success"}
        ]
        response = self.client.get("/api/model-analytics/export.csv")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("text/csv", response.headers["content-type"])
        self.assertIn("r1", response.text)

    # ---- feedback ----

    def test_put_feedback(self):
        self.store.put_feedback.return_value = {"run_id": "r1", "rating": "good"}
        response = self.client.put(
            "/api/model-runs/r1/feedback", json={"rating": "good"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.put_feedback.call_args.kwargs["rating"], "good")

    def test_put_feedback_not_found(self):
        self.store.put_feedback.side_effect = LookupError("运行不存在")
        response = self.client.put(
            "/api/model-runs/r404/feedback", json={"rating": "good"}
        )
        self.assertEqual(response.status_code, 404)

    def test_put_feedback_invalid_rating(self):
        self.store.put_feedback.side_effect = ValueError("评分无效")
        response = self.client.put(
            "/api/model-runs/r1/feedback", json={"rating": "meh"}
        )
        self.assertEqual(response.status_code, 400)

    # ---- budgets ----

    def test_list_budgets_refreshes_alerts(self):
        response = self.client.get("/api/model-budgets")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["currency"], "CNY")
        self.store.refresh_budget_alerts.assert_called_once()

    def test_create_budget(self):
        self.store.save_budget.return_value = {"budget_id": 1}
        response = self.client.post(
            "/api/model-budgets",
            json={"scope_type": "global", "monthly_limit_micros": 1000000},
        )
        self.assertEqual(response.status_code, 201, response.text)

    def test_create_budget_conflict(self):
        self.store.save_budget.side_effect = sqlite3.IntegrityError()
        response = self.client.post(
            "/api/model-budgets",
            json={"scope_type": "global", "monthly_limit_micros": 1000000},
        )
        self.assertEqual(response.status_code, 409)

    def test_update_budget_not_found(self):
        self.store.save_budget.side_effect = LookupError("预算不存在")
        response = self.client.put(
            "/api/model-budgets/99",
            json={"scope_type": "global", "monthly_limit_micros": 1000000},
        )
        self.assertEqual(response.status_code, 404)

    def test_delete_budget(self):
        response = self.client.delete("/api/model-budgets/1")
        self.assertEqual(response.status_code, 200)
        self.store.delete_budget.side_effect = LookupError("预算不存在")
        response = self.client.delete("/api/model-budgets/2")
        self.assertEqual(response.status_code, 404)

    # ---- permissions ----

    def test_viewer_can_read_but_not_manage(self):
        response = self.viewer_client.get("/api/model-analytics/overview")
        self.assertEqual(response.status_code, 200, response.text)
        response = self.viewer_client.post(
            "/api/model-budgets",
            json={"scope_type": "global", "monthly_limit_micros": 1000000},
        )
        self.assertEqual(response.status_code, 403)
