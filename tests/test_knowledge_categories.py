"""Tests for scoped knowledge libraries and drive lifecycle integration."""

from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from src.core.services.drive import DriveService
from src.core.services.knowledge import KnowledgeService
from src.core.storage.tenants import TenantRegistry
from src.core.storage.database import Database


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

    def test_v19_migration_preserves_sources_chunks_and_embeddings(self) -> None:
        path = self.root / "legacy.sqlite3"
        tenant_id = "00000000-0000-0000-0000-000000000123"
        connection = sqlite3.connect(str(path))
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations VALUES (19, '2026-01-01');
            CREATE TABLE tenants(
                tenant_id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, user_id TEXT NOT NULL,
                created_at TEXT NOT NULL, deleting INTEGER NOT NULL DEFAULT 0,
                UNIQUE(bot_id, user_id)
            );
            CREATE TABLE conversation_context_messages(
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE conversation_events(
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                image INTEGER NOT NULL DEFAULT 0,
                event_type TEXT NOT NULL DEFAULT 'message',
                created_at TEXT NOT NULL
            );
            CREATE TABLE admin_roles(
                role_id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                permissions TEXT NOT NULL DEFAULT '[]',
                builtin INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE knowledge_sources(
                source_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                source_type TEXT NOT NULL, name TEXT NOT NULL, relative_path TEXT,
                content_hash TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE knowledge_chunks(
                chunk_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES knowledge_sources(source_id) ON DELETE CASCADE,
                tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                position INTEGER NOT NULL, heading TEXT, content TEXT NOT NULL,
                content_hash TEXT NOT NULL, locator TEXT
            );
            CREATE VIRTUAL TABLE knowledge_fts USING fts5(
                chunk_id UNINDEXED, tenant_id UNINDEXED, heading, content, tokenize='trigram'
            );
            CREATE TABLE knowledge_embeddings(
                chunk_id TEXT PRIMARY KEY REFERENCES knowledge_chunks(chunk_id) ON DELETE CASCADE,
                model_id TEXT NOT NULL, dimensions INTEGER NOT NULL, vector BLOB NOT NULL,
                content_hash TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO tenants VALUES (?, 'ilink', 'legacy', '2026-01-01', 0)",
            (tenant_id,),
        )
        connection.execute(
            "INSERT INTO knowledge_sources VALUES "
            "('source-1', ?, 'file', 'manual.md', 'manual.md', 'hash', "
            "'ready', '2026-01-01', '2026-01-01')",
            (tenant_id,),
        )
        connection.execute(
            "INSERT INTO knowledge_chunks VALUES "
            "('chunk-1', 'source-1', ?, 0, '', 'legacy text', 'chunk-hash', 'chunk:1')",
            (tenant_id,),
        )
        connection.execute(
            "INSERT INTO knowledge_fts VALUES ('chunk-1', ?, '', 'legacy text')",
            (tenant_id,),
        )
        connection.execute(
            "INSERT INTO knowledge_embeddings VALUES "
            "('chunk-1', 'model', 1, ?, 'chunk-hash', '2026-01-01')",
            (b"\x00\x00\x80?",),
        )
        connection.commit()
        connection.close()

        database = Database(path)
        with database.read() as migrated:
            source = migrated.execute(
                "SELECT category_id, drive_path FROM knowledge_sources "
                "WHERE source_id='source-1'"
            ).fetchone()
            self.assertEqual(source["category_id"], "tenant-default-" + tenant_id)
            self.assertEqual(source["drive_path"], "workspace/manual.md")
            self.assertEqual(
                migrated.execute(
                    "SELECT COUNT(*) FROM knowledge_chunks WHERE chunk_id='chunk-1'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                migrated.execute(
                    "SELECT COUNT(*) FROM knowledge_embeddings WHERE chunk_id='chunk-1'"
                ).fetchone()[0],
                1,
            )


if __name__ == "__main__":
    unittest.main()
