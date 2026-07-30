"""Integration tests for the network drive web API."""

from __future__ import annotations

import unittest

from src.core.services.drive import DriveService
from src.core.storage.drive_audit import DriveAuditStore

from tests._web_api_base import WebApiTestBase


class DriveApiTest(WebApiTestBase):
    def setUp(self):
        super().setUp()
        self.tenant = self._make_tenant()

    def app_kwargs(self) -> dict:
        # setUp of the base class runs before our setUp body, so build the
        # drive services lazily from the registry created by the base class.
        self.drive_service = DriveService(self.registry, self.data_root / "public")
        self.drive_audit = DriveAuditStore(self.registry)
        return {
            "drive_service": self.drive_service,
            "drive_audit_store": self.drive_audit,
        }

    # ---- browsing ----

    def test_list_tenants(self):
        response = self.client.get("/api/drive/tenants")
        self.assertEqual(response.status_code, 200)
        tenant_ids = [item["tenant_id"] for item in response.json()]
        self.assertIn(self.tenant.tenant_id, tenant_ids)

    def test_list_public_entries(self):
        self.drive_service.create_folder("public", None, "", "docs")
        response = self.client.get(
            "/api/drive/entries", params={"scope": "public", "path": ""}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["path"], "")
        self.assertIn("docs", [entry["name"] for entry in body["entries"]])

    def test_list_tenant_entries_maps_existing_directories(self):
        response = self.client.get(
            "/api/drive/entries",
            params={"scope": "tenant", "tenant_id": self.tenant.tenant_id},
        )
        self.assertEqual(response.status_code, 200)
        names = {entry["name"] for entry in response.json()["entries"]}
        self.assertIn("workspace", names)
        self.assertIn("scripts", names)

    def test_unknown_tenant_returns_404(self):
        response = self.client.get(
            "/api/drive/entries",
            params={
                "scope": "tenant",
                "tenant_id": "00000000-0000-0000-0000-000000000009",
            },
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "租户不存在")

    def test_path_traversal_returns_400(self):
        response = self.client.get(
            "/api/drive/entries", params={"scope": "public", "path": "../etc"}
        )
        self.assertEqual(response.status_code, 400)

    def test_usage(self):
        self.drive_service.save_file("public", None, "", "a.txt", b"12345")
        response = self.client.get("/api/drive/usage", params={"scope": "public"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["file_count"], 1)
        self.assertEqual(response.json()["total_bytes"], 5)

    # ---- write operations ----

    def test_create_folder_and_duplicate_400(self):
        response = self.client.post(
            "/api/drive/folders",
            json={"scope": "public", "path": "", "name": "docs"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["path"], "docs")
        response = self.client.post(
            "/api/drive/folders",
            json={"scope": "public", "path": "", "name": "docs"},
        )
        self.assertEqual(response.status_code, 400)

    def test_upload_download_round_trip(self):
        payload = "你好，网盘".encode("utf-8")
        response = self.client.post(
            "/api/drive/upload",
            data={"scope": "public", "path": ""},
            files={"file": ("hello.txt", payload)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["path"], "hello.txt")
        download = self.client.get(
            "/api/drive/download",
            params={"scope": "public", "path": "hello.txt"},
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, payload)
        self.assertIn("hello.txt", download.headers.get("content-disposition", ""))

    def test_upload_duplicate_needs_overwrite(self):
        for expected, form in (
            (200, {"scope": "public", "path": ""}),
            (400, {"scope": "public", "path": ""}),
            (200, {"scope": "public", "path": "", "overwrite": "true"}),
        ):
            response = self.client.post(
                "/api/drive/upload",
                data=form,
                files={"file": ("a.txt", b"data")},
            )
            self.assertEqual(response.status_code, expected)

    def test_upload_empty_file_400(self):
        response = self.client.post(
            "/api/drive/upload",
            data={"scope": "public", "path": ""},
            files={"file": ("empty.txt", b"")},
        )
        self.assertEqual(response.status_code, 400)

    def test_preview(self):
        self.drive_service.save_file("public", None, "", "a.txt", "内容".encode())
        response = self.client.get(
            "/api/drive/preview", params={"scope": "public", "path": "a.txt"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "内容")

    def test_rename_and_move(self):
        self.drive_service.save_file("public", None, "", "a.txt", b"1")
        self.drive_service.create_folder("public", None, "", "docs")
        response = self.client.put(
            "/api/drive/entries",
            json={
                "scope": "public",
                "action": "rename",
                "path": "a.txt",
                "target": "b.txt",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["path"], "b.txt")
        response = self.client.put(
            "/api/drive/entries",
            json={
                "scope": "public",
                "action": "move",
                "path": "b.txt",
                "target": "docs",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["path"], "docs/b.txt")

    def test_invalid_action_400(self):
        response = self.client.put(
            "/api/drive/entries",
            json={
                "scope": "public",
                "action": "copy",
                "path": "a",
                "target": "b",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_delete_requires_recursive_for_non_empty(self):
        self.drive_service.create_folder("public", None, "", "docs")
        self.drive_service.save_file("public", None, "docs", "a.txt", b"1")
        response = self.client.delete(
            "/api/drive/entries", params={"scope": "public", "path": "docs"}
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.delete(
            "/api/drive/entries",
            params={"scope": "public", "path": "docs", "recursive": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["deleted"])

    # ---- permissions ----

    def test_viewer_forbidden(self):
        response = self.viewer_client.get(
            "/api/drive/entries", params={"scope": "public"}
        )
        self.assertEqual(response.status_code, 403)
        response = self.viewer_client.post(
            "/api/drive/folders",
            json={"scope": "public", "path": "", "name": "docs"},
        )
        self.assertEqual(response.status_code, 403)

    # ---- audit ----

    def test_operations_are_audited(self):
        self.client.post(
            "/api/drive/folders",
            json={"scope": "public", "path": "", "name": "docs"},
        )
        self.client.post(
            "/api/drive/upload",
            data={"scope": "public", "path": "docs"},
            files={"file": ("a.txt", b"data")},
        )
        self.client.get(
            "/api/drive/download",
            params={"scope": "public", "path": "docs/a.txt"},
        )
        # A failing operation is audited with 失败 status as well.
        self.client.delete(
            "/api/drive/entries", params={"scope": "public", "path": "missing.txt"}
        )
        response = self.client.get("/api/drive/audit")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 4)
        actions = {(item["action"], item["status"]) for item in body["items"]}
        self.assertIn(("mkdir", "成功"), actions)
        self.assertIn(("upload", "成功"), actions)
        self.assertIn(("download", "成功"), actions)
        self.assertIn(("delete", "失败"), actions)
        for item in body["items"]:
            self.assertEqual(item["operator"], "web:root")
            self.assertEqual(item["source"], "web")

    def test_audit_filters(self):
        self.client.post(
            "/api/drive/folders",
            json={"scope": "public", "path": "", "name": "docs"},
        )
        self.client.post(
            "/api/drive/upload",
            data={"scope": "public", "path": ""},
            files={"file": ("a.txt", b"data")},
        )
        response = self.client.get("/api/drive/audit", params={"action": "upload"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(response.json()["items"][0]["action"], "upload")


class DriveApiUnavailableTest(WebApiTestBase):
    """Without a drive service the endpoints answer 503."""

    def test_entries_503(self):
        response = self.client.get(
            "/api/drive/entries", params={"scope": "public"}
        )
        self.assertEqual(response.status_code, 503)

    def test_audit_503(self):
        response = self.client.get("/api/drive/audit")
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
