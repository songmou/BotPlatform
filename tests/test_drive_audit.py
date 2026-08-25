from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.core.storage.drive_audit import DriveAuditStore
from src.core.storage.tenants import TenantRegistry


class DriveAuditStoreTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.registry = TenantRegistry(Path(temporary.name) / "data")
        self.store = DriveAuditStore(self.registry)
        self.store.record(
            "web:root", "web", "public", None, "upload",
            "docs/readme.md", None, 128, "成功", None,
        )
        self.store.record(
            "web:root", "web", "tenant", "tenant-a", "delete",
            "workspace/a.txt", None, 0, "失败", "文件不存在",
        )
        self.store.record(
            "agent:general", "agent", "tenant", "tenant-a", "upload",
            "workspace/b.txt", None, 64, "成功", None,
        )

    def test_scope_and_tenant_filter(self):
        items = self.store.list_recent(scope="tenant", tenant_id="tenant-a")
        self.assertEqual(len(items), 2)
        self.assertEqual(self.store.count(scope="tenant", tenant_id="tenant-a"), 2)
        self.assertEqual(self.store.count(scope="public"), 1)

    def test_action_filter(self):
        items = self.store.list_recent(action="upload")
        self.assertEqual(len(items), 2)
        self.assertEqual(self.store.count(action="delete"), 1)

    def test_operator_substring_match(self):
        self.assertEqual(self.store.count(operator="web"), 2)
        self.assertEqual(self.store.count(operator="agent:general"), 1)

    def test_like_wildcards_are_escaped_literally(self):
        # "%" and "_" in the query must not act as SQL wildcards.
        self.assertEqual(self.store.count(operator="%"), 0)
        self.assertEqual(self.store.count(operator="web_root"), 0)
        self.assertEqual(self.store.count(operator="web:root"), 2)

    def test_pagination_orders_latest_first(self):
        first = self.store.list_recent(limit=1, offset=0)
        second = self.store.list_recent(limit=1, offset=1)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(first[0]["path"], "workspace/b.txt")
        self.assertGreater(first[0]["id"], second[0]["id"])

    def test_record_fields_round_trip(self):
        row = self.store.list_recent(action="delete")[0]
        self.assertEqual(row["operator"], "web:root")
        self.assertEqual(row["source"], "web")
        self.assertEqual(row["scope"], "tenant")
        self.assertEqual(row["tenant_id"], "tenant-a")
        self.assertEqual(row["status"], "失败")
        self.assertEqual(row["error"], "文件不存在")


if __name__ == "__main__":
    unittest.main()
