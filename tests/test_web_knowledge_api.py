"""Integration tests for the /api/knowledge endpoints."""

from __future__ import annotations

from tests._web_api_base import WebApiTestBase


class KnowledgeApiUnavailableTest(WebApiTestBase):
    """Without a knowledge service the endpoints must answer 503."""

    def test_endpoints_return_503(self):
        tenant = self._make_tenant()
        self.assertEqual(
            self.client.get(
                "/api/knowledge", params={"tenant_id": tenant.tenant_id}
            ).status_code,
            503,
        )
        self.assertEqual(
            self.client.post(
                "/api/knowledge/text",
                json={"tenant_id": tenant.tenant_id, "name": "n", "content": "c"},
            ).status_code,
            503,
        )


class KnowledgeApiTest(WebApiTestBase):
    def setUp(self):
        super().setUp()
        # The real service works against the shared registry; no embedding.
        from src.core.services.knowledge import KnowledgeService

        self.service = KnowledgeService(self.registry, None)
        self.app.state.knowledge_service = self.service
        self.tenant = self._make_tenant()

    def _add_text(self, name="入门指南", content="# 指南\n\n这是一个测试知识条目，包含入门说明。"):
        return self.client.post(
            "/api/knowledge/text",
            json={"tenant_id": self.tenant.tenant_id, "name": name, "content": content},
        )

    # ---- tenants / listing ----

    def test_list_tenants(self):
        response = self.client.get("/api/knowledge/tenants")
        self.assertEqual(response.status_code, 200, response.text)
        tenant_ids = {item["tenant_id"] for item in response.json()}
        self.assertIn(self.tenant.tenant_id, tenant_ids)

    def test_list_sources_empty(self):
        response = self.client.get(
            "/api/knowledge", params={"tenant_id": self.tenant.tenant_id}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"sources": []})

    def test_categories_report_embedding_disabled(self):
        response = self.client.get("/api/knowledge/categories")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertFalse(data["embedding_enabled"])
        # The seeded public default category is renamed by schema V21.
        names = {item["name"] for item in data["categories"]}
        self.assertIn("默认知识库", names)

    def test_embedding_config_is_read_only_status(self):
        response = self.client.get("/api/knowledge/embedding-config")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        # The base test config binds no embedding model.
        self.assertFalse(data["bound"])
        self.assertFalse(data["runtime_enabled"])
        # The write endpoint has been removed; models are managed on the models page.
        self.assertEqual(
            self.client.put(
                "/api/knowledge/embedding-config", json={}
            ).status_code,
            405,
        )

    def test_unknown_tenant_404(self):
        response = self.client.get(
            "/api/knowledge", params={"tenant_id": "00000000-0000-0000-0000-000000000009"}
        )
        self.assertEqual(response.status_code, 404)

    # ---- add text / search / delete ----

    def test_add_text_and_search(self):
        response = self._add_text()
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertGreater(data["chunks"], 0)
        # No embedding client -> pending status.
        self.assertEqual(data["status"], "pending_embedding")

        listed = self.client.get(
            "/api/knowledge", params={"tenant_id": self.tenant.tenant_id}
        ).json()["sources"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "入门指南")

        response = self.client.get(
            "/api/knowledge/search",
            params={"tenant_id": self.tenant.tenant_id, "q": "入门说明"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        results = response.json()["results"]
        self.assertGreater(len(results), 0)
        self.assertIn("入门说明", results[0]["content"])

    def test_add_text_empty_content_400(self):
        response = self._add_text(content="   ")
        self.assertEqual(response.status_code, 400)

    def test_delete_source(self):
        source_id = self._add_text().json()["source_id"]
        response = self.client.delete(
            "/api/knowledge/{}".format(source_id),
            params={"tenant_id": self.tenant.tenant_id},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"deleted": True})

    def test_delete_unknown_source_404(self):
        response = self.client.delete(
            "/api/knowledge/no-such-source",
            params={"tenant_id": self.tenant.tenant_id},
        )
        self.assertEqual(response.status_code, 404)

    # ---- upload ----

    def _upload(self, filename, payload=b"hello knowledge"):
        return self.client.post(
            "/api/knowledge/upload",
            data={"tenant_id": self.tenant.tenant_id},
            files={"file": (filename, payload, "application/octet-stream")},
        )

    def test_upload_txt_file(self):
        response = self._upload("notes.txt", "上传的文本知识内容。".encode("utf-8"))
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["status"], "pending_embedding")
        # File persisted inside the tenant workspace.
        saved = (
            self.registry.tenant_root(self.tenant.tenant_id)
            / "workspace"
            / "knowledge_uploads"
            / "notes.txt"
        )
        self.assertTrue(saved.exists())

    def test_upload_unsupported_suffix_400(self):
        response = self._upload("malware.exe")
        self.assertEqual(response.status_code, 400)

    def test_upload_empty_file_400(self):
        response = self._upload("empty.txt", b"")
        self.assertEqual(response.status_code, 400)

    # ---- reindex ----

    def test_reindex_without_embedding_400(self):
        response = self.client.post(
            "/api/knowledge/reindex", json={"tenant_id": self.tenant.tenant_id}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("embedding", response.json()["detail"])

    # ---- permissions ----

    def test_viewer_cannot_read_or_manage(self):
        self.assertEqual(
            self.viewer_client.get(
                "/api/knowledge", params={"tenant_id": self.tenant.tenant_id}
            ).status_code,
            403,
        )


    def test_import_public_drive_file_and_query_links(self):
        category = self.client.post(
            "/api/knowledge/categories",
            json={"scope": "public", "name": "公共资料", "description": ""},
        ).json()
        public_file = self.service.public_root / "notice.md"
        public_file.write_text("公共通知内容", encoding="utf-8")
        imported = self.client.post(
            "/api/knowledge/from-drive",
            json={
                "category_id": category["category_id"],
                "scope": "public",
                "paths": ["notice.md"],
            },
        )
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertTrue(imported.json()["items"][0]["ok"])
        source_id = imported.json()["items"][0]["source_id"]
        preview = self.client.get(
            "/api/knowledge/source-preview/{}".format(source_id)
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["content"], "公共通知内容")
        links = self.client.get(
            "/api/knowledge/drive-links",
            params={"scope": "public", "path": "notice.md"},
        )
        self.assertEqual(links.status_code, 200, links.text)
        self.assertEqual(links.json()["links"][0]["category_name"], "公共资料")
        self.assertEqual(
            self.viewer_client.post(
                "/api/knowledge/text",
                json={"tenant_id": self.tenant.tenant_id, "name": "n", "content": "c"},
            ).status_code,
            403,
        )

    def test_drive_import_rejects_more_than_one_thousand_files(self):
        category = self.client.post(
            "/api/knowledge/categories",
            json={"scope": "public", "name": "批量限制", "description": ""},
        ).json()
        response = self.client.post(
            "/api/knowledge/from-drive",
            json={
                "category_id": category["category_id"],
                "scope": "public",
                "paths": ["doc-{}.md".format(index) for index in range(1001)],
            },
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("1 到 1000", response.json()["detail"])
