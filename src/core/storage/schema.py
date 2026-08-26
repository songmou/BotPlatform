"""Canonical SQLite schema for new BotPlatform databases."""

from __future__ import annotations

SCHEMA_FORMAT_VERSION = 4


MODEL_ANALYTICS_SCHEMA_V3 = r"""
CREATE TABLE model_runs_v3 (
    run_id TEXT PRIMARY KEY,
    tenant_id TEXT REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (
        source IN ('wechat', 'wecom', 'feishu', 'web', 'schedule', 'internal')
    ),
    agent_id TEXT,
    conversation_id TEXT,
    status TEXT NOT NULL DEFAULT 'running' CHECK (
        status IN ('running', 'success', 'partial', 'failed', 'cancelled')
    ),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    response_event_id INTEGER
        REFERENCES conversation_events(event_id) ON DELETE SET NULL,
    error_category TEXT,
    user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL
);

INSERT INTO model_runs_v3(
    run_id, tenant_id, source, agent_id, conversation_id, status, started_at,
    finished_at, response_event_id, error_category, user_id
)
SELECT
    run_id, tenant_id, source, agent_id, conversation_id, status, started_at,
    finished_at, response_event_id, error_category, user_id
FROM model_runs;

DROP TABLE model_runs;
ALTER TABLE model_runs_v3 RENAME TO model_runs;

CREATE INDEX ix_model_runs_started
    ON model_runs(started_at DESC);

CREATE INDEX ix_model_runs_tenant_started
    ON model_runs(tenant_id, started_at DESC);

CREATE INDEX ix_model_runs_agent_started
    ON model_runs(agent_id, started_at DESC);
"""


WORKFLOW_SCHEMA_V2 = r"""
CREATE TABLE organization_workflows (
    workflow_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    workflow_key TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (
        status IN ('draft', 'published', 'disabled', 'archived')
    ),
    draft_json TEXT NOT NULL,
    draft_revision INTEGER NOT NULL DEFAULT 1,
    published_version INTEGER,
    template_resource_id TEXT,
    template_revision INTEGER,
    created_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    updated_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (organization_id, workflow_key)
);

CREATE INDEX ix_organization_workflows_status
    ON organization_workflows(organization_id, status, updated_at DESC);

CREATE TABLE organization_workflow_versions (
    workflow_id TEXT NOT NULL
        REFERENCES organization_workflows(workflow_id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    definition_hash TEXT NOT NULL,
    dependency_json TEXT NOT NULL DEFAULT '{}',
    published_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, version)
);

CREATE TABLE workflow_trigger_bindings (
    trigger_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL
        REFERENCES organization_workflows(workflow_id) ON DELETE CASCADE,
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    trigger_key TEXT NOT NULL,
    trigger_type TEXT NOT NULL CHECK (
        trigger_type IN ('manual', 'api', 'webhook', 'schedule')
    ),
    config_json TEXT NOT NULL DEFAULT '{}',
    published_version INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    secret_hash TEXT NOT NULL DEFAULT '',
    next_fire_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (workflow_id, trigger_key)
);

CREATE INDEX ix_workflow_triggers_due
    ON workflow_trigger_bindings(trigger_type, enabled, next_fire_at);

CREATE TABLE workflow_access_tokens (
    token_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL
        REFERENCES organization_workflows(workflow_id) ON DELETE CASCADE,
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT '',
    token_hash TEXT NOT NULL UNIQUE,
    created_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    last_used_at TEXT
);

CREATE TABLE workflow_runs (
    run_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL
        REFERENCES organization_workflows(workflow_id) ON DELETE CASCADE,
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    workflow_version INTEGER NOT NULL,
    trigger_type TEXT NOT NULL,
    trigger_ref TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (
        status IN (
            'queued', 'running', 'waiting', 'succeeded', 'failed',
            'canceled', 'timed_out', 'needs_attention'
        )
    ),
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT,
    state_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT,
    initiated_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    test_mode INTEGER NOT NULL DEFAULT 0 CHECK (test_mode IN (0, 1)),
    allow_side_effects INTEGER NOT NULL DEFAULT 0 CHECK (allow_side_effects IN (0, 1)),
    lease_owner TEXT,
    lease_expires_at TEXT,
    wake_at TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE UNIQUE INDEX ix_workflow_runs_idempotency
    ON workflow_runs(workflow_id, trigger_ref, idempotency_key)
    WHERE idempotency_key <> '';

CREATE INDEX ix_workflow_runs_queue
    ON workflow_runs(status, wake_at, lease_expires_at, created_at);

CREATE INDEX ix_workflow_runs_org_time
    ON workflow_runs(organization_id, created_at DESC);

CREATE TABLE workflow_node_runs (
    node_run_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    node_type TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK (
        status IN (
            'queued', 'running', 'waiting', 'succeeded', 'failed',
            'skipped', 'needs_attention'
        )
    ),
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT,
    error_json TEXT,
    operation_key TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE (run_id, node_id, attempt)
);

CREATE INDEX ix_workflow_node_runs_run
    ON workflow_node_runs(run_id, started_at);

CREATE TABLE workflow_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    node_id TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX ix_workflow_events_run
    ON workflow_events(run_id, event_id);

CREATE TABLE workflow_waits (
    wait_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    wait_type TEXT NOT NULL CHECK (
        wait_type IN ('approval', 'input', 'delay', 'attention')
    ),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'approved', 'rejected', 'resolved', 'expired', 'canceled')
    ),
    assignees_json TEXT NOT NULL DEFAULT '{}',
    payload_json TEXT NOT NULL DEFAULT '{}',
    payload_hash TEXT NOT NULL,
    response_json TEXT,
    expires_at TEXT,
    resolved_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX ix_workflow_waits_org_status
    ON workflow_waits(organization_id, status, created_at DESC);
"""

CURRENT_SCHEMA = r"""
CREATE TABLE schema_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    format_version INTEGER NOT NULL
);

CREATE TABLE tenants (
    tenant_id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleting INTEGER NOT NULL DEFAULT 0 CHECK (deleting IN (0, 1)),
    UNIQUE (bot_id, user_id)
);

CREATE TABLE tenant_settings (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    model_mode TEXT NOT NULL DEFAULT 'auto'
);

CREATE TABLE conversation_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    image INTEGER NOT NULL DEFAULT 0 CHECK (image IN (0, 1)),
    event_type TEXT NOT NULL DEFAULT 'message',
    created_at TEXT NOT NULL,
    session_key TEXT NOT NULL DEFAULT 'direct',
    user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL,
    actor_type TEXT NOT NULL DEFAULT 'system',
    actor_account TEXT NOT NULL DEFAULT ''
);

CREATE INDEX ix_conversation_events_tenant_time
    ON conversation_events(tenant_id, event_id);

CREATE TABLE conversation_context_messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
, session_key TEXT NOT NULL DEFAULT 'direct', user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL);

CREATE TABLE recipients (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    context_token TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE recipient_task_attempts (
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    interaction_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, task_id)
);

CREATE TABLE schedule_subscriptions (
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    PRIMARY KEY (tenant_id, task_id)
);

CREATE INDEX ix_schedule_enabled
    ON schedule_subscriptions(task_id, enabled);

CREATE TABLE schedule_attempts (
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    interaction_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, task_id)
);

CREATE TABLE integrations (
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    integration_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    updated_at TEXT NOT NULL, user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL,
    PRIMARY KEY (tenant_id, integration_id)
);

CREATE TABLE script_runs (
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
    notification_error TEXT,
    trigger_endpoint_id TEXT
, user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL);

CREATE INDEX ix_script_runs_tenant_created
    ON script_runs(tenant_id, created_at DESC);

CREATE TABLE script_run_artifacts (
    run_id TEXT NOT NULL REFERENCES script_runs(run_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    relative_path TEXT NOT NULL,
    content_hash TEXT,
    PRIMARY KEY (run_id, position)
);

CREATE TABLE todos (
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    todo_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    archived_at TEXT,
    reminder_at TEXT,
    is_one_off INTEGER NOT NULL DEFAULT 0 CHECK (is_one_off IN (0, 1)),
    user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL,
    PRIMARY KEY (tenant_id, todo_number)
);

CREATE INDEX ix_todos_tenant_status
    ON todos(tenant_id, status, todo_number);

CREATE TABLE deletion_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE tenant_cleanup_jobs (
    tenant_id TEXT PRIMARY KEY,
    requested_at TEXT NOT NULL,
    last_error TEXT
);

CREATE TABLE memory_items (
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
    last_used_at TEXT,
    evidence_type TEXT NOT NULL DEFAULT 'legacy'
        CHECK (evidence_type IN ('explicit', 'inferred', 'legacy')),
    confirmed_at TEXT,
    user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL
);

CREATE INDEX ix_memory_tenant_status
    ON memory_items(tenant_id, status, updated_at DESC);

CREATE TABLE todo_reminder_events (
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

CREATE INDEX ix_todo_reminders_due
    ON todo_reminder_events(delivery_status, due_at, tenant_id, todo_number);

CREATE TABLE soul_profiles (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    source_memory_ids TEXT NOT NULL DEFAULT '[]',
    last_scanned_event_id INTEGER NOT NULL DEFAULT 0,
    dirty INTEGER NOT NULL DEFAULT 1 CHECK (dirty IN (0, 1)),
    generated_at TEXT,
    compacted_at TEXT,
    last_error TEXT
, user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL);

CREATE INDEX ix_soul_profiles_dirty
    ON soul_profiles(dirty, tenant_id);

CREATE TABLE "notification_outbox" (
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
    last_error TEXT, selected_endpoint_id TEXT, user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL,
    UNIQUE (tenant_id, source_type, source_key)
);

CREATE INDEX ix_notification_outbox_delivery ON notification_outbox(delivery_status, next_attempt_at, lease_expires_at);

CREATE INDEX ix_notification_outbox_tenant_order ON notification_outbox(tenant_id, outbox_id);

CREATE TABLE channel_identities (
    identity_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    channel_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL,
    active_organization_id TEXT REFERENCES tenants(tenant_id) ON DELETE SET NULL,
    UNIQUE (channel_id, account_id, external_user_id)
);

CREATE INDEX ix_channel_identities_tenant
    ON channel_identities(tenant_id, last_seen_at DESC);

CREATE TABLE delivery_endpoints (
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

CREATE INDEX ix_delivery_endpoints_tenant_active
    ON delivery_endpoints(tenant_id, status, last_seen_at DESC);

CREATE TABLE message_inbox (
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

CREATE INDEX ix_message_inbox_delivery
    ON message_inbox(status, next_attempt_at, lease_expires_at, inbox_id);

CREATE TABLE tool_audit_log (
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
, user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL);

CREATE INDEX idx_tool_audit_ts ON tool_audit_log(ts);

CREATE INDEX idx_tool_audit_tool ON tool_audit_log(tool_name);

CREATE TABLE admin_roles (
    role_id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    permissions TEXT NOT NULL DEFAULT '[]',
    builtin INTEGER NOT NULL DEFAULT 0 CHECK (builtin IN (0, 1))
);

CREATE TABLE admin_users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role_id INTEGER NOT NULL REFERENCES admin_roles(role_id) ON DELETE RESTRICT,
    disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE admin_sessions (
    session_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES admin_users(user_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip TEXT,
    user_agent TEXT
);

CREATE INDEX ix_admin_sessions_user ON admin_sessions(user_id);

CREATE INDEX ix_admin_sessions_exp ON admin_sessions(expires_at);

CREATE TABLE conversation_delivery_receipts (
    delivery_key TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL
);

CREATE INDEX ix_conversation_delivery_receipts_tenant
    ON conversation_delivery_receipts(tenant_id);

CREATE TABLE model_runs (
    run_id TEXT PRIMARY KEY,
    tenant_id TEXT REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (
        source IN ('wechat', 'wecom', 'feishu', 'web', 'schedule', 'internal')
    ),
    agent_id TEXT,
    conversation_id TEXT,
    status TEXT NOT NULL DEFAULT 'running' CHECK (
        status IN ('running', 'success', 'partial', 'failed', 'cancelled')
    ),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    response_event_id INTEGER REFERENCES conversation_events(event_id) ON DELETE SET NULL,
    error_category TEXT
, user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL);

CREATE INDEX ix_model_runs_started
    ON model_runs(started_at DESC);

CREATE INDEX ix_model_runs_tenant_started
    ON model_runs(tenant_id, started_at DESC);

CREATE INDEX ix_model_runs_agent_started
    ON model_runs(agent_id, started_at DESC);

CREATE TABLE model_calls (
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
    request_json TEXT,
    response_json TEXT,
    error_message TEXT,
    UNIQUE (run_id, sequence)
);

CREATE INDEX ix_model_calls_run
    ON model_calls(run_id, sequence);

CREATE INDEX ix_model_calls_profile_time
    ON model_calls(profile_id, started_at DESC);

CREATE INDEX ix_model_calls_status_time
    ON model_calls(status, started_at DESC);

CREATE TABLE model_feedback (
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

CREATE INDEX ix_model_feedback_run
    ON model_feedback(run_id);

CREATE INDEX ix_model_feedback_rating_time
    ON model_feedback(rating, updated_at DESC);

CREATE TABLE model_budgets (
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

CREATE TABLE model_budget_alerts (
    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
    budget_id INTEGER NOT NULL REFERENCES model_budgets(budget_id) ON DELETE CASCADE,
    period TEXT NOT NULL,
    threshold INTEGER NOT NULL CHECK (threshold IN (80, 100)),
    spent_micros INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (budget_id, period, threshold)
);

CREATE INDEX ix_model_budget_alerts_time
    ON model_budget_alerts(created_at DESC);

CREATE TABLE drive_audit_log (
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
, user_id INTEGER REFERENCES admin_users(user_id) ON DELETE SET NULL);

CREATE INDEX ix_drive_audit_ts ON drive_audit_log(ts);

CREATE TABLE knowledge_categories (
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

CREATE UNIQUE INDEX ux_knowledge_categories_public_name
    ON knowledge_categories(name) WHERE scope = 'public';

CREATE UNIQUE INDEX ux_knowledge_categories_tenant_name
    ON knowledge_categories(tenant_id, name) WHERE scope = 'tenant';

CREATE TABLE "knowledge_sources" (
    source_id TEXT PRIMARY KEY,
    category_id TEXT NOT NULL REFERENCES knowledge_categories(category_id) ON DELETE RESTRICT,
    tenant_id TEXT REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    source_type TEXT NOT NULL CHECK (source_type IN ('text', 'file', 'web')),
    name TEXT NOT NULL,
    source_url TEXT,
    crawl_page_id TEXT,
    fetched_at TEXT,
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

CREATE TABLE "knowledge_chunks" (
    chunk_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES "knowledge_sources"(source_id) ON DELETE CASCADE,
    category_id TEXT NOT NULL REFERENCES knowledge_categories(category_id) ON DELETE CASCADE,
    tenant_id TEXT REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    heading TEXT,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    locator TEXT,
    UNIQUE (source_id, position)
);

CREATE TABLE "knowledge_embeddings" (
    chunk_id TEXT PRIMARY KEY REFERENCES "knowledge_chunks"(chunk_id) ON DELETE CASCADE,
    model_id TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector BLOB NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
, model_fingerprint TEXT);

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

CREATE UNIQUE INDEX ux_knowledge_sources_web_url
    ON knowledge_sources(category_id, source_url) WHERE source_type = 'web';

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

CREATE TABLE agent_knowledge_categories (
    agent_id TEXT NOT NULL,
    category_id TEXT NOT NULL REFERENCES knowledge_categories(category_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, category_id)
);

CREATE INDEX ix_agent_knowledge_categories_category
    ON agent_knowledge_categories(category_id, agent_id);

CREATE TABLE knowledge_bootstrap_state (
    key TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL
);

CREATE INDEX ix_context_tenant_session_message
    ON conversation_context_messages(tenant_id, session_key, message_id);

CREATE INDEX ix_conversation_events_tenant_session_time
    ON conversation_events(tenant_id, session_key, event_id);

CREATE TABLE channel_binding_codes (
    token_hash TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    identity_id TEXT REFERENCES channel_identities(identity_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);

CREATE INDEX ix_channel_binding_codes_expiry
    ON channel_binding_codes(expires_at, used_at);

CREATE TABLE channel_binding_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    attempted_at TEXT NOT NULL
);

CREATE INDEX ix_channel_binding_attempts_identity_time
    ON channel_binding_attempts(
        channel_id, account_id, external_user_id, attempted_at
    );

CREATE TABLE users (
    user_id INTEGER PRIMARY KEY REFERENCES admin_users(user_id) ON DELETE CASCADE,
    display_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE organizations (
    organization_id TEXT PRIMARY KEY REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'suspended', 'deleting')
    ),
    legacy INTEGER NOT NULL DEFAULT 0 CHECK (legacy IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE organization_memberships (
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

CREATE UNIQUE INDEX ix_org_memberships_legacy_owner
    ON organization_memberships(organization_id, legacy_subject_id)
    WHERE legacy_subject_id IS NOT NULL;

CREATE INDEX ix_org_memberships_user
    ON organization_memberships(user_id, status, organization_id);

CREATE TABLE organization_invitations (
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

CREATE INDEX ix_org_invitations_expiry
    ON organization_invitations(expires_at, accepted_at);

CREATE TABLE organization_agent_knowledge_categories (
    organization_id TEXT NOT NULL
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    category_id TEXT NOT NULL
        REFERENCES knowledge_categories(category_id) ON DELETE CASCADE,
    created_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (organization_id, agent_id, category_id)
);

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

CREATE UNIQUE INDEX ix_credential_org_resource
    ON credential_metadata(organization_id, resource_type, resource_id)
    WHERE credential_scope='organization';

CREATE UNIQUE INDEX ix_credential_personal_resource
    ON credential_metadata(
        organization_id, user_id, resource_type, resource_id
    )
    WHERE credential_scope='personal';

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

CREATE INDEX ix_security_audit_org_time
    ON security_audit_log(organization_id, occurred_at DESC);

CREATE INDEX ix_security_audit_actor_time
    ON security_audit_log(actor_user_id, occurred_at DESC);

CREATE TABLE organization_conversations (
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

CREATE INDEX ix_org_conversations_time
    ON organization_conversations(organization_id, status, updated_at DESC);

CREATE INDEX ix_org_conversations_channel
    ON organization_conversations(
        organization_id, channel_instance_id, external_participant_ref
    );

CREATE TABLE organization_channels (
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

CREATE INDEX ix_org_channels_enabled
    ON organization_channels(enabled, organization_id);

CREATE TABLE organization_schedules (
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

CREATE INDEX ix_org_schedules_enabled
    ON organization_schedules(enabled, organization_id);

CREATE TABLE organization_agent_settings (
    organization_id TEXT PRIMARY KEY
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    default_agent_id TEXT NOT NULL DEFAULT '',
    updated_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE organization_schedule_runs (
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
, script_run_id TEXT);

CREATE INDEX ix_org_schedule_runs_time
    ON organization_schedule_runs(organization_id, started_at DESC);

CREATE TABLE organization_runtime_revisions (
    organization_id TEXT PRIMARY KEY
        REFERENCES organizations(organization_id) ON DELETE CASCADE,
    channels_revision INTEGER NOT NULL DEFAULT 0,
    schedules_revision INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE organization_content_ownership (
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

CREATE INDEX ix_org_content_creator
    ON organization_content_ownership(
        organization_id, creator_user_id, resource_type
    );

CREATE TABLE platform_resources (
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

CREATE INDEX ix_platform_resources_type
    ON platform_resources(resource_type, resource_id);

CREATE TABLE platform_resource_versions (
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

CREATE INDEX ix_platform_resource_versions_lifecycle
    ON platform_resource_versions(resource_pk, lifecycle, revision DESC);

CREATE TABLE organization_agents (
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

CREATE INDEX ix_organization_agents_enabled
    ON organization_agents(organization_id, enabled, agent_id);

CREATE TABLE tenant_env (
    tenant_id TEXT PRIMARY KEY,
    env_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE datasource_query_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT,
    agent_id TEXT,
    session_id TEXT,
    user_id INTEGER,
    datasource_id TEXT NOT NULL,
    statement_kind TEXT NOT NULL,
    sql_text TEXT NOT NULL,
    tables TEXT NOT NULL DEFAULT '',
    row_count INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX ix_datasource_query_audit_time
    ON datasource_query_audit(datasource_id, created_at DESC);

CREATE TABLE "personal_channel_connections" (
    connection_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id)
        ON DELETE CASCADE,
    channel_instance_id TEXT NOT NULL UNIQUE
        REFERENCES organization_channels(channel_instance_id) ON DELETE CASCADE,
    platform TEXT NOT NULL CHECK (platform IN ('wechat', 'wecom', 'feishu')),
    bot_account_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX ix_personal_connections_user
    ON personal_channel_connections(user_id, created_at DESC);

CREATE TABLE mcp_call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    server_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('manual', 'agent')),
    status TEXT NOT NULL CHECK (status IN ('success', 'error')),
    duration_ms INTEGER NOT NULL DEFAULT 0,
    input_json TEXT,
    output_json TEXT,
    input_truncated INTEGER NOT NULL DEFAULT 0,
    output_truncated INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    tenant_id TEXT,
    agent_id TEXT,
    session_id TEXT,
    user_id INTEGER
);

CREATE INDEX ix_mcp_call_log_server_ts
    ON mcp_call_log(server_id, ts DESC);

CREATE INDEX ix_mcp_call_log_tool
    ON mcp_call_log(server_id, tool_name, ts DESC);

CREATE INDEX ix_mcp_call_log_source
    ON mcp_call_log(server_id, source, ts DESC);

""" + WORKFLOW_SCHEMA_V2 + r"""

INSERT INTO schema_metadata(singleton, format_version)
VALUES (1, __SCHEMA_FORMAT_VERSION__);
INSERT INTO admin_roles(role_id, code, name, permissions, builtin) VALUES
    (1, 'admin', '管理员', '["*"]', 1),
    (2, 'editor', '编辑',
        '["tenants.read","tenants.delete","panel.read","panel.write",' ||
        '"model_analytics.read","model_analytics.manage","plugins.read",' ||
        '"plugins.manage","channels.read","channels.manage"]', 1),
    (3, 'viewer', '只读',
        '["tenants.read","panel.read","model_analytics.read",' ||
        '"plugins.read","channels.read"]', 1),
    (4, 'tenant_user', '租户用户', '[]', 1);
INSERT INTO knowledge_categories(
    category_id, scope, tenant_id, name, description, created_at, updated_at
) VALUES (
    'public-default', 'public', NULL, '默认知识库', '平台公共知识',
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
);
"""

# Idempotent addenda applied to both fresh and existing databases to add new
# tables without bumping SCHEMA_FORMAT_VERSION. Each statement must be
# CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS so it is safe to run
# repeatedly on databases that already have the objects.
SCHEMA_ADDENDA = r"""
CREATE TABLE IF NOT EXISTS mcp_call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    server_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('manual', 'agent')),
    status TEXT NOT NULL CHECK (status IN ('success', 'error')),
    duration_ms INTEGER NOT NULL DEFAULT 0,
    input_json TEXT,
    output_json TEXT,
    input_truncated INTEGER NOT NULL DEFAULT 0,
    output_truncated INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    tenant_id TEXT,
    agent_id TEXT,
    session_id TEXT,
    user_id INTEGER
);

CREATE INDEX IF NOT EXISTS ix_mcp_call_log_server_ts
    ON mcp_call_log(server_id, ts DESC);

CREATE INDEX IF NOT EXISTS ix_mcp_call_log_tool
    ON mcp_call_log(server_id, tool_name, ts DESC);

CREATE INDEX IF NOT EXISTS ix_mcp_call_log_source
    ON mcp_call_log(server_id, source, ts DESC);

CREATE TABLE IF NOT EXISTS crawl_sources (
    source_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    config_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    schedule_cron TEXT NOT NULL DEFAULT '',
    next_run_at TEXT,
    created_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, name)
);

CREATE INDEX IF NOT EXISTS ix_crawl_sources_due
    ON crawl_sources(enabled, next_run_at);

CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES crawl_sources(source_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    trigger_type TEXT NOT NULL CHECK (trigger_type IN ('manual', 'schedule', 'retry')),
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'succeeded', 'failed', 'canceled')
    ),
    pages_queued INTEGER NOT NULL DEFAULT 0,
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    pages_changed INTEGER NOT NULL DEFAULT 0,
    pages_failed INTEGER NOT NULL DEFAULT 0,
    records_created INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    lease_owner TEXT,
    lease_expires_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_crawl_runs_queue
    ON crawl_runs(status, lease_expires_at, created_at);
CREATE INDEX IF NOT EXISTS ix_crawl_runs_tenant
    ON crawl_runs(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS crawl_frontier (
    frontier_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES crawl_runs(run_id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    parent_url TEXT NOT NULL DEFAULT '',
    depth INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued', 'processing', 'done', 'failed', 'skipped')
    ),
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, canonical_url)
);

CREATE INDEX IF NOT EXISTS ix_crawl_frontier_queue
    ON crawl_frontier(run_id, status, depth, frontier_id);

CREATE TABLE IF NOT EXISTS crawl_pages (
    page_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES crawl_sources(source_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    canonical_url TEXT NOT NULL,
    final_url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT '',
    etag TEXT NOT NULL DEFAULT '',
    last_modified TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    current_snapshot_id TEXT,
    knowledge_source_id TEXT REFERENCES knowledge_sources(source_id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('ready', 'unchanged', 'failed')),
    last_error TEXT NOT NULL DEFAULT '',
    last_fetched_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_id, canonical_url)
);

CREATE INDEX IF NOT EXISTS ix_crawl_pages_tenant
    ON crawl_pages(tenant_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS crawl_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    page_id TEXT NOT NULL REFERENCES crawl_pages(page_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    fetched_at TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    text_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    rendered INTEGER NOT NULL DEFAULT 0 CHECK (rendered IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_crawl_snapshots_page
    ON crawl_snapshots(page_id, created_at DESC);

CREATE TABLE IF NOT EXISTS crawl_records (
    record_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES crawl_snapshots(snapshot_id) ON DELETE CASCADE,
    page_id TEXT NOT NULL REFERENCES crawl_pages(page_id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES crawl_sources(source_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    template_name TEXT NOT NULL,
    data_json TEXT NOT NULL,
    extraction_method TEXT NOT NULL CHECK (extraction_method IN ('rules', 'model', 'mixed')),
    model_run_id TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_crawl_records_source
    ON crawl_records(source_id, created_at DESC);

CREATE TABLE IF NOT EXISTS crawl_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES crawl_runs(run_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_crawl_events_run
    ON crawl_events(run_id, event_id);
"""
