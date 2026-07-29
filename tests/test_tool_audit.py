from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import HTTPException

from src.api.routers.plugins import list_tool_audit
from src.core.storage.tenants import TenantRegistry
from src.core.storage.tool_audit import ToolAuditStore


class ToolAuditStoreTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.registry = TenantRegistry(Path(temporary.name) / "data")
        self.store = ToolAuditStore(self.registry)
        self.store.record(
            "tenant-a", "session-a", "agent-a", "read_text_file",
            "成功", 12, 128, "hash-a", None,
        )
        self.store.record(
            "tenant-a", "session-a", "agent-a", "read_text_file",
            "失败", 18, 0, "hash-b", "文件不存在",
        )
        self.store.record(
            "tenant-b", "session-b", "agent-b", "run_command",
            "失败", 30, 64, "hash-c", "命令失败",
        )

    def test_combined_tool_and_status_filter(self):
        items = self.store.list_recent(
            limit=20,
            tool_name="read_text_file",
            status="失败",
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["args_hash"], "hash-b")
        self.assertEqual(
            self.store.count(tool_name="read_text_file", status="失败"),
            1,
        )
        self.assertEqual(self.store.count(status="失败"), 2)

    def test_tool_name_substring_match(self):
        items = self.store.list_recent(limit=20, tool_name="read")
        self.assertEqual(len(items), 2)
        self.assertEqual(self.store.count(tool_name="read"), 2)
        self.assertEqual(self.store.count(tool_name="command"), 1)

    def test_like_wildcards_are_escaped_literally(self):
        # "%" and "_" in the query must not act as SQL wildcards.
        self.assertEqual(self.store.count(tool_name="%"), 0)
        self.assertEqual(self.store.count(tool_name="read_text"), 2)
        self.assertEqual(self.store.count(tool_name="readXtext"), 0)

    def test_pagination_and_empty_filter(self):
        self.assertEqual(len(self.store.list_recent(limit=1, offset=0)), 1)
        self.assertEqual(len(self.store.list_recent(limit=1, offset=1)), 1)
        self.assertEqual(
            self.store.list_recent(tool_name="missing", status="成功"),
            [],
        )
        self.assertEqual(
            self.store.count(tool_name="missing", status="成功"),
            0,
        )


class ToolAuditEndpointTest(unittest.TestCase):
    def test_endpoint_passes_filters_to_items_and_total(self):
        store = MagicMock()
        store.list_recent.return_value = [{"tool_name": "run_command"}]
        store.count.return_value = 1
        request = MagicMock()
        request.app.state.tool_audit_store = store

        result = list_tool_audit(
            request,
            limit=10,
            tool="run_command",
            status="失败",
            offset=20,
        )

        self.assertEqual(result["total"], 1)
        store.list_recent.assert_called_once_with(
            limit=10,
            tool_name="run_command",
            offset=20,
            status="失败",
        )
        store.count.assert_called_once_with(
            tool_name="run_command",
            status="失败",
        )

    def test_endpoint_rejects_unknown_status(self):
        with self.assertRaises(HTTPException) as context:
            list_tool_audit(MagicMock(), status="unknown")
        self.assertEqual(context.exception.status_code, 422)
        self.assertIn("状态仅支持", context.exception.detail)

    def test_endpoint_clamps_pagination_bounds(self):
        store = MagicMock()
        store.list_recent.return_value = []
        store.count.return_value = 0
        request = MagicMock()
        request.app.state.tool_audit_store = store

        list_tool_audit(request, limit=9999, offset=-5)

        store.list_recent.assert_called_once_with(
            limit=200,
            tool_name=None,
            offset=0,
            status=None,
        )


if __name__ == "__main__":
    unittest.main()
