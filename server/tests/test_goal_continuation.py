"""B2.4 Goal end-to-end continuation: real host run, lease, settlement, stop.

The fake runtime in this file does not stand in for a model. It stands in for
the *Node seam receipt* only: it arms the exact host-reserved revision through
``admit_goal_round_armed`` and then reports a synthetic usage receipt, exactly
as the real pinned driver seam does over RPC. It proves the host-side product
path (authorization, run/FIFO, lease, budget, visibility, stop and restart)
without claiming a real model or production billing.

The separate ``test_real_harness_goal_round_end_to_end`` uses the pinned Node
Harness and the loopback synthetic Anthropic server. It asserts the real
authoritative-usage fail-closed boundary: one round runs, then continuation is
refused because the pinned usage ledger reports no authoritative cost.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.goal_execution import admit_goal_round_armed, start_goal_round
from app.main import create_app
from app.security import AppError
from test_api import PASSWORD, FakeRuntime, conversation, login, poll
from test_harness_real_runtime import image_fixture_settings, model_server, wait_run

SESSION = "workspace-" + "c" * 32
GOAL_ID = "goal-00000000-0000-4000-8000-000000000002"
GOAL = {"id": GOAL_ID, "revision": 3, "objective": "synthetic continuation objective",
        "phase": "active", "maxGoalRounds": 8, "roundsStarted": 0,
        "createdAt": 1, "updatedAt": 2, "activation": "disarmed"}
SYNTHETIC_USAGE = {"capture_complete": True, "total_input_tokens": 100,
                   "output_tokens": 50, "cost_micros": 25,
                   "cost_source": "synthetic_fixture"}


class GoalContinuationRuntime(FakeRuntime):
    """Fake Node seam: one host run -> one admitted round, never a real model."""
    goal = None
    model_calls = 0
    block_after_admission = None
    block_before_admission = None
    fail_before_admission = None

    def __init__(self, service, conversation_id, config, approve, emit):
        super().__init__(service, conversation_id, config, approve, emit)
        self.session_id = config.get("native_session_id")

    async def goal_state(self, request):
        if request.get("action") != "get":
            raise AssertionError(f"unexpected goal action {request.get('action')!r}")
        goal = GoalContinuationRuntime.goal
        return {"goal": dict(goal) if goal else None,
                "continuation_driver": False}

    def _lease(self, run_id):
        return self.service.db.one(
            "SELECT * FROM goal_execution_leases WHERE run_id=?", (run_id,))

    async def execute(self, run, message):
        lease = self._lease(run["id"])
        assert lease is not None and lease["state"] == "admitted"
        if GoalContinuationRuntime.block_before_admission is not None:
            await asyncio.to_thread(GoalContinuationRuntime.block_before_admission.wait)
        if GoalContinuationRuntime.fail_before_admission is not None:
            raise AppError(502, GoalContinuationRuntime.fail_before_admission,
                           "synthetic Node arm failed before admission")
        snapshot = {"id": lease["goal_id"],
                    "revision": lease["expected_goal_revision"],
                    "phase": "active",
                    "roundsStarted": lease["native_rounds_before"],
                    "maxGoalRounds": GoalContinuationRuntime.goal["maxGoalRounds"],
                    "activation": "armed"}
        # The host seam is synchronous Python called from a worker thread in
        # production; mirror that here instead of blocking the event loop.
        await asyncio.to_thread(admit_goal_round_armed, self.service, lease["id"],
                                run["id"], snapshot, self.session_id,
                                lease["native_rounds_before"] + 1)
        GoalContinuationRuntime.model_calls += 1
        GoalContinuationRuntime.goal = {
            **GoalContinuationRuntime.goal,
            "revision": lease["expected_goal_revision"],
            "roundsStarted": lease["native_rounds_before"] + 1,
            "phase": "active"}
        if GoalContinuationRuntime.block_after_admission is not None:
            await asyncio.to_thread(GoalContinuationRuntime.block_after_admission.wait)
        await self.emit("message.delta", {
            "message_id": run["assistant_message_id"],
            "text": f"目标轮次 {lease['round_index']} 已完成"})
        # Synthetic loopback receipt: explicitly non-authoritative billing.
        await self.emit("usage.updated", dict(SYNTHETIC_USAGE))


def settings_for(tmp_path, *, state=True, execution=True, test_mode=True):
    return Settings(
        data_dir=tmp_path, provider_settings="", origins=("http://testserver",),
        mcp_hosts=("localhost", "127.0.0.1"), idle_seconds=60, approval_seconds=3,
        runtime_type="harness", harness_goal_state_enabled=state,
        harness_goal_execution_enabled=execution, provider_test_mode=test_mode)


def reset_runtime():
    GoalContinuationRuntime.goal = None
    GoalContinuationRuntime.model_calls = 0
    GoalContinuationRuntime.block_after_admission = None
    GoalContinuationRuntime.block_before_admission = None
    GoalContinuationRuntime.fail_before_admission = None


@pytest.fixture
def goal_env(tmp_path):
    reset_runtime()
    app = create_app(settings_for(tmp_path), runtime_factory=GoalContinuationRuntime)
    with TestClient(app) as client:
        service = app.state.service
        service.create_user({"username": "admin", "display_name": "admin",
                             "password": PASSWORD, "role": "admin"})
        login(client)
        yield client, service, app


def setup_conversation(client, service):
    cid = conversation(client)["id"]
    owner = service.require("conversations", cid)["user_id"]
    service.memory.initialize(owner, cid)
    service.db.execute("UPDATE conversations SET harness_session_id=? WHERE id=?", (SESSION, cid))
    GoalContinuationRuntime.goal = dict(GOAL)
    return cid


def auth_payload(goal=None, **overrides):
    goal = goal or GOAL
    payload = {"goal_id": goal["id"], "goal_revision": goal["revision"],
               "harness_session_id": SESSION, "max_rounds": 4,
               "token_budget": 10_000, "cost_budget_micros": 10_000,
               "time_budget_seconds": 3600, "confirm": True}
    payload.update(overrides)
    return payload


def authorize(client, cid, goal=None, **overrides):
    response = client.post(f"/api/v1/conversations/{cid}/goal/execution",
                           json=auth_payload(goal, **overrides))
    assert response.status_code == 200, response.text
    return response.json()


def current_goal(client, cid):
    response = client.get(f"/api/v1/conversations/{cid}/goal")
    assert response.status_code == 200, response.text
    return response.json()["goal"]


def start_round(client, cid, goal):
    return client.post(f"/api/v1/conversations/{cid}/goal/execution/rounds",
                       json={"goal_id": goal["id"], "goal_revision": goal["revision"],
                             "confirm": True})


def error_code(response):
    return response.json().get("error", {}).get("code")


def test_goal_round_surface_is_default_off(tmp_path):
    reset_runtime()
    app = create_app(settings_for(tmp_path, execution=False),
                     runtime_factory=GoalContinuationRuntime)
    with TestClient(app) as client:
        service = app.state.service
        service.create_user({"username": "admin", "display_name": "admin",
                             "password": PASSWORD, "role": "admin"})
        login(client)
        cid = setup_conversation(client, service)
        response = start_round(client, cid, GOAL)
        assert response.status_code == 404
        assert service.db.one("SELECT count(*) n FROM runs")["n"] == 0
        assert service.db.one("SELECT count(*) n FROM goal_execution_leases")["n"] == 0


def test_two_explicit_rounds_are_visible_and_settled_as_synthetic(goal_env):
    client, service, _app = goal_env
    cid = setup_conversation(client, service)
    authorized = authorize(client, cid)
    assert authorized["driver_integrated"] is True
    assert authorized["unattended_execution"] is False
    # Round one: the host reserves a real run; the fake Node seam admits the
    # exact expected revision and reports one synthetic receipt.
    first = start_round(client, cid, current_goal(client, cid))
    assert first.status_code == 200, first.text
    assert first.json()["submitted"] is True
    run_one = poll(client, first.json()["run_id"])
    assert run_one["status"] == "succeeded", run_one
    assert GoalContinuationRuntime.model_calls == 1
    view = client.get(f"/api/v1/conversations/{cid}/goal/execution").json()
    assert view["budget"]["rounds_used"] == 1
    assert view["budget"]["rounds_executed"] == 1
    assert view["budget"]["usage_complete"] is True
    assert view["budget"]["tokens_used"] == 150
    assert view["budget"]["cost_micros_used"] == 25
    assert view["budget"]["cost_authoritative"] is False
    assert view["budget"]["synthetic_rounds"] == 1
    assert view["rounds"][0]["state"] == "settled"
    assert view["rounds"][0]["usage_source"] == "synthetic_fixture"
    assert view["rounds"][0]["cost_authoritative"] is False
    run = service.require("runs", first.json()["run_id"])
    assert json.loads(run["execution_meta"])["goal_continuation"] is True
    messages = client.get(f"/api/v1/conversations/{cid}/messages").json()
    assert messages[-1]["role"] == "assistant"
    assert "目标轮次 1 已完成" in messages[-1]["text"]

    # The same explicit authorization covers the next bounded round. The fake
    # native projection advanced by one positive round; the host fence accepts
    # that exact chain and reserves a new revision plan.
    second_goal = current_goal(client, cid)
    assert second_goal["roundsStarted"] == 1
    second = start_round(client, cid, second_goal)
    assert second.status_code == 200, second.text
    run_two = poll(client, second.json()["run_id"])
    assert run_two["status"] == "succeeded", run_two
    assert GoalContinuationRuntime.model_calls == 2
    view = client.get(f"/api/v1/conversations/{cid}/goal/execution").json()
    assert view["budget"]["rounds_used"] == 2
    assert view["budget"]["rounds_executed"] == 2
    assert view["budget"]["tokens_used"] == 300
    assert [item["round_index"] for item in view["rounds"]] == [1, 2]
    assert all(item["state"] == "settled" for item in view["rounds"])
    messages = client.get(f"/api/v1/conversations/{cid}/messages").json()
    assert "目标轮次 2 已完成" in messages[-1]["text"]


def test_blocked_goal_resumes_with_one_revision_edge(goal_env):
    client, service, _app = goal_env
    cid = setup_conversation(client, service)
    blocked = {**GOAL, "phase": "blocked", "revision": 5}
    GoalContinuationRuntime.goal = blocked
    authorize(client, cid, goal=blocked)
    first = start_round(client, cid, current_goal(client, cid))
    assert first.status_code == 200, first.text
    assert first.json()["lease"]["arm_action"] == "resume"
    assert first.json()["lease"]["expected_goal_revision"] == 6
    assert poll(client, first.json()["run_id"])["status"] == "succeeded"
    lease = service.db.one("SELECT * FROM goal_execution_leases WHERE run_id=?",
                           (first.json()["run_id"],))
    assert lease["armed_goal_revision"] == 6
    assert lease["native_rounds_before"] == 0
    view = client.get(f"/api/v1/conversations/{cid}/goal/execution").json()
    assert view["rounds"][0]["state"] == "settled"
    assert view["authorization"]["goal_revision"] == 6


def test_round_count_budget_blocks_the_next_explicit_round(goal_env):
    client, service, _app = goal_env
    cid = setup_conversation(client, service)
    authorize(client, cid, max_rounds=1)
    first = start_round(client, cid, current_goal(client, cid))
    assert poll(client, first.json()["run_id"])["status"] == "succeeded"
    second = start_round(client, cid, current_goal(client, cid))
    assert second.status_code == 409
    assert error_code(second) == "GOAL_ROUND_BUDGET_EXHAUSTED"
    assert GoalContinuationRuntime.model_calls == 1


def test_token_and_cost_budgets_block_the_next_explicit_round(goal_env):
    client, service, _app = goal_env
    cid = setup_conversation(client, service)
    authorize(client, cid, max_rounds=4, token_budget=150)
    assert poll(client, start_round(client, cid, current_goal(client, cid)).json()["run_id"])["status"] == "succeeded"
    tokens = start_round(client, cid, current_goal(client, cid))
    assert tokens.status_code == 409
    assert error_code(tokens) == "GOAL_TOKEN_BUDGET_EXHAUSTED"

    assert client.delete(f"/api/v1/conversations/{cid}/goal/execution").status_code == 200
    # A new explicit approval still binds the current native revision and
    # round baseline; it does not replay the previous round.
    authorize(client, cid, goal=current_goal(client, cid), max_rounds=4,
              cost_budget_micros=25)
    assert poll(client, start_round(client, cid, current_goal(client, cid)).json()["run_id"])["status"] == "succeeded"
    cost = start_round(client, cid, current_goal(client, cid))
    assert cost.status_code == 409
    assert error_code(cost) == "GOAL_COST_BUDGET_EXHAUSTED"


def test_real_harness_usage_gap_fails_closed_after_one_round(goal_env):
    client, service, _app = goal_env
    cid = setup_conversation(client, service)
    authorize(client, cid)

    class UnknownCostRuntime(GoalContinuationRuntime):
        async def execute(self, run, message):
            lease = self.service.db.one(
                "SELECT * FROM goal_execution_leases WHERE run_id=?", (run["id"],))
            snapshot = {"id": lease["goal_id"], "revision": lease["expected_goal_revision"],
                        "phase": "active", "roundsStarted": lease["native_rounds_before"],
                        "maxGoalRounds": GoalContinuationRuntime.goal["maxGoalRounds"],
                        "activation": "armed"}
            await asyncio.to_thread(admit_goal_round_armed, self.service, lease["id"],
                                    run["id"], snapshot, self.session_id,
                                    lease["native_rounds_before"] + 1)
            GoalContinuationRuntime.model_calls += 1
            GoalContinuationRuntime.goal = {
                **GoalContinuationRuntime.goal,
                "revision": lease["expected_goal_revision"],
                "roundsStarted": lease["native_rounds_before"] + 1}
            await self.emit("message.delta", {
                "message_id": run["assistant_message_id"], "text": "本轮已完成"})
            # The real pinned Harness ledger shape: capture complete, cost None.
            await self.emit("usage.updated", {
                "source": "harness_provider", "capture_complete": True,
                "total_input_tokens": 100, "output_tokens": 50,
                "token_accounting": {"capture_complete": True,
                                     "provider_totals": {"input_tokens": 100,
                                                         "output_tokens": 50}},
                "estimated_cost_usd": None, "cost_is_estimate": True})

    service.manager.runtime_factory = UnknownCostRuntime
    first = start_round(client, cid, current_goal(client, cid))
    assert first.status_code == 200, first.text
    assert poll(client, first.json()["run_id"])["status"] == "succeeded"
    view = client.get(f"/api/v1/conversations/{cid}/goal/execution").json()
    assert view["budget"]["usage_complete"] is False
    assert view["budget"]["unknown_rounds"] == [1]
    assert view["budget"]["cost_micros_used"] is None
    assert view["can_start"] is False
    second = start_round(client, cid, current_goal(client, cid))
    assert second.status_code == 409, second.text
    assert error_code(second) == "GOAL_USAGE_UNVERIFIED"
    assert GoalContinuationRuntime.model_calls == 1


def test_stop_while_round_runs_prevents_replay_but_settles_usage(goal_env):
    client, service, _app = goal_env
    cid = setup_conversation(client, service)
    authorize(client, cid)
    release = threading.Event()
    GoalContinuationRuntime.block_after_admission = release
    first = start_round(client, cid, current_goal(client, cid))
    assert first.status_code == 200, first.text
    run_id = first.json()["run_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        lease = service.db.one("SELECT * FROM goal_execution_leases WHERE run_id=?", (run_id,))
        if lease and lease.get("armed_at"):
            break
        time.sleep(0.02)
    else:
        raise AssertionError("round was not admitted")
    stopped = client.delete(f"/api/v1/conversations/{cid}/goal/execution")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["authorization"]["status"] == "revoked"
    assert stopped.json()["authorization"]["revoked_reason"] == "user_revoked"
    assert stopped.json()["active_lease"] is None
    release.set()
    assert poll(client, run_id)["status"] == "succeeded"
    view = client.get(f"/api/v1/conversations/{cid}/goal/execution").json()
    assert view["rounds"][0]["state"] == "aborted"
    assert view["rounds"][0]["usage_status"] == "complete"
    assert view["budget"]["tokens_used"] == 150
    # The stop is durable: the next explicit start cannot reuse the approval.
    second = start_round(client, cid, current_goal(client, cid))
    assert second.status_code == 409
    assert error_code(second) == "GOAL_NOT_AUTHORIZED"
    assert GoalContinuationRuntime.model_calls == 1


def test_arm_failure_revokes_and_never_replays(goal_env):
    client, service, _app = goal_env
    cid = setup_conversation(client, service)
    authorize(client, cid)
    GoalContinuationRuntime.fail_before_admission = "GOAL_ARM_FAILED"
    first = start_round(client, cid, current_goal(client, cid))
    assert first.status_code == 200, first.text
    run_id = first.json()["run_id"]
    run = poll(client, run_id)
    assert run["status"] == "failed"
    assert run["error_code"] == "GOAL_ARM_FAILED"
    assert GoalContinuationRuntime.model_calls == 0
    authorization = service.db.one(
        "SELECT * FROM goal_execution_authorizations WHERE conversation_id=?", (cid,))
    assert authorization["status"] == "revoked"
    assert authorization["revoked_reason"] == "goal_round_not_admitted"
    lease = service.db.one("SELECT * FROM goal_execution_leases WHERE run_id=?", (run_id,))
    assert lease["state"] == "aborted"
    assert lease["usage_status"] is None
    second = start_round(client, cid, current_goal(client, cid))
    assert second.status_code == 409
    assert error_code(second) == "GOAL_NOT_AUTHORIZED"


def test_restart_aborts_a_reserved_round_without_replay(goal_env):
    client, service, _app = goal_env
    cid = setup_conversation(client, service)
    authorize(client, cid)
    owner = service.require("users", service.require("conversations", cid)["user_id"])
    reserved = start_goal_round(service, cid, owner, current_goal(client, cid), SESSION)
    run_id = reserved["run_id"]
    assert service.db.one("SELECT status FROM runs WHERE id=?", (run_id,))["status"] == "queued"
    # This is the exact startup recovery path: interrupt every non-terminal run
    # with approval_source='restart', then revoke/abort Goal reservations.
    service.manager.recover()
    assert service.db.one("SELECT status FROM runs WHERE id=?", (run_id,))["status"] == "interrupted"
    view = client.get(f"/api/v1/conversations/{cid}/goal/execution").json()
    assert view["authorization"]["status"] == "revoked"
    assert view["authorization"]["revoked_reason"] == "service_restarted"
    assert view["active_lease"] is None
    lease = service.db.one("SELECT * FROM goal_execution_leases WHERE run_id=?", (run_id,))
    assert lease["state"] == "aborted"
    assert lease["usage_status"] is None
    assert service.db.one("SELECT count(*) n FROM runs WHERE status IN ('queued','running')")["n"] == 0
    assert GoalContinuationRuntime.model_calls == 0


def test_start_rejects_stale_goal_and_unknown_usage(goal_env):
    client, service, _app = goal_env
    cid = setup_conversation(client, service)
    authorize(client, cid)
    stale = start_round(client, cid, {**GOAL, "revision": GOAL["revision"] + 1})
    assert stale.status_code == 409
    assert error_code(stale) == "GOAL_STALE_REVISION"
    assert service.db.one("SELECT count(*) n FROM runs")["n"] == 0
    # A paused goal cannot start; the owner must resume it explicitly.
    GoalContinuationRuntime.goal = {**GOAL, "phase": "paused"}
    paused = start_round(client, cid, current_goal(client, cid))
    assert paused.status_code == 409
    assert error_code(paused) == "GOAL_GOAL_NOT_ACTIVE"


def test_other_user_cannot_start_or_stop_round(goal_env):
    client, service, _app = goal_env
    cid = setup_conversation(client, service)
    authorize(client, cid)
    goal = current_goal(client, cid)
    service.create_user({"username": "other", "display_name": "other",
                         "password": PASSWORD, "role": "member"})
    login(client, "other")
    assert start_round(client, cid, goal).status_code == 404
    assert client.delete(f"/api/v1/conversations/{cid}/goal/execution").status_code == 404
    assert GoalContinuationRuntime.model_calls == 0


def test_execution_view_requires_owner_and_reports_rounds(goal_env):
    client, service, _app = goal_env
    cid = setup_conversation(client, service)
    authorize(client, cid)
    first = start_round(client, cid, current_goal(client, cid))
    assert first.status_code == 200
    poll(client, first.json()["run_id"])
    body = client.get(f"/api/v1/conversations/{cid}/goal/execution").json()
    assert body["harness_session_id"] == SESSION
    assert body["rounds"][0]["run_id"] == first.json()["run_id"]
    assert body["rounds"][0]["assistant_message_id"]
    assert body["rounds"][0]["expected_goal_revision"] == GOAL["revision"] + 2
    assert body["rounds"][0]["armed_goal_revision"] == GOAL["revision"] + 2
    assert body["rounds"][0]["native_rounds_before"] == 0


@pytest.mark.parametrize(("objective", "model_calls"), [
    ("Synthetic authorized goal", 1),
    ("CASE_DELEGATE summarize the two fixture tasks", 4),
], ids=["text", "two-children"])
def test_real_harness_goal_round_end_to_end(tmp_path, model_server, objective, model_calls):
    """Pinned Node + official driver + loopback synthetic model, one real round."""
    import os
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires pinned Node Harness")
    base, requests = model_server
    settings = image_fixture_settings(tmp_path, node, source, base)
    settings.harness_goal_state_enabled = True
    settings.harness_goal_execution_enabled = True
    settings.harness_team_state_enabled = True
    settings.harness_team_tasks_enabled = True
    app = create_app(settings)
    with TestClient(app) as client:
        service = app.state.service
        service.create_user({"username": "admin", "display_name": "admin",
                             "password": PASSWORD, "role": "admin"})
        login(client)
        cid = conversation(client)["id"]
        first = client.post(f"/api/v1/conversations/{cid}/messages",
                            json={"client_request_id": "goal-e2e-bootstrap",
                                  "text": "Goal execution fixture", "file_ids": []})
        assert first.status_code == 202, first.text
        assert wait_run(client, first.json()["run_id"])["status"] == "succeeded"
        baseline = len(requests)
        goal_path = f"/api/v1/conversations/{cid}/goal"
        created = client.post(goal_path, json={
            "action": "create", "objective": objective,
            "maxGoalRounds": 4})
        assert created.status_code == 200, created.text
        goal = created.json()["goal"]
        assert goal["activation"] == "disarmed" and goal["roundsStarted"] == 0
        owner = service.require("conversations", cid)
        authorization = client.post(goal_path + "/execution", json={
            "goal_id": goal["id"], "goal_revision": goal["revision"],
            "harness_session_id": owner["harness_session_id"], "max_rounds": 2,
            "token_budget": 10_000, "cost_budget_micros": 10_000,
            "time_budget_seconds": 3600, "confirm": True})
        assert authorization.status_code == 200, authorization.text
        started = client.post(goal_path + "/execution/rounds", json={
            "goal_id": goal["id"], "goal_revision": goal["revision"],
            "confirm": True})
        assert started.status_code == 200, started.text
        run_id = started.json()["run_id"]
        settled = wait_run(client, run_id)
        assert settled["status"] == "succeeded", settled
        # Multiple model calls and child tasks may belong to one native Goal round.
        assert len(requests) - baseline == model_calls
        view = client.get(goal_path + "/execution").json()
        assert view["budget"]["rounds_executed"] == 1
        assert view["rounds"][0]["armed"] is True
        assert view["rounds"][0]["armed_goal_revision"] == goal["revision"] + 2
        assert view["budget"]["usage_complete"] is False
        assert view["rounds"][0]["usage_status"] == "unknown"
        assert view["rounds"][0]["cost_micros"] is None
        native = client.get(goal_path).json()["goal"]
        assert native["roundsStarted"] == 1
        assert native["activation"] == "disarmed"
        messages = client.get(f"/api/v1/conversations/{cid}/messages").json()
        assert messages[-1]["role"] == "assistant" and messages[-1]["text"].strip()
        if model_calls > 1:
            plan = client.get(f"/api/v1/runs/{run_id}/subagents").json()
            assert plan["counts"]["succeeded"] == 2, plan
            assert "子任务结果已汇总" in messages[-1]["text"]
        # The pinned usage ledger has no authoritative cost. The next round is
        # refused rather than priced as zero; no second model request is made.
        second = client.post(goal_path + "/execution/rounds", json={
            "goal_id": native["id"], "goal_revision": native["revision"],
            "confirm": True})
        assert second.status_code == 409, second.text
        assert error_code(second) == "GOAL_USAGE_UNVERIFIED"
        assert len(requests) == baseline + model_calls
