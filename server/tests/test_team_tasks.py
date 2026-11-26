"""Host authorization/audit tests for the native Team task lifecycle pilot.

These tests use the injected fake runtime only for conversation setup; they do
not execute a model or the pinned Node Harness. Real native board mutations are
covered separately in test_harness_team_tasks_real.py.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.harness_runtime import _TeamTaskReceipt
from app.main import create_app
from app.security import AppError
from app.team_tasks import (
    acknowledge_team_operation, begin_team_operation, confirm_team_operation,
    recover_team_tasks, team_collaboration_view, uncertain_team_operation,
    require_pending_team_operation, stop_team_tasks,
)
from test_api import PASSWORD, FakeRuntime, client, configured, conversation, login

PIN = "fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159"
SESSION = "workspace-team-fixture"


def _settings(tmp_path, *, tasks=True):
    return Settings(data_dir=tmp_path / "data", provider_settings="",
                    origins=("http://testserver",), runtime_type="harness",
                    harness_team_state_enabled=True,
                    harness_team_tasks_enabled=tasks)


@pytest.fixture
def team_env(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings, runtime_factory=FakeRuntime)
    with TestClient(app) as client:
        service = app.state.service
        service.create_user({"username": "admin", "display_name": "管理员",
                             "password": PASSWORD, "role": "admin"})
        login(client)
        cid = conversation(client)["id"]
        service.db.execute(
            "UPDATE conversations SET runtime_type='harness',harness_session_id=? WHERE id=?",
            (SESSION, cid))
        user = service.db.one("SELECT * FROM users WHERE username='admin'")
        yield client, service, cid, user


def _auth(client, cid, *, max_operations=20, expires=3600, session=SESSION):
    return client.post(f"/api/v1/conversations/{cid}/team/authorization", json={
        "harness_session_id": session, "max_operations": max_operations,
        "expires_in_seconds": expires, "confirm": True,
    })


def _create_request(request_id="team-create-0001", **overrides):
    return {"action": "create", "harness_session_id": SESSION,
            "client_request_id": request_id, "subject": "合成任务",
            "description": "仅维护任务板元数据", "blocked_by": [],
            "write_scopes": [], **overrides}


def _valid_receipt(action="create", status="pending", task_id="task-0001"):
    task = {"id": task_id, "revision": 1, "subject": "合成任务",
            "description": "仅维护任务板元数据", "status": status,
            "blockedBy": [], "writeScopes": [], "ready": True,
            "writeScopeWarnings": []}
    board = {"enabled": True,
             "tasks": [{**task, "ownerId": None, "ownerName": None}]}
    return {"schema": "workspace-team-task/v1", "sourceCommit": PIN,
            "sessionId": SESSION, "action": action, "boardEnabled": True,
            "board": board, "task": task, "executionEnabled": False}


def _host_receipt():
    # The runtime validates the native camelCase RPC, then normalizes it
    # before handing it to the host's durable operation settlement.
    receipt = _TeamTaskReceipt.model_validate(_valid_receipt())
    return {"action": receipt.action, "board_enabled": receipt.boardEnabled,
            "board": receipt.board.model_dump(exclude_none=True),
            "task": receipt.task.model_dump(exclude_none=True),
            "execution_enabled": False}


def test_team_task_switch_requires_state(tmp_path):
    with pytest.raises(ValueError, match="AGENT_HARNESS_TEAM_STATE_ENABLED"):
        Settings(data_dir=tmp_path / "data", provider_settings="",
                 origins=("http://testserver",), runtime_type="harness",
                 harness_team_tasks_enabled=True)


def test_team_task_endpoints_are_absent_by_default(client):
    cid = conversation(client)["id"]
    base = f"/api/v1/conversations/{cid}/team"
    assert client.get(base + "/collaboration").status_code == 404
    assert _auth(client, cid).status_code == 404


def test_task_lifecycle_needs_its_own_switch(tmp_path):
    settings = _settings(tmp_path, tasks=False)
    app = create_app(settings, runtime_factory=FakeRuntime)
    with TestClient(app) as client:
        service = app.state.service
        service.create_user({"username": "admin", "display_name": "管理员",
                             "password": PASSWORD, "role": "admin"})
        login(client)
        cid = conversation(client)["id"]
        assert client.get(f"/api/v1/conversations/{cid}/team/collaboration").status_code == 404
        assert _auth(client, cid).status_code == 404


def test_team_task_receipt_fails_closed():
    assert _TeamTaskReceipt.model_validate(_valid_receipt()).action == "create"
    claimed = _valid_receipt("claim", "in_progress")
    assert _TeamTaskReceipt.model_validate(claimed).task.status == "in_progress"
    completed = _valid_receipt("complete", "completed")
    assert _TeamTaskReceipt.model_validate(completed).task.status == "completed"
    enable = {"schema": "workspace-team-task/v1", "sourceCommit": PIN,
              "sessionId": SESSION, "action": "enable", "boardEnabled": True,
              "board": {"enabled": True, "tasks": []}, "task": None,
              "executionEnabled": False}
    assert _TeamTaskReceipt.model_validate(enable).task is None
    for invalid in (
        {**enable, "executionEnabled": True},
        {**enable, "sourceCommit": "wrong"},
        {**enable, "action": "disable"},
        {**claimed, "task": {**claimed["task"], "status": "pending"}},
        {**_valid_receipt(), "boardEnabled": False},
        {**_valid_receipt(), "task": {**_valid_receipt()["task"], "id": "task-other"}},
    ):
        with pytest.raises(ValueError):
            _TeamTaskReceipt.model_validate(invalid)
    # Unknown task-level projection extras are dropped, never forwarded.
    extra = _valid_receipt()
    extra["task"]["internalHint"] = "must-not-cross"
    assert "internalHint" not in _TeamTaskReceipt.model_validate(extra).task.model_dump()


def test_owner_authorization_revoke_and_isolation(team_env):
    client, service, cid, user = team_env
    base = f"/api/v1/conversations/{cid}/team"
    wrong = _auth(client, cid, session="workspace-other")
    assert wrong.status_code == 409
    assert wrong.json()["error"]["code"] == "TEAM_SESSION_CHANGED"
    response = _auth(client, cid, max_operations=5)
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["schema"] == "team-collaboration/v1"
    assert view["authorization"]["status"] == "active"
    assert view["authorization"]["harness_session_id"] == SESSION
    assert view["session_matches"] is True
    assert view["permission_matches"] is True
    assert view["authorization_active"] is True
    assert view["operations_remaining"] == 5
    assert view["execution_enabled"] is False and view["task_execution_enabled"] is False
    assert _auth(client, cid).json()["error"]["code"] == "TEAM_AUTHORIZATION_EXISTS"

    service.create_user({"username": "other", "display_name": "其他",
                         "password": PASSWORD, "role": "member"})
    login(client, "other")
    assert client.get(base + "/collaboration").status_code == 404
    assert _auth(client, cid).status_code == 404
    login(client)

    revoked = client.delete(base + "/authorization")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["authorization"]["status"] == "revoked"
    assert revoked.json()["authorization"]["revoked_reason"] == "user_revoked"
    assert revoked.json()["authorization_active"] is False


def test_operation_admission_replay_uncertain_and_ack(team_env):
    client, service, cid, user = team_env
    assert _auth(client, cid, max_operations=5).status_code == 200
    request = _create_request()
    operation = begin_team_operation(service, cid, user, request)
    assert operation["native"] == {"action": "create", "subject": "合成任务",
                                   "description": "仅维护任务板元数据",
                                   "blockedBy": [], "writeScopes": []}
    assert operation["operation"]["status"] == "pending"
    with pytest.raises(AppError) as busy:
        begin_team_operation(service, cid, user, request)
    assert busy.value.code == "TEAM_OPERATION_IN_PROGRESS"

    confirmed = confirm_team_operation(service, operation["id"], _host_receipt())
    assert confirmed["status"] == "confirmed"
    assert confirmed["task_id"] == "task-0001" and confirmed["task_status"] == "pending"
    stored = service.db.one("SELECT result FROM team_task_operations WHERE id=?",
                            (operation["id"],))
    assert "合成任务" not in stored["result"] and "仅维护任务板元数据" not in stored["result"]
    replay = begin_team_operation(service, cid, user, request)
    assert replay["replay"] is True and replay["id"] == operation["id"]
    with pytest.raises(AppError) as conflict:
        begin_team_operation(service, cid, user, {**request, "subject": "不同任务"})
    assert conflict.value.code == "TEAM_REQUEST_CONFLICT"

    second = begin_team_operation(service, cid, user, _create_request("team-create-0002"))
    uncertain_team_operation(service, second["id"], "TEAM_TASK_UNCERTAIN")
    with pytest.raises(AppError) as blocked:
        begin_team_operation(service, cid, user, _create_request("team-create-0003"))
    assert blocked.value.code == "TEAM_OPERATION_UNCERTAIN"
    view = team_collaboration_view(service, cid, user)
    assert view["unresolved_operation"]["id"] == second["id"]
    assert view["unresolved_operation"]["status"] == "uncertain"

    acknowledged = acknowledge_team_operation(service, cid, user, second["id"])
    assert acknowledged["acknowledged_at"]
    third = begin_team_operation(service, cid, user, _create_request("team-create-0003"))
    assert third["operation"]["status"] == "pending"


def test_operation_limit_expiry_and_recovery(team_env):
    client, service, cid, user = team_env
    assert _auth(client, cid, max_operations=1, expires=600).status_code == 200
    first = begin_team_operation(service, cid, user, _create_request("team-create-0001"))
    confirm_team_operation(service, first["id"], _host_receipt())
    with pytest.raises(AppError) as exhausted:
        begin_team_operation(service, cid, user, _create_request("team-create-0002"))
    assert exhausted.value.code == "TEAM_AUTHORIZATION_EXHAUSTED"

    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    service.db.execute("UPDATE team_task_authorizations SET created_at=? WHERE conversation_id=?",
                       (old, cid))
    with pytest.raises(AppError) as expired:
        begin_team_operation(service, cid, user, _create_request("team-create-0003"))
    assert expired.value.code == "TEAM_AUTHORIZATION_EXPIRED"

    service.db.execute("UPDATE team_task_authorizations SET created_at=? WHERE conversation_id=?",
                       (datetime.now(timezone.utc).isoformat(), cid))
    service.db.execute("UPDATE team_task_authorizations SET max_operations=5 WHERE conversation_id=?",
                       (cid,))
    pending = begin_team_operation(service, cid, user, _create_request("team-create-0004"))
    recovered = recover_team_tasks(service)
    assert recovered["revoked"] >= 1 and recovered["fenced"] >= 1
    view = team_collaboration_view(service, cid, user)
    assert view["authorization"]["status"] == "revoked"
    assert view["authorization"]["revoked_reason"] == "service_restarted"
    assert view["unresolved_operation"]["id"] == pending["id"]
    assert view["unresolved_operation"]["status"] == "uncertain"


def test_user_role_change_revokes_team_authorization(team_env):
    client, service, cid, user = team_env
    assert _auth(client, cid).status_code == 200
    service.create_user({"username": "admin2", "display_name": "管理员二",
                         "password": PASSWORD, "role": "admin"})
    service.patch_user(user["id"], {"role": "member"}, user)
    view = team_collaboration_view(service, cid, user)
    assert view["authorization"]["status"] == "revoked"
    assert view["authorization"]["revoked_reason"] == "user_role_changed"


def test_pending_operation_requires_current_authorization_before_dispatch(team_env):
    client, service, cid, user = team_env
    assert _auth(client, cid, max_operations=1).status_code == 200
    operation = begin_team_operation(service, cid, user, _create_request())
    require_pending_team_operation(service, cid, user, SESSION, operation["id"])
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    service.db.execute("UPDATE team_task_authorizations SET created_at=? WHERE conversation_id=?",
                       (old, cid))
    with pytest.raises(AppError) as expired:
        require_pending_team_operation(service, cid, user, SESSION, operation["id"])
    assert expired.value.code == "TEAM_AUTHORIZATION_EXPIRED"
    stop_team_tasks(service, cid, "user_revoked")
    with pytest.raises(AppError) as revoked:
        require_pending_team_operation(service, cid, user, SESSION, operation["id"])
    assert revoked.value.code == "TEAM_NOT_AUTHORIZED"
