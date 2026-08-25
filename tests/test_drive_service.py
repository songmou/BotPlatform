"""Unit tests for the network drive service (path safety and file ops)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from src.core.services.drive import DriveService, MAX_UPLOAD_BYTES
from src.core.storage.tenants import TenantRegistry


class DriveServiceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_root = Path(temporary.name) / "data"
        self.registry = TenantRegistry(self.data_root)
        self.tenant = self.registry.resolve("ilink", "wxid_demo")
        self.service = DriveService(self.registry, self.data_root / "public")

    # ---- listing ----

    def test_tenant_root_maps_existing_directories(self):
        listing = self.service.list_entries("tenant", self.tenant.tenant_id)
        names = {entry["name"] for entry in listing["entries"]}
        self.assertIn("workspace", names)
        self.assertIn("scripts", names)

    def test_hidden_entries_are_not_listed(self):
        root = self.registry.tenant_root(self.tenant.tenant_id)
        (root / ".trash").mkdir(exist_ok=True)
        (root / ".secret").write_text("x", encoding="utf-8")
        listing = self.service.list_entries("tenant", self.tenant.tenant_id)
        names = {entry["name"] for entry in listing["entries"]}
        self.assertNotIn(".trash", names)
        self.assertNotIn(".secret", names)

    def test_list_returns_breadcrumbs_and_sorted_entries(self):
        self.service.create_folder("public", None, "", "docs")
        self.service.create_folder("public", None, "docs", "sub")
        self.service.save_file("public", None, "docs", "a.txt", b"hello")
        listing = self.service.list_entries("public", None, "docs")
        self.assertEqual(listing["path"], "docs")
        self.assertEqual(listing["breadcrumbs"], [{"name": "docs", "path": "docs"}])
        self.assertEqual(
            [(e["type"], e["name"]) for e in listing["entries"]],
            [("folder", "sub"), ("file", "a.txt")],
        )

    def test_list_missing_directory_raises(self):
        with self.assertRaises(ValueError):
            self.service.list_entries("public", None, "missing")

    # ---- scope handling ----

    def test_public_and_tenant_scopes_are_isolated(self):
        self.service.save_file("public", None, "", "shared.txt", b"public")
        self.service.save_file(
            "tenant", self.tenant.tenant_id, "workspace", "mine.txt", b"private"
        )
        public_names = {
            e["name"] for e in self.service.list_entries("public", None)["entries"]
        }
        tenant_names = {
            e["name"]
            for e in self.service.list_entries(
                "tenant", self.tenant.tenant_id, "workspace"
            )["entries"]
        }
        self.assertIn("shared.txt", public_names)
        self.assertNotIn("mine.txt", public_names)
        self.assertIn("mine.txt", tenant_names)

    def test_unknown_scope_rejected(self):
        with self.assertRaises(ValueError):
            self.service.list_entries("system", None)

    def test_tenant_scope_requires_valid_tenant(self):
        with self.assertRaises(ValueError):
            self.service.list_entries("tenant", None)
        with self.assertRaises(ValueError):
            self.service.list_entries(
                "tenant", "00000000-0000-0000-0000-000000000009"
            )

    # ---- path traversal / safety ----

    def test_parent_traversal_rejected(self):
        for bad in ("../x", "a/../../x", ".."):
            with self.assertRaises(ValueError, msg=bad):
                self.service.list_entries("public", None, bad)

    def test_absolute_path_rejected(self):
        with self.assertRaises(ValueError):
            self.service.read_file("public", None, "/etc/passwd")

    def test_hidden_path_segment_rejected(self):
        with self.assertRaises(ValueError):
            self.service.list_entries("tenant", self.tenant.tenant_id, ".trash")

    def test_null_byte_rejected(self):
        with self.assertRaises(ValueError):
            self.service.read_file("public", None, "a\x00b")

    @unittest.skipIf(os.name == "nt", "symlinks require privileges on Windows")
    def test_symlink_rejected(self):
        outside = self.data_root.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.service.public_root / "link.txt"
        link.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.service.read_file("public", None, "link.txt")
        listing = self.service.list_entries("public", None)
        self.assertNotIn("link.txt", {e["name"] for e in listing["entries"]})

    # ---- write operations ----

    def test_create_folder_and_duplicate_rejected(self):
        result = self.service.create_folder("public", None, "", "docs")
        self.assertEqual(result["path"], "docs")
        self.assertTrue(result["created"])
        with self.assertRaises(ValueError):
            self.service.create_folder("public", None, "", "docs")

    def test_create_folder_exist_ok_reuses_only_directories(self):
        created = self.service.create_folder(
            "public", None, "", "docs", exist_ok=True
        )
        reused = self.service.create_folder(
            "public", None, "", "docs", exist_ok=True
        )
        self.assertTrue(created["created"])
        self.assertFalse(reused["created"])
        self.assertEqual(reused["path"], "docs")

        self.service.save_file("public", None, "", "file.txt", b"data")
        with self.assertRaises(ValueError):
            self.service.create_folder(
                "public", None, "", "file.txt", exist_ok=True
            )

    def test_invalid_folder_names_rejected(self):
        for bad in ("", ".", "..", ".hidden", "a/b", "a\\b"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.service.create_folder("public", None, "", bad)

    def test_save_file_round_trip_and_overwrite_flag(self):
        self.service.save_file("public", None, "", "a.txt", b"v1")
        with self.assertRaises(ValueError):
            self.service.save_file("public", None, "", "a.txt", b"v2")
        self.service.save_file("public", None, "", "a.txt", b"v2", overwrite=True)
        real = self.service.read_file("public", None, "a.txt")
        self.assertEqual(real.read_bytes(), b"v2")

    def test_save_file_size_limit(self):
        with self.assertRaises(ValueError):
            self.service.save_file(
                "public", None, "", "big.bin", b"x" * (MAX_UPLOAD_BYTES + 1)
            )

    def test_rename(self):
        self.service.save_file("public", None, "", "a.txt", b"1")
        result = self.service.rename("public", None, "a.txt", "b.txt")
        self.assertEqual(result["path"], "b.txt")
        with self.assertRaises(ValueError):
            self.service.read_file("public", None, "a.txt")
        self.assertEqual(
            self.service.read_file("public", None, "b.txt").read_bytes(), b"1"
        )

    def test_rename_conflict_rejected(self):
        self.service.save_file("public", None, "", "a.txt", b"1")
        self.service.save_file("public", None, "", "b.txt", b"2")
        with self.assertRaises(ValueError):
            self.service.rename("public", None, "a.txt", "b.txt")

    def test_move_file_into_folder(self):
        self.service.create_folder("public", None, "", "docs")
        self.service.save_file("public", None, "", "a.txt", b"1")
        result = self.service.move("public", None, "a.txt", "docs")
        self.assertEqual(result["path"], "docs/a.txt")

    def test_move_folder_into_itself_rejected(self):
        self.service.create_folder("public", None, "", "docs")
        self.service.create_folder("public", None, "docs", "sub")
        with self.assertRaises(ValueError):
            self.service.move("public", None, "docs", "docs/sub")

    def test_move_conflict_rejected(self):
        self.service.create_folder("public", None, "", "docs")
        self.service.save_file("public", None, "", "a.txt", b"1")
        self.service.save_file("public", None, "docs", "a.txt", b"2")
        with self.assertRaises(ValueError):
            self.service.move("public", None, "a.txt", "docs")

    def test_delete_file_and_directory_rules(self):
        self.service.save_file("public", None, "", "a.txt", b"1")
        self.assertTrue(self.service.delete("public", None, "a.txt")["deleted"])
        self.service.create_folder("public", None, "", "docs")
        self.service.save_file("public", None, "docs", "b.txt", b"2")
        with self.assertRaises(ValueError):
            self.service.delete("public", None, "docs")
        self.service.delete("public", None, "docs", recursive=True)
        with self.assertRaises(ValueError):
            self.service.list_entries("public", None, "docs")

    def test_root_cannot_be_deleted_renamed_or_moved(self):
        with self.assertRaises(ValueError):
            self.service.delete("public", None, "")
        with self.assertRaises(ValueError):
            self.service.rename("public", None, "", "x")
        with self.assertRaises(ValueError):
            self.service.move("public", None, "", "docs")

    # ---- preview / stat / usage ----

    def test_read_text_preview(self):
        self.service.save_file("public", None, "", "a.txt", "你好".encode("utf-8"))
        preview = self.service.read_text("public", None, "a.txt")
        self.assertEqual(preview["content"], "你好")
        self.assertFalse(preview["truncated"])

    def test_read_text_rejects_binary(self):
        self.service.save_file("public", None, "", "bin.dat", b"\xff\xfe\x00\x01")
        with self.assertRaises(ValueError):
            self.service.read_text("public", None, "bin.dat")

    def test_stat_and_usage(self):
        self.service.save_file("public", None, "", "a.txt", b"12345")
        info = self.service.stat("public", None, "a.txt")
        self.assertEqual(info["type"], "file")
        self.assertEqual(info["size"], 5)
        usage = self.service.usage("public", None)
        self.assertEqual(usage["file_count"], 1)
        self.assertEqual(usage["total_bytes"], 5)


if __name__ == "__main__":
    unittest.main()
