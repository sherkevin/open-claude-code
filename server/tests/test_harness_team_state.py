"""Real pinned Team projection over owner-scoped maintenance RPC."""
import os
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.harness_runtime import _TeamReceipt
from test_api import PASSWORD, login, send, client, configured, conversation
from test_harness_real_runtime import image_fixture_settings, model_server, wait_run


def test_team_disabled_by_default(client):
    cid=conversation(client)["id"]
    assert client.get(f"/api/v1/conversations/{cid}/team").status_code==404


def test_team_receipt_fails_closed():
    value={"schema":"workspace-team-state/v1","sourceCommit":"fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159",
           "sessionId":"test","board":{"enabled":False,"tasks":[]},"executionEnabled":False}
    assert _TeamReceipt.model_validate(value).board.tasks==[]
    for invalid in (dict(value,executionEnabled=True),dict(value,sourceCommit="bad"),
                    dict(value,board={"enabled":False,"tasks":[],"failure":"corrupt"})):
        with pytest.raises(ValueError):
            _TeamReceipt.model_validate(invalid)


def test_real_team_read_only_owner_and_restart(tmp_path,model_server):
    node,source=os.getenv("HARNESS_TEST_NODE"),os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source: pytest.skip("requires pinned Node Harness")
    base,requests=model_server
    settings=image_fixture_settings(tmp_path,node,source,base)
    settings.harness_team_state_enabled=True
    settings.harness_goal_state_enabled=True
    app=create_app(settings)
    expected={"board":{"enabled":False,"tasks":[]},"execution_enabled":False}
    with TestClient(app) as c:
        service=app.state.service
        service.create_user({"username":"admin","display_name":"admin","password":PASSWORD,"role":"admin"})
        login(c)
        cid=conversation(c)["id"]
        path=f"/api/v1/conversations/{cid}/team"
        assert c.get(path).status_code==409
        run=send(c,cid,"Team state fixture")["run_id"]
        assert wait_run(c,run)["status"]=="succeeded"
        baseline=len(requests)
        response=c.get(path)
        assert response.status_code==200,response.text
        assert response.json()==expected
        assert response.headers["cache-control"]=="no-store"
        assert c.post(path,json={"action":"enable"}).status_code==405
        assert c.get(path).json()==expected
        assert c.get(f"/api/v1/conversations/{cid}/goal").json()=={"goal":None,"continuation_driver":False}
        assert len(requests)==baseline
        service.create_user({"username":"other","display_name":"other","password":PASSWORD,"role":"member"})
        login(c,"other")
        assert c.get(path).status_code==404
        login(c)
    with TestClient(create_app(settings)) as c:
        login(c)
        assert c.get(path).json()==expected
        assert len(requests)==baseline
        assert c.app.state.service.db.one("SELECT count(*) n FROM runs WHERE conversation_id=?",(cid,))["n"]==1
        c.app.state.service.db.execute("UPDATE conversations SET archived=1 WHERE id=?",(cid,))
        assert c.get(path).status_code==409
        assert len(requests)==baseline
