"""Integration tests for the /api/knowledge endpoints."""

from __future__ import annotations

import json
from unittest.mock import patch

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

    def test_embedding_config_can_be_read_and_saved_for_restart(self):
        path = self.data_root / "embeddings.json"
        path.write_text(
            json.dumps(
                {
                    "id": "bge_m3_local",
                    "enabled": False,
                    "base_url": "http://127.0.0.1:11434",
                    "model": "bge-m3",
                    "dimensions": 1024,
                    "timeout_seconds": 60,
                }
            ),
            encoding="utf-8",
        )
        with patch("src.api.routers.knowledge.EMBEDDINGS_FILE", path):
            response = self.client.get("/api/knowledge/embedding-config")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["runtime_enabled"])

            response = self.client.put(
                "/api/knowledge/embedding-config",
                json={
                    "id": "text_embedding",
                    "enabled": True,
                    "base_url": "https://embedding.example.com",
                    "model": "text-embedding-v1",
                    "dimensions": 1536,
                    "timeout_seconds": 45,
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["restart_required"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["id"], "text_embedding")
            self.assertEqual(saved["dimensions"], 1536)

            invalid = self.client.put(
                "/api/knowledge/embedding-config",
                json={
                    **saved,
                    "base_url": "http://embedding.example.com",
                },
            )
            self.assertEqual(invalid.status_code, 400, invalid.text)

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

    def test_category_crud_agent_binding_and_non_empty_conflict(self):
        created = self.client.post(
            "/api/knowledge/categories",
            json={
                "scope": "tenant",
                "tenant_id": self.tenant.tenant_id,
                "name": "产品知识",
                "description": "产品领域",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        category_id = created.json()["category_id"]
        agent_id = next(iter(self.config.agents))
        bound = self.client.put(
            "/api/agents/{}/knowledge-categories".format(agent_id),
            json={"category_ids": [category_id]},
        )
        self.assertEqual(bound.status_code, 200, bound.text)
        self.assertEqual(bound.json()["category_ids"], [category_id])

        added = self.client.post(
            "/api/knowledge/text",
            json={
                "tenant_id": self.tenant.tenant_id,
                "category_id": category_id,
                "name": "手册",
                "content": "产品知识内容",
            },
        )
        self.assertEqual(added.status_code, 200, added.text)
        conflict = self.client.delete(
            "/api/knowledge/categories/{}".format(category_id)
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)

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
