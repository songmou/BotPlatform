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

    def test_platform_drive_import_supports_benchmark_sized_batches(self):
        category = self.client.post(
            "/api/v2/platform/knowledge/categories", json={"name": "批量评测库"}
        ).json()
        paths = ["benchmark/doc-{}.md".format(index) for index in range(800)]
        imported = self.client.post(
            "/api/v2/platform/knowledge/from-drive",
            json={"category_id": category["category_id"], "paths": paths},
        )
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(len(imported.json()["items"]), 800)

        rejected = self.client.post(
            "/api/v2/platform/knowledge/from-drive",
            json={
                "category_id": category["category_id"],
                "paths": paths + ["benchmark/overflow-{}.md".format(index) for index in range(201)],
            },
        )
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertIn("1 到 1000", rejected.json()["detail"])

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

    def _invite_member(self, owner: TestClient, organization_id: str, suffix: str):
        invitation = owner.post(
            f"/api/v2/orgs/{organization_id}/invitations", json={"role": "member"}
        )
        self.assertEqual(invitation.status_code, 201, invitation.text)
        member = TestClient(self.app)
        accepted = member.post(
            "/api/v2/invitations/accept",
            json={
                "token": invitation.json()["invitation_token"],
                "username": "cm_" + suffix,
                "password": "password-" + suffix,
            },
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        login = member.post(
            "/api/auth/login",
            json={"username": "cm_" + suffix, "password": "password-" + suffix},
        )
        self.assertEqual(login.status_code, 200, login.text)
        return member

    def test_organization_drive_and_sources_are_creator_managed(self):
        organization_id, owner = self._create_owner("creator")
        creator = self._invite_member(owner, organization_id, "creator")
        other = self._invite_member(owner, organization_id, "other")

        uploaded = creator.post(
            f"/api/v2/orgs/{organization_id}/drive/upload",
            files={"file": ("note.txt", b"creator file", "text/plain")},
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        denied_delete = other.delete(
            f"/api/v2/orgs/{organization_id}/drive/entries",
            params={"path": "note.txt"},
        )
        self.assertEqual(denied_delete.status_code, 403, denied_delete.text)
        deleted = creator.delete(
            f"/api/v2/orgs/{organization_id}/drive/entries",
            params={"path": "note.txt"},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)

        category = creator.post(
            f"/api/v2/orgs/{organization_id}/knowledge/categories",
            json={"name": "创建者库"},
        )
        self.assertEqual(category.status_code, 201, category.text)
        category_id = category.json()["category_id"]

        legacy = creator.post(
            f"/api/v2/orgs/{organization_id}/drive/upload",
            files={"file": ("legacy.txt", b"legacy file", "text/plain")},
        )
        self.assertEqual(legacy.status_code, 200, legacy.text)
        with self.app.state.organization_store.database.transaction(
            immediate=True
        ) as connection:
            connection.execute(
                "DELETE FROM organization_content_ownership "
                "WHERE organization_id=? AND resource_type='drive_entry' "
                "AND resource_key='legacy.txt'",
                (organization_id,),
            )
        denied_legacy = other.delete(
            f"/api/v2/orgs/{organization_id}/drive/entries",
            params={"path": "legacy.txt"},
        )
        self.assertEqual(denied_legacy.status_code, 403, denied_legacy.text)
        owner_legacy = owner.delete(
            f"/api/v2/orgs/{organization_id}/drive/entries",
            params={"path": "legacy.txt"},
        )
        self.assertEqual(owner_legacy.status_code, 200, owner_legacy.text)

        added = creator.post(
            f"/api/v2/orgs/{organization_id}/knowledge/text",
            json={"name": "创建者条目", "content": "内容"},
        )
        self.assertEqual(added.status_code, 200, added.text)
        source_id = added.json()["source_id"]
        denied_move = other.patch(
            f"/api/v2/orgs/{organization_id}/knowledge/sources/move",
            json={"source_ids": [source_id], "target_category_id": category_id},
        )
        self.assertEqual(denied_move.status_code, 403, denied_move.text)
        denied_refresh = other.post(
            f"/api/v2/orgs/{organization_id}/knowledge/refresh",
            json={"source_ids": [source_id]},
        )
        self.assertEqual(denied_refresh.status_code, 403, denied_refresh.text)
        owner_deleted = owner.delete(
            f"/api/v2/orgs/{organization_id}/knowledge/sources/{source_id}"
        )
        self.assertEqual(owner_deleted.status_code, 200, owner_deleted.text)

    def test_v1_knowledge_and_drive_are_public_only_for_non_admins(self):
        import json as _json

        with self.admin_roles.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO admin_roles(code, name, permissions, builtin) "
                "VALUES (?, ?, ?, 0)",
                (
                    "content_curator",
                    "内容编辑",
                    _json.dumps(
                        [
                            "panel.read",
                            "knowledge.read",
                            "knowledge.manage",
                            "drive.read",
                            "drive.manage",
                        ]
                    ),
                ),
            )
        role = self.admin_roles.get_by_code("content_curator")
        self.admin_users.create("curator", "password12345", role.role_id)
        curator = self._login("curator")

        organization_id, owner = self._create_owner("v1guard")
        org_source = owner.post(
            f"/api/v2/orgs/{organization_id}/knowledge/text",
            json={"name": "组织条目", "content": "组织内容"},
        )
        self.assertEqual(org_source.status_code, 200, org_source.text)
        org_source_id = org_source.json()["source_id"]

        public_category = self.client.post(
            "/api/v2/platform/knowledge/categories", json={"name": "公共库守卫"}
        ).json()
        public_source = self.client.post(
            "/api/v2/platform/knowledge/text",
            json={
                "category_id": public_category["category_id"],
                "name": "公共条目",
                "content": "所有人可见",
            },
        ).json()["source_id"]

        denied_list = curator.get(
            "/api/knowledge", params={"tenant_id": organization_id}
        )
        self.assertEqual(denied_list.status_code, 403, denied_list.text)
        denied_search = curator.get(
            "/api/knowledge/search",
            params={"tenant_id": organization_id, "q": "组织"},
        )
        self.assertEqual(denied_search.status_code, 403, denied_search.text)
        denied_tenants = curator.get("/api/knowledge/tenants")
        self.assertEqual(denied_tenants.status_code, 403, denied_tenants.text)
        denied_delete = curator.delete(f"/api/knowledge/{org_source_id}")
        self.assertEqual(denied_delete.status_code, 403, denied_delete.text)
        denied_drive = curator.get(
            "/api/drive/entries",
            params={"scope": "tenant", "tenant_id": organization_id, "path": ""},
        )
        self.assertEqual(denied_drive.status_code, 403, denied_drive.text)

        categories = curator.get("/api/knowledge/categories")
        self.assertEqual(categories.status_code, 200, categories.text)
        listed = {item["category_id"] for item in categories.json()["categories"]}
        self.assertIn(public_category["category_id"], listed)
        preview = curator.get(f"/api/knowledge/source-preview/{public_source}")
        self.assertEqual(preview.status_code, 200, preview.text)
        public_drive = curator.get(
            "/api/drive/entries", params={"scope": "public", "path": ""}
        )
        self.assertEqual(public_drive.status_code, 200, public_drive.text)

    def test_cross_organization_knowledge_access_is_denied(self):
        org_a, owner_a = self._create_owner("cross-a")
        org_b, owner_b = self._create_owner("cross-b")
        cat_b = owner_b.post(
            f"/api/v2/orgs/{org_b}/knowledge/categories", json={"name": "B 组织库"}
        )
        self.assertEqual(cat_b.status_code, 201, cat_b.text)
        cat_b_id = cat_b.json()["category_id"]
        src_b = owner_b.post(
            f"/api/v2/orgs/{org_b}/knowledge/text",
            json={"name": "B 条目", "content": "机密内容"},
        )
        self.assertEqual(src_b.status_code, 200, src_b.text)
        src_b_id = src_b.json()["source_id"]

        denied_sources = owner_a.get(
            f"/api/v2/orgs/{org_a}/knowledge/sources",
            params={"category_id": cat_b_id},
        )
        self.assertEqual(denied_sources.status_code, 404, denied_sources.text)
        search = owner_a.get(
            f"/api/v2/orgs/{org_a}/knowledge/search",
            params={"q": "机密内容", "category_ids": cat_b_id},
        )
        self.assertEqual(search.status_code, 200, search.text)
        self.assertEqual(search.json()["results"], [])
        denied_preview = owner_a.get(
            f"/api/v2/orgs/{org_a}/knowledge/sources/{src_b_id}"
        )
        self.assertEqual(denied_preview.status_code, 404, denied_preview.text)
        denied_delete = owner_a.delete(
            f"/api/v2/orgs/{org_a}/knowledge/sources/{src_b_id}"
        )
        self.assertEqual(denied_delete.status_code, 404, denied_delete.text)

        binding = owner_a.put(
            f"/api/v2/orgs/{org_a}/agents/general/knowledge-categories",
            json={"category_ids": [cat_b_id]},
        )
        self.assertEqual(binding.status_code, 400, binding.text)
        unknown_agent = owner_a.put(
            f"/api/v2/orgs/{org_a}/agents/no_such_agent/knowledge-categories",
            json={"category_ids": []},
        )
        self.assertEqual(unknown_agent.status_code, 404, unknown_agent.text)

        denied_b_categories = owner_a.get(
            f"/api/v2/orgs/{org_b}/knowledge/categories"
        )
        self.assertEqual(denied_b_categories.status_code, 403, denied_b_categories.text)
        denied_b_drive = owner_a.get(
            f"/api/v2/orgs/{org_b}/drive/entries", params={"path": ""}
        )
        self.assertEqual(denied_b_drive.status_code, 403, denied_b_drive.text)
