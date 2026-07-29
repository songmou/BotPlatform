"""Versioned SQLite schema scripts for the BotPlatform database.

Each ``SCHEMA_V{n}`` script upgrades the schema from version ``n - 1`` to
``n``. Versions 12 and 13 additionally need Python-side inspection and are
applied by dedicated ``Database`` methods instead of ``SCHEMA_SCRIPTS``.
"""

from __future__ import annotations


LATEST_SCHEMA_VERSION = 19


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


# Versions applied as plain SQL scripts. 12 and 13 are intentionally absent:
# they require Python-side logic and run via dedicated Database methods.
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
}
