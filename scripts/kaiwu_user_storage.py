"""Archive a read-only Kaiwu SQLite snapshot into one PostgreSQL database per user."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.admin_activity import activity_page
from app.prompt_safety import PromptPolicy


SCHEMA = """
CREATE SCHEMA IF NOT EXISTS archive;
REVOKE ALL ON SCHEMA public FROM PUBLIC;

CREATE TABLE IF NOT EXISTS archive.profile (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    user_id text NOT NULL UNIQUE,
    username text NOT NULL,
    display_name text NOT NULL,
    role text NOT NULL CHECK (role IN ('admin', 'member')),
    enabled boolean NOT NULL,
    created_at timestamptz NOT NULL,
    archived_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS archive.policy_version (
    user_id text NOT NULL REFERENCES archive.profile(user_id),
    revision bigint NOT NULL CHECK (revision > 0),
    document jsonb CHECK (jsonb_typeof(document) = 'object' AND octet_length(document::text) <= 262144),
    updated_at timestamptz NOT NULL,
    updated_by text NOT NULL,
    PRIMARY KEY (user_id, revision)
);

CREATE TABLE IF NOT EXISTS archive.activity (
    event_id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES archive.profile(user_id),
    created_at timestamptz NOT NULL,
    operation text NOT NULL,
    status text NOT NULL,
    code text,
    tool_name text,
    model_label text,
    input_tokens bigint CHECK (input_tokens >= 0),
    output_tokens bigint CHECK (output_tokens >= 0)
);
CREATE INDEX IF NOT EXISTS activity_cursor ON archive.activity (created_at DESC, event_id DESC);
"""
PROFILE_FIELDS = ("user_id", "username", "display_name", "role", "enabled", "created_at")
ACTIVITY_FIELDS = ("id", "user_id", "created_at", "operation", "status", "code", "tool_name", "model_label", "input_tokens", "output_tokens")
FINAL_STATUSES = {"succeeded", "failed", "cancelled", "interrupted", "not_sent"}


class Snapshot:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.tables = {row["name"] for row in self.all("SELECT name FROM sqlite_master WHERE type='table'")}

    def all(self, query: str, values=()):
        return [dict(row) for row in self.connection.execute(query, values)]

    def find_users(self, usernames: list[str]) -> list[dict]:
        profiles = []
        for username in dict.fromkeys(usernames):
            matches = self.all(
                "SELECT id AS user_id,username,display_name,role,enabled,created_at FROM users WHERE username=?",
                (username,),
            )
            if not matches:
                raise ValueError(f"账号不存在：{username}")
            profiles.extend(matches)
        return profiles

    def policies(self, user_id: str) -> list[dict]:
        if "prompt_safety_user_policy" not in self.tables:
            return []
        fields = "user_id,revision,config,updated_at,updated_by"
        current = f"SELECT {fields} FROM prompt_safety_user_policy WHERE user_id=?"
        if "prompt_safety_user_history" in self.tables:
            history = f"SELECT {fields} FROM prompt_safety_user_history WHERE user_id=?"
            return self.all(f"{current} UNION {history} ORDER BY revision", (user_id, user_id))
        return self.all(current, (user_id,))

    def activities(self, user_id: str):
        before = None
        service = SimpleNamespace(db=self)
        while True:
            page = activity_page(service, user_id=user_id, before=before, limit=100,
                                 include_reviews="prompt_safety_activity" in self.tables)
            # Run/model rows can change while executing. Archive those rows
            # after they settle so replay never replaces a final receipt.
            items = (
                item for item in page["items"]
                if item["id"].startswith(("tool:", "review:")) or item["status"] in FINAL_STATUSES
            )
            yield [tuple(item.get(key) for key in ACTIVITY_FIELDS) for item in items]
            before = page["next_cursor"]
            if before is None:
                break


def archive_names(user_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(user_id.encode()).hexdigest()[:32]
    return f"kaiwu_user_{digest}", f"kaiwu_reader_{digest}"


def provision(connection: psycopg.Connection, user_id: str) -> tuple[str, str]:
    database, reader = archive_names(user_id)
    db_identifier, reader_identifier = map(sql.Identifier, (database, reader))
    # CREATE DATABASE requires autocommit. Serialize provisioning across
    # workers with a session lock that is also released if the client dies.
    connection.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (database,))
    try:
        if not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
            connection.execute(
                sql.SQL("CREATE DATABASE {} WITH TEMPLATE template0 ALLOW_CONNECTIONS false").format(db_identifier)
            )
        connection.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(db_identifier))
        if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (reader,)).fetchone():
            connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT").format(reader_identifier))
        connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db_identifier, reader_identifier))
        connection.execute(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(db_identifier))
    finally:
        connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (database,))
    return database, reader


@contextmanager
def archive_connection(database: str):
    with psycopg.connect(dbname=database, connect_timeout=5, row_factory=dict_row) as connection:
        connection.execute("SET LOCAL lock_timeout = '10s'")
        connection.execute("SET LOCAL statement_timeout = '60s'")
        yield connection


def store_profile(connection: psycopg.Connection, profile: dict) -> None:
    result = connection.execute(
        """INSERT INTO archive.profile (user_id,username,display_name,role,enabled,created_at)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT (singleton) DO UPDATE SET username=excluded.username,
             display_name=excluded.display_name,role=excluded.role,enabled=excluded.enabled,
             archived_at=clock_timestamp()
           WHERE archive.profile.user_id=excluded.user_id""",
        tuple(bool(profile[key]) if key == "enabled" else profile[key] for key in PROFILE_FIELDS),
    )
    if result.rowcount != 1:
        raise ValueError("数据库已关联其他账号。")


def store_policy(connection: psycopg.Connection, policy: dict) -> None:
    document = policy["config"]
    if document is not None:
        document = PromptPolicy.model_validate_json(document).model_dump_json()
    values = (policy["user_id"], policy["revision"], document, policy["updated_at"], policy["updated_by"])
    result = connection.execute(
        """INSERT INTO archive.policy_version (user_id,revision,document,updated_at,updated_by)
           VALUES (%s,%s,%s::jsonb,%s,%s)
           ON CONFLICT (user_id,revision) DO NOTHING""",
        values,
    )
    if not result.rowcount and not connection.execute(
        """SELECT 1 FROM archive.policy_version
           WHERE user_id=%s AND revision=%s AND document IS NOT DISTINCT FROM %s::jsonb
             AND updated_at=%s AND updated_by=%s""",
        values,
    ).fetchone():
        raise ValueError("同一版本的审核规范内容冲突。")


def store_activity(connection: psycopg.Connection, rows: list[tuple]) -> None:
    with connection.cursor() as cursor:
        cursor.executemany(
            """INSERT INTO archive.activity (event_id,user_id,created_at,operation,status,code,tool_name,model_label,input_tokens,output_tokens)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (event_id) DO NOTHING""",
            rows,
        )


def archive_user(control: psycopg.Connection, source: Snapshot, profile: dict) -> dict:
    user_id = profile["user_id"]
    database, reader = provision(control, user_id)
    with archive_connection(database) as target:
        target.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (database,))
        target.execute(SCHEMA)
        target.execute(sql.SQL("GRANT USAGE ON SCHEMA archive TO {}").format(sql.Identifier(reader)))
        target.execute(sql.SQL(
            "GRANT SELECT ON archive.profile, archive.policy_version, archive.activity TO {}"
        ).format(sql.Identifier(reader)))
        store_profile(target, profile)
        for policy in source.policies(user_id):
            store_policy(target, policy)
        for batch in source.activities(user_id):
            store_activity(target, batch)
        return target.execute(
            """SELECT (SELECT count(*) FROM archive.profile) AS profiles,
                 (SELECT count(*) FROM archive.policy_version) AS policy_versions,
                 (SELECT count(*) FROM archive.activity) AS activities"""
        ).fetchone()


def archive_snapshot(snapshot: Path, usernames: list[str]) -> list[dict]:
    if not snapshot.is_file():
        raise ValueError("找不到 SQLite 快照。")
    with closing(sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True)) as sqlite:
        sqlite.row_factory = sqlite3.Row
        sqlite.execute("PRAGMA query_only=ON")
        sqlite.execute("BEGIN")
        source = Snapshot(sqlite)
        profiles = source.find_users(usernames)
        with psycopg.connect(autocommit=True, connect_timeout=5) as control:
            control.execute("SET statement_timeout = '60s'")
            control.execute("SET lock_timeout = '10s'")
            return [archive_user(control, source, profile) for profile in profiles]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--user", action="append", required=True, help="Exact account name, repeat for each account")
    args = parser.parse_args()
    try:
        records = archive_snapshot(args.snapshot, args.user)
    except (ValueError, sqlite3.Error, psycopg.Error) as exc:
        # PostgreSQL error context can include positions and connection details.
        parser.exit(1, f"归档失败（{type(exc).__name__}）。\n")
    for index, counts in enumerate(records, start=1):
        print(f"账号 {index} · 资料 {counts['profiles']} · 规范版本 {counts['policy_versions']} · 操作记录 {counts['activities']}")


if __name__ == "__main__":
    main()
