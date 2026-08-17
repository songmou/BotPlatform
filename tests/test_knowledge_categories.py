"""Tests for scoped knowledge libraries and drive lifecycle integration."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.core.services.drive import DriveService
from src.core.services.knowledge import KnowledgeService
from src.core.storage.tenants import TenantRegistry
from src.core.modeling.contracts import EmbeddingError


class KnowledgeCategoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = TenantRegistry(self.root)
        self.tenant_a = self.registry.resolve("ilink", "user-a")
        self.tenant_b = self.registry.resolve("ilink", "user-b")
        self.service = KnowledgeService(self.registry, None)
        self.drive = DriveService(
            self.registry, self.root / "public", knowledge_service=self.service
        )

    def test_public_private_visibility_and_strict_agent_bindings(self) -> None:
        public = self.service.create_category("public", "公共制度")
        private_a = self.service.create_category(
            "tenant", "业务甲", tenant_id=self.tenant_a.tenant_id
        )
        private_b = self.service.create_category(
            "tenant", "业务乙", tenant_id=self.tenant_b.tenant_id
        )
        self.service.add_text_to_category(
            public["category_id"], "公共说明", "统一关键字 公共内容"
        )
        self.service.add_text_to_category(
            private_a["category_id"], "甲说明", "统一关键字 甲租户内容"
        )
        self.service.add_text_to_category(
            private_b["category_id"], "乙说明", "统一关键字 乙租户内容"
        )
        self.service.set_agent_bindings(
            "agent-a", [public["category_id"], private_a["category_id"], private_b["category_id"]]
        )

        hits_a = self.service.search(
            self.tenant_a.tenant_id, "统一关键字", agent_id="agent-a", limit=10
        )
        self.assertEqual(
            {item["source_name"] for item in hits_a}, {"公共说明", "甲说明"}
        )
        hits_b = self.service.search(
            self.tenant_b.tenant_id, "统一关键字", agent_id="agent-a", limit=10
        )
        self.assertEqual({item["source_name"] for item in hits_b}, {"公共说明", "乙说明"})
        self.assertEqual(
            self.service.search(
                self.tenant_a.tenant_id, "统一关键字", agent_id="unbound"
            ),
            [],
        )

    def test_drive_overwrite_move_delete_and_manual_refresh(self) -> None:
        category = self.service.create_category(
            "tenant", "文件知识", tenant_id=self.tenant_a.tenant_id
        )
        self.drive.save_file(
            "tenant",
            self.tenant_a.tenant_id,
            "workspace",
            "manual.md",
            b"original searchable text",
        )
        indexed = self.service.index_drive_file(
            category["category_id"],
            "tenant",
            self.tenant_a.tenant_id,
            "workspace/manual.md",
        )
        self.assertEqual(indexed["status"], "pending_embedding")

        self.drive.save_file(
            "tenant",
            self.tenant_a.tenant_id,
            "workspace",
            "manual.md",
            b"updated searchable text",
            overwrite=True,
        )
        source = self.service.list_category(category["category_id"])[0]
        self.assertEqual(source["status"], "stale_modified")
        self.assertEqual(
            self.service.search(
                self.tenant_a.tenant_id,
                "original searchable",
                category_ids=[category["category_id"]],
            ),
            [],
        )

        refreshed = self.service.refresh([indexed["source_id"]])
        self.assertTrue(refreshed[0]["ok"])
        self.assertTrue(
            self.service.search(
                self.tenant_a.tenant_id,
                "updated searchable",
                category_ids=[category["category_id"]],
            )
        )

        self.drive.rename(
            "tenant", self.tenant_a.tenant_id, "workspace/manual.md", "renamed.md"
        )
        source = self.service.list_category(category["category_id"])[0]
        self.assertEqual(source["drive_path"], "workspace/renamed.md")
        self.assertIn(source["status"], {"ready", "pending_embedding"})

        self.drive.delete(
            "tenant", self.tenant_a.tenant_id, "workspace/renamed.md"
        )
        source = self.service.list_category(category["category_id"])[0]
        self.assertEqual(source["status"], "source_missing")

    def test_external_file_change_is_detected_on_listing(self) -> None:
        category = self.service.create_category(
            "tenant", "外部变更", tenant_id=self.tenant_a.tenant_id
        )
        path = (
            self.registry.tenant_root(self.tenant_a.tenant_id)
            / "workspace"
            / "external.md"
        )
        path.write_text("first version", encoding="utf-8")
        self.service.index_drive_file(
            category["category_id"],
            "tenant",
            self.tenant_a.tenant_id,
            "workspace/external.md",
        )
        path.write_text("second version with different size", encoding="utf-8")
        source = self.service.list_category(category["category_id"])[0]
        self.assertEqual(source["status"], "stale_modified")

    def test_non_empty_category_requires_moving_sources_first(self) -> None:
        first = self.service.create_category(
            "tenant", "待删除", tenant_id=self.tenant_a.tenant_id
        )
        second = self.service.create_category(
            "tenant", "目标库", tenant_id=self.tenant_a.tenant_id
        )
        source = self.service.add_text_to_category(
            first["category_id"], "资料", "可移动资料"
        )
        with self.assertRaisesRegex(ValueError, "仍包含"):
            self.service.delete_category(first["category_id"])
        self.assertEqual(
            self.service.move_sources([source["source_id"]], second["category_id"]),
            1,
        )
        self.assertTrue(self.service.delete_category(first["category_id"]))

    def test_citation_metadata_and_appendix_are_server_generated(self) -> None:
        category = self.service.create_category(
            "tenant", "引用库", tenant_id=self.tenant_a.tenant_id
        )
        self.service.add_text_to_category(
            category["category_id"], "制度文本", "引用关键字"
        )
        hits = self.service.search(
            self.tenant_a.tenant_id,
            "引用关键字",
            category_ids=[category["category_id"]],
        )
        self.assertEqual(hits[0]["citation"], 1)
        answer = self.service.append_citations("结论 [1]", hits)
        self.assertIn("参考来源：", answer)
        self.assertIn("[1] 引用库 / 制度文本", answer)

if __name__ == "__main__":
    unittest.main()


class FakeEmbedding:
    """Minimal embedding client for tests with a configurable fingerprint."""

    def __init__(
        self, profile_id: str = "emb1", model: str = "fake-model",
        dimensions: int = 8, fail: bool = False,
    ) -> None:
        self._model_id = profile_id
        self.model = model
        self._dimensions = dimensions
        self.fail = fail
        self.calls = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def fingerprint(self) -> str:
        return "{}@{}@{}".format(self._model_id, self.model, self._dimensions)

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise EmbeddingError("boom")
        return [
            [float((i + 1) % self._dimensions) for i in range(self._dimensions)]
            for _ in texts
        ]

    def close(self) -> None:
        pass


class KnowledgeVectorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = TenantRegistry(self.root)
        self.embedding = FakeEmbedding(dimensions=8)
        self.service = KnowledgeService(self.registry, self.embedding)

    def _embedding_rows(self, source_id):
        with self.registry.database.read() as conn:
            rows = conn.execute(
                "SELECT e.dimensions, e.model_fingerprint FROM knowledge_embeddings e "
                "JOIN knowledge_chunks c ON c.chunk_id=e.chunk_id "
                "WHERE c.source_id=?", (source_id,)
            ).fetchall()
        return [(int(r["dimensions"]), r["model_fingerprint"]) for r in rows]

    def _source_status(self, source_id):
        with self.registry.database.read() as conn:
            return conn.execute(
                "SELECT status, last_error FROM knowledge_sources WHERE source_id=?",
                (source_id,),
            ).fetchone()

    def test_add_text_stores_fingerprint_and_ready(self) -> None:
        public = self.service.create_category("public", "公共")
        result = self.service.add_text_to_category(
            public["category_id"], "doc", "一段知识内容，用于验证向量指纹。"
        )
        self.assertEqual(result["status"], "ready")
        rows = self._embedding_rows(result["source_id"])
        self.assertTrue(rows)
        for dim, fp in rows:
            self.assertEqual(dim, 8)
            self.assertEqual(fp, self.service.embedding_fingerprint)

    def test_embedding_text_includes_heading_and_versioned_fingerprint(self) -> None:
        public = self.service.create_category("public", "标题测试")
        self.service.add_text_to_category(
            public["category_id"], "doc", "# 稀有标题\n\n正文没有标题关键词。"
        )
        embedded = "\n".join(text for call in self.embedding.calls for text in call)
        self.assertIn("标题：稀有标题", embedded)
        self.assertIn("正文：正文没有标题关键词。", embedded)
        self.assertTrue(self.service.embedding_fingerprint.endswith("heading-body-v2"))

    def test_reembed_sources_forces_revectorize(self) -> None:
        public = self.service.create_category("public", "公共")
        result = self.service.add_text_to_category(
            public["category_id"], "doc", "内容一。内容二。内容三。内容四段。"
        )
        source_id = result["source_id"]
        # Switch to a different model (different dimensions -> different fingerprint).
        service2 = KnowledgeService(self.registry, FakeEmbedding(dimensions=4))
        out = service2.reembed_sources(None, [source_id])
        self.assertEqual(out["completed"], 1)
        self.assertEqual(out["chunks"], result["chunks"])
        for dim, fp in self._embedding_rows(source_id):
            self.assertEqual(dim, 4)
            self.assertEqual(fp, service2.embedding_fingerprint)
        health = service2.embedding_health(None)
        self.assertEqual(health["stale"], 0)
        self.assertEqual(health["total"], result["chunks"])

    def test_reindex_force_overwrites_stale_vectors(self) -> None:
        public = self.service.create_category("public", "公共")
        result = self.service.add_text_to_category(
            public["category_id"], "doc", "内容一。内容二。内容三。内容四段。"
        )
        service2 = KnowledgeService(self.registry, FakeEmbedding(dimensions=4))
        out = service2.reindex(None, [public["category_id"]], force=True)
        self.assertEqual(out["completed"], result["chunks"])
        for dim, fp in self._embedding_rows(result["source_id"]):
            self.assertEqual(dim, 4)
            self.assertEqual(fp, service2.embedding_fingerprint)

    def test_reindex_marks_fully_embedded_source_ready(self) -> None:
        public = self.service.create_category("public", "重建状态")
        result = self.service.add_text_to_category(
            public["category_id"], "doc", "需要完成向量重建的正文。"
        )
        source_id = result["source_id"]
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM knowledge_embeddings WHERE chunk_id IN "
                "(SELECT chunk_id FROM knowledge_chunks WHERE source_id=?)",
                (source_id,),
            )
            connection.execute(
                "UPDATE knowledge_sources SET status='pending_embedding' WHERE source_id=?",
                (source_id,),
            )

        rebuilt = self.service.reindex(None, [public["category_id"]], force=True)

        self.assertGreater(rebuilt["completed"], 0)
        self.assertEqual(self._source_status(source_id)["status"], "ready")

    def test_embedding_health_reports_stale_after_model_change(self) -> None:
        public = self.service.create_category("public", "公共")
        result = self.service.add_text_to_category(
            public["category_id"], "doc", "内容一。内容二。内容三。内容四段。"
        )
        service2 = KnowledgeService(self.registry, FakeEmbedding(dimensions=4))
        health = service2.embedding_health(None)
        self.assertEqual(health["total"], result["chunks"])
        self.assertEqual(health["stale"], result["chunks"])
        self.assertNotEqual(health["current_fingerprint"], self.service.embedding_fingerprint)

    def test_reembed_without_embedding_raises(self) -> None:
        service_noemb = KnowledgeService(self.registry, None)
        public = self.service.create_category("public", "公共")
        result = self.service.add_text_to_category(public["category_id"], "doc", "内容。")
        with self.assertRaises(ValueError):
            service_noemb.reembed_sources(None, [result["source_id"]])

    def test_failed_embedding_writes_last_error_and_pending(self) -> None:
        failing = KnowledgeService(self.registry, FakeEmbedding(fail=True))
        public = failing.create_category("public", "公共")
        result = failing.add_text_to_category(public["category_id"], "doc", "内容。")
        self.assertEqual(result["status"], "pending_embedding")
        row = self._source_status(result["source_id"])
        self.assertEqual(row["status"], "pending_embedding")
        self.assertTrue(row["last_error"])
        # Recover with a working embedding.
        recovered = self.service.reembed_sources(None, [result["source_id"]])
        self.assertEqual(recovered["completed"], 1)
        row = self._source_status(result["source_id"])
        self.assertEqual(row["status"], "ready")
        self.assertIsNone(row["last_error"])
