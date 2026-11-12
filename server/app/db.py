from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return uuid.uuid4().hex


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS connections(id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, auth_type TEXT NOT NULL, secret TEXT, enabled INTEGER NOT NULL, status TEXT NOT NULL, tools TEXT NOT NULL DEFAULT '[]', last_error TEXT, last_tested_at TEXT, revision INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS assistants(id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, instructions TEXT NOT NULL, enabled INTEGER NOT NULL, access TEXT NOT NULL, connections TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), assistant_id TEXT NOT NULL REFERENCES assistants(id), title TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0, active_run_id TEXT, sdk_session_id TEXT, sdk_cost_total REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id), role TEXT NOT NULL, text TEXT NOT NULL, file_ids TEXT NOT NULL DEFAULT '[]', run_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id), user_id TEXT NOT NULL REFERENCES users(id), client_request_id TEXT NOT NULL, message_id TEXT NOT NULL, assistant_message_id TEXT, status TEXT NOT NULL, error TEXT, usage TEXT, created_at TEXT NOT NULL, completed_at TEXT, UNIQUE(conversation_id,client_request_id));
CREATE TABLE IF NOT EXISTS events(run_id TEXT NOT NULL REFERENCES runs(id), id INTEGER NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(run_id,id));
CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), user_id TEXT NOT NULL, tool_name TEXT NOT NULL, arguments TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, subagent_child_id TEXT);
CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, mime_type TEXT NOT NULL, size INTEGER NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL, conversation_id TEXT);
CREATE TABLE IF NOT EXISTS raw_events(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS conversations_owner ON conversations(user_id,updated_at);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id,created_at);
CREATE INDEX IF NOT EXISTS runs_owner ON runs(user_id,created_at);
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
    result_json TEXT
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
"""


class Database:
    def __init__(self, path: Path):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
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
            "assistants": {"owner_id": "TEXT", "scope": "TEXT NOT NULL DEFAULT 'team'", "knowledge_base_ids": "TEXT NOT NULL DEFAULT '[]'", "skill_ids": "TEXT NOT NULL DEFAULT '[]'", "web_search_enabled": "INTEGER NOT NULL DEFAULT 0", "default_model_id": "TEXT", "default_reasoning_level": "TEXT"},
            "conversations": {"category": "TEXT NOT NULL DEFAULT ''", "tags": "TEXT NOT NULL DEFAULT '[]'"},
            "messages": {"skills": "TEXT NOT NULL DEFAULT '[]'", "citations": "TEXT NOT NULL DEFAULT '[]'"},
            "runs": {"capability_snapshot": "TEXT", "skill_snapshots": "TEXT NOT NULL DEFAULT '[]'", "timings": "TEXT NOT NULL DEFAULT '{}'"},
        }
        for table, additions_for_table in additions.items():
            present = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for column, definition in additions_for_table.items():
                if column not in present:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.conn.executescript("""
CREATE TABLE IF NOT EXISTS skills(id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',instructions TEXT NOT NULL,required_tools TEXT NOT NULL DEFAULT '[]',resources TEXT NOT NULL DEFAULT '{}',owner_id TEXT,scope TEXT NOT NULL DEFAULT 'private',enabled INTEGER NOT NULL DEFAULT 1,revision INTEGER NOT NULL DEFAULT 1,deleted INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS skill_versions(skill_id TEXT NOT NULL,revision INTEGER NOT NULL,snapshot TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(skill_id,revision));
CREATE INDEX IF NOT EXISTS skills_owner ON skills(owner_id,scope);
CREATE TABLE IF NOT EXISTS conversation_memory_scopes(conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),revision INTEGER NOT NULL DEFAULT 0,digest TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL,origin TEXT NOT NULL DEFAULT 'empty');
CREATE TABLE IF NOT EXISTS workflows(id TEXT PRIMARY KEY,assistant_id TEXT NOT NULL REFERENCES assistants(id),name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',owner_id TEXT,scope TEXT NOT NULL DEFAULT 'private',revision INTEGER NOT NULL DEFAULT 1,draft_version INTEGER NOT NULL DEFAULT 1,published_version INTEGER,deleted INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS workflows_assistant ON workflows(assistant_id,deleted,updated_at);
CREATE TABLE IF NOT EXISTS workflow_versions(workflow_id TEXT NOT NULL REFERENCES workflows(id),version INTEGER NOT NULL,status TEXT NOT NULL,graph TEXT NOT NULL,graph_hash TEXT NOT NULL,created_by TEXT NOT NULL,created_at TEXT NOT NULL,published_at TEXT,PRIMARY KEY(workflow_id,version));
CREATE INDEX IF NOT EXISTS workflow_versions_status ON workflow_versions(workflow_id,status,version);
CREATE TABLE IF NOT EXISTS workflow_runs(id TEXT PRIMARY KEY,workflow_id TEXT NOT NULL REFERENCES workflows(id),conversation_id TEXT REFERENCES conversations(id),version INTEGER NOT NULL,graph_hash TEXT NOT NULL,permission_digest TEXT NOT NULL,permission_snapshot_json TEXT NOT NULL DEFAULT '{}',user_id TEXT NOT NULL REFERENCES users(id),status TEXT NOT NULL,input_json TEXT NOT NULL DEFAULT '{}',output_json TEXT NOT NULL DEFAULT '{}',trace_json TEXT NOT NULL DEFAULT '[]',error TEXT,created_at TEXT NOT NULL,completed_at TEXT);
CREATE INDEX IF NOT EXISTS workflow_runs_owner ON workflow_runs(user_id,created_at);
CREATE INDEX IF NOT EXISTS workflow_runs_workflow ON workflow_runs(workflow_id,created_at);
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
""")
        # Missing rows belong to conversations predating session-isolated memory.
        # New conversations create their explicit empty scope in the same
        # transaction as the conversation, so a restart never inherits legacy
        # shared memory into a newly created session.
        self.conn.execute("INSERT OR IGNORE INTO conversation_memory_scopes(conversation_id,updated_at,origin) SELECT id,?,'legacy_pending' FROM conversations", (now(),))

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
        with self.transaction():
            index = self.one("SELECT COALESCE(MAX(id),0)+1 n FROM events WHERE run_id=?", (run_id,))["n"]
            event = {"id": index, "run_id": run_id, "type": kind, "created_at": now(), "data": data}
            self.execute("INSERT INTO events VALUES(?,?,?,?,?)", (run_id, index, kind, dump(data), event["created_at"]))
        return event

    def events(self, run_id: str, after: int):
        return [{**row, "data": json.loads(row["data"])} for row in self.all("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after))]

    def close(self):
        self.conn.close()
