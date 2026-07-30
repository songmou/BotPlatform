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
