"""SQLite infrastructure for all structured BotPlatform runtime data."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


LATEST_SCHEMA_VERSION = 11


SCHEMA_V1 = r"""
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleting INTEGER NOT NULL DEFAULT 0 CHECK (deleting IN (0, 1)),
    UNIQUE (bot_id, user_id)
);

CREATE TABLE IF NOT EXISTS tenant_settings (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    model_mode TEXT NOT NULL DEFAULT 'auto'
);

CREATE TABLE IF NOT EXISTS conversation_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    image INTEGER NOT NULL DEFAULT 0 CHECK (image IN (0, 1)),
    event_type TEXT NOT NULL DEFAULT 'message',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_conversation_events_tenant_time
    ON conversation_events(tenant_id, event_id);

CREATE TABLE IF NOT EXISTS conversation_context_messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_context_tenant_message
    ON conversation_context_messages(tenant_id, message_id);

CREATE TABLE IF NOT EXISTS recipients (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    context_token TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recipient_task_attempts (
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    interaction_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, task_id)
);

CREATE TABLE IF NOT EXISTS schedule_subscriptions (
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    PRIMARY KEY (tenant_id, task_id)
);
CREATE INDEX IF NOT EXISTS ix_schedule_enabled
    ON schedule_subscriptions(task_id, enabled);
CREATE TABLE IF NOT EXISTS schedule_attempts (
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    interaction_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, task_id)
);

CREATE TABLE IF NOT EXISTS integrations (
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    integration_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, integration_id)
);

CREATE TABLE IF NOT EXISTS script_runs (
    run_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    script_id TEXT NOT NULL,
    script_name TEXT NOT NULL,
    trigger TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    error TEXT,
    notification_error TEXT
);
CREATE INDEX IF NOT EXISTS ix_script_runs_tenant_created
    ON script_runs(tenant_id, created_at DESC);
CREATE TABLE IF NOT EXISTS script_run_artifacts (
    run_id TEXT NOT NULL REFERENCES script_runs(run_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    relative_path TEXT NOT NULL,
    content_hash TEXT,
    PRIMARY KEY (run_id, position)
);

CREATE TABLE IF NOT EXISTS todos (
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    todo_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    archived_at TEXT,
    PRIMARY KEY (tenant_id, todo_number)
);
CREATE INDEX IF NOT EXISTS ix_todos_tenant_status
    ON todos(tenant_id, status, todo_number);

CREATE TABLE IF NOT EXISTS deletion_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tenant_cleanup_jobs (
    tenant_id TEXT PRIMARY KEY,
    requested_at TEXT NOT NULL,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_sources (
    source_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    source_type TEXT NOT NULL CHECK (source_type IN ('text', 'file')),
    name TEXT NOT NULL,
    relative_path TEXT,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ready', 'pending_embedding', 'failed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, source_type, name)
);
CREATE INDEX IF NOT EXISTS ix_knowledge_sources_tenant
    ON knowledge_sources(tenant_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES knowledge_sources(source_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    heading TEXT,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    locator TEXT,
    UNIQUE (source_id, position)
);
CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_tenant
    ON knowledge_chunks(tenant_id, source_id, position);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    chunk_id UNINDEXED,
    tenant_id UNINDEXED,
    heading,
    content,
    tokenize='trigram'
);
CREATE TABLE IF NOT EXISTS knowledge_embeddings (
    chunk_id TEXT PRIMARY KEY REFERENCES knowledge_chunks(chunk_id) ON DELETE CASCADE,
    model_id TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector BLOB NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_items (
    memory_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('preference', 'identity', 'goal', 'constraint')),
    content TEXT NOT NULL,
    normalized_key TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'superseded', 'deleted')),
    source_event_ids TEXT NOT NULL DEFAULT '[]',
    superseded_by TEXT REFERENCES memory_items(memory_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_memory_tenant_status
    ON memory_items(tenant_id, status, updated_at DESC);
"""


SCHEMA_V2 = r"""
CREATE TABLE IF NOT EXISTS codex_task_runs (
    thread_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'completed', 'failed', 'interrupted')
    ),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    result_excerpt TEXT,
    error TEXT,
    notification_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        notification_status IN ('pending', 'sent', 'failed', 'disabled')
    )
);
CREATE INDEX IF NOT EXISTS ix_codex_task_runs_tenant_status
    ON codex_task_runs(tenant_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_codex_task_runs_project_created
    ON codex_task_runs(project_id, created_at DESC);
"""


SCHEMA_V3 = r"""
CREATE TABLE IF NOT EXISTS legacy_imports (
    source_tenant_id TEXT PRIMARY KEY,
    target_tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    imported_at TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_legacy_imports_target
    ON legacy_imports(target_tenant_id, imported_at);
"""


SCHEMA_V4 = r"""
ALTER TABLE codex_task_runs ADD COLUMN origin TEXT NOT NULL DEFAULT 'botplatform'
    CHECK (origin IN ('botplatform', 'external'));
ALTER TABLE codex_task_runs ADD COLUMN phase TEXT NOT NULL DEFAULT 'queued'
    CHECK (phase IN (
        'queued', 'running', 'waiting_approval', 'waiting_input',
        'completed', 'failed', 'interrupted'
    ));
ALTER TABLE codex_task_runs ADD COLUMN updated_at TEXT;
ALTER TABLE codex_task_runs ADD COLUMN last_seen_at TEXT;

UPDATE codex_task_runs
SET phase=status,
    updated_at=COALESCE(finished_at, started_at, created_at),
    last_seen_at=COALESCE(finished_at, started_at, created_at);

CREATE TABLE IF NOT EXISTS codex_task_interactions (
    interaction_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES codex_task_runs(thread_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('approval', 'user_input')),
    method TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    response_json TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'pending', 'approved', 'declined', 'answered',
            'expired', 'cancelled'
        )
    ),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(thread_id, turn_id, item_id, method)
);
CREATE INDEX IF NOT EXISTS ix_codex_interactions_tenant_status
    ON codex_task_interactions(tenant_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS codex_task_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL REFERENCES codex_task_runs(thread_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'waiting_approval', 'waiting_input', 'completed', 'failed',
            'interrupted', 'interaction_expired'
        )
    ),
    message TEXT NOT NULL,
    delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        delivery_status IN ('pending', 'sending', 'retry', 'sent', 'failed', 'disabled')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS ix_codex_events_delivery
    ON codex_task_events(delivery_status, next_attempt_at, event_id);
"""


SCHEMA_V5 = r"""
CREATE TABLE codex_task_events_v5 (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL REFERENCES codex_task_runs(thread_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'queued', 'running', 'waiting_approval', 'waiting_input',
            'completed', 'failed', 'interrupted', 'interaction_expired'
        )
    ),
    message TEXT NOT NULL,
    delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        delivery_status IN ('pending', 'sending', 'retry', 'sent', 'failed', 'disabled')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    last_error TEXT
);
INSERT INTO codex_task_events_v5(
    event_id, event_key, thread_id, tenant_id, event_type, message,
    delivery_status, attempt_count, next_attempt_at, created_at, sent_at, last_error
)
SELECT
    event_id, event_key, thread_id, tenant_id, event_type, message,
    delivery_status, attempt_count, next_attempt_at, created_at, sent_at, last_error
FROM codex_task_events;
DROP TABLE codex_task_events;
ALTER TABLE codex_task_events_v5 RENAME TO codex_task_events;
CREATE INDEX ix_codex_events_delivery
    ON codex_task_events(delivery_status, next_attempt_at, event_id);
"""


SCHEMA_V6 = r"""
ALTER TABLE codex_task_runs ADD COLUMN source_cwd TEXT;

CREATE TABLE codex_task_events_v6 (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL REFERENCES codex_task_runs(thread_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'queued', 'running', 'waiting_approval', 'waiting_input',
            'completed', 'failed', 'interrupted', 'interaction_expired'
        )
    ),
    message TEXT NOT NULL,
    delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        delivery_status IN (
            'pending', 'sending', 'retry', 'waiting_recipient',
            'sent', 'failed', 'disabled'
        )
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    last_error TEXT
);
INSERT INTO codex_task_events_v6(
    event_id, event_key, thread_id, tenant_id, event_type, message,
    delivery_status, attempt_count, next_attempt_at, created_at, sent_at, last_error
)
SELECT
    event_id, event_key, thread_id, tenant_id, event_type, message,
    delivery_status, attempt_count, next_attempt_at, created_at, sent_at, last_error
FROM codex_task_events;
DROP TABLE codex_task_events;
ALTER TABLE codex_task_events_v6 RENAME TO codex_task_events;
CREATE INDEX ix_codex_events_delivery
    ON codex_task_events(delivery_status, next_attempt_at, event_id);
UPDATE codex_task_events
SET delivery_status='waiting_recipient', next_attempt_at=NULL
WHERE delivery_status='retry'
  AND attempt_count>=3
  AND lower(COALESCE(last_error, '')) LIKE '%prepare failed%';
"""


SCHEMA_V7 = r"""
ALTER TABLE todos ADD COLUMN reminder_at TEXT;

CREATE TABLE IF NOT EXISTS todo_reminder_events (
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    todo_number INTEGER NOT NULL,
    due_at TEXT NOT NULL,
    delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        delivery_status IN ('pending', 'sending', 'sent', 'cancelled')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT,
    last_error TEXT,
    PRIMARY KEY (tenant_id, todo_number)
);
CREATE INDEX IF NOT EXISTS ix_todo_reminders_due
    ON todo_reminder_events(delivery_status, due_at, tenant_id, todo_number);
"""


SCHEMA_V8 = r"""
ALTER TABLE todos ADD COLUMN is_one_off INTEGER NOT NULL DEFAULT 0 CHECK (is_one_off IN (0, 1));
"""

SCHEMA_V9 = r"""
ALTER TABLE memory_items ADD COLUMN evidence_type TEXT NOT NULL DEFAULT 'legacy'
    CHECK (evidence_type IN ('explicit', 'inferred', 'legacy'));
ALTER TABLE memory_items ADD COLUMN confirmed_at TEXT;

CREATE TABLE IF NOT EXISTS soul_profiles (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    source_memory_ids TEXT NOT NULL DEFAULT '[]',
    last_scanned_event_id INTEGER NOT NULL DEFAULT 0,
    dirty INTEGER NOT NULL DEFAULT 1 CHECK (dirty IN (0, 1)),
    generated_at TEXT,
    compacted_at TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS ix_soul_profiles_dirty
    ON soul_profiles(dirty, tenant_id);
"""


SCHEMA_V10 = r"""
CREATE TABLE IF NOT EXISTS tool_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    tenant_id TEXT,
    session_id TEXT,
    agent_id TEXT,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    output_bytes INTEGER NOT NULL DEFAULT 0,
    args_hash TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_audit_ts ON tool_audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_tool_audit_tool ON tool_audit_log(tool_name);
"""


SCHEMA_V11 = r"""
CREATE TABLE IF NOT EXISTS admin_roles (
    role_id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    permissions TEXT NOT NULL DEFAULT '[]',
    builtin INTEGER NOT NULL DEFAULT 0 CHECK (builtin IN (0, 1))
);

CREATE TABLE IF NOT EXISTS admin_users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role_id INTEGER NOT NULL REFERENCES admin_roles(role_id) ON DELETE RESTRICT,
    disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    session_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES admin_users(user_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip TEXT,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS ix_admin_sessions_user ON admin_sessions(user_id);
CREATE INDEX IF NOT EXISTS ix_admin_sessions_exp ON admin_sessions(expires_at);

INSERT INTO admin_roles(code, name, permissions, builtin) VALUES
    ('admin', '管理员', '["*"]', 1),
    ('editor', '编辑', '["tenants.read","tenants.delete","panel.read","panel.write"]', 1),
    ('viewer', '只读', '["tenants.read","panel.read"]', 1);
"""


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
                connection.execute("PRAGMA busy_timeout={}".format(self.busy_timeout_ms))
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
                if current < 1:
                    connection.executescript(SCHEMA_V1)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) "
                        "VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                    )
                    current = 1
                if current < 2:
                    connection.executescript(SCHEMA_V2)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) "
                        "VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                    )
                    current = 2
                if current < 3:
                    connection.executescript(SCHEMA_V3)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) "
                        "VALUES (3, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                    )
                    current = 3
                if current < 4:
                    connection.executescript(SCHEMA_V4)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) "
                        "VALUES (4, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                    )
                    current = 4
                if current < 5:
                    connection.executescript(SCHEMA_V5)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) "
                        "VALUES (5, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                    )
                    current = 5
                if current < 6:
                    connection.executescript(SCHEMA_V6)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) "
                        "VALUES (6, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                    )
                    current = 6
                if current < 7:
                    connection.executescript(SCHEMA_V7)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) "
                        "VALUES (7, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                    )
                    current = 7
                if current < 8:
                    connection.executescript(SCHEMA_V8)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) "
                        "VALUES (8, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                    )
                    current = 8
                if current < 9:
                    connection.executescript(SCHEMA_V9)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) "
                        "VALUES (9, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                    )
                    current = 9
                if current < 10:
                    connection.executescript(SCHEMA_V10)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) "
                        "VALUES (10, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                    )
                    current = 10
                if current < 11:
                    connection.executescript(SCHEMA_V11)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) "
                        "VALUES (11, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                    )
                    current = 11
            except sqlite3.Error as exc:
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
                # SQLite can remove its transient WAL/SHM files between the
                # directory lookup and chmod when the last connection closes.
                continue
