from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.core.storage.database import (
    Database,
    LATEST_SCHEMA_VERSION,
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_V3,
    SCHEMA_V4,
    SCHEMA_V5,
)


class DatabaseMigrationTests(unittest.TestCase):
    def test_v6_preserves_codex_events_and_adds_recipient_wait_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "botplatform.sqlite3"
            connection = sqlite3.connect(str(path), isolation_level=None)
            try:
                for schema in (SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5):
                    connection.executescript(schema)
                for version in range(1, 6):
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) "
                        "VALUES (?, '2026-01-01T00:00:00+00:00')",
                        (version,),
                    )
                connection.execute(
                    "INSERT INTO tenants(tenant_id, bot_id, user_id, created_at) "
                    "VALUES ('tenant', 'bot', 'user', '2026-01-01T00:00:00+00:00')"
                )
                connection.execute(
                    "INSERT INTO codex_task_runs("
                    "thread_id, tenant_id, project_id, title, status, created_at, "
                    "notification_status, origin, phase, updated_at, last_seen_at) "
                    "VALUES ('thread', 'tenant', 'project', 'Title', 'running', "
                    "'2026-01-01T00:00:00+00:00', 'pending', 'external', 'running', "
                    "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
                )
                connection.execute(
                    "INSERT INTO codex_task_events("
                    "event_key, thread_id, tenant_id, event_type, message, "
                    "delivery_status, attempt_count, created_at, last_error) "
                    "VALUES ('first', 'thread', 'tenant', 'running', 'message', "
                    "'retry', 7, '2026-01-01T00:00:00+00:00', "
                    "'微信接口返回错误：prepare failed')"
                )
            finally:
                connection.close()

            database = Database(path)
            with database.transaction(immediate=True) as migrated:
                version = migrated.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                event = migrated.execute(
                    "SELECT event_key, delivery_status, attempt_count, last_error "
                    "FROM codex_task_events ORDER BY event_id"
                ).fetchone()
                migrated.execute(
                    "UPDATE codex_task_events SET delivery_status='waiting_recipient' "
                    "WHERE event_key='first'"
                )
                columns = {
                    row[1]
                    for row in migrated.execute(
                        "PRAGMA table_info(codex_task_runs)"
                    ).fetchall()
                }
                memory_columns = {
                    row[1]
                    for row in migrated.execute(
                        "PRAGMA table_info(memory_items)"
                    ).fetchall()
                }
                soul_columns = {
                    row[1]
                    for row in migrated.execute(
                        "PRAGMA table_info(soul_profiles)"
                    ).fetchall()
                }

            self.assertEqual(version, LATEST_SCHEMA_VERSION)
            self.assertEqual(
                tuple(event),
                (
                    "first",
                    "waiting_recipient",
                    7,
                    "微信接口返回错误：prepare failed",
                ),
            )
            self.assertIn("source_cwd", columns)
            self.assertIn("evidence_type", memory_columns)
            self.assertIn("confirmed_at", memory_columns)
            self.assertIn("content_hash", soul_columns)
            self.assertIn("last_scanned_event_id", soul_columns)


if __name__ == "__main__":
    unittest.main()
