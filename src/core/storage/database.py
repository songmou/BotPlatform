"""SQLite infrastructure for all structured BotPlatform runtime data."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Schema scripts are re-exported here for backward compatibility: existing
# callers and tests import them from ``database``.
from src.core.storage.schema import (  # noqa: F401
    LATEST_SCHEMA_VERSION,
    SCHEMA_SCRIPTS,
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_V3,
    SCHEMA_V4,
    SCHEMA_V5,
    SCHEMA_V6,
    SCHEMA_V7,
    SCHEMA_V8,
    SCHEMA_V9,
    SCHEMA_V10,
    SCHEMA_V11,
    SCHEMA_V12_OUTBOX_TABLE,
    SCHEMA_V13,
    SCHEMA_V14,
    SCHEMA_V15,
    SCHEMA_V16,
    SCHEMA_V17,
    SCHEMA_V18,
    SCHEMA_V19,
    SCHEMA_V20,
    SCHEMA_V21,
    SCHEMA_V22,
    SCHEMA_V22_PERMISSIONS,
    SCHEMA_V23,
    SCHEMA_V23_PERMISSIONS,
    SCHEMA_V24,
    SCHEMA_V24_PERMISSIONS,
    SCHEMA_V25,
    SCHEMA_V26,
    SCHEMA_V27,
    SCHEMA_V28,
    SCHEMA_V29,
)


class DatabaseError(RuntimeError):
    pass


class Database:
    """Open short-lived, correctly configured SQLite connections."""

    def __init__(self, path: Path, busy_timeout_ms: int = 5000) -> None:
        self.path = path.resolve()
        self.busy_timeout_ms = busy_timeout_ms
        self._migration_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(str(self.path.parent), 0o700)
        self._migrate()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout={}".format(self.busy_timeout_ms))
        self._secure_files()
        return connection

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._secure_files()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()
            self._secure_files()

    def _migrate(self) -> None:
        with self._migration_lock:
            connection = sqlite3.connect(str(self.path), isolation_level=None)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute(
                    "PRAGMA busy_timeout={}".format(self.busy_timeout_ms)
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()
                current = int(row[0])
                if current > LATEST_SCHEMA_VERSION:
                    raise DatabaseError("数据库 schema 版本高于当前程序支持版本")
                for version in range(current + 1, LATEST_SCHEMA_VERSION + 1):
                    if version == 12:
                        self._migrate_v12(connection)
                    elif version == 13:
                        self._migrate_v13(connection)
                    elif version == 22:
                        self._migrate_v22(connection)
                    elif version == 23:
                        self._migrate_v23(connection)
                    elif version == 24:
                        self._migrate_v24(connection)
                    elif version == 28:
                        self._migrate_v28(connection)
                    else:
                        self._apply_schema_script(
                            connection, version, SCHEMA_SCRIPTS[version]
                        )
            except sqlite3.Error as exc:
                raise DatabaseError("无法初始化 SQLite 数据库：{}".format(exc)) from exc
            finally:
                connection.close()
            self._secure_files()

    @staticmethod
    def _apply_schema_script(
        connection: sqlite3.Connection, version: int, script: str
    ) -> None:
        """Apply one schema script and its version record atomically.

        ``executescript`` implicitly commits a transaction opened through
        ``execute("BEGIN")``, so the transaction statements are embedded in
        the script itself. If the script fails midway the transaction stays
        open and is rolled back when the migration connection closes.
        """
        record = (
            "INSERT INTO schema_migrations(version, applied_at) "
            "VALUES ({}, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));"
        ).format(version)
        connection.executescript(
            "BEGIN IMMEDIATE;\n" + script + "\n" + record + "\nCOMMIT;"
        )

    @classmethod
    def _migrate_v22(cls, connection: sqlite3.Connection) -> None:
        """Remove retired plugin data while tolerating partial legacy schemas."""
        has_outbox = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='notification_outbox'"
        ).fetchone()
        cleanup = (
            "DELETE FROM notification_outbox WHERE source_type = 'codex';\n"
            if has_outbox
            else ""
        )
        has_roles = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='admin_roles'"
        ).fetchone()
        permissions = SCHEMA_V22_PERMISSIONS if has_roles else ""
        cls._apply_schema_script(
            connection, 22, cleanup + SCHEMA_SCRIPTS[22] + permissions
        )

    @classmethod
    def _migrate_v23(cls, connection: sqlite3.Connection) -> None:
        """Add conversation sessions and channel bindings idempotently."""
        script = ""
        for table in (
            "conversation_context_messages",
            "conversation_events",
        ):
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info({})".format(table)
                ).fetchall()
            }
            if "session_key" not in columns:
                script += (
                    "ALTER TABLE {} ADD COLUMN session_key TEXT "
                    "NOT NULL DEFAULT 'direct';\n"
                ).format(table)
        has_roles = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='admin_roles'"
        ).fetchone()
        permissions = SCHEMA_V23_PERMISSIONS if has_roles else ""
        cls._apply_schema_script(
            connection, 23, script + SCHEMA_SCRIPTS[23] + permissions
        )

    @classmethod
    def _migrate_v24(cls, connection: sqlite3.Connection) -> None:
        """Add organization identity, scoped resources, and member attribution."""
        script = ""
        member_columns = {
            "conversation_events": "user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL",
            "conversation_context_messages": "user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL",
            "memory_items": "user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL",
            "soul_profiles": "user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL",
            "todos": "user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL",
            "integrations": "user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL",
            "notification_outbox": "user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL",
            "script_runs": "user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL",
            "model_runs": "user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL",
            "tool_audit_log": "user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL",
            "drive_audit_log": "user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL",
            "channel_identities": "user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL",
        }
        for table, definition in member_columns.items():
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info({})".format(table)
                ).fetchall()
            }
            if "user_id" not in columns:
                script += "ALTER TABLE {} ADD COLUMN {};\n".format(
                    table, definition
                )
        channel_exists = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='channel_identities'"
        ).fetchone()
        if channel_exists:
            channel_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(channel_identities)"
                ).fetchall()
            }
            if "active_organization_id" not in channel_columns:
                script += (
                    "ALTER TABLE channel_identities ADD COLUMN "
                    "active_organization_id TEXT "
                    "REFERENCES tenants(tenant_id) ON DELETE SET NULL;\n"
                )
        has_roles = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='admin_roles'"
        ).fetchone()
        permissions = SCHEMA_V24_PERMISSIONS if has_roles else ""
        cls._apply_schema_script(
            connection, 24, script + SCHEMA_SCRIPTS[24] + permissions
        )

    @classmethod
    def _migrate_v28(cls, connection: sqlite3.Connection) -> None:
        """Add unified organization controls while tolerating partial fixtures."""
        script = ""
        preferences = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='user_organization_preferences'"
        ).fetchone()
        if preferences:
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(user_organization_preferences)"
                ).fetchall()
            }
            if "active_scope" not in columns:
                script += (
                    "ALTER TABLE user_organization_preferences ADD COLUMN "
                    "active_scope TEXT NOT NULL DEFAULT 'organization' "
                    "CHECK (active_scope IN ('platform', 'organization'));\n"
                )
        script += SCHEMA_SCRIPTS[28]
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "conversation_events" in tables:
            event_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(conversation_events)"
                ).fetchall()
            }
            if "actor_type" not in event_columns:
                script += (
                    "ALTER TABLE conversation_events ADD COLUMN actor_type TEXT "
                    "NOT NULL DEFAULT 'system';\n"
                )
            if "actor_account" not in event_columns:
                script += (
                    "ALTER TABLE conversation_events ADD COLUMN actor_account "
                    "TEXT NOT NULL DEFAULT '';\n"
                )
            script += (
                "UPDATE conversation_events SET actor_type=CASE "
                "WHEN user_id IS NOT NULL THEN 'member' "
                "WHEN role='assistant' THEN 'agent' "
                "WHEN role='user' THEN 'channel_user' ELSE 'system' END "
                "WHERE actor_type='system';\n"
            )
        if "web_conversations" in tables:
            script += r"""
INSERT OR IGNORE INTO organization_conversations(
    conversation_id, organization_id, creator_user_id, source, title,
    legacy_tenant_id, created_at, updated_at
)
SELECT
    conversation_id, organization_id, user_id, 'web', title,
    legacy_tenant_id, created_at, updated_at
FROM web_conversations;
"""
            for table in (
                "conversation_context_messages",
                "conversation_events",
            ):
                if table not in tables:
                    continue
                script += r"""
UPDATE {table}
SET session_key = 'organization:' || (
    SELECT w.conversation_id FROM web_conversations w
    WHERE w.organization_id={table}.tenant_id
      AND {table}.session_key =
          'web:' || w.user_id || ':' || w.conversation_id
    LIMIT 1
)
WHERE EXISTS (
    SELECT 1 FROM web_conversations w
    WHERE w.organization_id={table}.tenant_id
      AND {table}.session_key =
          'web:' || w.user_id || ':' || w.conversation_id
);
""".format(table=table)
        cls._apply_schema_script(connection, 28, script)

    @staticmethod
    def _migrate_v12(connection: sqlite3.Connection) -> None:
        """Rebuild notification_outbox so legacy layouts regain outbox_id order."""
        connection.execute("BEGIN IMMEDIATE")
        try:
            migrated_version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) "
                    "FROM schema_migrations"
                ).fetchone()[0]
            )
            if migrated_version < 12:
                columns = {
                    str(info[1])
                    for info in connection.execute(
                        "PRAGMA table_info(notification_outbox)"
                    ).fetchall()
                }
                connection.execute(
                    "DROP TABLE IF EXISTS notification_outbox_v12"
                )
                connection.execute(SCHEMA_V12_OUTBOX_TABLE)
                if columns:
                    target_columns = [
                        "notification_id",
                        "tenant_id",
                        "batch_id",
                        "batch_position",
                        "source_type",
                        "source_key",
                        "source_ref",
                        "kind",
                        "text_payload",
                        "image_path",
                        "delivery_status",
                        "attempt_count",
                        "next_attempt_at",
                        "lease_expires_at",
                        "created_at",
                        "sent_at",
                        "last_error",
                    ]
                    select_columns = [
                        column if column in columns else "NULL"
                        for column in target_columns
                    ]
                    if "outbox_id" in columns:
                        target_columns.insert(0, "outbox_id")
                        select_columns.insert(0, "outbox_id")
                        order_by = "outbox_id"
                    else:
                        order_by = (
                            "created_at, batch_position, notification_id"
                        )
                    connection.execute(
                        "INSERT INTO notification_outbox_v12({}) "
                        "SELECT {} FROM notification_outbox "
                        "ORDER BY {}".format(
                            ", ".join(target_columns),
                            ", ".join(select_columns),
                            order_by,
                        )
                    )
                connection.execute(
                    "DROP TABLE IF EXISTS notification_outbox"
                )
                connection.execute(
                    "ALTER TABLE notification_outbox_v12 "
                    "RENAME TO notification_outbox"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS "
                    "ix_notification_outbox_delivery "
                    "ON notification_outbox("
                    "delivery_status, next_attempt_at, lease_expires_at)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS "
                    "ix_notification_outbox_tenant_order "
                    "ON notification_outbox(tenant_id, outbox_id)"
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (12, "
                    "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_v13(connection: sqlite3.Connection) -> None:
        """Create channel routing tables and add the outbox endpoint column."""
        # SCHEMA_V13 only creates objects guarded by IF NOT EXISTS, so it runs
        # outside the transaction below; ``executescript`` would otherwise
        # implicitly commit the open transaction.
        connection.executescript(SCHEMA_V13)
        connection.execute("BEGIN IMMEDIATE")
        try:
            outbox_columns = {
                str(info[1])
                for info in connection.execute(
                    "PRAGMA table_info(notification_outbox)"
                ).fetchall()
            }
            if "selected_endpoint_id" not in outbox_columns:
                connection.execute(
                    "ALTER TABLE notification_outbox "
                    "ADD COLUMN selected_endpoint_id TEXT"
                )
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (13, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _secure_files(self) -> None:
        if os.name == "nt":
            return
        for path in (
            self.path,
            Path(str(self.path) + "-wal"),
            Path(str(self.path) + "-shm"),
        ):
            try:
                os.chmod(str(path), 0o600)
            except FileNotFoundError:
                # SQLite can remove its transient WAL/SHM files between the
                # directory lookup and chmod when the last connection closes.
                continue
