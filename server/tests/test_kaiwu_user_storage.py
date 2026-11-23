import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import pytest

from app.config import Settings
from app.db import Database, dump, now
from app.prompt_safety import PromptPolicy, PromptPolicyUpdate, PromptSafety


@pytest.mark.skipif(not os.getenv("KAIWU_TEST_POSTGRES_BIN"), reason="requires a private PostgreSQL test cluster")
def test_real_postgres_user_archives(tmp_path):
    import psycopg
    from psycopg import sql

    from scripts.kaiwu_user_storage import archive_connection, archive_names, archive_snapshot, store_activity

    binaries = Path(os.environ["KAIWU_TEST_POSTGRES_BIN"])
    snapshot = tmp_path / "snapshot.sqlite3"
    root_id, huawei_id = uuid4().hex, uuid4().hex
    users = ((root_id, "root", "admin"), (huawei_id, "huawei", "member"))
    source = Database(snapshot)
    try:
        for user_id, username, role in users:
            source.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)", (
                user_id, username, f"{username} 合成资料", "synthetic-password-hash", role, 1, now(),
            ))
            for status in ("succeeded", "started"):
                timestamp = now()
                source.execute("INSERT INTO model_usage_calls VALUES(?,?,?,?,?,?,?,?,?)", (
                    uuid4().hex, user_id, "chat", "synthetic-model", "合成模型", status,
                    dump({"input_tokens": 42, "output_tokens": 10}), timestamp, timestamp,
                ))
        review = PromptSafety(Settings(data_dir=tmp_path, prompt_safety_mode="rules"), source)
        for policy in (PromptPolicy(rules=[], blocked_message="合成规范"), None):
            version = review.admin_view(huawei_id)["version"]
            review.save_policy(PromptPolicyUpdate(version=version, policy=policy), root_id, huawei_id)
    finally:
        source.close()

    original = snapshot.read_bytes()
    expected = [
        {"profiles": 1, "policy_versions": 0, "activities": 1},
        {"profiles": 1, "policy_versions": 2, "activities": 3},
    ]
    assert archive_snapshot(snapshot, ["root", "huawei"]) == expected
    assert archive_snapshot(snapshot, ["root", "huawei"]) == expected
    assert snapshot.read_bytes() == original

    root_db, root_reader = archive_names(root_id)
    huawei_db, _ = archive_names(huawei_id)
    login = "kaiwu_test_" + uuid4().hex
    restore_db = "kaiwu_restore_" + uuid4().hex
    with psycopg.connect(autocommit=True) as control:
        control.execute(sql.SQL("CREATE ROLE {} LOGIN IN ROLE {}").format(sql.Identifier(login), sql.Identifier(root_reader)))
        control.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(restore_db)))
    try:
        with psycopg.connect(dbname=root_db, user=login) as reader:
            assert reader.execute("SELECT username FROM archive.profile").fetchone() == ("root",)
            assert reader.execute("SELECT count(*) FROM archive.activity").fetchone() == (1,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                reader.execute("DELETE FROM archive.activity")
        with pytest.raises(psycopg.OperationalError, match="permission denied"):
            psycopg.connect(dbname=huawei_db, user=login)

        with archive_connection(huawei_db) as writer:
            with pytest.raises(psycopg.errors.ForeignKeyViolation), writer.transaction():
                store_activity(writer, [("bad", root_id, now(), "chat", "succeeded", None, None, None, 0, 0)])
            assert writer.execute("SELECT username FROM archive.profile").fetchone()["username"] == "huawei"
            rows = writer.execute("SELECT revision,document FROM archive.policy_version ORDER BY revision").fetchall()
            assert rows[0]["document"]["blocked_message"] == "合成规范"
            assert rows[1] == {"revision": 2, "document": None}

        def write(index):
            database, user_id = (root_db, root_id) if index % 2 == 0 else (huawei_db, huawei_id)
            records = [
                (f"concurrent:{index}:{offset}", user_id, now(), "chat", "succeeded", None, None, "合成模型", 42, 10)
                for offset in range(40)
            ]
            started = perf_counter()
            with archive_connection(database) as writer:
                store_activity(writer, records)
                store_activity(writer, records)
            return perf_counter() - started

        started = perf_counter()
        with ThreadPoolExecutor(max_workers=16) as workers:
            durations = sorted(workers.map(write, range(16)))
        print(f"PostgreSQL: 16 concurrent transactions, 640 unique operations, 640 replayed; "
              f"wall {perf_counter() - started:.3f}s, slowest transaction {durations[-1]:.3f}s")
        for database, count in ((root_db, 321), (huawei_db, 323)):
            with archive_connection(database) as reader:
                assert reader.execute("SELECT count(*) FROM archive.activity").fetchone()["count"] == count

        backup = tmp_path / "huawei.dump"
        subprocess.run([binaries / "pg_dump", "--format=custom", "--file", backup, huawei_db], check=True)
        subprocess.run([binaries / "pg_restore", "--exit-on-error", "--no-owner", "--no-privileges",
                        "--dbname", restore_db, backup], check=True)
        with archive_connection(restore_db) as restored:
            assert restored.execute("SELECT username FROM archive.profile").fetchone()["username"] == "huawei"
            assert restored.execute("SELECT count(*) FROM archive.policy_version").fetchone()["count"] == 2
            assert restored.execute("SELECT count(*) FROM archive.activity").fetchone()["count"] == 323
    finally:
        with psycopg.connect(autocommit=True) as control:
            for database in (root_db, huawei_db, restore_db):
                control.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
            for role in (login, root_reader, archive_names(huawei_id)[1]):
                control.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
