"""Tests for the canonical SQLite schema and format guard."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.core.storage.database import Database, DatabaseError
from src.core.storage.schema import SCHEMA_FORMAT_VERSION


class DatabaseSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "botplatform.sqlite3"

    def test_fresh_database_has_current_schema_and_seed_data(self) -> None:
        database = Database(self.path)
        with database.read() as connection:
            version = connection.execute(
                "SELECT format_version FROM schema_metadata WHERE singleton=1"
            ).fetchone()[0]
            self.assertEqual(version, SCHEMA_FORMAT_VERSION)
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertTrue(
                {
                    "tenants",
                    "organizations",
                    "organization_schedules",
                    "knowledge_sources",
                    "knowledge_fts",
                    "notification_outbox",
                    "personal_channel_connections",
                }.issubset(tables)
            )
            self.assertTrue(
                {
                    "schema_migrations",
                    "tenant_script_schedules",
                    "web_conversations",
                    "legacy_organization_credentials",
                    "organization_data_migrations",
                    "platform_catalog_migrations",
                }.isdisjoint(tables)
            )
            roles = connection.execute(
                "SELECT code FROM admin_roles ORDER BY role_id"
            ).fetchall()
            self.assertEqual(
                [str(row[0]) for row in roles],
                ["admin", "editor", "viewer", "tenant_user"],
            )
            category = connection.execute(
                "SELECT name FROM knowledge_categories WHERE category_id='public-default'"
            ).fetchone()
            self.assertEqual(str(category[0]), "默认知识库")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_existing_empty_file_is_initialized(self) -> None:
        self.path.touch()
        Database(self.path)
        with closing(sqlite3.connect(str(self.path))) as connection:
            row = connection.execute(
                "SELECT format_version FROM schema_metadata WHERE singleton=1"
            ).fetchone()
        self.assertEqual(row[0], SCHEMA_FORMAT_VERSION)

    def test_reopen_preserves_current_database(self) -> None:
        database = Database(self.path)
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO tenants(tenant_id, bot_id, user_id, created_at) "
                "VALUES ('tenant-1', 'bot', 'user', '2026-08-10T00:00:00Z')"
            )
        Database(self.path)
        with database.read() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM tenants WHERE tenant_id='tenant-1'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_foreign_keys_are_enforced(self) -> None:
        database = Database(self.path)
        with self.assertRaises(sqlite3.IntegrityError):
            with database.transaction() as connection:
                connection.execute(
                    "INSERT INTO tenant_settings(tenant_id, model_mode) "
                    "VALUES ('missing', 'auto')"
                )

    def test_old_database_is_rejected_without_modification(self) -> None:
        with closing(sqlite3.connect(str(self.path))) as connection:
            connection.execute("CREATE TABLE schema_migrations(version INTEGER)")
            connection.execute("INSERT INTO schema_migrations VALUES (37)")
        before = self.path.read_bytes()
        before_mtime = self.path.stat().st_mtime_ns
        with self.assertRaisesRegex(DatabaseError, "不兼容的旧版数据库"):
            Database(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mtime_ns, before_mtime)

    def test_unknown_format_version_is_rejected_without_modification(self) -> None:
        with closing(sqlite3.connect(str(self.path))) as connection:
            connection.execute(
                "CREATE TABLE schema_metadata("
                "singleton INTEGER PRIMARY KEY, format_version INTEGER NOT NULL)"
            )
            connection.execute("INSERT INTO schema_metadata VALUES (1, 999)")
            connection.commit()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(DatabaseError, "格式版本 999"):
            Database(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits only")
    def test_database_permissions_are_private(self) -> None:
        Database(self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
