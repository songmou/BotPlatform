from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.core.config.loader import ModelPricing
from src.core.modeling import (
    CanonicalMessage,
    ModelCallContext,
    ModelIdentity,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelStreamEvent,
    ModelUsage,
)
from src.core.modeling.observability import ObservedModelClient
from src.core.services.auth import AdminAuthService
from src.core.storage.admin_users import (
    AdminRoleStore,
    AdminSessionStore,
    AdminUserStore,
)
from src.core.storage.model_analytics import ModelAnalyticsStore
from src.core.storage.tenants import ConversationStore, TenantRegistry
from tests.test_web_api import FakeClient, _make_config


class ModelAnalyticsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = _make_config()
        original = self.config.models["test_model"]
        self.config.models["test_model"] = replace(
            original,
            billing_currency="CNY",
            pricing=ModelPricing(
                input_per_million="1",
                cached_input_per_million="0.2",
                output_per_million="2",
                reasoning_output_per_million="4",
            ),
        )
        self.registry = TenantRegistry(Path(self.temporary.name))
        self.tenant = self.registry.resolve("bot", "user")
        self.store = ModelAnalyticsStore(self.registry, self.config)

    def record(self, run_id: str, *, usage: ModelUsage | None = None) -> None:
        self.store.record_model_call(
            ModelIdentity("test_model", "test", "configured"),
            "actual",
            "成功",
            0.25,
            usage
            or ModelUsage(
                input_tokens=1000,
                output_tokens=500,
                total_tokens=1500,
                cached_input_tokens=400,
                uncached_input_tokens=600,
                reasoning_output_tokens=100,
            ),
            1,
            "provider-request",
            ModelCallContext(
                run_id=run_id,
                tenant_id=self.tenant.tenant_id,
                source="web",
                operation="answer",
                agent_id="general",
            ),
            "stop",
            0.05,
            None,
        )

    def test_cost_feedback_budget_and_tenant_cascade(self) -> None:
        run_id = self.store.start_run(
            tenant_id=self.tenant.tenant_id,
            source="web",
            agent_id="general",
        )
        self.record(run_id)
        detail = self.store.run_detail(run_id)
        assert detail is not None
        call = detail["calls"][0]
        self.assertEqual(call["cost_micros"], 1880)
        self.assertEqual(call["cost_status"], "priced")
        self.assertEqual(call["input_price_micros_per_million"], 1_000_000)

        self.store.put_feedback(
            run_id,
            actor_type="tenant",
            actor_ref=self.tenant.tenant_id,
            rating="bad",
            reasons=["事实错误"],
            comment="答案不准确",
            tenant_id=self.tenant.tenant_id,
        )
        start = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        end = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        overview = self.store.overview(date_from=start, date_to=end)
        self.assertEqual(overview["run_count"], 1)
        self.assertEqual(overview["feedback_count"], 1)
        self.assertEqual(overview["positive_rate"], 0)

        budget = self.store.save_budget(
            budget_id=None,
            scope_type="global",
            scope_id="",
            monthly_limit_micros=1000,
            enabled=True,
        )
        self.assertGreater(budget["usage_ratio"], 1)
        self.store.refresh_budget_alerts()
        self.assertEqual(
            [item["threshold"] for item in self.store.list_alerts()], [100, 80]
        )

        self.registry.delete(self.tenant)
        self.assertIsNone(self.store.run_detail(run_id))

    def test_usage_unknown_and_unpriced_are_not_zero_cost(self) -> None:
        first = self.store.start_run(
            tenant_id=self.tenant.tenant_id, source="internal"
        )
        self.record(first, usage=ModelUsage())
        self.config.models["test_model"] = replace(
            self.config.models["test_model"], pricing=None
        )
        second = self.store.start_run(
            tenant_id=self.tenant.tenant_id, source="internal"
        )
        self.record(
            second,
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        )
        self.assertEqual(
            self.store.run_detail(first)["calls"][0]["cost_status"],
            "usage_unknown",
        )
        self.assertIsNone(self.store.run_detail(first)["calls"][0]["cost_micros"])
        self.assertEqual(
            self.store.run_detail(second)["calls"][0]["cost_status"], "unpriced"
        )

    def test_stream_observer_records_terminal_usage_without_prompt(self) -> None:
        class StreamClient(FakeClient):
            def complete_stream(self, request):
                yield ModelStreamEvent(text="你")
                yield ModelStreamEvent(text="好")
                yield ModelStreamEvent(
                    response=ModelResponse(
                        CanonicalMessage("assistant", ""),
                        actual_model="actual",
                        usage=ModelUsage(3, 2, 5),
                        request_id="request",
                        finish_reason="stop",
                    )
                )

        logs = []
        client = ObservedModelClient(StreamClient(), lambda *values: logs.append(values))
        chunks = list(
            client.complete_stream(
                ModelRequest(messages=[CanonicalMessage("user", "private prompt")])
            )
        )
        self.assertEqual(chunks, ["你", "好"])
        self.assertEqual(logs[0][2], "成功")
        self.assertEqual(logs[0][4].total_tokens, 5)
        self.assertNotIn("private prompt", repr(logs[0]))


class ModelAnalyticsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = _make_config()
        self.registry = TenantRegistry(Path(temporary.name))
        self.analytics = ModelAnalyticsStore(self.registry, self.config)
        database = self.registry.database
        users = AdminUserStore(database)
        roles = AdminRoleStore(database)
        sessions = AdminSessionStore(database, b"analytics-secret")
        auth = AdminAuthService(users, roles, sessions, Path(temporary.name))
        users.create("admin", "password12345", roles.get_by_code("admin").role_id)
        app = create_app(
            self.config,
            ModelRouter.single(FakeClient()),
            self.registry,
            ConversationStore(self.registry, 20),
            model_analytics_store=self.analytics,
            admin_auth=auth,
            admin_user_store=users,
            admin_role_store=roles,
        )
        self.client = TestClient(app)
        response = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "password12345"},
        )
        self.assertEqual(response.status_code, 200)

    def test_overview_feedback_budget_and_csv_endpoints(self) -> None:
        tenant = self.registry.resolve("web", "conversation")
        run_id = self.analytics.start_run(
            tenant_id=tenant.tenant_id,
            source="web",
            agent_id="general",
        )
        self.analytics.record_model_call(
            ModelIdentity("test_model", "test", "test-model"),
            "test-model",
            "成功",
            0.1,
            ModelUsage(10, 5, 15),
            0,
            "request",
            ModelCallContext(
                run_id=run_id,
                tenant_id=tenant.tenant_id,
                source="web",
                operation="answer",
                agent_id="general",
            ),
        )

        response = self.client.get("/api/model-analytics/overview")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["call_count"], 1)

        response = self.client.put(
            "/api/model-runs/{}/feedback".format(run_id),
            json={"rating": "good", "reasons": [], "comment": ""},
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.post(
            "/api/model-budgets",
            json={
                "scope_type": "global",
                "scope_id": "",
                "monthly_limit_micros": 1_000_000,
                "enabled": True,
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.client.get("/api/model-budgets").status_code, 200)
        csv_response = self.client.get("/api/model-analytics/export.csv")
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("run_id", csv_response.text)


if __name__ == "__main__":
    unittest.main()
