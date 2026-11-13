"""Deterministic races between approval waiting, HTTP decisions and timeouts.

The reviewer reproduced two new edge cases after SQLite writes moved to worker
threads:

* a durable ``allowed`` UPDATE could race a runtime ``wait_for`` timeout,
  raising ``InvalidStateError`` (HTTP 500) and leaving the database saying
  ``allowed`` while the approved event stream said ``deny``;
* ``record_vote`` woke the waiter with ``Future.set_result`` from a worker
  thread, which asyncio explicitly does not allow.

Both are covered here: the conditional ``UPDATE ... WHERE status='pending'`` is
the single audit arbiter, the waiter aligns to that durable row, and every
future transition happens on the future's own loop thread.
"""
import asyncio
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.high_risk_approval import HighRiskApprovalService
from app.main import create_app
from test_api import FakeRuntime, PASSWORD, conversation, login, poll, send

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import trial_smoke  # noqa: E402


def _wait_row(service, approval_id: str, statuses: set[str], timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        row = service.db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if row and row["status"] in statuses:
            return row
        time.sleep(0.02)
    raise AssertionError(f"approval {approval_id} never reached {statuses}: {row}")


def test_timeout_wins_and_a_late_decision_is_a_conflict_not_a_500(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        provider_settings="",
        origins=("http://testserver",),
        mcp_hosts=("localhost", "127.0.0.1"),
        idle_seconds=60,
        approval_seconds=0.5,
    )
    app = create_app(settings, runtime_factory=FakeRuntime)
    with TestClient(app) as client:
        service = app.state.service
        service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
        login(client)
        cid = conversation(client)["id"]
        run = send(client, cid, "approval", "approval-timeout-race")
        approval = poll(client, run["run_id"], {"awaiting_approval"})["approvals"][0]

        # The runtime's timeout claims the row first (denied/timeout); a late
        # authenticated decision must lose the conditional update and be a
        # conflict, never an InvalidStateError.
        row = _wait_row(service, approval["id"], {"denied"})
        assert row["decision_source"] == "timeout"
        assert row["decided_by"] is None and row["decided_at"]

        late = client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "allow"})
        assert late.status_code == 409, late.text
        assert late.json()["error"]["code"] == "APPROVAL_EXPIRED"

        assert poll(client, run["run_id"])["status"] == "succeeded"
        messages = client.get(f"/api/v1/conversations/{cid}/messages").json()
        assert "已拒绝" in messages[-1]["text"], messages[-1]["text"]

        events = service.db.events(run["run_id"], 0)
        resolved = [item for item in events if item["type"] == "approval.resolved"]
        assert len(resolved) == 1, resolved
        assert resolved[0]["data"]["decision"] == "deny"
        assert resolved[0]["data"]["decision_source"] == "timeout"

        # The durable row and the event stream agree.
        final = service.db.one("SELECT * FROM approvals WHERE id=?", (approval["id"],))
        assert final["status"] == "denied"
        assert final["decision_source"] == resolved[0]["data"]["decision_source"]


class _NoopRuntime:
    """Runs are never delivered in the alignment test; the actor is idle."""

    def __init__(self, service, conversation_id, config, approve, emit):
        self.emit = emit

    async def connect(self):
        return None

    async def execute(self, run, message):
        return None

    async def interrupt(self):
        return None

    async def close(self):
        return None


async def _login(client) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": "synthetic-admin", "password": trial_smoke.PASSWORD},
    )
    assert response.status_code == 200, response.text


async def _assistant(client) -> dict:
    return await trial_smoke.request_json(
        client,
        "POST",
        "/api/v1/assistants",
        expected=(201,),
        json={"name": "审批竞态助手", "instructions": "仅用于学校云合成回归。", "knowledge_base_ids": [], "connections": [], "web_search_enabled": False},
    )


async def _conversation(client, assistant: dict, title: str) -> dict:
    return await trial_smoke.request_json(
        client,
        "POST",
        "/api/v1/conversations",
        expected=(201,),
        json={"assistant_id": assistant["id"], "title": title},
    )


def _insert_pending_approval(service, approval_id: str, run_id: str, user_id: str) -> None:
    stamp = datetime.now(timezone.utc)
    service.db.execute(
        "INSERT INTO approvals(id,run_id,user_id,tool_name,arguments,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            approval_id,
            run_id,
            user_id,
            "test_business_write",
            "{}",
            "pending",
            stamp.isoformat(),
            (stamp + timedelta(seconds=60)).isoformat(),
        ),
    )


async def _queued_run(service, manager, user, conversation_id: str, request_id: str) -> dict:
    result, created = service.prepare_run(
        conversation_id,
        {"client_request_id": request_id, "text": "审批对齐", "file_ids": [], "skill_ids": []},
        user,
    )
    assert created is True
    service.db.execute("UPDATE runs SET status='awaiting_approval' WHERE id=?", (result["run_id"],))
    actor = manager.actor_for(conversation_id)
    actor.current_run_id = result["run_id"]
    return {"run_id": result["run_id"], "actor": actor}


async def _scenario_waiter_aligns_to_durable_row(tmp_path) -> None:
    server = await trial_smoke.SyntheticTrialServer.start(
        tmp_path / "approval-align",
        max_active_sessions=4,
        idle_seconds=60,
        runtime_factory=_NoopRuntime,
    )
    server.create_admin()
    service = server.app.state.service
    manager = server.app.state.manager
    try:
        async with trial_smoke.make_client(server.base_url, timeout=30.0, max_connections=8) as client:
            await _login(client)
            assistant = await _assistant(client)
            user = service.db.one("SELECT * FROM users WHERE username='synthetic-admin'")

            # 1. The HTTP decision won the conditional update while the
            #    waiter's timer was firing: the waiter must follow the row.
            conversation = await _conversation(client, assistant, "HTTP 先赢")
            first = await _queued_run(service, manager, user, conversation["id"], "align-http")
            approval_id = "approval-align-http"
            _insert_pending_approval(service, approval_id, first["run_id"], user["id"])
            stamp = datetime.now(timezone.utc).isoformat()
            service.db.execute(
                "UPDATE approvals SET status='allowed',decided_at=?,decided_by=?,decision_source='user' WHERE id=?",
                (stamp, user["id"], approval_id),
            )
            decision = await first["actor"].resolve_waited_approval(
                approval_id, first["run_id"], "deny", "timeout"
            )
            assert decision == "allow"
            row = service.db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
            assert row["status"] == "allowed" and row["decision_source"] == "user"
            resolved = [
                item for item in service.db.events(first["run_id"], 0)
                if item["type"] == "approval.resolved"
            ]
            assert len(resolved) == 1, resolved
            assert resolved[0]["data"]["decision"] == "allow"
            assert resolved[0]["data"]["decision_source"] == "user"

            # 2. The timeout claims the pending row first: the waiter stays
            #    fail-closed and the event matches denied/timeout.
            conversation = await _conversation(client, assistant, "超时先赢")
            second = await _queued_run(service, manager, user, conversation["id"], "align-timeout")
            approval_id = "approval-align-timeout"
            _insert_pending_approval(service, approval_id, second["run_id"], user["id"])
            decision = await second["actor"].resolve_waited_approval(
                approval_id, second["run_id"], "deny", "timeout"
            )
            assert decision == "deny"
            row = service.db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
            assert row["status"] == "denied" and row["decision_source"] == "timeout"
            resolved = [
                item for item in service.db.events(second["run_id"], 0)
                if item["type"] == "approval.resolved"
            ]
            assert len(resolved) == 1, resolved
            assert resolved[0]["data"]["decision"] == "deny"
            assert resolved[0]["data"]["decision_source"] == "timeout"

            # 3. A revoked high-risk policy overrides a persisted allow even
            #    when the row was won by the HTTP endpoint (fail-closed).
            conversation = await _conversation(client, assistant, "策略撤回")
            third = await _queued_run(service, manager, user, conversation["id"], "align-policy")
            approval_id = "approval-align-policy"
            _insert_pending_approval(service, approval_id, third["run_id"], user["id"])
            service.db.execute(
                "UPDATE approvals SET status='allowed',decided_at=?,decided_by=?,decision_source='user' WHERE id=?",
                (stamp, user["id"], approval_id),
            )
            original = service.high_risk_approval.snapshot_current
            service.high_risk_approval.snapshot_current = lambda *args: False
            try:
                decision = await third["actor"].resolve_waited_approval(
                    approval_id, third["run_id"], "deny", "timeout",
                    dual_snapshot={}, run=service.db.one("SELECT * FROM runs WHERE id=?", (third["run_id"],)),
                    tool="test_business_write", arguments={},
                )
            finally:
                service.high_risk_approval.snapshot_current = original
            assert decision == "deny"
            row = service.db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
            assert row["status"] == "denied" and row["decision_source"] == "policy"
            resolved = [
                item for item in service.db.events(third["run_id"], 0)
                if item["type"] == "approval.resolved"
            ]
            assert len(resolved) == 1, resolved
            assert resolved[0]["data"]["decision"] == "deny"
            assert resolved[0]["data"]["decision_source"] == "policy"
    finally:
        await server.stop()


def test_waiter_alignment_matches_the_durable_row(tmp_path):
    asyncio.run(_scenario_waiter_aligns_to_durable_row(tmp_path))


def test_dual_vote_wakes_the_waiter_on_the_event_loop_thread():
    """F3: ``record_vote`` runs on a worker thread; the future must not."""

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        service = SimpleNamespace(manager=SimpleNamespace(approvals={"approval-1": future}))
        policy = HighRiskApprovalService(service)
        callback_threads: list[int] = []
        future.add_done_callback(lambda _: callback_threads.append(threading.get_ident()))

        await asyncio.to_thread(policy._wake_waiter, "approval-1", "allow")

        assert await asyncio.wait_for(future, 2) == "allow"
        await asyncio.sleep(0)
        # ``Future.set_result`` and its callbacks ran on the loop thread, not
        # on the executor thread that persisted the vote.
        assert callback_threads == [threading.get_ident()]

    asyncio.run(scenario())
