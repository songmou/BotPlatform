from __future__ import annotations

import unittest

from tests._web_api_base import WebApiTestBase
from src.core.storage.mcp_call_log import McpCallLogStore


class McpCallLogApiTest(WebApiTestBase):
    def app_kwargs(self):
        self.call_log_store = McpCallLogStore(self.registry)
        return {"mcp_call_log_store": self.call_log_store}

    def test_empty_logs(self):
        r = self.client.get("/api/v2/platform/mcp/feishu/call-logs")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["items"], [])
        self.assertEqual(data["total"], 0)

    def test_list_after_record(self):
        self.call_log_store.record(
            server_id="feishu", tool_name="get_user", source="manual",
            status="success", duration_ms=10,
            arguments={"q": 1}, result={"ok": True},
        )
        self.call_log_store.record(
            server_id="feishu", tool_name="get_user", source="agent",
            status="error", duration_ms=20,
            arguments={"q": 2}, result=None, error="boom",
        )
        r = self.client.get("/api/v2/platform/mcp/feishu/call-logs")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["total"], 2)
        self.assertEqual(len(data["items"]), 2)

    def test_filters(self):
        self.call_log_store.record(
            server_id="feishu", tool_name="get_user", source="manual",
            status="success", duration_ms=10, arguments={}, result={},
        )
        self.call_log_store.record(
            server_id="feishu", tool_name="list_docs", source="agent",
            status="error", duration_ms=20, arguments={}, result=None, error="x",
        )
        r = self.client.get(
            "/api/v2/platform/mcp/feishu/call-logs?source=manual&status=success"
        )
        data = r.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["tool_name"], "get_user")

    def test_tool_filter(self):
        self.call_log_store.record(
            server_id="feishu", tool_name="get_user", source="manual",
            status="success", duration_ms=10, arguments={}, result={},
        )
        self.call_log_store.record(
            server_id="feishu", tool_name="list_docs", source="manual",
            status="success", duration_ms=20, arguments={}, result={},
        )
        r = self.client.get(
            "/api/v2/platform/mcp/feishu/call-logs?tool=get_user"
        )
        data = r.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["tool_name"], "get_user")

    def test_pagination(self):
        for i in range(5):
            self.call_log_store.record(
                server_id="feishu", tool_name="t", source="manual",
                status="success", duration_ms=i, arguments={"i": i}, result={},
            )
        r = self.client.get(
            "/api/v2/platform/mcp/feishu/call-logs?limit=2&offset=0"
        )
        data = r.json()
        self.assertEqual(data["total"], 5)
        self.assertEqual(len(data["items"]), 2)
        r2 = self.client.get(
            "/api/v2/platform/mcp/feishu/call-logs?limit=2&offset=2"
        )
        self.assertEqual(len(r2.json()["items"]), 2)
        r3 = self.client.get(
            "/api/v2/platform/mcp/feishu/call-logs?limit=2&offset=4"
        )
        self.assertEqual(len(r3.json()["items"]), 1)

    def test_limit_clamped(self):
        r = self.client.get("/api/v2/platform/mcp/feishu/call-logs?limit=500")
        self.assertEqual(r.json()["limit"], 200)
        r2 = self.client.get("/api/v2/platform/mcp/feishu/call-logs?limit=0")
        self.assertEqual(r2.json()["limit"], 1)

    def test_other_server_isolated(self):
        self.call_log_store.record(
            server_id="feishu", tool_name="t", source="manual",
            status="success", duration_ms=1, arguments={}, result={},
        )
        r = self.client.get("/api/v2/platform/mcp/github/call-logs")
        self.assertEqual(r.json()["total"], 0)

    def test_viewer_can_read(self):
        # viewer role has panel.read permission
        self.call_log_store.record(
            server_id="feishu", tool_name="t", source="manual",
            status="success", duration_ms=1, arguments={}, result={},
        )
        r = self.viewer_client.get("/api/v2/platform/mcp/feishu/call-logs")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["total"], 1)


if __name__ == "__main__":
    unittest.main()
