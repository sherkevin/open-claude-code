"""Round ceilings, durable request identities, distinct session usage and admission errors."""
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import dump, now
from app.goal_execution import admit_goal_round_armed, settle_goal_round, start_goal_round
from app.goal_round_cost import (LoopbackGoalMeter, begin_goal_attempt, goal_attempt_endpoint,
                                 settle_goal_attempt)
from app.security import AppError
from test_goal_execution import (GOAL, SESSION, auth_payload, authorize_ok, close_run,
                                 goal_env, path, setup_conversation, user_row)

BASE = "http://127.0.0.1:65431"
ENDPOINT = BASE + "/v1/messages"


def active_round(goal_env, monkeypatch, meter, *, base=BASE + "/v1/"):
    client, service = goal_env
    cid = setup_conversation(client, service)
    # This test's provider configuration is local. No harness/model is launched.
    provider = service.validated_provider
    monkeypatch.setattr(service, "validated_provider", lambda s: {**provider(s), "ANTHROPIC_BASE_URL": base})
    service.goal_round_meter = meter
    authorize_ok(client, cid, round_cost_budget_micros=39)
    run = start_goal_round(service, cid, user_row(service, client), GOAL, SESSION)
    rid, lease = run["run_id"], run["lease"]
    service.db.execute("UPDATE runs SET status='running' WHERE id=?", (rid,))
    service.db.execute("UPDATE conversations SET active_run_id=? WHERE id=?", (rid, cid))
    armed = {**GOAL, "revision": lease["expected_goal_revision"], "activation": "armed"}
    admit_goal_round_armed(service, lease["id"], rid, armed, SESSION, lease["native_rounds_before"] + 1)
    return client, service, cid, rid, lease["id"]


def receipt(ids_and_counts):
    # Same persisted schema as HarnessUsageLedger.receipt(), one record per real UUID.
    stages = [{"attempt_id": aid, "source": "harness_provider", "provider_call": True,
               "reported_prompt_tokens": prompt, "provider_usage": {"output_tokens": output}}
              for aid, prompt, output in ids_and_counts]
    return {"token_accounting": {"receipt_schema": "harness-http-usage/v1",
                                 "capture_complete": True, "stages": stages}}


def child(service, cid, rid):
    child_id = str(uuid.uuid4())
    owner = service.require("conversations", cid)["user_id"]
    service.db.execute(
        "INSERT INTO subagent_children(child_id,parent_run_id,root_run_id,user_id,conversation_id,"
        "depth,role,status,task_digest,capability_snapshot,workspace_id,memory_scope_id,created_at) "
        "VALUES(?,?,?,?,?,1,'harness_analysis','running','fixture','{}',?,?,?)",
        (child_id, rid, rid, owner, cid, child_id, child_id, now()))
    return child_id, owner


def test_optional_budget_and_rejects_before_run_or_lease(goal_env):
    client, service = goal_env
    cid = setup_conversation(client, service)
    for invalid in (0, True, -1, 10_001, 2.5):
        result = client.post(path(cid), json=auth_payload(round_cost_budget_micros=invalid))
        assert result.status_code == 422, result.text
    assert authorize_ok(client, cid, round_cost_budget_micros=5)["round_cost_budget_micros"] == 5
    assert client.get(path(cid)).json()["round_cost_block"]["code"] == "GOAL_ROUND_PRICE_UNAVAILABLE"
    count = service.db.one("SELECT COUNT(*) AS n FROM runs")["n"]
    response = client.post(path(cid) + "/rounds", json={"goal_id": GOAL["id"], "goal_revision": GOAL["revision"], "confirm": True})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "GOAL_ROUND_PRICE_UNAVAILABLE"
    assert service.db.one("SELECT COUNT(*) AS n FROM runs")["n"] == count
    assert not service.db.all("SELECT * FROM goal_round_cost_attempts")


@pytest.mark.parametrize("base", [BASE, BASE + "/", BASE + "/v1", BASE + "/v1/"])
def test_price_includes_unused_children_and_retries_before_start(goal_env, monkeypatch, base):
    with pytest.raises(AppError) as err:
        active_round(goal_env, monkeypatch, LoopbackGoalMeter(ENDPOINT, (10, 10), (7, 7), (6,)), base=base)
    assert err.value.code == "GOAL_ROUND_RESERVE_EXCEEDED"
    assert not goal_env[1].db.all("SELECT * FROM goal_execution_leases")


def test_atomic_attempts_settle_each_session_and_recheck_stored_sources(goal_env, monkeypatch):
    meter = LoopbackGoalMeter(ENDPOINT, (10, 10), (7, 7), (5,))
    client, service, cid, rid, lid = active_round(goal_env, monkeypatch, meter)
    assert goal_attempt_endpoint(service, rid) == ENDPOINT
    assert client.get(path(cid)).json()["budget"]["cost_micros_remaining"] is None
    pid, kid, retry = (str(uuid.uuid4()) for _ in range(3))
    def try_parent():
        try:
            return begin_goal_attempt(service, rid, pid, "parent")
        except AppError as e:
            return e.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(map(str, pool.map(lambda _: try_parent(), range(2)))) == ["10", "GOAL_ATTEMPT_REPLAY"]
    child_id, owner = child(service, cid, rid)
    assert begin_goal_attempt(service, rid, kid, "child", child_id=child_id) == 7
    assert goal_attempt_endpoint(service, rid) == ENDPOINT
    with pytest.raises(AppError) as bad_retry:
        begin_goal_attempt(service, rid, retry, "retry", retry_of=pid)
    assert bad_retry.value.code == "GOAL_ATTEMPT_INVALID"
    settle_goal_attempt(service, rid, pid, 0, retryable=True)
    assert begin_goal_attempt(service, rid, retry, "retry", retry_of=pid) == 5
    settle_goal_attempt(service, rid, kid, 4)
    settle_goal_attempt(service, rid, kid, 4)
    for conflicting in (6, 8, None):
        with pytest.raises(AppError):
            settle_goal_attempt(service, rid, kid, conflicting)
    settle_goal_attempt(service, rid, retry, 3)
    service.db.execute("INSERT INTO model_usage_calls VALUES(?,?,?,?,?,?,?,?,?)",
                       (child_id, owner, "subagent", "fixture", "fixture", "succeeded",
                        dump(receipt([(kid, 11, 5)])), now(), now()))
    close_run(service, rid, receipt([(pid, 0, 0), (retry, 12, 7)]))
    summary = settle_goal_round(service, lid, "succeeded")["lease"]
    assert summary["total_tokens"] == 35 and summary["cost_micros"] == 7
    ledger = summary["cost_reservation"]
    assert ledger["reserved_micros"] == 39 and ledger["settled_micros"] == 7
    assert [a["state"] for a in ledger["attempts"]].count("unused") == 2
    assert client.get(path(cid)).json()["budget"]["tokens_used"] == 35
    # A foreign request or a missing child's tokens cannot be replaced with parent usage.
    service.db.execute("UPDATE model_usage_calls SET usage=? WHERE id=?",
                       (dump(receipt([(pid, 11, 5)])), child_id))
    assert client.get(path(cid)).json()["budget"]["tokens_used"] is None
    service.db.execute("UPDATE model_usage_calls SET usage=? WHERE id=?", (dump(receipt([(kid, 11, 5)])), child_id))
    service.db.execute("UPDATE runs SET usage=? WHERE id=?", (dump(receipt([(pid, None, None), (retry, 12, 7)])), rid))
    view = client.get(path(cid)).json()
    assert view["budget"]["tokens_used"] is None and view["can_start"] is False
    assert view["rounds"][0]["cost_reservation"]["settled_micros"] == 7
    assert view["rounds"][0]["cost_micros"] == view["budget"]["cost_micros_used"] == 7
    assert view["budget"]["cost_micros_remaining"] == 9_993 and view["budget"]["synthetic_rounds"] == 1


def test_missing_receipt_and_changed_meter_stay_unknown(goal_env, monkeypatch):
    client, service, cid, rid, lid = active_round(goal_env, monkeypatch, LoopbackGoalMeter(ENDPOINT, (9,)))
    aid = str(uuid.uuid4())
    assert begin_goal_attempt(service, rid, aid, "parent") == 9
    with pytest.raises(AppError) as exhausted:
        begin_goal_attempt(service, rid, str(uuid.uuid4()), "parent")
    assert exhausted.value.code == "GOAL_ATTEMPT_NOT_RESERVED"
    with pytest.raises(AppError):
        settle_goal_attempt(service, rid, aid, 10)
    service.goal_round_meter = LoopbackGoalMeter(ENDPOINT, (8,))
    with pytest.raises(AppError):
        settle_goal_attempt(service, rid, aid, 3)
    del service.goal_round_meter
    with pytest.raises(AppError):
        goal_attempt_endpoint(service, rid)
    close_run(service, rid, receipt([(aid, 4, 3)]))
    settled = settle_goal_round(service, lid, "succeeded")["lease"]
    assert settled["usage_status"] == "unknown"
    assert settled["cost_reservation"]["settled_micros"] is None
    assert settled["cost_reservation"]["attempts"][0]["state"] == "started"
    assert client.get(path(cid)).json()["can_start"] is False
