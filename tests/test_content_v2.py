"""Full public and organization content-management API regression tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.core.services.drive import DriveService
from src.core.services.knowledge import KnowledgeService
from src.core.storage.drive_audit import DriveAuditStore
from tests._web_api_base import WebApiTestBase


class ContentV2ApiTest(WebApiTestBase):
    def app_kwargs(self) -> dict:
        self.drive_service = DriveService(self.registry, self.data_root / "public")
        self.drive_audit = DriveAuditStore(self.registry)
        self.knowledge_service = KnowledgeService(self.registry, None, None)
        return {
            "drive_service": self.drive_service,
            "drive_audit_store": self.drive_audit,
            "knowledge_service": self.knowledge_service,
        }

    def _create_owner(self, suffix: str):
        created = self.client.post(
            "/api/v2/platform/organizations", json={"name": "内容组织 " + suffix}
        )
        self.assertEqual(created.status_code, 201, created.text)
        payload = created.json()
        owner = TestClient(self.app)
        accepted = owner.post(
            "/api/v2/invitations/accept",
            json={
                "token": payload["owner_invitation_token"],
                "username": "content_" + suffix,
                "password": "password-" + suffix,
            },
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        login = owner.post(
            "/api/auth/login",
            json={
                "username": "content_" + suffix,
                "password": "password-" + suffix,
            },
        )
        self.assertEqual(login.status_code, 200, login.text)
        return payload["organization"]["organization_id"], owner

    def test_platform_public_knowledge_and_drive_full_flow(self):
        self.assertEqual(
            self.viewer_client.get("/api/v2/platform/knowledge/categories").status_code,
            403,
        )
        self.assertEqual(
            self.viewer_client.get("/api/v2/platform/drive/entries").status_code,
            403,
        )
        category = self.client.post(
            "/api/v2/platform/knowledge/categories",
            json={"name": "公共手册", "description": "平台公共知识"},
        )
        self.assertEqual(category.status_code, 201, category.text)
        category_id = category.json()["category_id"]

        text = self.client.post(
            "/api/v2/platform/knowledge/text",
            json={
                "category_id": category_id,
                "name": "服务规则",
                "content": "公共服务规则内容",
            },
        )
        self.assertEqual(text.status_code, 200, text.text)
        searched = self.client.get(
            "/api/v2/platform/knowledge/search",
            params={"q": "公共服务规则", "category_ids": category_id},
        )
        self.assertEqual(searched.status_code, 200, searched.text)
        self.assertTrue(searched.json()["results"])

        uploaded = self.client.post(
            "/api/v2/platform/drive/upload",
            data={"path": ""},
            files={"file": ("guide.txt", b"public drive guide", "text/plain")},
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        imported = self.client.post(
            "/api/v2/platform/knowledge/from-drive",
            json={"category_id": category_id, "paths": ["guide.txt"]},
        )
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertTrue(imported.json()["items"][0]["ok"])

        entries = self.client.get("/api/v2/platform/drive/entries")
        self.assertEqual(entries.status_code, 200, entries.text)
        self.assertIn("guide.txt", [item["name"] for item in entries.json()["entries"]])
        preview = self.client.get(
            "/api/v2/platform/drive/preview", params={"path": "guide.txt"}
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        audit = self.client.get("/api/v2/platform/drive/audit")
        self.assertEqual(audit.status_code, 200, audit.text)
        self.assertGreaterEqual(audit.json()["total"], 1)

    def test_organization_full_flow_and_public_content_is_read_only(self):
        organization_id, owner = self._create_owner("primary")
        other_id, _other = self._create_owner("other")

        category = owner.post(
            f"/api/v2/orgs/{organization_id}/knowledge/categories",
            json={"name": "组织手册"},
        )
        self.assertEqual(category.status_code, 201, category.text)
        category_id = category.json()["category_id"]
        uploaded = owner.post(
            f"/api/v2/orgs/{organization_id}/drive/upload",
            files={"file": ("team.md", b"organization handbook", "text/markdown")},
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        imported = owner.post(
            f"/api/v2/orgs/{organization_id}/knowledge/from-drive",
            json={"category_id": category_id, "paths": ["team.md"]},
        )
        self.assertEqual(imported.status_code, 200, imported.text)
        source_id = imported.json()["items"][0]["source_id"]
        searched = owner.get(
            f"/api/v2/orgs/{organization_id}/knowledge/search",
            params={"q": "organization handbook", "category_ids": category_id},
        )
        self.assertEqual(searched.status_code, 200, searched.text)
        self.assertTrue(searched.json()["results"])
        preview = owner.get(
            f"/api/v2/orgs/{organization_id}/knowledge/sources/{source_id}"
        )
        self.assertEqual(preview.status_code, 200, preview.text)

        public_category = self.client.post(
            "/api/v2/platform/knowledge/categories", json={"name": "只读公共库"}
        ).json()
        public_category_id = public_category["category_id"]
        public_source = self.client.post(
            "/api/v2/platform/knowledge/text",
            json={
                "category_id": public_category_id,
                "name": "公共条目",
                "content": "所有组织可见",
            },
        ).json()["source_id"]
        self.client.post(
            "/api/v2/platform/drive/upload",
            files={"file": ("public.txt", b"readonly", "text/plain")},
        )
        categories = owner.get(
            f"/api/v2/orgs/{organization_id}/knowledge/categories"
        ).json()["items"]
        self.assertIn(public_category_id, [item["category_id"] for item in categories])
        public_files = owner.get(
            f"/api/v2/orgs/{organization_id}/drive/entries",
            params={"scope": "public"},
        )
        self.assertEqual(public_files.status_code, 200, public_files.text)
        self.assertIn("public.txt", [item["name"] for item in public_files.json()["entries"]])

        denied_category = owner.put(
            f"/api/v2/orgs/{organization_id}/knowledge/categories/{public_category_id}",
            json={"name": "不允许"},
        )
        self.assertEqual(denied_category.status_code, 403, denied_category.text)
        denied_refresh = owner.post(
            f"/api/v2/orgs/{organization_id}/knowledge/refresh",
            json={"source_ids": [public_source]},
        )
        self.assertEqual(denied_refresh.status_code, 403, denied_refresh.text)
        denied_file = owner.post(
            f"/api/v2/orgs/{organization_id}/drive/folders",
            json={"scope": "public", "path": "", "name": "forbidden"},
        )
        self.assertEqual(denied_file.status_code, 403, denied_file.text)
        denied_upload = owner.post(
            f"/api/v2/orgs/{organization_id}/drive/upload",
            data={"scope": "public"},
            files={"file": ("forbidden.txt", b"no", "text/plain")},
        )
        self.assertEqual(denied_upload.status_code, 403, denied_upload.text)
        cross_organization = owner.put(
            f"/api/v2/orgs/{other_id}/knowledge/categories/{category_id}",
            json={"name": "越权"},
        )
        self.assertEqual(cross_organization.status_code, 403, cross_organization.text)

        audit = owner.get(f"/api/v2/orgs/{organization_id}/drive/audit")
        self.assertEqual(audit.status_code, 200, audit.text)
        self.assertGreaterEqual(audit.json()["total"], 1)
