"""SQLite infrastructure for all structured BotPlatform runtime data."""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from src.core.storage.schema import (
    CURRENT_SCHEMA,
    SCHEMA_FORMAT_VERSION,
    WORKFLOW_SCHEMA_V2,
)


class DatabaseError(RuntimeError):
    pass


class Database:
    """Open short-lived, correctly configured SQLite connections."""

    def __init__(self, path: Path, busy_timeout_ms: int = 5000) -> None:
        self.path = path.resolve()
        self.busy_timeout_ms = busy_timeout_ms
        self._schema_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(str(self.path.parent), 0o700)
        self._initialize_or_validate()

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

    def _initialize_or_validate(self) -> None:
        """Create a fresh database or validate an existing current-format one.

        Existing databases are inspected read-only. The supported v1 format is
        backed up and migrated atomically; unknown formats are rejected.
        """
        with self._schema_lock:
            if self.path.exists() and self.path.stat().st_size > 0:
                self._validate_existing_read_only()
                return
            self._initialize_fresh()

    def _validate_existing_read_only(self) -> None:
        try:
            connection = sqlite3.connect(
                self.path.as_uri() + "?mode=ro",
                uri=True,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            raise DatabaseError("无法只读打开 SQLite 数据库：{}".format(exc)) from exc
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if not tables:
                connection.close()
                self._initialize_fresh()
                return
            if "schema_metadata" not in tables:
                raise DatabaseError(
                    "检测到不兼容的旧版数据库。请先备份并移走 {}，"
                    "再重新启动以创建新数据库。".format(self.path)
                )
            row = connection.execute(
                "SELECT format_version FROM schema_metadata WHERE singleton=1"
            ).fetchone()
            version = int(row[0]) if row is not None else None
            if version == 1 and SCHEMA_FORMAT_VERSION == 2:
                connection.close()
                self._backup_and_migrate_v2()
                return
            if version != SCHEMA_FORMAT_VERSION:
                raise DatabaseError(
                    "数据库格式版本 {} 与当前程序要求的版本 {} 不一致。"
                    "请先备份并移走 {}，再重新启动。".format(
                        version if version is not None else "未知",
                        SCHEMA_FORMAT_VERSION,
                        self.path,
                    )
                )
        except sqlite3.Error as exc:
            raise DatabaseError("无法读取 SQLite 数据库格式：{}".format(exc)) from exc
        finally:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def _backup_and_migrate_v2(self) -> None:
        """Back up and atomically add the durable workflow schema."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_root = self.path.parent / "backups"
        backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup_path = backup_root / "{}-v1-{}{}".format(
            self.path.stem, stamp, self.path.suffix or ".sqlite3"
        )
        try:
            source = sqlite3.connect(
                self.path.as_uri() + "?mode=ro", uri=True, isolation_level=None
            )
            destination = sqlite3.connect(str(backup_path), isolation_level=None)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            connection = sqlite3.connect(str(self.path), isolation_level=None)
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute(
                    "PRAGMA busy_timeout={}".format(self.busy_timeout_ms)
                )
                connection.executescript("BEGIN IMMEDIATE;\n" + WORKFLOW_SCHEMA_V2)
                missing = {
                    "organization_workflows",
                    "organization_workflow_versions",
                    "workflow_runs",
                    "workflow_node_runs",
                    "workflow_events",
                    "workflow_waits",
                } - {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if missing:
                    raise DatabaseError(
                        "工作流数据库升级缺少表：{}".format(", ".join(sorted(missing)))
                    )
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise DatabaseError("工作流数据库升级后的外键检查失败")
                connection.execute(
                    "UPDATE schema_metadata SET format_version=2 WHERE singleton=1"
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        except (sqlite3.Error, OSError, DatabaseError) as exc:
            raise DatabaseError(
                "工作流数据库升级失败，原数据库未修改，备份位于 {}：{}".format(
                    backup_path, exc
                )
            ) from exc
        finally:
            self._secure_files()
            if backup_path.exists() and os.name != "nt":
                os.chmod(str(backup_path), 0o600)

    def _initialize_fresh(self) -> None:
        connection = sqlite3.connect(str(self.path), isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "PRAGMA busy_timeout={}".format(self.busy_timeout_ms)
            )
            schema = CURRENT_SCHEMA.replace(
                "__SCHEMA_FORMAT_VERSION__", str(SCHEMA_FORMAT_VERSION)
            )
            connection.executescript(
                "BEGIN IMMEDIATE;\n" + schema + "\nCOMMIT;"
            )
        except sqlite3.Error as exc:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise DatabaseError("无法初始化 SQLite 数据库：{}".format(exc)) from exc
        finally:
            connection.close()
        self._secure_files()

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
                continue
