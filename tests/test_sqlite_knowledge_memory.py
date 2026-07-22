from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.core.services.knowledge import KnowledgeService
from src.core.services.memory import MemoryService
from src.core.plugins.todo import execute_action
from src.core.storage.tenants import ConversationStore, TenantRegistry


class FakeEmbedding:
    profile = SimpleNamespace(id="fake-embedding", dimensions=4)

    def embed(self, texts):
        vectors = []
        for text in texts:
            if "水果" in text or "苹果" in text:
                vectors.append([1.0, 0.0, 0.0, 0.0])
            else:
                vectors.append([0.0, 1.0, 0.0, 0.0])
        return vectors


class FakeExtractor:
    def __init__(self, values):
        self.values = values

    def extract(self, _question, _answer):
        return list(self.values)

    def close(self):
        pass


class SqliteKnowledgeMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "data"
        self.registry = TenantRegistry(self.data)
        self.tenant = self.registry.resolve("bot", "user")

    def test_database_pragmas_permissions_and_no_runtime_json(self):
        self.assertEqual(os.stat(self.registry.database_path).st_mode & 0o777, 0o600)
        with self.registry.database.read() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        conversation = ConversationStore(self.registry, 12)
        conversation.append_transcript(self.tenant.tenant_id, "user", "只进入数据库")
        self.assertFalse(list(self.registry.tenant_root(self.tenant.tenant_id).rglob("*.json")))
        self.assertFalse(list(self.registry.tenant_root(self.tenant.tenant_id).rglob("*.jsonl")))

    def test_legacy_user_index_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            (root / "system").mkdir(parents=True)
            (root / "system" / "users.json").write_text(
                '{"version":1,"users":{"old":{"tenant_id":"00000000-0000-0000-0000-000000000001"}}}',
                encoding="utf-8",
            )
            registry = TenantRegistry(root)
            self.assertEqual(registry.list_contexts(), [])

    def test_knowledge_idempotence_file_boundary_and_hybrid_search(self):
        service = KnowledgeService(self.registry, FakeEmbedding())
        first = service.add_text(
            self.tenant.tenant_id, "饮食", "苹果是一种常见水果，也可以用于制作甜点。"
        )
        again = service.add_text(
            self.tenant.tenant_id, "饮食", "苹果是一种常见水果，也可以用于制作甜点。"
        )
        self.assertFalse(first["unchanged"])
        self.assertTrue(again["unchanged"])
        self.assertEqual(len(service.list(self.tenant.tenant_id)), 1)
        results = service.search(self.tenant.tenant_id, "我想找水果资料")
        self.assertIn("苹果", results[0]["content"])

        workspace = self.registry.tenant_root(self.tenant.tenant_id) / "workspace"
        document = workspace / "notes.md"
        document.write_text("# 标题\n\nworkspace 知识", encoding="utf-8")
        indexed = service.index_file(self.tenant, document)
        self.assertEqual(indexed["status"], "ready")
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        with self.assertRaises(ValueError):
            service.index_file(self.tenant, outside)
        self.assertTrue(service.delete(self.tenant.tenant_id, indexed["source_id"]))

    def test_embedding_failure_keeps_searchable_chunks(self):
        service = KnowledgeService(self.registry, None)
        result = service.add_text(self.tenant.tenant_id, "离线", "中文全文检索仍然可用。")
        self.assertEqual(result["status"], "pending_embedding")
        self.assertEqual(len(service.search(self.tenant.tenant_id, "全文检索")), 1)

    def test_memory_review_conflict_secret_and_forget(self):
        extractor = FakeExtractor([
            {"kind": "preference", "key": "drink", "content": "用户喜欢喝绿茶", "confidence": 0.95},
            {"kind": "goal", "key": "career", "content": "用户想学习数据库", "confidence": 0.6},
            {"kind": "identity", "key": "secret", "content": "token: abc123", "confidence": 1.0},
        ])
        service = MemoryService(self.registry, extractor)
        self.addCleanup(service.close)
        ConversationStore(self.registry, 12).append_transcript(
            self.tenant.tenant_id, "user", "记住我的偏好"
        )
        created = service.extract(self.tenant.tenant_id, "记住我的偏好", "好的")
        self.assertEqual(len(created), 2)
        with self.registry.database.read() as connection:
            sources = connection.execute(
                "SELECT source_event_ids FROM memory_items WHERE memory_id=?", (created[0],)
            ).fetchone()[0]
        self.assertNotEqual(sources, "[]")
        items = service.list(self.tenant.tenant_id)
        self.assertEqual({item["status"] for item in items}, {"active", "pending"})
        pending = next(item for item in items if item["status"] == "pending")
        self.assertTrue(service.confirm(self.tenant.tenant_id, pending["memory_id"][:8]))
        active = service.search(self.tenant.tenant_id, "绿茶")
        tea = next(item for item in active if "绿茶" in item["content"])
        self.assertTrue(service.forget(self.tenant.tenant_id, tea["memory_id"][:8]))
        self.assertNotIn("绿茶", " ".join(item["content"] for item in service.search(self.tenant.tenant_id, "绿茶")))

    def test_sqlite_todo_backend(self):
        result = execute_action(
            self.registry.database_path,
            self.tenant.tenant_id,
            "add",
            title="SQLite 待办",
        )
        self.assertIn("T0001", result.summary)
        listed = execute_action(
            self.registry.database_path,
            self.tenant.tenant_id,
            "list",
        )
        self.assertIn("SQLite 待办", listed.summary)
        self.assertFalse((self.registry.tenant_root(self.tenant.tenant_id) / "scripts" / "todo" / "todos.json").exists())

    def test_tenant_deletion_purges_fts_and_keeps_audit(self):
        service = KnowledgeService(self.registry, None)
        service.add_text(self.tenant.tenant_id, "private", "只能属于当前租户的秘密知识")
        tenant_id = self.tenant.tenant_id
        self.registry.delete(self.tenant)
        with self.registry.database.read() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_fts WHERE tenant_id=?", (tenant_id,)
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM deletion_audit WHERE tenant_id=?", (tenant_id,)
                ).fetchone()[0],
                "complete",
            )


if __name__ == "__main__":
    unittest.main()
