"""Versioned SQLite schema scripts for the BotPlatform database.

Each ``SCHEMA_V{n}`` script upgrades the schema from version ``n - 1`` to
``n``. Versions 12, 13, 22, 23, and 24 additionally need Python-side inspection and are
applied by dedicated ``Database`` methods instead of ``SCHEMA_SCRIPTS``.
"""

from __future__ import annotations


LATEST_SCHEMA_VERSION = 30


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


SCHEMA_V2 = "-- Retired legacy plugin schema."


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


SCHEMA_V4 = "-- Retired legacy plugin schema."


SCHEMA_V5 = "-- Retired legacy plugin schema."


SCHEMA_V6 = "-- Retired legacy plugin schema."


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
-- Reminder times are one-shot by default. Repair reminders that were already
-- delivered under v8/v9 but remained pending because is_one_off defaulted to 0.
UPDATE todos
SET status='completed',
    completed_at=COALESCE(
        (
            SELECT event.sent_at
            FROM todo_reminder_events AS event
            WHERE event.tenant_id=todos.tenant_id
              AND event.todo_number=todos.todo_number
        ),
        updated_at
    ),
    updated_at=COALESCE(
        (
            SELECT event.sent_at
            FROM todo_reminder_events AS event
            WHERE event.tenant_id=todos.tenant_id
              AND event.todo_number=todos.todo_number
        ),
        updated_at
    ),
    reminder_at=NULL,
    is_one_off=1
WHERE status='pending'
  AND reminder_at IS NOT NULL
  AND EXISTS (
      SELECT 1
      FROM todo_reminder_events AS event
      WHERE event.tenant_id=todos.tenant_id
        AND event.todo_number=todos.todo_number
        AND event.due_at=todos.reminder_at
        AND event.delivery_status='sent'
  );

-- Existing reminders that have not fired yet should follow the corrected
-- one-shot default as well.
UPDATE todos
SET is_one_off=1
WHERE status='pending' AND reminder_at IS NOT NULL;
"""


SCHEMA_V11 = r"""
CREATE TABLE IF NOT EXISTS notification_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL,
    batch_position INTEGER NOT NULL DEFAULT 0,
    source_type TEXT NOT NULL,
    source_key TEXT,
    source_ref TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('text', 'image')),
    text_payload TEXT,
    image_path TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        delivery_status IN (
            'pending', 'sending', 'retry', 'waiting_recipient',
            'sent', 'failed', 'cancelled'
        )
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    last_error TEXT,
    UNIQUE (tenant_id, source_type, source_key)
);
CREATE INDEX IF NOT EXISTS ix_notification_outbox_delivery
    ON notification_outbox(delivery_status, next_attempt_at, lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_notification_outbox_tenant_order
    ON notification_outbox(tenant_id, outbox_id);
"""

SCHEMA_V12_OUTBOX_TABLE = r"""
CREATE TABLE notification_outbox_v12 (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL,
    batch_position INTEGER NOT NULL DEFAULT 0,
    source_type TEXT NOT NULL,
    source_key TEXT,
    source_ref TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('text', 'image')),
    text_payload TEXT,
    image_path TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        delivery_status IN (
            'pending', 'sending', 'retry', 'waiting_recipient',
            'sent', 'failed', 'cancelled'
        )
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    last_error TEXT,
    UNIQUE (tenant_id, source_type, source_key)
);
"""

SCHEMA_V13 = r"""
CREATE TABLE IF NOT EXISTS channel_identities (
    identity_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    channel_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (channel_id, account_id, external_user_id)
);
CREATE INDEX IF NOT EXISTS ix_channel_identities_tenant
    ON channel_identities(tenant_id, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS delivery_endpoints (
    endpoint_id TEXT PRIMARY KEY,
    identity_id TEXT NOT NULL REFERENCES channel_identities(identity_id)
        ON DELETE CASCADE,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    channel_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    conversation_type TEXT NOT NULL CHECK (
        conversation_type IN ('direct', 'group', 'channel', 'thread')
    ),
    conversation_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    route_context_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'stale', 'disabled')
    ),
    last_seen_at TEXT NOT NULL,
    UNIQUE (
        channel_id, account_id, conversation_type,
        conversation_id, recipient_id, thread_id
    )
);
CREATE INDEX IF NOT EXISTS ix_delivery_endpoints_tenant_active
    ON delivery_endpoints(tenant_id, status, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS message_inbox (
    inbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'processing', 'retry', 'done', 'ignored', 'failed')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    lease_expires_at TEXT,
    received_at TEXT NOT NULL,
    processed_at TEXT,
    last_error TEXT,
    UNIQUE (channel_id, event_id)
);
CREATE INDEX IF NOT EXISTS ix_message_inbox_delivery
    ON message_inbox(status, next_attempt_at, lease_expires_at, inbox_id);
"""


SCHEMA_V14 = r"""
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


SCHEMA_V15 = r"""
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

INSERT OR IGNORE INTO admin_roles(code, name, permissions, builtin) VALUES
    ('admin', '管理员', '["*"]', 1),
    ('editor', '编辑', '["tenants.read","tenants.delete","panel.read","panel.write"]', 1),
    ('viewer', '只读', '["tenants.read","panel.read"]', 1);
"""

SCHEMA_V16 = r"""
CREATE TABLE IF NOT EXISTS conversation_delivery_receipts (
    delivery_key TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_conversation_delivery_receipts_tenant
    ON conversation_delivery_receipts(tenant_id);
"""

SCHEMA_V17 = r"""
CREATE TABLE IF NOT EXISTS tenant_script_schedules (
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    schedule_id TEXT NOT NULL,
    script_id TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    crons_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    authorized_sha256 TEXT NOT NULL,
    authorized_at TEXT NOT NULL,
    authorized_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_run_id TEXT,
    last_status TEXT,
    PRIMARY KEY (tenant_id, schedule_id)
);
CREATE INDEX IF NOT EXISTS ix_tenant_script_schedules_enabled
    ON tenant_script_schedules(enabled, tenant_id, schedule_id);
"""

SCHEMA_V18 = r"""
CREATE TABLE IF NOT EXISTS model_runs (
    run_id TEXT PRIMARY KEY,
    tenant_id TEXT REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('wechat', 'web', 'schedule', 'internal')),
    agent_id TEXT,
    conversation_id TEXT,
    status TEXT NOT NULL DEFAULT 'running' CHECK (
        status IN ('running', 'success', 'partial', 'failed', 'cancelled')
    ),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    response_event_id INTEGER REFERENCES conversation_events(event_id) ON DELETE SET NULL,
    error_category TEXT
);
CREATE INDEX IF NOT EXISTS ix_model_runs_started
    ON model_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS ix_model_runs_tenant_started
    ON model_runs(tenant_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_model_runs_agent_started
    ON model_runs(agent_id, started_at DESC);

CREATE TABLE IF NOT EXISTS model_calls (
    call_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES model_runs(run_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    operation TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    configured_model TEXT NOT NULL,
    actual_model TEXT,
    attempt INTEGER NOT NULL DEFAULT 1,
    is_retry INTEGER NOT NULL DEFAULT 0 CHECK (is_retry IN (0, 1)),
    is_fallback INTEGER NOT NULL DEFAULT 0 CHECK (is_fallback IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('success', 'failed', 'cancelled')),
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    first_token_ms INTEGER,
    http_status INTEGER,
    error_category TEXT,
    retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1)),
    finish_reason TEXT,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    provider_request_id TEXT,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    uncached_input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_output_tokens INTEGER,
    total_tokens INTEGER,
    currency TEXT NOT NULL,
    input_price_micros_per_million INTEGER,
    cached_input_price_micros_per_million INTEGER,
    output_price_micros_per_million INTEGER,
    reasoning_output_price_micros_per_million INTEGER,
    cost_micros INTEGER,
    cost_status TEXT NOT NULL CHECK (
        cost_status IN ('priced', 'free', 'unpriced', 'usage_unknown')
    ),
    UNIQUE (run_id, sequence)
);
CREATE INDEX IF NOT EXISTS ix_model_calls_run
    ON model_calls(run_id, sequence);
CREATE INDEX IF NOT EXISTS ix_model_calls_profile_time
    ON model_calls(profile_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_model_calls_status_time
    ON model_calls(status, started_at DESC);

CREATE TABLE IF NOT EXISTS model_feedback (
    feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES model_runs(run_id) ON DELETE CASCADE,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('tenant', 'admin')),
    actor_ref TEXT NOT NULL,
    rating TEXT NOT NULL CHECK (rating IN ('good', 'bad')),
    reasons_json TEXT NOT NULL DEFAULT '[]',
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, actor_type, actor_ref)
);
CREATE INDEX IF NOT EXISTS ix_model_feedback_run
    ON model_feedback(run_id);
CREATE INDEX IF NOT EXISTS ix_model_feedback_rating_time
    ON model_feedback(rating, updated_at DESC);

CREATE TABLE IF NOT EXISTS model_budgets (
    budget_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL CHECK (
        scope_type IN ('global', 'tenant', 'profile', 'agent')
    ),
    scope_id TEXT NOT NULL DEFAULT '',
    monthly_limit_micros INTEGER NOT NULL CHECK (monthly_limit_micros > 0),
    currency TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (scope_type, scope_id)
);

CREATE TABLE IF NOT EXISTS model_budget_alerts (
    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
    budget_id INTEGER NOT NULL REFERENCES model_budgets(budget_id) ON DELETE CASCADE,
    period TEXT NOT NULL,
    threshold INTEGER NOT NULL CHECK (threshold IN (80, 100)),
    spent_micros INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (budget_id, period, threshold)
);
CREATE INDEX IF NOT EXISTS ix_model_budget_alerts_time
    ON model_budget_alerts(created_at DESC);

UPDATE admin_roles
SET permissions = CASE
    WHEN trim(permissions) = '[]' THEN '["model_analytics.read"]'
    ELSE substr(trim(permissions), 1, length(trim(permissions)) - 1)
         || ',"model_analytics.read"]'
END
WHERE code IN ('editor', 'viewer')
  AND instr(permissions, '"model_analytics.read"') = 0;

UPDATE admin_roles
SET permissions = CASE
    WHEN trim(permissions) = '[]' THEN '["model_analytics.manage"]'
    ELSE substr(trim(permissions), 1, length(trim(permissions)) - 1)
         || ',"model_analytics.manage"]'
END
WHERE code = 'editor'
  AND instr(permissions, '"model_analytics.manage"') = 0;
"""


# V19: audit log table for the network drive (file management) module.
SCHEMA_V19 = r"""
CREATE TABLE IF NOT EXISTS drive_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    operator TEXT NOT NULL,
    source TEXT NOT NULL,
    scope TEXT NOT NULL,
    tenant_id TEXT,
    action TEXT NOT NULL,
    path TEXT NOT NULL,
    target_path TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_drive_audit_ts ON drive_audit_log(ts);
"""


# V20: scoped knowledge categories, agent bindings, and drive-backed sources.
SCHEMA_V20 = r"""
CREATE TABLE IF NOT EXISTS knowledge_categories (
    category_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('public', 'tenant')),
    tenant_id TEXT REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (scope = 'public' AND tenant_id IS NULL)
        OR (scope = 'tenant' AND tenant_id IS NOT NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_knowledge_categories_public_name
    ON knowledge_categories(name) WHERE scope = 'public';
CREATE UNIQUE INDEX IF NOT EXISTS ux_knowledge_categories_tenant_name
    ON knowledge_categories(tenant_id, name) WHERE scope = 'tenant';

INSERT OR IGNORE INTO knowledge_categories(
    category_id, scope, tenant_id, name, description, created_at, updated_at
) VALUES (
    'public-default', 'public', NULL, '公共知识库', '平台公共知识',
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
);

INSERT OR IGNORE INTO knowledge_categories(
    category_id, scope, tenant_id, name, description, created_at, updated_at
)
SELECT
    'tenant-default-' || tenant_id,
    'tenant',
    tenant_id,
    '默认知识库',
    '由升级迁移生成的默认知识库',
    MIN(created_at),
    MAX(updated_at)
FROM knowledge_sources
GROUP BY tenant_id;

CREATE TABLE knowledge_sources_v20 (
    source_id TEXT PRIMARY KEY,
    category_id TEXT NOT NULL REFERENCES knowledge_categories(category_id) ON DELETE RESTRICT,
    tenant_id TEXT REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    source_type TEXT NOT NULL CHECK (source_type IN ('text', 'file')),
    name TEXT NOT NULL,
    relative_path TEXT,
    drive_scope TEXT CHECK (drive_scope IN ('public', 'tenant')),
    drive_tenant_id TEXT REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    drive_path TEXT,
    file_size INTEGER,
    file_mtime_ns INTEGER,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'ready', 'pending_embedding', 'stale_modified',
            'source_missing', 'failed'
        )
    ),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO knowledge_sources_v20(
    source_id, category_id, tenant_id, source_type, name, relative_path,
    drive_scope, drive_tenant_id, drive_path, file_size, file_mtime_ns,
    content_hash, status, last_error, created_at, updated_at
)
SELECT
    source_id,
    'tenant-default-' || tenant_id,
    tenant_id,
    source_type,
    name,
    relative_path,
    CASE WHEN source_type = 'file' THEN 'tenant' ELSE NULL END,
    CASE WHEN source_type = 'file' THEN tenant_id ELSE NULL END,
    CASE
        WHEN source_type = 'file' AND relative_path IS NOT NULL
        THEN 'workspace/' || relative_path
        ELSE NULL
    END,
    NULL,
    NULL,
    content_hash,
    status,
    NULL,
    created_at,
    updated_at
FROM knowledge_sources;

CREATE TABLE knowledge_chunks_v20 (
    chunk_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES knowledge_sources_v20(source_id) ON DELETE CASCADE,
    category_id TEXT NOT NULL REFERENCES knowledge_categories(category_id) ON DELETE CASCADE,
    tenant_id TEXT REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    heading TEXT,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    locator TEXT,
    UNIQUE (source_id, position)
);

INSERT INTO knowledge_chunks_v20(
    chunk_id, source_id, category_id, tenant_id, position, heading,
    content, content_hash, locator
)
SELECT
    c.chunk_id,
    c.source_id,
    s.category_id,
    c.tenant_id,
    c.position,
    c.heading,
    c.content,
    c.content_hash,
    c.locator
FROM knowledge_chunks c
JOIN knowledge_sources_v20 s ON s.source_id = c.source_id;

CREATE TABLE knowledge_embeddings_v20 (
    chunk_id TEXT PRIMARY KEY REFERENCES knowledge_chunks_v20(chunk_id) ON DELETE CASCADE,
    model_id TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector BLOB NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
INSERT INTO knowledge_embeddings_v20
SELECT chunk_id, model_id, dimensions, vector, content_hash, created_at
FROM knowledge_embeddings;

DROP TABLE knowledge_embeddings;
DROP TABLE knowledge_chunks;
DROP TABLE knowledge_sources;
DROP TABLE knowledge_fts;

ALTER TABLE knowledge_sources_v20 RENAME TO knowledge_sources;
ALTER TABLE knowledge_chunks_v20 RENAME TO knowledge_chunks;
ALTER TABLE knowledge_embeddings_v20 RENAME TO knowledge_embeddings;

CREATE INDEX ix_knowledge_sources_category
    ON knowledge_sources(category_id, updated_at DESC);
CREATE INDEX ix_knowledge_sources_tenant
    ON knowledge_sources(tenant_id, updated_at DESC);
CREATE UNIQUE INDEX ux_knowledge_sources_text_name
    ON knowledge_sources(category_id, name) WHERE source_type = 'text';
CREATE UNIQUE INDEX ux_knowledge_sources_drive_path
    ON knowledge_sources(
        category_id, drive_scope, COALESCE(drive_tenant_id, ''), drive_path
    ) WHERE source_type = 'file';
CREATE INDEX ix_knowledge_sources_drive
    ON knowledge_sources(drive_scope, drive_tenant_id, drive_path);
CREATE INDEX ix_knowledge_chunks_category
    ON knowledge_chunks(category_id, source_id, position);
CREATE INDEX ix_knowledge_chunks_tenant
    ON knowledge_chunks(tenant_id, source_id, position);

CREATE VIRTUAL TABLE knowledge_fts USING fts5(
    chunk_id UNINDEXED,
    category_id UNINDEXED,
    tenant_id UNINDEXED,
    heading,
    content,
    tokenize='trigram'
);
INSERT INTO knowledge_fts(chunk_id, category_id, tenant_id, heading, content)
SELECT chunk_id, category_id, COALESCE(tenant_id, ''), heading, content
FROM knowledge_chunks;

CREATE TABLE IF NOT EXISTS agent_knowledge_categories (
    agent_id TEXT NOT NULL,
    category_id TEXT NOT NULL REFERENCES knowledge_categories(category_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, category_id)
);
CREATE INDEX IF NOT EXISTS ix_agent_knowledge_categories_category
    ON agent_knowledge_categories(category_id, agent_id);

CREATE TABLE IF NOT EXISTS knowledge_bootstrap_state (
    key TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL
);
"""


# V21: rename the seeded public default category so the category picker no
# longer duplicates the "公共知识库" scope label. Guarded by NOT EXISTS to
# respect the unique public-name index when users already created one.
SCHEMA_V21 = r"""
UPDATE knowledge_categories SET
    name = '默认知识库',
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE category_id = 'public-default'
    AND scope = 'public'
    AND name = '公共知识库'
    AND NOT EXISTS (
        SELECT 1 FROM knowledge_categories
        WHERE scope = 'public' AND name = '默认知识库'
    );
"""


SCHEMA_V22 = r"""
DROP TABLE IF EXISTS codex_task_events;
DROP TABLE IF EXISTS codex_task_interactions;
DROP TABLE IF EXISTS codex_task_runs;
"""

SCHEMA_V22_PERMISSIONS = r"""
UPDATE admin_roles
SET permissions = CASE
    WHEN trim(permissions) = '[]' THEN '["plugins.read"]'
    ELSE substr(trim(permissions), 1, length(trim(permissions)) - 1)
         || ',"plugins.read"]'
END
WHERE code IN ('editor', 'viewer')
  AND instr(permissions, '"plugins.read"') = 0;

UPDATE admin_roles
SET permissions = CASE
    WHEN trim(permissions) = '[]' THEN '["plugins.manage"]'
    ELSE substr(trim(permissions), 1, length(trim(permissions)) - 1)
         || ',"plugins.manage"]'
END
WHERE code = 'editor'
  AND instr(permissions, '"plugins.manage"') = 0;
"""

SCHEMA_V23 = r"""
DROP INDEX IF EXISTS ix_context_tenant_message;
CREATE INDEX IF NOT EXISTS ix_context_tenant_session_message
    ON conversation_context_messages(tenant_id, session_key, message_id);
CREATE INDEX IF NOT EXISTS ix_conversation_events_tenant_session_time
    ON conversation_events(tenant_id, session_key, event_id);

CREATE TABLE IF NOT EXISTS channel_binding_codes (
    token_hash TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    identity_id TEXT REFERENCES channel_identities(identity_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_channel_binding_codes_expiry
    ON channel_binding_codes(expires_at, used_at);

CREATE TABLE IF NOT EXISTS channel_binding_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    attempted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_channel_binding_attempts_identity_time
    ON channel_binding_attempts(
        channel_id, account_id, external_user_id, attempted_at
    );
"""

SCHEMA_V23_PERMISSIONS = r"""
UPDATE admin_roles
SET permissions = CASE
    WHEN trim(permissions) = '[]' THEN '["channels.read"]'
    ELSE substr(trim(permissions), 1, length(trim(permissions)) - 1)
         || ',"channels.read"]'
END
WHERE code IN ('editor', 'viewer')
  AND instr(permissions, '"channels.read"') = 0;

UPDATE admin_roles
SET permissions = CASE
    WHEN trim(permissions) = '[]' THEN '["channels.manage"]'
    ELSE substr(trim(permissions), 1, length(trim(permissions)) - 1)
         || ',"channels.manage"]'
END
WHERE code = 'editor'
  AND instr(permissions, '"channels.manage"') = 0;
"""


SCHEMA_V24 = r"""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY REFERENCES admin_users(user_id) ON DELETE CASCADE,
    display_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'suspended', 'deleting')
    ),
    legacy INTEGER NOT NULL DEFAULT 0 CHECK (legacy IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organization_memberships (
    membership_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(user_id) ON DELETE CASCADE,
    legacy_subject_id TEXT,
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('invited', 'active', 'disabled')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (organization_id, user_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_org_memberships_legacy_owner
    ON organization_memberships(organization_id, legacy_subject_id)
    WHERE legacy_subject_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_org_memberships_user
    ON organization_memberships(user_id, status, organization_id);

CREATE TABLE IF NOT EXISTS organization_invitations (
    invitation_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    created_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    accepted_at TEXT,
    accepted_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_org_invitations_expiry
    ON organization_invitations(expires_at, accepted_at);

CREATE TABLE IF NOT EXISTS user_organization_preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    active_organization_id TEXT
        REFERENCES organizations(organization_id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_conversations (
    conversation_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '新对话',
    legacy_tenant_id TEXT REFERENCES tenants(tenant_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_web_conversations_owner
    ON web_conversations(organization_id, user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS scoped_resources (
    resource_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('public', 'organization')),
    organization_id TEXT
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    base_resource_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'published' CHECK (
        status IN ('draft', 'published', 'deprecated', 'disabled')
    ),
    payload_json TEXT NOT NULL,
    created_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    updated_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (scope = 'public' AND organization_id IS NULL)
        OR (scope = 'organization' AND organization_id IS NOT NULL)
    ),
    UNIQUE (resource_type, resource_id, scope, organization_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_scoped_resources_public
    ON scoped_resources(resource_type, resource_id)
    WHERE scope = 'public';
CREATE INDEX IF NOT EXISTS ix_scoped_resources_org
    ON scoped_resources(organization_id, resource_type, status);

CREATE TABLE IF NOT EXISTS organization_resource_overrides (
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    resource_type TEXT NOT NULL,
    public_resource_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    list_modes_json TEXT NOT NULL DEFAULT '{}',
    patch_json TEXT NOT NULL DEFAULT '{}',
    base_revision INTEGER NOT NULL DEFAULT 1,
    updated_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (organization_id, resource_type, public_resource_id)
);

CREATE TABLE IF NOT EXISTS organization_agent_knowledge_categories (
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    category_id TEXT NOT NULL
        REFERENCES knowledge_categories(category_id) ON DELETE CASCADE,
    created_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (organization_id, agent_id, category_id)
);
"""


SCHEMA_V24_PERMISSIONS = r"""
INSERT OR IGNORE INTO admin_roles(code, name, permissions, builtin) VALUES
    ('tenant_user', '租户用户', '[]', 1);
"""

SCHEMA_V25 = r"""
CREATE TABLE IF NOT EXISTS credential_metadata (
    credential_id TEXT NOT NULL,
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(user_id) ON DELETE CASCADE,
    credential_scope TEXT NOT NULL CHECK (
        credential_scope IN ('organization', 'personal')
    ),
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    secret_service TEXT NOT NULL,
    secret_account TEXT NOT NULL,
    created_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (credential_scope='organization' AND user_id IS NULL)
        OR (credential_scope='personal' AND user_id IS NOT NULL)
    ),
    PRIMARY KEY (organization_id, credential_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_credential_org_resource
    ON credential_metadata(organization_id, resource_type, resource_id)
    WHERE credential_scope='organization';
CREATE UNIQUE INDEX IF NOT EXISTS ix_credential_personal_resource
    ON credential_metadata(
        organization_id, user_id, resource_type, resource_id
    )
    WHERE credential_scope='personal';

CREATE TABLE IF NOT EXISTS security_audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    actor_user_id INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    organization_id TEXT,
    action TEXT NOT NULL,
    resource TEXT NOT NULL DEFAULT '',
    status_code INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_security_audit_org_time
    ON security_audit_log(organization_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_security_audit_actor_time
    ON security_audit_log(actor_user_id, occurred_at DESC);
"""


SCHEMA_V26 = r"""
ALTER TABLE credential_metadata RENAME TO credential_metadata_v25;

CREATE TABLE credential_metadata (
    credential_id TEXT NOT NULL,
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(user_id) ON DELETE CASCADE,
    credential_scope TEXT NOT NULL CHECK (
        credential_scope IN ('organization', 'personal')
    ),
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    secret_service TEXT NOT NULL,
    secret_account TEXT NOT NULL,
    created_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (credential_scope='organization' AND user_id IS NULL)
        OR (credential_scope='personal' AND user_id IS NOT NULL)
    ),
    PRIMARY KEY (organization_id, credential_id)
);

INSERT INTO credential_metadata(
    credential_id, organization_id, user_id, credential_scope,
    resource_type, resource_id, label, secret_service, secret_account,
    created_by, created_at, updated_at
)
SELECT
    credential_id, organization_id, user_id, credential_scope,
    resource_type, resource_id, label, secret_service, secret_account,
    created_by, created_at, updated_at
FROM credential_metadata_v25;

DROP TABLE credential_metadata_v25;

CREATE UNIQUE INDEX ix_credential_org_resource
    ON credential_metadata(organization_id, resource_type, resource_id)
    WHERE credential_scope='organization';
CREATE UNIQUE INDEX ix_credential_personal_resource
    ON credential_metadata(
        organization_id, user_id, resource_type, resource_id
    )
    WHERE credential_scope='personal';
"""


SCHEMA_V27 = r"""
ALTER TABLE security_audit_log RENAME TO security_audit_log_v26;

CREATE TABLE security_audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    actor_user_id INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    organization_id TEXT,
    action TEXT NOT NULL,
    resource TEXT NOT NULL DEFAULT '',
    status_code INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);

INSERT INTO security_audit_log(
    audit_id, occurred_at, request_id, source, actor_user_id,
    organization_id, action, resource, status_code, detail
)
SELECT
    audit_id, occurred_at, request_id, source, actor_user_id,
    organization_id, action, resource, status_code, detail
FROM security_audit_log_v26;

DROP TABLE security_audit_log_v26;

CREATE INDEX ix_security_audit_org_time
    ON security_audit_log(organization_id, occurred_at DESC);
CREATE INDEX ix_security_audit_actor_time
    ON security_audit_log(actor_user_id, occurred_at DESC);
"""


SCHEMA_V28 = r"""
CREATE TABLE IF NOT EXISTS organization_conversations (
    conversation_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    creator_user_id INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    source TEXT NOT NULL DEFAULT 'web'
        CHECK (source IN ('web', 'channel', 'system')),
    channel_instance_id TEXT,
    external_participant_ref TEXT NOT NULL DEFAULT '',
    external_participant_name TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '新对话',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived')),
    legacy_tenant_id TEXT REFERENCES tenants(tenant_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_org_conversations_time
    ON organization_conversations(organization_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_org_conversations_channel
    ON organization_conversations(
        organization_id, channel_instance_id, external_participant_ref
    );

CREATE TABLE IF NOT EXISTS organization_channels (
    channel_instance_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    channel_id TEXT NOT NULL,
    channel_type TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    settings_json TEXT NOT NULL DEFAULT '{}',
    migration_error TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    updated_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (organization_id, channel_id)
);
CREATE INDEX IF NOT EXISTS ix_org_channels_enabled
    ON organization_channels(enabled, organization_id);

CREATE TABLE IF NOT EXISTS organization_schedules (
    schedule_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    schedule_key TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    crons_json TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT 'last_active_user'
        CHECK (target IN ('last_active_user')),
    action_json TEXT NOT NULL,
    condition_json TEXT,
    dependency_revision TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    updated_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (organization_id, schedule_key)
);
CREATE INDEX IF NOT EXISTS ix_org_schedules_enabled
    ON organization_schedules(enabled, organization_id);

CREATE TABLE IF NOT EXISTS organization_agent_settings (
    organization_id TEXT PRIMARY KEY
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    default_agent_id TEXT NOT NULL DEFAULT '',
    updated_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organization_schedule_runs (
    run_id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL
        REFERENCES organization_schedules(schedule_id) ON DELETE CASCADE,
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'succeeded', 'failed', 'skipped')
    ),
    detail TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_org_schedule_runs_time
    ON organization_schedule_runs(organization_id, started_at DESC);

CREATE TABLE IF NOT EXISTS organization_runtime_revisions (
    organization_id TEXT PRIMARY KEY
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    channels_revision INTEGER NOT NULL DEFAULT 0,
    schedules_revision INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organization_data_migrations (
    migration_key TEXT PRIMARY KEY,
    detail TEXT NOT NULL DEFAULT '',
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organization_content_ownership (
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    resource_type TEXT NOT NULL CHECK (
        resource_type IN ('drive_entry', 'knowledge_source')
    ),
    resource_key TEXT NOT NULL,
    creator_user_id INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (organization_id, resource_type, resource_key)
);
CREATE INDEX IF NOT EXISTS ix_org_content_creator
    ON organization_content_ownership(
        organization_id, creator_user_id, resource_type
    );
"""


SCHEMA_V29 = r"""
CREATE TABLE IF NOT EXISTS platform_resources (
    resource_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    draft_revision INTEGER,
    published_revision INTEGER,
    active_revision INTEGER,
    activation_state TEXT NOT NULL DEFAULT 'inactive' CHECK (
        activation_state IN (
            'inactive', 'active', 'restart_required', 'failed'
        )
    ),
    activation_error TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    updated_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (resource_type, resource_id)
);
CREATE INDEX IF NOT EXISTS ix_platform_resources_type
    ON platform_resources(resource_type, resource_id);

CREATE TABLE IF NOT EXISTS platform_resource_versions (
    resource_pk INTEGER NOT NULL
        REFERENCES platform_resources(resource_pk) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    lifecycle TEXT NOT NULL DEFAULT 'draft' CHECK (
        lifecycle IN ('draft', 'published', 'deprecated')
    ),
    payload_json TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'database' CHECK (
        source IN ('bootstrap', 'database', 'migration')
    ),
    created_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    published_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    PRIMARY KEY (resource_pk, revision)
);
CREATE INDEX IF NOT EXISTS ix_platform_resource_versions_lifecycle
    ON platform_resource_versions(resource_pk, lifecycle, revision DESC);

CREATE TABLE IF NOT EXISTS organization_agents (
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    payload_json TEXT NOT NULL,
    template_resource_id TEXT,
    template_revision INTEGER,
    created_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    updated_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (organization_id, agent_id)
);
CREATE INDEX IF NOT EXISTS ix_organization_agents_enabled
    ON organization_agents(organization_id, enabled, agent_id);

CREATE TABLE IF NOT EXISTS platform_catalog_migrations (
    migration_key TEXT PRIMARY KEY,
    detail TEXT NOT NULL DEFAULT '',
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legacy_organization_credentials (
    organization_id TEXT NOT NULL,
    credential_id TEXT NOT NULL,
    user_id INTEGER,
    credential_scope TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    secret_service TEXT NOT NULL,
    secret_account TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    PRIMARY KEY (organization_id, credential_id)
);

DROP TABLE IF EXISTS user_organization_preferences;
"""

SCHEMA_V30 = r"""
CREATE TABLE IF NOT EXISTS tenant_env (
    tenant_id TEXT PRIMARY KEY,
    env_json TEXT NOT NULL DEFAULT '{}'
);
"""


# Versions applied as plain SQL scripts. Specialized versions with inspection
# or permission backfills are dispatched through dedicated Database methods.
SCHEMA_SCRIPTS: dict[int, str] = {
    1: SCHEMA_V1,
    2: SCHEMA_V2,
    3: SCHEMA_V3,
    4: SCHEMA_V4,
    5: SCHEMA_V5,
    6: SCHEMA_V6,
    7: SCHEMA_V7,
    8: SCHEMA_V8,
    9: SCHEMA_V9,
    10: SCHEMA_V10,
    11: SCHEMA_V11,
    14: SCHEMA_V14,
    15: SCHEMA_V15,
    16: SCHEMA_V16,
    17: SCHEMA_V17,
    18: SCHEMA_V18,
    19: SCHEMA_V19,
    20: SCHEMA_V20,
    21: SCHEMA_V21,
    22: SCHEMA_V22,
    23: SCHEMA_V23,
    24: SCHEMA_V24,
    25: SCHEMA_V25,
    26: SCHEMA_V26,
    27: SCHEMA_V27,
    28: SCHEMA_V28,
    29: SCHEMA_V29,
    30: SCHEMA_V30,
}
