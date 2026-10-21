"""Additive schema for durable agent execution."""

AGENT_RUN_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS script_input_links (
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    pending_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result_json TEXT,
    successor_id TEXT,
    PRIMARY KEY(tenant_id, run_id)
);
CREATE INDEX IF NOT EXISTS ix_script_input_successor ON script_input_links(tenant_id, successor_id);
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL,
    parent_run_id TEXT REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'queued','running','waiting_approval','waiting_input','paused','succeeded','failed','cancelled'
    )),
    state_json TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_run_subscriptions (
    run_id TEXT PRIMARY KEY REFERENCES agent_runs(run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS agent_run_publications (
    run_id TEXT PRIMARY KEY REFERENCES agent_runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_agent_runs_queue ON agent_runs(status, updated_at);
CREATE INDEX IF NOT EXISTS ix_agent_runs_scope ON agent_runs(tenant_id, conversation_id);
CREATE TABLE IF NOT EXISTS agent_run_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_agent_run_events ON agent_run_events(run_id, event_id);
CREATE TABLE IF NOT EXISTS agent_run_steps (
    run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    step_key TEXT NOT NULL,
    status TEXT NOT NULL,
    call_json TEXT NOT NULL,
    result_json TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY(run_id, step_key)
);
CREATE TABLE IF NOT EXISTS agent_run_approvals (
    approval_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    expires_at REAL NOT NULL,
    payload_json TEXT NOT NULL,
    resolved_by INTEGER,
    resolved_at REAL
);
"""
