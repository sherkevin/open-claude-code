"""Synthetic host-contract tests for the Goal execution pilot.

No test in this file starts a real Node Harness, a model request, a business
tool or an unattended driver. Run records are created through the normal
``Service.prepare_run`` transaction and then settled synthetically so the
admission, budget and lease boundaries can be checked in isolation.
"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import dump, now
from app.goal_execution import (
    admit_goal_round,
    goal_execution_view,
    recover_goal_execution,
    require_goal_lease,
    settle_goal_round,
    stop_goal_execution,
)
from app.main import create_app
from app.security import AppError
from test_api import PASSWORD, FakeRuntime, conversation, login, poll, send

SESSION = "workspace-" + "a" * 32
OTHER_SESSION = "workspace-" + "b" * 32
GOAL_ID = "goal-00000000-0000-4000-8000-000000000001"
GOAL = {"id": GOAL_ID, "revision": 3, "objective": "synthetic goal objective",
        "phase": "active", "maxGoalRounds": 8, "roundsStarted": 0,
        "createdAt": 1, "updatedAt": 2, "activation": "disarmed"}
COMPLETE_USAGE = {"capture_complete": True, "total_input_tokens": 100,
                  "output_tokens": 50, "cost_micros": 25}


class GoalRuntime(FakeRuntime):
    """Loopback-free fake runtime; only Goal reads and one fake pause."""
    goal = None

    def __init__(self, service, conversation_id, config, approve, emit):
        super().__init__(service, conversation_id, config, approve, emit)
        self.session_id = config.get("native_session_id")

    async def goal_state(self, request):
        action = request.get("action")
        goal = type(self).goal
        if action == "get":
            return {"goal": dict(goal) if goal else None, "continuation_driver": False}
        if action == "pause":
            if goal is None:
                return {"goal": None, "continuation_driver": False}
            goal = {**goal, "phase": "paused", "revision": goal["revision"] + 1}
            type(self).goal = goal
            return {"goal": goal, "continuation_driver": False}
        raise AssertionError(f"unexpected goal action {action!r}")


def settings_for(tmp_path, *, state=True, execution=True):
    return Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",),
                    mcp_hosts=("localhost", "127.0.0.1"), idle_seconds=60, approval_seconds=3,
                    runtime_type="harness", harness_goal_state_enabled=state,
                    harness_goal_execution_enabled=execution)


@pytest.fixture
def goal_env(tmp_path):
    GoalRuntime.goal = None
    app = create_app(settings_for(tmp_path), runtime_factory=GoalRuntime)
    with TestClient(app) as client:
        service = app.state.service
        service.create_user({"username": "admin", "display_name": "admin",
                             "password": PASSWORD, "role": "admin"})
        login(client)
        yield client, service


def setup_conversation(client, service):
    cid = conversation(client)["id"]
    # The first native memory migration intentionally clears the old
    # native session marker. Complete the empty snapshot before simulating
    # a previously initialized Harness session in this fake-runtime test.
    owner = service.require("conversations", cid)["user_id"]
    service.memory.initialize(owner, cid)
    service.db.execute("UPDATE conversations SET harness_session_id=? WHERE id=?", (SESSION, cid))
    GoalRuntime.goal = dict(GOAL)
    return cid


def path(cid):
    return f"/api/v1/conversations/{cid}/goal/execution"


def auth_payload(**overrides):
    payload = {"goal_id": GOAL_ID, "goal_revision": GOAL["revision"],
               "harness_session_id": SESSION, "max_rounds": 2,
               "token_budget": 10_000, "cost_budget_micros": 10_000,
               "time_budget_seconds": 3600, "confirm": True}
    payload.update(overrides)
    return payload


def authorize_http(client, cid, **overrides):
    return client.post(path(cid), json=auth_payload(**overrides))


def authorize_ok(client, cid, **overrides):
    response = authorize_http(client, cid, **overrides)
    assert response.status_code == 200, response.text
    return response.json()["authorization"]


def user_row(service, client):
    return service.require("users", client.get("/api/v1/auth/me").json()["id"])


def make_run(service, cid, user, request_id=None):
    result, created = service.prepare_run(
        cid, {"client_request_id": request_id or uuid.uuid4().hex,
              "text": "synthetic goal round", "file_ids": []},
        user, goal_continuation=True)
    assert created
    return result["run_id"]


def close_run(service, run_id, usage=None, status="succeeded", error_code=None):
    service.db.execute(
        "UPDATE runs SET status=?,usage=?,error_code=?,completed_at=? WHERE id=?",
        (status, dump(usage) if usage is not None else None, error_code, now(), run_id))
    row = service.db.one("SELECT conversation_id FROM runs WHERE id=?", (run_id,))
    service.db.execute("UPDATE conversations SET active_run_id=NULL WHERE id=? AND active_run_id=?",
                       (row["conversation_id"], run_id))


def admit(service, cid, run_id, goal=None, session=SESSION):
    return admit_goal_round(service, cid, run_id, {**(goal or GOAL)}, session)


def assert_code(error, code):
    assert isinstance(error.value, AppError)
    assert error.value.code == code


def test_execution_surface_is_absent_when_pilot_is_off(tmp_path):
    GoalRuntime.goal = None
    app = create_app(settings_for(tmp_path, state=True, execution=False), runtime_factory=GoalRuntime)
    with TestClient(app) as client:
        service = app.state.service
        service.create_user({"username": "admin", "display_name": "admin",
                             "password": PASSWORD, "role": "admin"})
        login(client)
        cid = conversation(client)["id"]
        goal_path = path(cid)
        assert client.get(goal_path).status_code == 404
        assert client.post(goal_path, json=auth_payload()).status_code == 404
        assert client.delete(goal_path).status_code == 404
        assert "harness_goal_execution" not in client.get("/api/v1/config").json()
        run = send(client, cid, "ordinary message", request_id="ordinary-1")["run_id"]
        assert poll(client, run)["status"] == "succeeded"
        assert service.db.one("SELECT count(*) n FROM goal_execution_authorizations")["n"] == 0
        assert service.db.one("SELECT count(*) n FROM goal_execution_leases")["n"] == 0


def test_owner_authorizes_and_other_user_is_denied(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    assert service.db.one("SELECT count(*) n FROM runs WHERE conversation_id=?", (cid,))["n"] == 0
    authorization = authorize_ok(client, cid, max_rounds=2)
    assert authorization["status"] == "active"
    assert authorization["goal_id"] == GOAL_ID
    assert authorization["goal_revision"] == GOAL["revision"]
    assert authorization["assistant_revision"] >= 1
    assert authorization["harness_session_id"] == SESSION
    view = client.get(path(cid))
    assert view.status_code == 200
    body = view.json()
    assert body["authorization"]["max_rounds"] == 2
    assert body["budget"]["rounds_used"] == 0
    assert body["budget"]["usage_complete"] is True
    assert body["budget"]["tokens_used"] == 0
    assert body["continuation_driver"] is False and body["unattended_execution"] is False
    assert body["driver_integrated"] is False
    # Authorization is a host record, not a run, approval or model call.
    assert service.db.one("SELECT count(*) n FROM runs WHERE conversation_id=?", (cid,))["n"] == 0
    assert service.db.one("SELECT count(*) n FROM approvals")["n"] == 0
    service.create_user({"username": "other", "display_name": "other",
                         "password": PASSWORD, "role": "member"})
    login(client, "other")
    assert client.get(path(cid)).status_code == 404
    assert authorize_http(client, cid, max_rounds=1).status_code == 404
    # A different user cannot revoke or mutate the owner's goal execution.
    assert client.delete(path(cid)).status_code == 404
    blocked = client.post(f"/api/v1/conversations/{cid}/goal",
                          json={"action": "pause", "ref": {"id": GOAL_ID,
                                                           "revision": GOAL["revision"]}})
    assert blocked.status_code == 404
    row = service.db.one("SELECT status FROM goal_execution_authorizations WHERE conversation_id=?", (cid,))
    assert row["status"] == "active"


def test_authorization_requires_current_goal_and_strict_budgets(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    GoalRuntime.goal = {**GOAL, "revision": 4}
    assert authorize_http(client, cid).status_code == 409
    assert authorize_http(client, cid).json()["error"]["code"] == "GOAL_STALE_REVISION"
    GoalRuntime.goal = dict(GOAL)
    changed = authorize_http(client, cid, harness_session_id=OTHER_SESSION)
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "GOAL_SESSION_CHANGED"
    GoalRuntime.goal = {**GOAL, "phase": "paused"}
    paused = authorize_http(client, cid)
    assert paused.status_code == 409
    assert paused.json()["error"]["code"] == "GOAL_GOAL_NOT_ACTIVE"
    GoalRuntime.goal = {**GOAL, "roundsStarted": 6}
    excessive = authorize_http(client, cid, max_rounds=4)
    assert excessive.status_code == 422
    assert excessive.json()["error"]["code"] == "GOAL_ROUNDS_EXCEED_NATIVE_CAP"
    GoalRuntime.goal = dict(GOAL)
    for invalid in (auth_payload(confirm=False), auth_payload(token_budget=0),
                    auth_payload(max_rounds=True), auth_payload(cost_budget_micros=0),
                    auth_payload(time_budget_seconds=1), {**auth_payload(), "extra": True}):
        assert client.post(path(cid), json=invalid).status_code == 422
    assert service.db.one("SELECT count(*) n FROM goal_execution_authorizations")["n"] == 0


def test_admission_is_unique_and_never_starts_execution(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=2)
    user = user_row(service, client)
    run_id = make_run(service, cid, user)
    before_connects = GoalRuntime.connects
    barrier = threading.Barrier(8)

    def attempt():
        barrier.wait()
        try:
            return "ok", admit(service, cid, run_id)
        except AppError as error:
            return "error", error.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: attempt(), range(8)))
    assert sum(1 for kind, _ in results if kind == "ok") == 1
    assert {value for kind, value in results if kind == "error"} == {"GOAL_LEASE_ACTIVE"}
    leases = service.db.all("SELECT * FROM goal_execution_leases WHERE conversation_id=?", (cid,))
    assert len(leases) == 1 and leases[0]["state"] == "admitted"
    assert leases[0]["round_index"] == 1 and leases[0]["run_id"] == run_id
    assert service.db.one("SELECT status FROM runs WHERE id=?", (run_id,))["status"] == "queued"
    assert service.db.one("SELECT count(*) n FROM approvals")["n"] == 0
    assert GoalRuntime.connects == before_connects
    admitted_events = service.db.all(
        "SELECT data FROM events WHERE run_id=? AND type='goal.execution.admitted'", (run_id,))
    assert len(admitted_events) == 1
    assert require_goal_lease(service, leases[0]["id"], run_id)["state"] == "admitted"
    with pytest.raises(AppError) as error:
        require_goal_lease(service, leases[0]["id"], run_id, goal={**GOAL, "phase": "blocked"})
    assert_code(error, "GOAL_GOAL_NOT_ACTIVE")
    with pytest.raises(AppError) as error:
        require_goal_lease(service, leases[0]["id"], run_id, goal={**GOAL, "revision": 4})
    assert_code(error, "GOAL_STALE_REVISION")


def test_estimated_cost_is_not_a_settled_cost(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=4)
    user = user_row(service, client)
    run_id = make_run(service, cid, user)
    lease = admit(service, cid, run_id)["lease"]
    close_run(service, run_id, usage={"capture_complete": True, "total_input_tokens": 10,
                                      "output_tokens": 5, "estimated_cost_usd": 0.001,
                                      "cost_is_estimate": True})
    settled = settle_goal_round(service, lease["id"], "succeeded")
    assert settled["lease"]["usage_status"] == "unknown"
    assert settled["lease"]["cost_micros"] is None


def test_nested_harness_receipt_is_read_and_missing_cost_stays_unknown(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=4)
    user = user_row(service, client)
    nested = {"source": "harness_provider", "input_tokens": 60, "output_tokens": 50,
              "cache_read_input_tokens": 20, "cache_creation_input_tokens": 20,
              "total_input_tokens": 100, "estimated_cost_usd": None,
              "cost_is_estimate": True,
              "token_accounting": {
                  "receipt_schema": "harness-http-usage/v1", "capture_complete": True,
                  "provider_totals": {"input_tokens": 60, "output_tokens": 50,
                                      "cache_read_input_tokens": 20,
                                      "cache_write_input_tokens": 20}}}
    run_id = make_run(service, cid, user)
    lease = admit(service, cid, run_id)["lease"]
    close_run(service, run_id, usage=nested)
    settled = settle_goal_round(service, lease["id"], "succeeded")
    # Capture was complete; only the authoritative provider cost is absent.
    assert settled["lease"]["usage_status"] == "unknown"
    assert settled["lease"]["error_code"] == "USAGE_INCOMPLETE"
    assert settled["lease"]["cost_micros"] is None
    assert settled["budget"]["usage_complete"] is False

    assert client.delete(path(cid)).status_code == 200
    authorize_ok(client, cid, max_rounds=4)
    second = make_run(service, cid, user)
    second_lease = admit(service, cid, second)["lease"]
    close_run(service, second, usage={**nested, "cost_micros": 17})
    settled = settle_goal_round(service, second_lease["id"], "succeeded")
    assert settled["lease"]["usage_status"] == "complete"
    assert settled["lease"]["total_tokens"] == 150
    assert settled["lease"]["cost_micros"] == 17


def test_plain_user_run_is_not_admissible(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=4)
    user = user_row(service, client)
    run_id = make_run(service, cid, user)
    service.db.execute("UPDATE runs SET execution_meta='{}' WHERE id=?", (run_id,))
    with pytest.raises(AppError) as error:
        admit(service, cid, run_id)
    assert_code(error, "GOAL_RUN_NOT_CONTINUATION")
    assert service.db.one("SELECT count(*) n FROM goal_execution_leases")["n"] == 0


def test_settlement_reads_real_run_usage_and_missing_is_unknown(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=4)
    user = user_row(service, client)
    run_id = make_run(service, cid, user)
    lease = admit(service, cid, run_id)["lease"]
    close_run(service, run_id, usage=None)
    settled = settle_goal_round(service, lease["id"], "succeeded")
    assert settled["lease"]["usage_status"] == "unknown"
    assert settled["lease"]["total_tokens"] is None
    assert settled["budget"]["usage_complete"] is False
    assert settled["budget"]["tokens_used"] is None
    assert settled["budget"]["tokens_remaining"] is None
    second = make_run(service, cid, user)
    with pytest.raises(AppError) as error:
        admit(service, cid, second)
    assert_code(error, "GOAL_USAGE_UNVERIFIED")

    # Idempotent replay is not a second model round and cannot rewrite the result.
    again = settle_goal_round(service, lease["id"], "succeeded")
    assert again["lease"]["usage_status"] == "unknown"
    with pytest.raises(AppError) as error:
        settle_goal_round(service, lease["id"], "failed")
    assert_code(error, "GOAL_LEASE_SETTLED")
    assert service.db.one("SELECT count(*) n FROM goal_execution_leases WHERE conversation_id=?",
                          (cid,))["n"] == 1


def test_complete_usage_allows_the_next_round(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=4)
    user = user_row(service, client)
    first = make_run(service, cid, user)
    lease = admit(service, cid, first)["lease"]
    close_run(service, first, usage=COMPLETE_USAGE)
    settled = settle_goal_round(service, lease["id"], "succeeded")
    assert settled["lease"]["usage_status"] == "complete"
    assert settled["lease"]["total_tokens"] == 150
    assert settled["lease"]["cost_micros"] == 25
    assert settled["budget"]["usage_complete"] is True
    second = make_run(service, cid, user)
    with pytest.raises(AppError) as error:
        admit(service, cid, second)
    assert_code(error, "GOAL_ROUND_FENCE_MISMATCH")
    admitted = admit(service, cid, second, goal={**GOAL, "roundsStarted": 1})
    assert admitted["budget"]["rounds_used"] == 2
    assert admitted["lease"]["round_index"] == 2
    assert admitted["execution_started"] is False


def test_round_budget_boundary(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=1, token_budget=10_000, cost_budget_micros=10_000)
    user = user_row(service, client)
    run_id = make_run(service, cid, user)
    lease = admit(service, cid, run_id)["lease"]
    close_run(service, run_id, usage=COMPLETE_USAGE)
    settle_goal_round(service, lease["id"], "succeeded")
    second = make_run(service, cid, user)
    with pytest.raises(AppError) as error:
        admit(service, cid, second)
    assert_code(error, "GOAL_ROUND_BUDGET_EXHAUSTED")


def test_token_budget_boundary(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=4, token_budget=150, cost_budget_micros=10_000)
    user = user_row(service, client)
    run_id = make_run(service, cid, user)
    lease = admit(service, cid, run_id)["lease"]
    close_run(service, run_id, usage=COMPLETE_USAGE)
    settle_goal_round(service, lease["id"], "succeeded")
    second = make_run(service, cid, user)
    with pytest.raises(AppError) as error:
        admit(service, cid, second)
    assert_code(error, "GOAL_TOKEN_BUDGET_EXHAUSTED")


def test_cost_budget_boundary(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=4, token_budget=10_000, cost_budget_micros=25)
    user = user_row(service, client)
    run_id = make_run(service, cid, user)
    lease = admit(service, cid, run_id)["lease"]
    close_run(service, run_id, usage=COMPLETE_USAGE)
    settle_goal_round(service, lease["id"], "succeeded")
    second = make_run(service, cid, user)
    with pytest.raises(AppError) as error:
        admit(service, cid, second)
    assert_code(error, "GOAL_COST_BUDGET_EXHAUSTED")


def test_time_budget_boundary(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, time_budget_seconds=60)
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    service.db.execute("UPDATE goal_execution_authorizations SET created_at=? "
                       "WHERE conversation_id=? AND status='active'", (old, cid))
    user = user_row(service, client)
    run_id = make_run(service, cid, user)
    with pytest.raises(AppError) as error:
        admit(service, cid, run_id)
    assert_code(error, "GOAL_TIME_BUDGET_EXHAUSTED")


def test_stale_revision_session_and_round_fence(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=4)
    user = user_row(service, client)
    run_id = make_run(service, cid, user)
    with pytest.raises(AppError) as error:
        admit(service, cid, run_id, goal={**GOAL, "revision": 4})
    assert_code(error, "GOAL_STALE_REVISION")
    with pytest.raises(AppError) as error:
        admit(service, cid, run_id, session=OTHER_SESSION)
    assert_code(error, "GOAL_SESSION_CHANGED")
    with pytest.raises(AppError) as error:
        admit(service, cid, run_id, goal={**GOAL, "phase": "paused"})
    assert_code(error, "GOAL_GOAL_NOT_ACTIVE")
    with pytest.raises(AppError) as error:
        admit(service, cid, run_id, goal={**GOAL, "activation": "unknown"})
    assert_code(error, "GOAL_SNAPSHOT_INVALID")
    lease = admit(service, cid, run_id)["lease"]
    close_run(service, run_id, usage=COMPLETE_USAGE)
    settle_goal_round(service, lease["id"], "succeeded")
    second = make_run(service, cid, user)
    with pytest.raises(AppError) as error:
        admit(service, cid, second, goal={**GOAL, "roundsStarted": 3})
    assert_code(error, "GOAL_ROUND_FENCE_MISMATCH")


def test_stop_cancels_future_continuation_but_settles_usage(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=4)
    user = user_row(service, client)
    run_id = make_run(service, cid, user)
    lease = admit(service, cid, run_id)["lease"]
    result = stop_goal_execution(service, cid, "user_paused")
    assert result == {"revoked": 1, "aborted": 1}
    with pytest.raises(AppError) as error:
        require_goal_lease(service, lease["id"], run_id)
    assert_code(error, "GOAL_LEASE_INACTIVE")
    view = goal_execution_view(service, cid, service.require("users", user["id"]))
    assert view["authorization"]["status"] == "revoked"
    assert view["authorization"]["revoked_reason"] == "user_paused"
    close_run(service, run_id, usage=COMPLETE_USAGE)
    settled = settle_goal_round(service, lease["id"], "succeeded")
    assert settled["lease"]["state"] == "aborted"
    assert settled["lease"]["usage_status"] == "complete"
    assert settled["budget"]["tokens_used"] == 150
    second = make_run(service, cid, user)
    with pytest.raises(AppError) as error:
        admit(service, cid, second)
    assert_code(error, "GOAL_NOT_AUTHORIZED")


def test_cancelled_run_revokes_execution_without_replay(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=4)
    user = user_row(service, client)
    run_id = make_run(service, cid, user)
    lease = admit(service, cid, run_id)["lease"]
    service.manager.finish(run_id, "cancelled")
    row = service.db.one("SELECT * FROM goal_execution_authorizations WHERE conversation_id=?", (cid,))
    assert row["status"] == "revoked" and row["revoked_reason"] == "run_cancelled"
    assert service.db.one("SELECT state FROM goal_execution_leases WHERE id=?",
                          (lease["id"],))["state"] == "aborted"
    with pytest.raises(AppError) as error:
        require_goal_lease(service, lease["id"], run_id)
    assert_code(error, "GOAL_LEASE_INACTIVE")
    second = make_run(service, cid, user)
    with pytest.raises(AppError) as error:
        admit(service, cid, second)
    assert_code(error, "GOAL_NOT_AUTHORIZED")


def test_owner_can_stop_while_a_round_is_running(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=4)
    user = user_row(service, client)
    run_id = make_run(service, cid, user)
    lease = admit(service, cid, run_id)["lease"]
    # The run is active, so the native Goal state route is busy; the execution
    # stop endpoint must still be available to the owner.
    stopped = client.delete(path(cid))
    assert stopped.status_code == 200, stopped.text
    body = stopped.json()
    assert body["authorization"]["status"] == "revoked"
    assert body["authorization"]["revoked_reason"] == "user_revoked"
    assert body["active_lease"] is None
    with pytest.raises(AppError) as error:
        require_goal_lease(service, lease["id"], run_id)
    assert_code(error, "GOAL_LEASE_INACTIVE")


def test_recovery_revokes_and_aborts_without_replay(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=4)
    user = user_row(service, client)
    run_id = make_run(service, cid, user)
    lease = admit(service, cid, run_id)["lease"]
    recovered = recover_goal_execution(service)
    assert recovered == {"revoked": 1, "aborted": 1}
    row = service.db.one("SELECT * FROM goal_execution_authorizations WHERE conversation_id=?", (cid,))
    assert row["status"] == "revoked" and row["revoked_reason"] == "service_restarted"
    assert service.db.one("SELECT state FROM goal_execution_leases WHERE id=?",
                          (lease["id"],))["state"] == "aborted"
    with pytest.raises(AppError) as error:
        require_goal_lease(service, lease["id"], run_id)
    assert_code(error, "GOAL_LEASE_INACTIVE")
    # Restart recovery finishes the abandoned run before serving again.
    close_run(service, run_id, usage=None, status="interrupted")
    second = make_run(service, cid, user)
    with pytest.raises(AppError) as error:
        admit(service, cid, second)
    assert_code(error, "GOAL_NOT_AUTHORIZED")
    # Second recovery is a no-op, not a replay.
    assert recover_goal_execution(service) == {"revoked": 0, "aborted": 0}


def test_application_restart_does_not_resume_authorization(tmp_path):
    GoalRuntime.goal = None
    settings = settings_for(tmp_path)
    app = create_app(settings, runtime_factory=GoalRuntime)
    with TestClient(app) as client:
        service = app.state.service
        service.create_user({"username": "admin", "display_name": "admin",
                             "password": PASSWORD, "role": "admin"})
        login(client)
        cid = setup_conversation(client, service)
        authorize_ok(client, cid, max_rounds=4)
        user = user_row(service, client)
        run_id = make_run(service, cid, user)
        admit(service, cid, run_id)
    app2 = create_app(settings, runtime_factory=GoalRuntime)
    with TestClient(app2) as client:
        login(client)
        service = app2.state.service
        view = client.get(path(cid)).json()
        assert view["authorization"]["status"] == "revoked"
        assert view["authorization"]["revoked_reason"] == "service_restarted"
        assert view["active_lease"] is None
        assert service.db.one("SELECT state FROM goal_execution_leases WHERE conversation_id=?",
                              (cid,))["state"] == "aborted"


def test_new_user_instruction_and_permission_change_stop_continuation(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=4)
    run = send(client, cid, "plain user instruction", request_id="instruction-1")["run_id"]
    assert poll(client, run)["status"] == "succeeded"
    row = service.db.one("SELECT * FROM goal_execution_authorizations WHERE conversation_id=?", (cid,))
    assert row["status"] == "revoked" and row["revoked_reason"] == "user_instruction"
    user = user_row(service, client)
    second = make_run(service, cid, user)
    with pytest.raises(AppError) as error:
        admit(service, cid, second)
    assert_code(error, "GOAL_NOT_AUTHORIZED")
    # Clear the rejected queued run before requesting a fresh approval.
    close_run(service, second, usage=None)
    # The native goal remains active; only the host approval was invalidated.
    authorize_ok(client, cid, max_rounds=4)
    changed = client.patch(f"/api/v1/conversations/{cid}", json={"permission_mode": "auto"})
    assert changed.status_code == 200, changed.text
    row = service.db.one("SELECT * FROM goal_execution_authorizations WHERE conversation_id=? "
                         "ORDER BY rowid DESC LIMIT 1", (cid,))
    assert row["status"] == "revoked" and row["revoked_reason"] == "permission_changed"


def test_goal_pause_transition_revokes_execution_authorization(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=4)
    paused = client.post(f"/api/v1/conversations/{cid}/goal",
                         json={"action": "pause", "ref": {"id": GOAL_ID,
                                                          "revision": GOAL["revision"]}})
    assert paused.status_code == 200, paused.text
    row = service.db.one("SELECT * FROM goal_execution_authorizations WHERE conversation_id=?", (cid,))
    assert row["status"] == "revoked" and row["revoked_reason"] == "goal_pause"


def test_disabled_user_cannot_admit(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=4)
    user = user_row(service, client)
    run_id = make_run(service, cid, user)
    service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (user["id"],))
    with pytest.raises(AppError) as error:
        admit(service, cid, run_id)
    assert_code(error, "USER_DISABLED")


def test_disabled_account_revokes_execution(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=4)
    user = user_row(service, client)
    run_id = make_run(service, cid, user)
    lease = admit(service, cid, run_id)["lease"]
    service.create_user({"username": "admin2", "display_name": "admin2",
                         "password": PASSWORD, "role": "admin"})
    service.patch_user(user["id"], {"enabled": False}, service.require("users", user["id"]))
    row = service.db.one("SELECT * FROM goal_execution_authorizations WHERE conversation_id=?", (cid,))
    assert row["status"] == "revoked" and row["revoked_reason"] == "user_disabled"
    assert service.db.one("SELECT state FROM goal_execution_leases WHERE id=?",
                          (lease["id"],))["state"] == "aborted"
    with pytest.raises(AppError) as error:
        require_goal_lease(service, lease["id"], run_id)
    assert_code(error, "GOAL_LEASE_INACTIVE")


def test_assistant_revision_change_stops_continuation(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=4)
    user = user_row(service, client)
    run_id = make_run(service, cid, user)
    lease = admit(service, cid, run_id)["lease"]
    assistant_id = service.require("conversations", cid)["assistant_id"]
    service.db.execute("UPDATE assistants SET revision=revision+1 WHERE id=?", (assistant_id,))
    with pytest.raises(AppError) as error:
        require_goal_lease(service, lease["id"], run_id)
    assert_code(error, "GOAL_PERMISSION_CHANGED")
    view = goal_execution_view(service, cid, service.require("users", user["id"]))
    assert view["permission_matches"] is False
    close_run(service, run_id, usage=COMPLETE_USAGE)
    second = make_run(service, cid, user)
    with pytest.raises(AppError) as error:
        admit(service, cid, second)
    assert_code(error, "GOAL_PERMISSION_CHANGED")


def test_disabled_assistant_stops_continuation(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid, max_rounds=4)
    user = user_row(service, client)
    run_id = make_run(service, cid, user)
    lease = admit(service, cid, run_id)["lease"]
    assistant_id = service.require("conversations", cid)["assistant_id"]
    service.db.execute("UPDATE assistants SET enabled=0 WHERE id=?", (assistant_id,))
    with pytest.raises(AppError) as error:
        require_goal_lease(service, lease["id"], run_id)
    assert_code(error, "GOAL_PERMISSION_CHANGED")


def test_admin_downgrade_stops_full_permission_continuation(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    changed = client.patch(f"/api/v1/conversations/{cid}", json={"permission_mode": "full"})
    assert changed.status_code == 200, changed.text
    authorize_ok(client, cid, max_rounds=4)
    user = user_row(service, client)
    run_id = make_run(service, cid, user)
    lease = admit(service, cid, run_id)["lease"]
    # Keep another admin so the last-admin guard does not block the downgrade.
    service.create_user({"username": "admin2", "display_name": "admin2",
                         "password": PASSWORD, "role": "admin"})
    service.patch_user(user["id"], {"role": "member"}, service.require("users", user["id"]))
    row = service.db.one("SELECT * FROM goal_execution_authorizations WHERE conversation_id=?", (cid,))
    assert row["status"] == "revoked" and row["revoked_reason"] == "user_role_changed"
    assert service.db.one("SELECT state FROM goal_execution_leases WHERE id=?",
                          (lease["id"],))["state"] == "aborted"
    with pytest.raises(AppError) as error:
        require_goal_lease(service, lease["id"], run_id)
    assert_code(error, "GOAL_LEASE_INACTIVE")
    view = goal_execution_view(service, cid, service.require("users", user["id"]))
    assert view["permission_matches"] is False


def test_generic_actor_cannot_execute_unintegrated_driver(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    authorize_ok(client, cid)
    run_id = make_run(service, cid, user_row(service, client))
    lease = admit(service, cid, run_id)["lease"]
    before = GoalRuntime.connects
    client.portal.call(service.manager.ensure_submitted, run_id)
    run = poll(client, run_id)
    assert run["status"] == "failed"
    assert run["error_code"] == "GOAL_DRIVER_NOT_INTEGRATED"
    assert GoalRuntime.connects == before
    assert service.db.one("SELECT state FROM goal_execution_leases WHERE id=?", (lease["id"],))["state"] == "aborted"
