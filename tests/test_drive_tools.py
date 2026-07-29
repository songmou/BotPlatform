"""Tests for the built-in network drive tools on ToolRuntime."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from src.core.config.loader import load_project_config
from src.core.services.drive import DriveService
from src.core.storage.drive_audit import DriveAuditStore
from src.core.storage.tenants import TenantRegistry
from src.core.tooling import ToolAuditContext, ToolRuntime
from src.core.tooling.definitions import APPROVAL_TOOLS

SOURCE_CONFIG = Path(__file__).resolve().parents[1] / "config"


class DriveToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_root = Path(self.temp.name) / "data"
        self.registry = TenantRegistry(self.data_root)
        self.tenant = self.registry.resolve("ilink", "wxid_demo")
        self.drive_service = DriveService(self.registry, self.data_root / "public")
        self.drive_audit = DriveAuditStore(self.registry)

        original = load_project_config(SOURCE_CONFIG).tools
        config = replace(
            original,
            default_working_directory=str(self.data_root),
            allowed_roots=[str(self.data_root)],
        )
        self.runtime = ToolRuntime(
            config,
            "Asia/Shanghai",
            sandbox_available=True,
            tenant_registry=self.registry,
            drive_service=self.drive_service,
            drive_audit_store=self.drive_audit,
        )
        self.runtime.bind_tenant(self.tenant)
        self.context = ToolAuditContext(agent_id="assistant")

    def test_list_tenant_drive(self) -> None:
        result = self.runtime.execute("drive_list_files", {}, self.context)
        self.assertTrue(result.ok, result.error)
        names = {entry["name"] for entry in result.data["entries"]}
        self.assertIn("workspace", names)
        self.assertIn("scripts", names)

    def test_list_public_drive(self) -> None:
        self.drive_service.create_folder("public", None, "", "docs")
        result = self.runtime.execute(
            "drive_list_files", {"scope": "public"}, self.context
        )
        self.assertTrue(result.ok, result.error)
        self.assertIn("docs", {entry["name"] for entry in result.data["entries"]})

    def test_save_and_read_round_trip(self) -> None:
        saved = self.runtime.execute(
            "drive_save_file",
            {"path": "workspace/notes.txt", "content": "第一行\n第二行"},
            self.context,
        )
        self.assertTrue(saved.ok, saved.error)
        self.assertEqual(saved.data["path"], "workspace/notes.txt")
        read = self.runtime.execute(
            "drive_read_file", {"path": "workspace/notes.txt"}, self.context
        )
        self.assertTrue(read.ok, read.error)
        self.assertEqual(read.data["content"], "第一行\n第二行")
        self.assertFalse(read.data["truncated"])

    def test_read_file_max_lines_truncates(self) -> None:
        content = "\n".join("行{}".format(i) for i in range(10))
        self.drive_service.save_file(
            "tenant", self.tenant.tenant_id, "workspace", "long.txt", content.encode()
        )
        read = self.runtime.execute(
            "drive_read_file",
            {"path": "workspace/long.txt", "max_lines": 3},
            self.context,
        )
        self.assertTrue(read.ok, read.error)
        self.assertTrue(read.data["truncated"])
        self.assertEqual(read.data["content"].count("\n"), 2)

    def test_save_requires_overwrite_for_existing(self) -> None:
        arguments = {"path": "workspace/a.txt", "content": "v1"}
        self.assertTrue(self.runtime.execute("drive_save_file", arguments, self.context).ok)
        conflict = self.runtime.execute("drive_save_file", arguments, self.context)
        self.assertFalse(conflict.ok)
        arguments["overwrite"] = True
        self.assertTrue(self.runtime.execute("drive_save_file", arguments, self.context).ok)

    def test_public_scope_is_read_only_for_agents(self) -> None:
        # drive_save_file/drive_delete_file expose no scope parameter, so any
        # attempt to target the public area is rejected as an unknown argument.
        result = self.runtime.execute(
            "drive_save_file",
            {"scope": "public", "path": "a.txt", "content": "x"},
            self.context,
        )
        self.assertFalse(result.ok)
        self.assertIn("未知参数", result.error)

    def test_delete_file_requires_approval_and_works(self) -> None:
        self.assertIn("drive_delete_file", APPROVAL_TOOLS)
        self.drive_service.save_file(
            "tenant", self.tenant.tenant_id, "workspace", "gone.txt", b"bye"
        )
        preview = self.runtime.preview(
            "drive_delete_file", {"path": "workspace/gone.txt"}
        )
        self.assertIn("workspace/gone.txt", preview)
        result = self.runtime.execute(
            "drive_delete_file", {"path": "workspace/gone.txt"}, self.context
        )
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.data["deleted"])

    def test_path_traversal_rejected(self) -> None:
        result = self.runtime.execute(
            "drive_read_file", {"path": "../secret.txt"}, self.context
        )
        self.assertFalse(result.ok)

    def test_operations_are_audited_with_agent_operator(self) -> None:
        self.runtime.execute(
            "drive_save_file",
            {"path": "workspace/a.txt", "content": "x"},
            self.context,
        )
        self.runtime.execute(
            "drive_read_file", {"path": "workspace/missing.txt"}, self.context
        )
        rows = self.drive_audit.list_recent()
        self.assertEqual(len(rows), 2)
        statuses = {(row["action"], row["status"]) for row in rows}
        self.assertIn(("upload", "成功"), statuses)
        self.assertIn(("preview", "失败"), statuses)
        for row in rows:
            self.assertEqual(row["operator"], "agent:assistant")
            self.assertEqual(row["source"], "agent")
            self.assertEqual(row["tenant_id"], self.tenant.tenant_id)

    def test_without_drive_service_tools_fail_gracefully(self) -> None:
        self.runtime.drive_service = None
        result = self.runtime.execute("drive_list_files", {}, self.context)
        self.assertFalse(result.ok)
        self.assertIn("网盘服务", result.error)


if __name__ == "__main__":
    unittest.main()
