"""Safety receipts and approval state across a real application restart."""

import pytest
from fastapi.testclient import TestClient

from app.db import now, uid
from app.main import create_app
from app.run_recovery import record_dispatch
from test_api import FakeRuntime, PASSWORD, configured, conversation, login  # noqa: F401


@pytest.mark.parametrize("stage,dispatched", [("queued", False), ("running", True), ("awaiting_approval", False)])
def test_restart_interrupts_without_replaying(configured, stage, dispatched):
    app = create_app(configured, FakeRuntime)
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        cid = conversation(client)["id"]
        result, _ = service.prepare_run(cid, {"text": "do not execute", "client_request_id": "restart-case", "file_ids": []}, user)
        run_id = result["run_id"]
        service.db.execute("UPDATE runs SET status=? WHERE id=?", (stage, run_id))
        tool = "test_business_write"
        if stage != "queued":
            service.db.event(run_id, "tool.started", {"tool_call_id": "restart-tool", "tool_name": tool, "dispatch_tracked": True})
        if dispatched:
            record_dispatch(service, run_id, "restart-tool", tool)
        if stage == "awaiting_approval":
            service.db.execute("INSERT INTO approvals(id,run_id,user_id,tool_name,arguments,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                               (uid(), run_id, user["id"], tool, "{}", "pending", now(), now()))
    with TestClient(create_app(configured, FakeRuntime)) as client:
        login(client)
        run = client.get(f"/api/v1/runs/{result['run_id']}").json()
        assert run["status"] == "interrupted"
        assert run["error_code"] == "SERVICE_RESTARTED"
        assert run["recovery"]["interrupted_from"] == stage
        assert run["recovery"]["mode"] == ("answer_only" if dispatched else "retry")
        assert run["recovery"]["operations"] == ([{"tool_name": tool, "state": "unknown"}] if dispatched else [])
        if stage == "awaiting_approval":
            approval = client.app.state.service.db.one("SELECT status,decision_source FROM approvals WHERE run_id=?", (run["id"],))
            assert approval == {"status": "expired", "decision_source": "restart"}
        assert "run.completed" in client.get(f"/api/v1/runs/{run['id']}/events").text
        assert client.get(f"/api/v1/conversations/{cid}").json()["active_run_id"] is None
