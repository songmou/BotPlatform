from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from src.core.storage.mcp_call_log import (
    McpCallLogStore,
    _serialize_truncated,
)
from src.core.storage.tenants import TenantRegistry


class McpCallLogStoreTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.registry = TenantRegistry(Path(temporary.name) / "data")
        self.store = McpCallLogStore(self.registry)
        self.store.record(
            server_id="feishu", tool_name="get_user", source="manual",
            status="success", duration_ms=12,
            arguments={"user_id": "u1"}, result={"name": "alice"},
        )
        self.store.record(
            server_id="feishu", tool_name="get_user", source="agent",
            status="error", duration_ms=30,
            arguments={"user_id": "u2"}, result=None, error="token expired",
        )
        self.store.record(
            server_id="feishu", tool_name="list_docs", source="manual",
            status="success", duration_ms=55,
            arguments={"page": 1}, result={"docs": []},
        )
        self.store.record(
            server_id="github", tool_name="search", source="agent",
            status="success", duration_ms=80,
            arguments={"q": "test"}, result={"count": 0},
        )

    def test_list_by_server_filters_by_server(self):
        items = self.store.list_by_server("feishu", limit=20)
        self.assertEqual(len(items), 3)
        github = self.store.list_by_server("github", limit=20)
        self.assertEqual(len(github), 1)

    def test_tool_filter(self):
        items = self.store.list_by_server("feishu", limit=20, tool="get_user")
        self.assertEqual(len(items), 2)
        self.store.count_by_server("feishu", tool="get_user")
        self.assertEqual(self.store.count_by_server("feishu", tool="get_user"), 2)

    def test_source_and_status_filter(self):
        items = self.store.list_by_server(
            "feishu", limit=20, source="manual", status="success"
        )
        self.assertEqual(len(items), 2)
        self.assertEqual(
            self.store.count_by_server("feishu", source="manual", status="success"), 2
        )
        errors = self.store.list_by_server("feishu", limit=20, status="error")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error"], "token expired")

    def test_pagination(self):
        page1 = self.store.list_by_server("feishu", limit=2, offset=0)
        page2 = self.store.list_by_server("feishu", limit=2, offset=2)
        self.assertEqual(len(page1), 2)
        self.assertEqual(len(page2), 1)
        # Ensure no overlap
        ids_1 = {item["id"] for item in page1}
        ids_2 = {item["id"] for item in page2}
        self.assertFalse(ids_1 & ids_2)

    def test_count_matches_list(self):
        total = self.store.count_by_server("feishu")
        self.assertEqual(total, 3)

    def test_arguments_and_result_serialized(self):
        items = self.store.list_by_server("feishu", limit=20, tool="get_user", status="success")
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertIn('"user_id"', item["input_json"])
        self.assertIn('"name"', item["output_json"])
        self.assertEqual(item["input_truncated"], 0)
        self.assertEqual(item["output_truncated"], 0)

    def test_empty_server(self):
        self.assertEqual(self.store.list_by_server("nonexistent", limit=10), [])
        self.assertEqual(self.store.count_by_server("nonexistent"), 0)


class SerializeTruncatedTest(unittest.TestCase):
    def test_none_yields_none(self):
        text, flag = _serialize_truncated(None)
        self.assertIsNone(text)
        self.assertEqual(flag, 0)

    def test_small_object_not_truncated(self):
        text, flag = _serialize_truncated({"a": 1})
        self.assertEqual(flag, 0)
        self.assertIn("a", text)

    def test_large_payload_truncated(self):
        big = "x" * 70000
        text, flag = _serialize_truncated(big)
        self.assertEqual(flag, 1)
        self.assertIn("已截断", text)
        # Truncated text should be shorter than the original
        self.assertLess(len(text.encode("utf-8")), 70000)

    def test_non_serializable_falls_back_to_repr(self):
        class Weird:
            pass
        text, flag = _serialize_truncated(Weird())
        self.assertEqual(flag, 0)
        self.assertIn("Weird", text)


if __name__ == "__main__":
    unittest.main()
