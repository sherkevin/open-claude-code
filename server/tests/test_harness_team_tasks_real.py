"""Real pinned Node Team task lifecycle over the owner-scoped product API.

Opt in with HARNESS_TEST_NODE and HARNESS_TEST_SOURCE. The model endpoint is a
local synthetic Messages server; the Node process, JSONL session log, official
agentTeams board, FastAPI app and SQLite host are real. No customer data,
external MCP, shell, subagent or business write is involved.
"""
import os
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from test_api import PASSWORD, conversation, login, send
from test_harness_real_runtime import image_fixture_settings, model_server, wait_run


def _base(cid):
    return f"/api/v1/conversations/{cid}/team"


def _authorize(client, cid, session, operations=20):
    return client.post(_base(cid) + "/authorization", json={
        "harness_session_id": session, "max_operations": operations,
        "expires_in_seconds": 3600, "confirm": True,
    })


def _create(client, cid, session, request_id, subject, description,
            blocked_by=None, write_scopes=None):
    return client.post(_base(cid) + "/tasks", json={
        "harness_session_id": session, "client_request_id": request_id,
        "subject": subject, "description": description,
        "blocked_by": blocked_by or [], "write_scopes": write_scopes or [],
    })


def _transition(client, cid, session, request_id, task, action):
    return client.post(_base(cid) + f"/tasks/{quote(task['id'], safe='')}/{action}", json={
        "harness_session_id": session, "client_request_id": request_id,
        "expected_revision": task["revision"],
    })


def test_real_team_task_lifecycle_restart_and_zero_model(tmp_path, model_server):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires pinned Node Harness")
    base, requests = model_server
    settings = image_fixture_settings(tmp_path, node, source, base)
    settings.harness_team_state_enabled = True
    settings.harness_team_tasks_enabled = True
    app = create_app(settings)
    with TestClient(app) as client:
        service = app.state.service
        service.create_user({"username": "admin", "display_name": "管理员",
                             "password": PASSWORD, "role": "admin"})
        login(client)
        cid = conversation(client)["id"]
        run = send(client, cid, "Team task lifecycle fixture")["run_id"]
        assert wait_run(client, run)["status"] == "succeeded"
        session = service.db.one(
            "SELECT harness_session_id FROM conversations WHERE id=?", (cid,))["harness_session_id"]
        assert session
        baseline = len(requests)
        team = _base(cid)

        assert client.get(team).json() == {
            "board": {"enabled": False, "tasks": []}, "execution_enabled": False}
        assert _create(client, cid, session, "team-create-too-early",
                       "过早任务", "任务板尚未启用").json()["error"]["code"] == "TEAM_NOT_AUTHORIZED"
        authorized = _authorize(client, cid, session)
        assert authorized.status_code == 200, authorized.text
        assert authorized.json()["authorization_active"] is True
        assert authorized.json()["operations_remaining"] == 20

        disabled = _create(client, cid, session, "team-create-disabled",
                           "未启用任务板", "预检应拒绝")
        assert disabled.status_code == 409, disabled.text
        assert disabled.json()["error"]["code"] == "TEAM_BOARD_DISABLED"

        enabled = client.post(team + "/board", json={
            "harness_session_id": session, "client_request_id": "team-board-enable",
            "action": "enable", "confirm": True})
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["board"]["enabled"] is True
        assert enabled.json()["execution_enabled"] is False

        first = _create(client, cid, session, "team-create-first", "整理交付清单",
                        "只维护任务板元数据", write_scopes=["server/team/a"])
        assert first.status_code == 200, first.text
        task_a = first.json()["task"]
        assert task_a["status"] == "pending"
        second = _create(client, cid, session, "team-create-second", "复核交付清单",
                         "依赖第一项任务", blocked_by=[task_a["id"]],
                         write_scopes=["server/team/b"])
        assert second.status_code == 200, second.text
        task_b = second.json()["task"]
        board = client.get(team).json()["board"]
        assert next(item for item in board["tasks"] if item["id"] == task_b["id"])["ready"] is False

        claimed = _transition(client, cid, session, "team-claim-first", task_a, "claim")
        assert claimed.status_code == 200, claimed.text
        task_a_claimed = claimed.json()["task"]
        assert task_a_claimed["status"] == "in_progress"
        assert task_a_claimed["revision"] > task_a["revision"]

        stale = _transition(client, cid, session, "team-complete-stale",
                            {**task_a, "revision": task_a["revision"]}, "complete")
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["code"] == "TEAM_TASK_STALE_REVISION"

        completed = _transition(client, cid, session, "team-complete-first",
                                task_a_claimed, "complete")
        assert completed.status_code == 200, completed.text
        assert completed.json()["task"]["status"] == "completed"
        board = client.get(team).json()["board"]
        assert next(item for item in board["tasks"] if item["id"] == task_b["id"])["ready"] is True

        replay = _create(client, cid, session, "team-create-first", "整理交付清单",
                         "只维护任务板元数据", write_scopes=["server/team/a"])
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["task"]["id"] == task_a["id"]
        assert len(client.get(team).json()["board"]["tasks"]) == 2
        collaboration = client.get(team + "/collaboration").json()
        assert collaboration["operations_used"] == 5
        assert any(op["status"] == "rejected" and op["error_code"] == "TEAM_TASK_STALE_REVISION"
                   for op in collaboration["operations"])

        revoked = client.delete(team + "/authorization")
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["authorization"]["status"] == "revoked"
        after_revoke = _create(client, cid, session, "team-create-after-revoke",
                               "撤销后任务", "应被拒绝")
        assert after_revoke.status_code == 409, after_revoke.text
        assert after_revoke.json()["error"]["code"] == "TEAM_NOT_AUTHORIZED"

        # Leave an active grant so restart recovery has something to revoke.
        assert _authorize(client, cid, session).status_code == 200
        assert len(requests) == baseline

        service.create_user({"username": "other", "display_name": "其他",
                             "password": PASSWORD, "role": "member"})
        login(client, "other")
        assert client.get(team + "/collaboration").status_code == 404
        assert _authorize(client, cid, session).status_code == 404
        login(client)

    with TestClient(create_app(settings)) as client:
        login(client)
        team = _base(cid)
        recovered = client.get(team)
        assert recovered.status_code == 200, recovered.text
        tasks = recovered.json()["board"]["tasks"]
        assert len(tasks) == 2
        assert next(item for item in tasks if item["id"] == task_a["id"])["status"] == "completed"
        collaboration = client.get(team + "/collaboration").json()
        assert collaboration["authorization"]["status"] == "revoked"
        assert collaboration["authorization"]["revoked_reason"] == "service_restarted"
        assert len(collaboration["operations"]) >= 5
        assert len(requests) == baseline

        assert _authorize(client, cid, session).status_code == 200
        current_b = next(item for item in tasks if item["id"] == task_b["id"])
        claimed_b = _transition(client, cid, session, "team-claim-second", current_b, "claim")
        assert claimed_b.status_code == 200, claimed_b.text
        assert claimed_b.json()["task"]["status"] == "in_progress"
        completed_b = _transition(client, cid, session, "team-complete-second",
                                  claimed_b.json()["task"], "complete")
        assert completed_b.status_code == 200, completed_b.text
        assert completed_b.json()["task"]["status"] == "completed"
        assert len(requests) == baseline
