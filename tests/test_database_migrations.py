from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.core.storage.database import (
    Database,
    LATEST_SCHEMA_VERSION,
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_V3,
    SCHEMA_V4,
    SCHEMA_V5,
    SCHEMA_V25,
)


class DatabaseMigrationTests(unittest.TestCase):
    def test_v26_makes_credential_ids_unique_within_organization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "botplatform.sqlite3"
            connection = sqlite3.connect(str(path))
            try:
                connection.executescript(
                    "PRAGMA foreign_keys=ON;"
                    "CREATE TABLE schema_migrations("
                    "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);"
                    "INSERT INTO schema_migrations VALUES(25, 'now');"
                    "CREATE TABLE users(user_id INTEGER PRIMARY KEY);"
                    "CREATE TABLE organizations("
                    "organization_id TEXT PRIMARY KEY);"
                    + SCHEMA_V25
                )
                connection.execute(
                    "INSERT INTO users(user_id) VALUES (1)"
                )
                connection.execute(
                    "INSERT INTO organizations(organization_id) VALUES ('a')"
                )
                connection.execute(
                    "INSERT INTO organizations(organization_id) VALUES ('b')"
                )
                connection.execute(
                    "INSERT INTO credential_metadata("
                    "credential_id, organization_id, credential_scope, "
                    "resource_type, resource_id, label, secret_service, "
                    "secret_account, created_by, created_at, updated_at"
                    ") VALUES ("
                    "'shared', 'a', 'organization', 'models', 'model', '', "
                    "'service-a', 'credential', 1, 'now', 'now')"
                )
                connection.commit()
            finally:
                connection.close()

            Database(path)
            with closing(sqlite3.connect(str(path))) as migrated:
                migrated.execute(
                    "INSERT INTO credential_metadata("
                    "credential_id, organization_id, credential_scope, "
                    "resource_type, resource_id, label, secret_service, "
                    "secret_account, created_by, created_at, updated_at"
                    ") VALUES ("
                    "'shared', 'b', 'organization', 'models', 'model', '', "
                    "'service-b', 'credential', 1, 'now', 'now')"
                )
                rows = migrated.execute(
                    "SELECT organization_id FROM credential_metadata "
                    "WHERE credential_id='shared' ORDER BY organization_id"
                ).fetchall()
            self.assertEqual(rows, [("a",), ("b",)])

    def test_latest_schema_has_proactive_context_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "botplatform.sqlite3")
            with database.read() as connection:
                table = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='conversation_delivery_receipts'"
                ).fetchone()
                version = connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]

            self.assertIsNotNone(table)
            self.assertEqual(version, LATEST_SCHEMA_VERSION)

    def test_v21_renames_seeded_public_default_category(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "botplatform.sqlite3")
            with database.read() as connection:
                name = connection.execute(
                    "SELECT name FROM knowledge_categories "
                    "WHERE category_id='public-default'"
                ).fetchone()[0]
            self.assertEqual(name, "默认知识库")

    def test_v12_repairs_intermediate_outbox_schema_without_losing_rows(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "botplatform.sqlite3"
            database = Database(path)
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT INTO tenants(tenant_id, bot_id, user_id, created_at) "
                    "VALUES ('tenant', 'bot', 'user', "
                    "'2026-01-01T00:00:00+00:00')"
                )
                connection.execute("DROP TABLE notification_outbox")
                connection.execute(
                    "CREATE TABLE notification_outbox ("
                    "notification_id TEXT PRIMARY KEY,"
                    "tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) "
                    "ON DELETE CASCADE,"
                    "batch_id TEXT NOT NULL,"
                    "batch_position INTEGER NOT NULL DEFAULT 0,"
                    "source_type TEXT NOT NULL,"
                    "source_key TEXT,"
                    "kind TEXT NOT NULL CHECK (kind IN ('text', 'image')),"
                    "text_payload TEXT,"
                    "image_path TEXT,"
                    "delivery_status TEXT NOT NULL DEFAULT 'pending',"
                    "attempt_count INTEGER NOT NULL DEFAULT 0,"
                    "next_attempt_at TEXT,"
                    "lease_expires_at TEXT,"
                    "created_at TEXT NOT NULL,"
                    "sent_at TEXT,"
                    "last_error TEXT,"
                    "UNIQUE (tenant_id, source_type, source_key)"
                    ")"
                )
                connection.executemany(
                    "INSERT INTO notification_outbox("
                    "notification_id, tenant_id, batch_id, batch_position, "
                    "source_type, source_key, kind, text_payload, image_path, "
                    "delivery_status, attempt_count, next_attempt_at, "
                    "lease_expires_at, created_at, sent_at, last_error"
                    ") VALUES (?, 'tenant', 'batch', ?, 'cli', ?, 'text', ?, "
                    "NULL, ?, ?, ?, NULL, '2026-01-01T00:00:00+00:00', NULL, ?)",
                    [
                        (
                            "notification-second",
                            1,
                            "second",
                            "第二条",
                            "retry",
                            3,
                            "2026-01-01T00:01:00+00:00",
                            "temporary failure",
                        ),
                        (
                            "notification-first",
                            0,
                            "first",
                            "第一条",
                            "pending",
                            0,
                            "2026-01-01T00:00:00+00:00",
                            None,
                        ),
                    ],
                )
                connection.execute(
                    "DELETE FROM schema_migrations WHERE version>=12"
                )

            migrated = Database(path)
            with migrated.read() as connection:
                version = connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(notification_outbox)"
                    ).fetchall()
                }
                rows = connection.execute(
                    "SELECT outbox_id, notification_id, source_ref, text_payload, "
                    "delivery_status, attempt_count, next_attempt_at, last_error "
                    "FROM notification_outbox ORDER BY outbox_id"
                ).fetchall()

            self.assertEqual(version, LATEST_SCHEMA_VERSION)
            self.assertIn("outbox_id", columns)
            self.assertIn("source_ref", columns)
            self.assertEqual(
                [tuple(row) for row in rows],
                [
                    (
                        1,
                        "notification-first",
                        None,
                        "第一条",
                        "pending",
                        0,
                        "2026-01-01T00:00:00+00:00",
                        None,
                    ),
                    (
                        2,
                        "notification-second",
                        None,
                        "第二条",
                        "retry",
                        3,
                        "2026-01-01T00:01:00+00:00",
                        "temporary failure",
                    ),
                ],
            )

    def test_v10_repairs_delivered_reminders_and_updates_pending_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "botplatform.sqlite3"
            database = Database(path)
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT INTO tenants(tenant_id, bot_id, user_id, created_at) "
                    "VALUES ('tenant', 'bot', 'user', '2026-01-01T00:00:00+00:00')"
                )
                todos = [
                    (1, "已提醒", "2026-01-01T01:00:00+00:00"),
                    (2, "待提醒", "2026-01-01T02:00:00+00:00"),
                    (3, "无提醒", None),
                ]
                connection.executemany(
                    "INSERT INTO todos("
                    "tenant_id, todo_number, title, status, created_at, updated_at, "
                    "reminder_at, is_one_off"
                    ") VALUES ('tenant', ?, ?, 'pending', "
                    "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', ?, 0)",
                    todos,
                )
                connection.executemany(
                    "INSERT INTO todo_reminder_events("
                    "tenant_id, todo_number, due_at, delivery_status, attempt_count, "
                    "created_at, updated_at, sent_at"
                    ") VALUES ('tenant', ?, ?, ?, 1, "
                    "'2026-01-01T00:00:00+00:00', ?, ?)",
                    [
                        (
                            1,
                            "2026-01-01T01:00:00+00:00",
                            "sent",
                            "2026-01-01T01:00:01+00:00",
                            "2026-01-01T01:00:01+00:00",
                        ),
                        (
                            2,
                            "2026-01-01T02:00:00+00:00",
                            "pending",
                            "2026-01-01T00:00:00+00:00",
                            None,
                        ),
                    ],
                )
                connection.execute("DELETE FROM schema_migrations WHERE version>=10")

            migrated = Database(path)
            with migrated.read() as connection:
                rows = connection.execute(
                    "SELECT todo_number, status, completed_at, reminder_at, is_one_off "
                    "FROM todos ORDER BY todo_number"
                ).fetchall()

            self.assertEqual(
                [tuple(row) for row in rows],
                [
                    (
                        1,
                        "completed",
                        "2026-01-01T01:00:01+00:00",
                        None,
                        1,
                    ),
                    (
                        2,
                        "pending",
                        None,
                        "2026-01-01T02:00:00+00:00",
                        1,
                    ),
                    (3, "pending", None, None, 0),
                ],
            )

    def test_v22_removes_retired_plugin_tables_and_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "botplatform.sqlite3"
            database = Database(path)
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    "DELETE FROM schema_migrations WHERE version>=22"
                )
                connection.execute(
                    "DROP INDEX IF EXISTS ix_context_tenant_session_message"
                )
                connection.execute(
                    "DROP INDEX IF EXISTS ix_conversation_events_tenant_session_time"
                )
                connection.execute(
                    "ALTER TABLE conversation_context_messages "
                    "DROP COLUMN session_key"
                )
                connection.execute(
                    "ALTER TABLE conversation_events DROP COLUMN session_key"
                )
                connection.execute(
                    "INSERT INTO tenants(tenant_id, bot_id, user_id, created_at) "
                    "VALUES ('tenant', 'bot', 'user', '2026-01-01T00:00:00+00:00')"
                )
                connection.execute(
                    "CREATE TABLE codex_task_runs(thread_id TEXT PRIMARY KEY)"
                )
                connection.execute(
                    "CREATE TABLE codex_task_events(event_id INTEGER PRIMARY KEY)"
                )
                connection.execute(
                    "CREATE TABLE codex_task_interactions("
                    "interaction_id INTEGER PRIMARY KEY)"
                )
                connection.execute(
                    "INSERT INTO notification_outbox("
                    "notification_id, tenant_id, batch_id, batch_position, "
                    "source_type, source_key, source_ref, kind, text_payload, "
                    "delivery_status, attempt_count, created_at"
                    ") VALUES ('legacy-plugin', 'tenant', 'batch', 0, 'codex', "
                    "'legacy', NULL, 'text', '旧消息', 'pending', 0, "
                    "'2026-01-01T00:00:00+00:00')"
                )
                connection.execute(
                    "INSERT INTO notification_outbox("
                    "notification_id, tenant_id, batch_id, batch_position, "
                    "source_type, source_key, source_ref, kind, text_payload, "
                    "delivery_status, attempt_count, created_at"
                    ") VALUES ('todo-message', 'tenant', 'batch', 0, 'todo', "
                    "'todo', NULL, 'text', '保留消息', 'pending', 0, "
                    "'2026-01-01T00:00:00+00:00')"
                )

            migrated_database = Database(path)
            with migrated_database.read() as migrated:
                version = migrated.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in migrated.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                sources = [
                    row[0]
                    for row in migrated.execute(
                        "SELECT source_type FROM notification_outbox"
                    )
                ]
            self.assertEqual(version, LATEST_SCHEMA_VERSION)
            self.assertNotIn("codex_task_runs", tables)
            self.assertNotIn("codex_task_events", tables)
            self.assertNotIn("codex_task_interactions", tables)
            self.assertEqual(sources, ["todo"])

    def test_v19_creates_drive_audit_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "botplatform.sqlite3")
            with database.read() as connection:
                table = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='drive_audit_log'"
                ).fetchone()
                version = connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]

            self.assertIsNotNone(table)
            self.assertGreaterEqual(version, 19)
            self.assertEqual(version, LATEST_SCHEMA_VERSION)

    def test_v11_creates_admin_tables_with_builtin_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "botplatform.sqlite3"
            database = Database(path)
            with database.read() as connection:
                version = connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                roles = {
                    str(row["code"]): (str(row["permissions"]), int(row["builtin"]))
                    for row in connection.execute(
                        "SELECT code, permissions, builtin FROM admin_roles"
                    ).fetchall()
                }

            self.assertEqual(version, LATEST_SCHEMA_VERSION)
            self.assertLessEqual(
                {"admin_users", "admin_roles", "admin_sessions"}, tables
            )
            self.assertEqual(
                set(roles),
                {"admin", "editor", "viewer", "tenant_user"},
            )
            self.assertEqual(roles["admin"], ('["*"]', 1))
            self.assertIn("tenants.read", roles["viewer"][0])
            self.assertNotIn("tenants.delete", roles["viewer"][0])


if __name__ == "__main__":
    unittest.main()
