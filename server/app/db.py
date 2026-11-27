from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


async def offload(fn, *args):
    """Run a blocking call (normally a SQLite read) on a worker thread.

    ``Database`` owns one ``sqlite3`` connection guarded by ``Database.lock``.
    Every public read/write serialises on that re-entrant lock, so a worker
    thread sees exactly the same connection, transaction and read-your-writes
    visibility as the event-loop thread would.  A worker never awaits the
    event loop, therefore moving work here cannot deadlock it; the loop only
    waits for the returned result.
    """
    return await asyncio.to_thread(fn, *args)


async def offload_write(fn, *args):
    """Offload a durable write without abandoning it on cancellation.

    ``asyncio.to_thread`` cannot interrupt a worker that has already started.
    If a cancelled actor simply unwound, an in-flight ``UPDATE runs SET
    status='running'`` could commit after the actor's terminal transaction and
    resurrect a finished run, or a late ``message.delta`` could land after
    ``run.completed``.  This helper waits for the already-started write to
    settle before propagating the cancellation, so transaction order and
    durability are preserved while task cancellation still takes effect.

    A cancellation does not hide a real failure: if the worker itself raised
    while the caller was cancelling, that exception is what the caller sees,
    with the cancellation kept as its cause.  Silently swallowing it would
    turn a failed (or half-applied) transaction into an apparently cancelled
    request.
    """
    future = asyncio.get_running_loop().run_in_executor(None, fn, *args)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError as cancelled:
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            raise exc from cancelled
        raise


async def shielded(coro):
    """Run a coroutine to completion even if its caller is cancelled.

    Used for the ``prepare_run`` -> ``submit`` hand-off.  ``prepare_run``
    durably creates the run and claims ``conversations.active_run_id``; if the
    caller were cancelled between that commit and the submit, the run would
    stay ``queued`` and keep the conversation busy for the rest of the process
    lifetime.  The inner task keeps running after the caller is cancelled, and
    a real failure from it is surfaced instead of the cancellation.
    """
    task = asyncio.ensure_future(coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            raise exc from cancelled
        raise


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return uuid.uuid4().hex


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS capability_settings(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, config TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capability_settings_audit(revision INTEGER PRIMARY KEY, config TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prompt_safety_policy(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, config TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prompt_safety_user_policy(user_id TEXT PRIMARY KEY REFERENCES users(id), revision INTEGER NOT NULL, config TEXT, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL REFERENCES users(id));
CREATE TABLE IF NOT EXISTS prompt_safety_user_history(user_id TEXT NOT NULL REFERENCES users(id), revision INTEGER NOT NULL, config TEXT, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL REFERENCES users(id), PRIMARY KEY(user_id,revision));
CREATE TABLE IF NOT EXISTS prompt_safety_activity(id INTEGER PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), actor_id TEXT REFERENCES users(id), operation TEXT NOT NULL, status TEXT NOT NULL, code TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS prompt_safety_activity_time ON prompt_safety_activity(created_at,id);
CREATE INDEX IF NOT EXISTS prompt_safety_activity_owner ON prompt_safety_activity(user_id,created_at,id);
CREATE TABLE IF NOT EXISTS registration_attempts(id TEXT PRIMARY KEY, source_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS registration_attempts_time ON registration_attempts(created_at);
CREATE INDEX IF NOT EXISTS registration_attempts_source ON registration_attempts(source_hash,created_at);
CREATE TABLE IF NOT EXISTS model_usage_calls(id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), kind TEXT NOT NULL, model_id TEXT NOT NULL, model_label TEXT NOT NULL, status TEXT NOT NULL, usage TEXT, created_at TEXT NOT NULL, completed_at TEXT);
CREATE INDEX IF NOT EXISTS model_usage_owner ON model_usage_calls(user_id,created_at);
CREATE INDEX IF NOT EXISTS model_usage_time_model ON model_usage_calls(created_at,model_id);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS connections(id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, auth_type TEXT NOT NULL, secret TEXT, enabled INTEGER NOT NULL, status TEXT NOT NULL, tools TEXT NOT NULL DEFAULT '[]', last_error TEXT, last_tested_at TEXT, revision INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS mcp_oauth_pending(
    state_hash TEXT PRIMARY KEY, connection_id TEXT UNIQUE NOT NULL REFERENCES connections(id),
    revision INTEGER NOT NULL, user_id TEXT NOT NULL REFERENCES users(id), session_hash TEXT NOT NULL,
    profile_digest TEXT NOT NULL, verifier TEXT NOT NULL, endpoints TEXT NOT NULL, expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_oauth_grants(
    connection_id TEXT PRIMARY KEY REFERENCES connections(id), profile_digest TEXT NOT NULL,
    endpoints TEXT NOT NULL, refresh_token TEXT, expires INTEGER NOT NULL,
    refresh_owner TEXT, refresh_until INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mcp_oauth_events(
    id TEXT PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES connections(id),
    user_id TEXT NOT NULL REFERENCES users(id), event TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assistants(id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, instructions TEXT NOT NULL, enabled INTEGER NOT NULL, access TEXT NOT NULL, connections TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), assistant_id TEXT NOT NULL REFERENCES assistants(id), title TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0, active_run_id TEXT, sdk_session_id TEXT, sdk_cost_total REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS message_queue(sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, conversation_id TEXT NOT NULL REFERENCES conversations(id), payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'pending', run_id TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(conversation_id,id));
CREATE TABLE IF NOT EXISTS conversation_interactions(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), kind TEXT NOT NULL, prompt TEXT NOT NULL, options TEXT NOT NULL, status TEXT NOT NULL, answer TEXT, answered_by TEXT, created_at TEXT NOT NULL, answered_at TEXT);
CREATE INDEX IF NOT EXISTS conversation_interactions_run ON conversation_interactions(run_id,status,created_at);
CREATE TABLE IF NOT EXISTS run_steers(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),request_id TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(run_id,request_id));
CREATE TABLE IF NOT EXISTS conversation_fork_attachments(
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id), native_epoch INTEGER NOT NULL,
    receipts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_forks(conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),inherited_through_id TEXT NOT NULL REFERENCES messages(id));
CREATE INDEX IF NOT EXISTS message_queue_pending ON message_queue(conversation_id,sequence) WHERE status='pending';
CREATE TABLE IF NOT EXISTS conversation_schedules(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id), text TEXT NOT NULL, scheduled_at TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, error TEXT);
CREATE INDEX IF NOT EXISTS conversation_schedules_due ON conversation_schedules(status,scheduled_at);
CREATE TABLE IF NOT EXISTS conversation_schedule_series(
    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
    text TEXT NOT NULL, next_at TEXT NOT NULL, recurrence TEXT NOT NULL,
    timezone TEXT NOT NULL, local_time TEXT NOT NULL, execution_mode TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled', created_at TEXT NOT NULL,
    skipped_count INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE INDEX IF NOT EXISTS conversation_schedule_series_due ON conversation_schedule_series(status,next_at);
CREATE TABLE IF NOT EXISTS conversation_schedule_run_requests(
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    request_id TEXT NOT NULL, plan_id TEXT NOT NULL,
    expected_token TEXT NOT NULL,
    occurrence_id TEXT NOT NULL UNIQUE REFERENCES conversation_schedules(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(conversation_id,request_id)
);
CREATE INDEX IF NOT EXISTS conversation_schedule_run_requests_plan ON conversation_schedule_run_requests(conversation_id,plan_id,created_at);
CREATE TABLE IF NOT EXISTS conversation_schedule_create_requests(
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    approval_id TEXT NOT NULL UNIQUE REFERENCES approvals(id),
    series_id TEXT NOT NULL UNIQUE REFERENCES conversation_schedule_series(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_schedule_edit_requests(
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    approval_id TEXT NOT NULL UNIQUE REFERENCES approvals(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    plan_id TEXT NOT NULL, operation TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_feedback(
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id), run_id TEXT REFERENCES runs(id),
    message_id TEXT REFERENCES messages(id), description TEXT NOT NULL,
    request_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(user_id,request_id)
);
CREATE INDEX IF NOT EXISTS conversation_feedback_recent ON conversation_feedback(created_at,id);
CREATE TABLE IF NOT EXISTS conversation_feedback_images(
    id TEXT PRIMARY KEY, feedback_id TEXT NOT NULL REFERENCES conversation_feedback(id) ON DELETE CASCADE,
    mime TEXT NOT NULL, content BLOB NOT NULL, size INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_draft_expiry(file_id TEXT PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE, expires_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chat_draft_expiry_due ON chat_draft_expiry(expires_at);
CREATE TABLE IF NOT EXISTS chat_draft_upload_requests(user_id TEXT NOT NULL REFERENCES users(id),request_id TEXT NOT NULL,file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,content_digest TEXT NOT NULL,PRIMARY KEY(user_id,request_id));
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id), role TEXT NOT NULL, text TEXT NOT NULL, file_ids TEXT NOT NULL DEFAULT '[]', run_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id), user_id TEXT NOT NULL REFERENCES users(id), client_request_id TEXT NOT NULL, message_id TEXT NOT NULL, assistant_message_id TEXT, status TEXT NOT NULL, error TEXT, usage TEXT, created_at TEXT NOT NULL, completed_at TEXT, UNIQUE(conversation_id,client_request_id));
CREATE TABLE IF NOT EXISTS events(run_id TEXT NOT NULL REFERENCES runs(id), id INTEGER NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(run_id,id));
CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), user_id TEXT NOT NULL, tool_name TEXT NOT NULL, arguments TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, subagent_child_id TEXT);
CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, mime_type TEXT NOT NULL, size INTEGER NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL, conversation_id TEXT);
CREATE TABLE IF NOT EXISTS raw_events(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS conversations_owner ON conversations(user_id,updated_at);
CREATE TABLE IF NOT EXISTS conversation_permission_changes(
    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
    actor_id TEXT NOT NULL REFERENCES users(id), prior_mode TEXT NOT NULL, mode TEXT NOT NULL,
    changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_read_only_certifications(
    assistant_id TEXT NOT NULL REFERENCES assistants(id), assistant_revision INTEGER NOT NULL,
    connection_id TEXT NOT NULL REFERENCES connections(id), tool_name TEXT NOT NULL,
    confirmation_digest TEXT NOT NULL, admin_id TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY(assistant_id, connection_id, tool_name)
);
CREATE TABLE IF NOT EXISTS connection_read_only_review_audit(
    id TEXT PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES connections(id),
    connection_revision INTEGER NOT NULL, review_revision INTEGER NOT NULL,
    reviewer_id TEXT NOT NULL REFERENCES users(id), certifications TEXT NOT NULL,
    reviewed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id,created_at);
CREATE INDEX IF NOT EXISTS runs_owner ON runs(user_id,created_at);
CREATE INDEX IF NOT EXISTS runs_conversation ON runs(conversation_id,created_at);
CREATE INDEX IF NOT EXISTS runs_conversation_message ON runs(conversation_id,message_id,created_at);
CREATE INDEX IF NOT EXISTS runs_time ON runs(created_at,id);
CREATE INDEX IF NOT EXISTS model_usage_time ON model_usage_calls(created_at,id);
CREATE INDEX IF NOT EXISTS tool_events_time ON events(created_at,run_id,id) WHERE type IN ('tool.started','tool.completed','tool.discarded');
CREATE TABLE IF NOT EXISTS memory_scopes(user_id TEXT NOT NULL REFERENCES users(id),assistant_id TEXT NOT NULL REFERENCES assistants(id),revision INTEGER NOT NULL DEFAULT 0,digest TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL,PRIMARY KEY(user_id,assistant_id));
CREATE TABLE IF NOT EXISTS subagent_children(
    child_id TEXT PRIMARY KEY,
    parent_run_id TEXT NOT NULL REFERENCES runs(id),
    root_run_id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    depth INTEGER NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    stop_reason TEXT,
    task_digest TEXT NOT NULL,
    capability_snapshot TEXT NOT NULL,
    workspace_id TEXT NOT NULL UNIQUE,
    memory_scope_id TEXT NOT NULL UNIQUE,
    reserved_budget REAL NOT NULL DEFAULT 0,
    observed_usage TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    cancelled_at TEXT,
    result_json TEXT,
    display_title_key TEXT
);
CREATE INDEX IF NOT EXISTS subagent_children_parent ON subagent_children(parent_run_id,created_at);
CREATE INDEX IF NOT EXISTS subagent_children_root ON subagent_children(root_run_id,created_at);
CREATE INDEX IF NOT EXISTS subagent_children_status ON subagent_children(status);
CREATE TABLE IF NOT EXISTS model_approval_policies(
    assistant_id TEXT PRIMARY KEY REFERENCES assistants(id),
    enabled INTEGER NOT NULL DEFAULT 0,
    reviewer_mode TEXT NOT NULL DEFAULT 'synthetic',
    model_id TEXT NOT NULL DEFAULT 'synthetic-policy-v1',
    allowed_tools TEXT NOT NULL DEFAULT '[]',
    allowed_data_scopes TEXT NOT NULL DEFAULT '[]',
    max_amount REAL NOT NULL DEFAULT 0,
    allowed_operations TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
CREATE INDEX IF NOT EXISTS model_approval_policies_enabled ON model_approval_policies(enabled);
CREATE TABLE IF NOT EXISTS high_risk_approval_policies(
    assistant_id TEXT PRIMARY KEY REFERENCES assistants(id),
    enabled INTEGER NOT NULL DEFAULT 0,
    policy_id TEXT NOT NULL,
    allowed_tools TEXT NOT NULL DEFAULT '[]',
    admin_allowlist TEXT NOT NULL DEFAULT '[]',
    required_approvals INTEGER NOT NULL DEFAULT 2,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
CREATE INDEX IF NOT EXISTS high_risk_approval_policies_enabled ON high_risk_approval_policies(enabled);
CREATE TABLE IF NOT EXISTS approval_votes(
    approval_id TEXT NOT NULL REFERENCES approvals(id),
    voter_id TEXT NOT NULL REFERENCES users(id),
    decision TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY(approval_id,voter_id)
);
CREATE INDEX IF NOT EXISTS approval_votes_approval ON approval_votes(approval_id,decided_at);
CREATE TABLE IF NOT EXISTS feishu_chat_bindings(
    chat_id TEXT PRIMARY KEY,
    open_id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS feishu_chat_bindings_user ON feishu_chat_bindings(user_id,updated_at);
CREATE TABLE IF NOT EXISTS feishu_inbound_events(
    event_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    open_id TEXT NOT NULL,
    message_id TEXT,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS feishu_inbound_events_received ON feishu_inbound_events(received_at);
-- Kaiwu intranet connector control plane. These tables intentionally retain
-- only hashes, public keys and operational metadata; device tokens, enrollment
-- codes and relay secrets are never stored in plaintext.
CREATE TABLE IF NOT EXISTS kaiwu_enrollments(
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    tenant_name TEXT NOT NULL,
    code_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    connector_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS kaiwu_enrollments_expiry ON kaiwu_enrollments(expires_at,used_at);
CREATE TABLE IF NOT EXISTS kaiwu_connectors(
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    tenant_name TEXT NOT NULL,
    device_public_key TEXT NOT NULL,
    device_key_id TEXT NOT NULL,
    device_token_hash TEXT NOT NULL UNIQUE,
    relay_token_hash TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    draining INTEGER NOT NULL DEFAULT 0,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS kaiwu_connectors_tenant ON kaiwu_connectors(tenant_id,created_at);
CREATE TABLE IF NOT EXISTS kaiwu_persistent_enrollments(
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    tenant_name TEXT NOT NULL,
    issued_to TEXT NOT NULL,
    code_hash TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS kaiwu_persistent_enrollments_created
    ON kaiwu_persistent_enrollments(created_at DESC,id);
CREATE TABLE IF NOT EXISTS kaiwu_persistent_enrollment_devices(
    connector_id TEXT PRIMARY KEY REFERENCES kaiwu_connectors(id),
    enrollment_id TEXT NOT NULL REFERENCES kaiwu_persistent_enrollments(id),
    bound_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS kaiwu_persistent_enrollment_devices_bound
    ON kaiwu_persistent_enrollment_devices(enrollment_id,bound_at DESC,connector_id);
CREATE TABLE IF NOT EXISTS kaiwu_native_relay_devices(
    connector_id TEXT PRIMARY KEY REFERENCES kaiwu_connectors(id)
);
CREATE TABLE IF NOT EXISTS kaiwu_sources(
    connector_id TEXT NOT NULL REFERENCES kaiwu_connectors(id),
    tenant_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    allowed_tools TEXT NOT NULL,
    tool_schema_sha256 TEXT NOT NULL,
    target_binding_sha256 TEXT NOT NULL,
    transport TEXT NOT NULL,
    stcp_secret TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(connector_id,source_id)
);
CREATE INDEX IF NOT EXISTS kaiwu_sources_tenant ON kaiwu_sources(tenant_id,connector_id);
CREATE TABLE IF NOT EXISTS kaiwu_device_nonces(
    connector_id TEXT NOT NULL REFERENCES kaiwu_connectors(id),
    nonce TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(connector_id,nonce)
);
CREATE INDEX IF NOT EXISTS kaiwu_device_nonces_expiry ON kaiwu_device_nonces(expires_at);
CREATE TABLE IF NOT EXISTS kaiwu_heartbeats(
    connector_id TEXT PRIMARY KEY REFERENCES kaiwu_connectors(id),
    state TEXT NOT NULL,
    connector_version TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source_ids TEXT NOT NULL,
    last_error TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kaiwu_control_audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    connector_id TEXT,
    source_id TEXT,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    trace_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS kaiwu_control_audit_connector ON kaiwu_control_audit(connector_id,id);
CREATE INDEX IF NOT EXISTS kaiwu_control_audit_tenant ON kaiwu_control_audit(tenant_id,id);
-- Enterprise integration credentials are hashed at rest. A route is the
-- loopback visitor listener paired with one connector source.
CREATE TABLE IF NOT EXISTS kaiwu_integrations(
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    connector_id TEXT NOT NULL REFERENCES kaiwu_connectors(id),
    display_name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    token_prefix TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    rotated_at TEXT,
    revoked_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS kaiwu_integrations_tenant ON kaiwu_integrations(tenant_id,created_at);
CREATE INDEX IF NOT EXISTS kaiwu_integrations_connector ON kaiwu_integrations(connector_id,created_at);
CREATE TABLE IF NOT EXISTS kaiwu_integration_sources(
    integration_id TEXT NOT NULL REFERENCES kaiwu_integrations(id),
    connector_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    PRIMARY KEY(integration_id,connector_id,source_id),
    FOREIGN KEY(connector_id,source_id) REFERENCES kaiwu_sources(connector_id,source_id)
);
CREATE INDEX IF NOT EXISTS kaiwu_integration_sources_source ON kaiwu_integration_sources(connector_id,source_id);
CREATE TABLE IF NOT EXISTS kaiwu_platform_handoffs(
    ticket_hash TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    connector_id TEXT NOT NULL REFERENCES kaiwu_connectors(id),
    source_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    redeemed_by TEXT REFERENCES users(id),
    imported_connection_id TEXT REFERENCES connections(id),
    FOREIGN KEY(connector_id,source_id) REFERENCES kaiwu_sources(connector_id,source_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS kaiwu_platform_handoffs_expiry ON kaiwu_platform_handoffs(expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS kaiwu_platform_handoffs_pending ON kaiwu_platform_handoffs(connector_id,source_id) WHERE redeemed_by IS NULL;
CREATE TABLE IF NOT EXISTS kaiwu_assist_sessions(
    id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL REFERENCES kaiwu_connectors(id),
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','active','stopped','expired')),
    expires_at TEXT NOT NULL,
    -- 'legacy' keeps the original 24h absolute + 30min sliding lease. A
    -- 'week_168h' session has one fixed deadline counted from local consent
    -- (consent_expires_at) and is never extended by polling.
    consent_policy TEXT NOT NULL DEFAULT 'legacy',
    consent_expires_at TEXT,
    last_poll_at TEXT,
    discovered_tools TEXT NOT NULL DEFAULT '[]',
    imported_connection_id TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS kaiwu_assist_device_live
    ON kaiwu_assist_sessions(connector_id) WHERE status IN ('pending','active');
CREATE TABLE IF NOT EXISTS kaiwu_assist_jobs(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL REFERENCES kaiwu_assist_sessions(id),
    action TEXT NOT NULL CHECK(action IN ('discover','connect','command')),
    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
    selected_tools TEXT NOT NULL DEFAULT '[]',
    sealed_bearer TEXT,
    command TEXT,
    timeout_seconds INTEGER NOT NULL DEFAULT 60,
    result TEXT,
    source_revision INTEGER,
    expires_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS kaiwu_assist_jobs_session ON kaiwu_assist_jobs(session_id,seq DESC);
CREATE UNIQUE INDEX IF NOT EXISTS kaiwu_assist_job_live
    ON kaiwu_assist_jobs(session_id) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS kaiwu_assist_workbench_imports(
    session_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    connection_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kaiwu_edge_routes(
    connector_id TEXT NOT NULL REFERENCES kaiwu_connectors(id),
    source_id TEXT NOT NULL,
    visitor_host TEXT NOT NULL,
    visitor_port INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(connector_id,source_id),
    FOREIGN KEY(connector_id,source_id) REFERENCES kaiwu_sources(connector_id,source_id),
    UNIQUE(visitor_port)
);
CREATE INDEX IF NOT EXISTS kaiwu_edge_routes_source ON kaiwu_edge_routes(source_id);
-- Customer diagnostics: privacy-bounded operational events. There is no
-- free-text column; message text is derived from the fixed catalog at read
-- time and never stored from the wire. Identity columns are always taken from
-- the authenticated connector, never from the event body.
CREATE TABLE IF NOT EXISTS kaiwu_diagnostic_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    connector_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    level TEXT NOT NULL,
    component TEXT NOT NULL,
    event_code TEXT NOT NULL,
    message_code TEXT NOT NULL,
    operation_id TEXT,
    trace_id TEXT,
    source_id TEXT,
    tool_name TEXT,
    http_status INTEGER,
    rpc_error_code TEXT,
    duration_ms INTEGER,
    attempt INTEGER,
    install_attempt_id TEXT,
    install_seq INTEGER,
    install_stage TEXT,
    install_outcome TEXT,
    install_code TEXT,
    origin TEXT NOT NULL DEFAULT 'connector' CHECK(origin IN ('connector','edge')),
    received_at TEXT NOT NULL,
    UNIQUE(tenant_id,connector_id,event_id)
);
CREATE INDEX IF NOT EXISTS kaiwu_diagnostic_events_connector_time
    ON kaiwu_diagnostic_events(connector_id,received_at,id);
CREATE INDEX IF NOT EXISTS kaiwu_diagnostic_events_tenant_time
    ON kaiwu_diagnostic_events(tenant_id,received_at,id);
CREATE INDEX IF NOT EXISTS kaiwu_diagnostic_events_time
    ON kaiwu_diagnostic_events(occurred_at,id);
CREATE INDEX IF NOT EXISTS kaiwu_diagnostic_events_source
    ON kaiwu_diagnostic_events(connector_id,source_id,received_at);
CREATE INDEX IF NOT EXISTS kaiwu_diagnostic_events_trace
    ON kaiwu_diagnostic_events(trace_id,received_at);
CREATE INDEX IF NOT EXISTS kaiwu_diagnostic_events_level
    ON kaiwu_diagnostic_events(level,received_at);
CREATE INDEX IF NOT EXISTS kaiwu_diagnostic_events_code
    ON kaiwu_diagnostic_events(event_code,received_at);
-- Goal execution pilot: the owner's explicit budget approval and the host
-- round lease. These tables never store model output or business data.
-- Rounds are a separate limit from token, cost and wall-clock budgets.
CREATE TABLE IF NOT EXISTS goal_execution_authorizations(
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    goal_id TEXT NOT NULL,
    goal_revision INTEGER NOT NULL,
    harness_session_id TEXT NOT NULL,
    native_rounds_started INTEGER NOT NULL,
    native_max_rounds INTEGER NOT NULL,
    assistant_revision INTEGER NOT NULL,
    permission_mode TEXT NOT NULL,
    max_rounds INTEGER NOT NULL,
    token_budget INTEGER NOT NULL,
    cost_budget_micros INTEGER NOT NULL,
    time_budget_seconds INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','revoked')),
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS goal_execution_authorizations_active
    ON goal_execution_authorizations(conversation_id) WHERE status='active';
CREATE INDEX IF NOT EXISTS goal_execution_authorizations_owner
    ON goal_execution_authorizations(conversation_id,created_at DESC);
CREATE TABLE IF NOT EXISTS goal_execution_leases(
    id TEXT PRIMARY KEY,
    authorization_id TEXT NOT NULL REFERENCES goal_execution_authorizations(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    run_id TEXT NOT NULL REFERENCES runs(id),
    goal_id TEXT NOT NULL,
    goal_revision INTEGER NOT NULL,
    harness_session_id TEXT NOT NULL,
    round_index INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('admitted','settled','aborted')),
    admitted_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    usage_status TEXT CHECK(usage_status IS NULL OR usage_status IN ('complete','unknown')),
    total_tokens INTEGER,
    cost_micros INTEGER,
    abort_reason TEXT,
    error_code TEXT,
    UNIQUE(authorization_id,round_index)
);
CREATE UNIQUE INDEX IF NOT EXISTS goal_execution_leases_active
    ON goal_execution_leases(conversation_id) WHERE state='admitted';
CREATE INDEX IF NOT EXISTS goal_execution_leases_authorization
    ON goal_execution_leases(authorization_id,round_index);
CREATE INDEX IF NOT EXISTS goal_execution_leases_run
    ON goal_execution_leases(run_id);
"""


class Database:
    def __init__(self, path: Path, synchronous: str = "FULL"):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        mode = str(synchronous or "FULL").strip().upper()
        if mode not in {"FULL", "NORMAL"}:
            # OFF disables fsync entirely and is never an acceptable trial or
            # deployment setting; NORMAL remains an explicitly diagnostic mode.
            raise ValueError("sqlite synchronous must be FULL or NORMAL; OFF is not permitted")
        self.conn.execute(f"PRAGMA synchronous={mode}")
        self.conn.execute("PRAGMA busy_timeout=5000")
        # Schema creation is a bootstrap migration, not request traffic.  Each
        # statement used to autocommit, and with synchronous=FULL every commit
        # fsyncs; on the school-cloud data disk that measured ~18 s per fresh
        # database.  One transaction keeps FULL durability and still makes the
        # whole migration atomic, so a crash leaves either no schema or all of
        # it rather than a half-applied one.
        self.conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
        # Additive column migrations are one batch as well: a fresh database
        # needs dozens of ALTERs, and each autocommit would fsync the same
        # migrated schema separately.  executescript() commits a pending
        # transaction before it runs, so COMMIT before every script.
        self.conn.execute("BEGIN IMMEDIATE")
        assist_job_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(kaiwu_assist_jobs)")
        }
        if "command" not in assist_job_columns:
            # SQLite cannot widen a CHECK constraint in place. Rebuild the one
            # live job table so command jobs are accepted by databases that
            # predate remote CMD assistance. No other table references it.
            # Keep every statement on the open transaction: executescript()
            # would commit first and could leave a half-rebuilt table.
            self.conn.execute(
                "CREATE TABLE kaiwu_assist_jobs_new("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                "id TEXT NOT NULL UNIQUE,"
                "session_id TEXT NOT NULL REFERENCES kaiwu_assist_sessions(id),"
                "action TEXT NOT NULL CHECK(action IN ('discover','connect','command')),"
                "status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),"
                "selected_tools TEXT NOT NULL DEFAULT '[]',"
                "sealed_bearer TEXT,"
                "command TEXT,"
                "timeout_seconds INTEGER NOT NULL DEFAULT 60,"
                "result TEXT,"
                "source_revision INTEGER,"
                "expires_at TEXT NOT NULL,"
                "finished_at TEXT"
                ")"
            )
            self.conn.execute(
                "INSERT INTO kaiwu_assist_jobs_new"
                "(seq,id,session_id,action,status,selected_tools,sealed_bearer,result,"
                "source_revision,expires_at,finished_at,command,timeout_seconds) "
                "SELECT seq,id,session_id,action,status,selected_tools,sealed_bearer,result,"
                "source_revision,expires_at,finished_at,NULL,60 FROM kaiwu_assist_jobs"
            )
            self.conn.execute("DROP TABLE kaiwu_assist_jobs")
            self.conn.execute("ALTER TABLE kaiwu_assist_jobs_new RENAME TO kaiwu_assist_jobs")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS kaiwu_assist_jobs_session "
                "ON kaiwu_assist_jobs(session_id,seq DESC)"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS kaiwu_assist_job_live "
                "ON kaiwu_assist_jobs(session_id) WHERE status IN ('queued','running')"
            )
        elif "timeout_seconds" not in assist_job_columns:
            self.conn.execute(
                "ALTER TABLE kaiwu_assist_jobs ADD COLUMN timeout_seconds INTEGER NOT NULL DEFAULT 60"
            )
        # Consent policy is additive and idempotent. Existing rows keep the
        # original 24h absolute + 30min sliding semantics; only a session that
        # explicitly negotiates the week policy gets a fixed consent deadline.
        assist_session_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(kaiwu_assist_sessions)")
        }
        if "consent_policy" not in assist_session_columns:
            self.conn.execute(
                "ALTER TABLE kaiwu_assist_sessions ADD COLUMN consent_policy TEXT NOT NULL DEFAULT 'legacy'"
            )
        if "consent_expires_at" not in assist_session_columns:
            self.conn.execute(
                "ALTER TABLE kaiwu_assist_sessions ADD COLUMN consent_expires_at TEXT"
            )
        connection_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(connections)")}
        if "read_only_certifications" not in connection_columns:
            self.conn.execute("ALTER TABLE connections ADD COLUMN read_only_certifications TEXT NOT NULL DEFAULT '{}'")
        if "read_only_review_revision" not in connection_columns:
            self.conn.execute("ALTER TABLE connections ADD COLUMN read_only_review_revision INTEGER NOT NULL DEFAULT 0")
        schedule_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(conversation_schedules)")}
        if "execution_mode" not in schedule_columns:
            self.conn.execute("ALTER TABLE conversation_schedules ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'text'")
        if "series_id" not in schedule_columns:
            self.conn.execute("ALTER TABLE conversation_schedules ADD COLUMN series_id TEXT")
        self.conn.execute("CREATE INDEX IF NOT EXISTS conversation_schedules_series ON conversation_schedules(series_id,created_at)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS conversation_schedules_catalog ON conversation_schedules(conversation_id,created_at DESC,id DESC)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS conversation_schedule_series_catalog ON conversation_schedule_series(conversation_id,created_at DESC,id DESC)")
        series_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(conversation_schedule_series)")}
        if "rule_expression" not in series_columns:
            self.conn.execute("ALTER TABLE conversation_schedule_series ADD COLUMN rule_expression TEXT")
        if "coalesced_batches" not in series_columns:
            self.conn.execute("ALTER TABLE conversation_schedule_series ADD COLUMN coalesced_batches INTEGER NOT NULL DEFAULT 0")
        if "acknowledged_occurrence_id" not in series_columns:
            self.conn.execute("ALTER TABLE conversation_schedule_series ADD COLUMN acknowledged_occurrence_id TEXT")

        feedback_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(conversation_feedback)")}
        if "resolution_note" not in feedback_columns:
            self.conn.execute("ALTER TABLE conversation_feedback ADD COLUMN resolution_note TEXT NOT NULL DEFAULT ''")
        for column in ("notification_version", "reporter_seen_version"):
            if column not in feedback_columns:
                self.conn.execute(f"ALTER TABLE conversation_feedback ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        self.conn.execute("CREATE INDEX IF NOT EXISTS conversation_feedback_owner_recent ON conversation_feedback(user_id,updated_at,id)")

        diagnostic_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(kaiwu_diagnostic_events)")
        }
        if "origin" not in diagnostic_columns:
            self.conn.execute(
                "ALTER TABLE kaiwu_diagnostic_events "
                "ADD COLUMN origin TEXT NOT NULL DEFAULT 'connector'"
            )
        # Create the origin index only after the additive migration above, so
        # an intermediate database that predates the column still upgrades.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS kaiwu_diagnostic_events_origin "
            "ON kaiwu_diagnostic_events(origin,connector_id,received_at)"
        )
        for column, kind in (
            ("install_attempt_id", "TEXT"), ("install_seq", "INTEGER"),
            ("install_stage", "TEXT"), ("install_outcome", "TEXT"), ("install_code", "TEXT"),
        ):
            if column not in diagnostic_columns:
                self.conn.execute(f"ALTER TABLE kaiwu_diagnostic_events ADD COLUMN {column} {kind}")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS kaiwu_diagnostic_events_install_attempt "
            "ON kaiwu_diagnostic_events(install_attempt_id,id) WHERE install_attempt_id IS NOT NULL"
        )
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(runs)")}
        if "execution_meta" not in columns:
            self.conn.execute("ALTER TABLE runs ADD COLUMN execution_meta TEXT")
        if "kind" not in columns:
            self.conn.execute("ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat'")
        if "compaction" not in columns:
            self.conn.execute("ALTER TABLE runs ADD COLUMN compaction TEXT")
        conversation_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(conversations)")}
        if "context_usage" not in conversation_columns:
            self.conn.execute("ALTER TABLE conversations ADD COLUMN context_usage TEXT")
        for column in ("model_id", "model_label", "reasoning_level", "model_snapshot"):
            if column not in conversation_columns:
                self.conn.execute(f"ALTER TABLE conversations ADD COLUMN {column} TEXT")
        if "auto_model_routing" not in conversation_columns:
            # Existing sessions were explicitly pinned before task-submit
            # routing existed; keep their behavior unchanged. New sessions
            # opt in through the create path below.
            self.conn.execute("ALTER TABLE conversations ADD COLUMN auto_model_routing INTEGER NOT NULL DEFAULT 0")
        if "auto_title_pending" not in conversation_columns:
            # Existing titles have no provenance and stay as the user saw them.
            self.conn.execute("ALTER TABLE conversations ADD COLUMN auto_title_pending INTEGER NOT NULL DEFAULT 0")
        approval_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(approvals)")}
        for column, definition in (("decided_at", "TEXT"), ("decided_by", "TEXT"), ("decision_source", "TEXT"),
                                   ("strategy", "TEXT NOT NULL DEFAULT 'user'"), ("review_model_id", "TEXT"),
                                   ("review_policy_id", "TEXT"), ("review_policy_revision", "INTEGER"),
                                   ("review_policy_digest", "TEXT"), ("review_operation", "TEXT"),
                                   ("review_data_scope", "TEXT"), ("review_amount", "REAL"),
                                   ("review_reason", "TEXT"), ("review_result", "TEXT"),
                                   ("review_decided_at", "TEXT"), ("approval_mode", "TEXT NOT NULL DEFAULT 'single'"),
                                   ("required_approvals", "INTEGER NOT NULL DEFAULT 1"),
                                   ("approver_allowlist", "TEXT NOT NULL DEFAULT '[]'"),
                                   ("approver_ids", "TEXT NOT NULL DEFAULT '[]'"),
                                   ("approval_policy_id", "TEXT"), ("approval_policy_revision", "INTEGER"),
                                   ("approval_policy_digest", "TEXT"), ("approval_request_digest", "TEXT"),
                                   ("approval_decision_reason", "TEXT"), ("subagent_child_id", "TEXT")):
            if column not in approval_columns:
                self.conn.execute(f"ALTER TABLE approvals ADD COLUMN {column} {definition}")
        additions = {
            "connections": {"owner_id": "TEXT", "scope": "TEXT NOT NULL DEFAULT 'team'", "source_metadata": "TEXT NOT NULL DEFAULT '{}'"},
            "assistants": {"owner_id": "TEXT", "scope": "TEXT NOT NULL DEFAULT 'team'", "knowledge_base_ids": "TEXT NOT NULL DEFAULT '[]'", "skill_ids": "TEXT NOT NULL DEFAULT '[]'", "web_search_enabled": "INTEGER NOT NULL DEFAULT 0", "capabilities": "TEXT NOT NULL DEFAULT '{}'", "default_model_id": "TEXT", "default_reasoning_level": "TEXT", "editor_user_ids": "TEXT NOT NULL DEFAULT '[]'"},
            "conversations": {
                "category": "TEXT NOT NULL DEFAULT ''", "tags": "TEXT NOT NULL DEFAULT '[]'",
                "permission_mode": "TEXT NOT NULL DEFAULT 'manual' CHECK(permission_mode IN ('manual','auto','full'))",
                "permission_mode_by": "TEXT",
                # Upgrade old rows to Claude regardless of the current default.
                # Never reinterpret sdk_session_id as a Harness resume token.
                "runtime_type": "TEXT NOT NULL DEFAULT 'claude' CHECK(runtime_type IN ('claude','harness'))",
                "harness_session_id": "TEXT",
                "native_epoch": "INTEGER NOT NULL DEFAULT 0",
            },
            "messages": {"skills": "TEXT NOT NULL DEFAULT '[]'", "citations": "TEXT NOT NULL DEFAULT '[]'"},
            "subagent_children": {"display_title_key": "TEXT"},
            "runs": {"capability_snapshot": "TEXT", "skill_snapshots": "TEXT NOT NULL DEFAULT '[]'", "timings": "TEXT NOT NULL DEFAULT '{}'", "error_code": "TEXT"},
        }
        for table, additions_for_table in additions.items():
            present = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for column, definition in additions_for_table.items():
                if column not in present:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.conn.execute("COMMIT")
        self.conn.executescript("""BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS skills(id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',instructions TEXT NOT NULL,required_tools TEXT NOT NULL DEFAULT '[]',resources TEXT NOT NULL DEFAULT '{}',owner_id TEXT,scope TEXT NOT NULL DEFAULT 'private',enabled INTEGER NOT NULL DEFAULT 1,revision INTEGER NOT NULL DEFAULT 1,deleted INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS skill_versions(skill_id TEXT NOT NULL,revision INTEGER NOT NULL,snapshot TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(skill_id,revision));
CREATE INDEX IF NOT EXISTS skills_owner ON skills(owner_id,scope);
CREATE TABLE IF NOT EXISTS conversation_memory_scopes(conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),revision INTEGER NOT NULL DEFAULT 0,digest TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL,origin TEXT NOT NULL DEFAULT 'empty');
CREATE TABLE IF NOT EXISTS workflows(
    id TEXT PRIMARY KEY,
    assistant_id TEXT NOT NULL REFERENCES assistants(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    owner_id TEXT,
    scope TEXT NOT NULL DEFAULT 'private',
    revision INTEGER NOT NULL DEFAULT 1,
    draft_version INTEGER NOT NULL DEFAULT 1,
    published_version INTEGER,
    deleted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS workflows_assistant ON workflows(assistant_id,deleted,updated_at);
CREATE TABLE IF NOT EXISTS workflow_versions(
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    graph TEXT NOT NULL,
    graph_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    PRIMARY KEY(workflow_id,version)
);
CREATE INDEX IF NOT EXISTS workflow_versions_status ON workflow_versions(workflow_id,status,version);
CREATE TABLE IF NOT EXISTS workflow_runs(
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    conversation_id TEXT REFERENCES conversations(id),
    version INTEGER NOT NULL,
    graph_hash TEXT NOT NULL,
    permission_digest TEXT NOT NULL,
    permission_snapshot_json TEXT NOT NULL DEFAULT '{}',
    user_id TEXT NOT NULL REFERENCES users(id),
    status TEXT NOT NULL,
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    trace_json TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS workflow_runs_owner ON workflow_runs(user_id,created_at);
CREATE INDEX IF NOT EXISTS workflow_runs_workflow ON workflow_runs(workflow_id,created_at);
COMMIT;""")
        self.conn.execute("BEGIN IMMEDIATE")
        workflow_run_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(workflow_runs)")}
        if "graph_hash" not in workflow_run_columns:
            self.conn.execute("ALTER TABLE workflow_runs ADD COLUMN graph_hash TEXT NOT NULL DEFAULT ''")
        if "permission_digest" not in workflow_run_columns:
            self.conn.execute("ALTER TABLE workflow_runs ADD COLUMN permission_digest TEXT NOT NULL DEFAULT ''")
        if "conversation_id" not in workflow_run_columns:
            self.conn.execute("ALTER TABLE workflow_runs ADD COLUMN conversation_id TEXT REFERENCES conversations(id)")
        if "permission_snapshot_json" not in workflow_run_columns:
            self.conn.execute("ALTER TABLE workflow_runs ADD COLUMN permission_snapshot_json TEXT NOT NULL DEFAULT '{}'")
        # Missing rows belong to conversations predating session-isolated memory.
        # New conversations create their explicit empty scope in the same
        # transaction as the conversation, so a restart never inherits legacy
        # shared memory into a newly created session.
        self.conn.execute("INSERT OR IGNORE INTO conversation_memory_scopes(conversation_id,updated_at,origin) SELECT id,?,'legacy_pending' FROM conversations", (now(),))
        memory_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(conversation_memory_scopes)")}
        for column, definition in (("active_snapshot_id", "TEXT"), ("manifest_digest", "TEXT"),
                                   ("mutation_id", "TEXT"), ("storage_version", "INTEGER NOT NULL DEFAULT 0")):
            if column not in memory_columns:
                self.conn.execute(f"ALTER TABLE conversation_memory_scopes ADD COLUMN {column} {definition}")
        self.conn.execute("COMMIT")
        self.conn.executescript("""BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS memory_snapshot_records(
    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id), user_id TEXT NOT NULL,
    state TEXT NOT NULL, digest TEXT, manifest_digest TEXT, reserved_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_snapshot_conversation ON memory_snapshot_records(conversation_id);
CREATE TABLE IF NOT EXISTS memory_operations(
    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id), user_id TEXT NOT NULL,
    run_id TEXT REFERENCES runs(id), operation_key TEXT NOT NULL, request_digest TEXT NOT NULL,
    binding TEXT NOT NULL, path TEXT, content TEXT, kind TEXT NOT NULL, status TEXT NOT NULL,
    approval_id TEXT, policy_binding TEXT, approval_digest TEXT, snapshot_id TEXT,
    reserved_bytes INTEGER NOT NULL DEFAULT 0, owner_pid INTEGER NOT NULL, owner_start TEXT NOT NULL,
    result TEXT, reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(conversation_id,operation_key)
);
CREATE INDEX IF NOT EXISTS memory_operation_run ON memory_operations(run_id);
CREATE TABLE IF NOT EXISTS workspace_file_versions(
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    path TEXT NOT NULL,
    revision INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY(conversation_id,path)
);
CREATE INDEX IF NOT EXISTS workspace_file_versions_conversation
    ON workspace_file_versions(conversation_id,deleted,path);
CREATE TABLE IF NOT EXISTS workspace_file_version_history(
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    path TEXT NOT NULL,
    revision INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY(conversation_id,path,revision)
);
CREATE TABLE IF NOT EXISTS plugin_registry(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    source_url TEXT NOT NULL,
    license TEXT NOT NULL,
    package_sha256 TEXT NOT NULL,
    declared_permissions TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    review_note TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_by TEXT REFERENCES users(id),
    approved_at TEXT,
    revoked_by TEXT REFERENCES users(id),
    revoked_at TEXT,
    UNIQUE(name,version,package_sha256)
);
CREATE INDEX IF NOT EXISTS plugin_registry_status ON plugin_registry(status,updated_at);
CREATE TABLE IF NOT EXISTS plugin_registry_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plugin_id TEXT NOT NULL REFERENCES plugin_registry(id),
    action TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES users(id),
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS plugin_registry_events_plugin ON plugin_registry_events(plugin_id,id);
CREATE TABLE IF NOT EXISTS plugin_catalog_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plugin_name TEXT,
    action TEXT NOT NULL,
    reason TEXT,
    detail TEXT NOT NULL DEFAULT '',
    actor_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS plugin_catalog_events_created ON plugin_catalog_events(created_at,id);
-- Capability governance: one logical call, one row per actual provider attempt.
-- Credentials are stored only as SecretStore ciphertext; no plaintext column.
CREATE TABLE IF NOT EXISTS capability_dispatch_claims(
    run_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_digest TEXT NOT NULL,
    owner_token TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    PRIMARY KEY(run_id,tool_call_id)
);
CREATE TABLE IF NOT EXISTS capability_calls(
    call_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    assistant_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    started_at TEXT NOT NULL,
    state TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS capability_calls_run ON capability_calls(run_id,started_at);
CREATE TABLE IF NOT EXISTS capability_attempts(
    id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL REFERENCES capability_calls(call_id),
    attempt_index INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    assistant_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_revision INTEGER NOT NULL DEFAULT 1,
    settings_revision INTEGER NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    error_code TEXT,
    bytes_sent INTEGER NOT NULL DEFAULT 0,
    bytes_received INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS capability_attempts_call ON capability_attempts(call_id,attempt_index);
-- One receipt per exact host invocation; a receipt is claimed once and then
-- only its own call may re-verify it. Never select the latest allowed row.
CREATE TABLE IF NOT EXISTS capability_approval_receipts(
    receipt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_digest TEXT NOT NULL,
    decision TEXT NOT NULL,
    source TEXT NOT NULL,
    approval_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    claimed_by_call TEXT,
    claimed_at TEXT,
    UNIQUE(run_id,tool_call_id,tool_name,arguments_digest)
);
CREATE TABLE IF NOT EXISTS capability_run_counters(
    run_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(run_id,capability)
);
CREATE TABLE IF NOT EXISTS capability_day_counters(
    actor_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    day_key TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(actor_id,capability,day_key)
);
CREATE TABLE IF NOT EXISTS capability_credentials(
    provider_id TEXT PRIMARY KEY,
    secret TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS capability_provider_verification(
    provider_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    status TEXT NOT NULL,
    checked_at TEXT,
    provider_revision INTEGER NOT NULL DEFAULT 1,
    settings_revision INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    sample_id TEXT,
    error_code TEXT,
    PRIMARY KEY(provider_id,capability)
);
CREATE TABLE IF NOT EXISTS capability_credential_audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    action TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
COMMIT;""")
        operation_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(memory_operations)")}
        if "old_content_sha256" not in operation_columns:
            self.conn.execute("ALTER TABLE memory_operations ADD COLUMN old_content_sha256 TEXT")
        goal_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(goal_execution_authorizations)")}
        if "assistant_revision" not in goal_columns:
            # A row written by an earlier candidate cannot verify the assistant
            # configuration; revision 0 fails closed until the owner re-approves.
            self.conn.execute("ALTER TABLE goal_execution_authorizations "
                              "ADD COLUMN assistant_revision INTEGER NOT NULL DEFAULT 0")

    def one(self, query, values=()):
        with self.lock:
            row = self.conn.execute(query, values).fetchone()
            return dict(row) if row else None

    def all(self, query, values=()):
        with self.lock:
            return [dict(row) for row in self.conn.execute(query, values).fetchall()]

    def execute(self, query, values=()):
        with self.lock:
            return self.conn.execute(query, values)

    def executemany(self, query, values):
        with self.lock:
            return self.conn.executemany(query, values)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def event(self, run_id: str, kind: str, data: dict) -> dict:
        with self.lock:
            if self.conn.in_transaction:
                return self._event(run_id, kind, data)
            with self.transaction():
                return self._event(run_id, kind, data)

    def _event(self, run_id: str, kind: str, data: dict) -> dict:
            index = self.one("SELECT COALESCE(MAX(id),0)+1 n FROM events WHERE run_id=?", (run_id,))["n"]
            event = {"id": index, "run_id": run_id, "type": kind, "created_at": now(), "data": data}
            self.execute("INSERT INTO events VALUES(?,?,?,?,?)", (run_id, index, kind, dump(data), event["created_at"]))
            return event

    def events(self, run_id: str, after: int):
        return [{**row, "data": json.loads(row["data"])} for row in self.all("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after))]

    def close(self):
        self.conn.close()
