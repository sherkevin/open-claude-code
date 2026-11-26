"""Real pinned Goal service, existing owned actor, no autonomous driver."""
import os
from pathlib import Path
from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.conversation_goal import GoalChange
from app.config import Settings
from test_api import PASSWORD, login, send, client, configured, conversation
from test_harness_real_runtime import image_fixture_settings, model_server, wait_run


def ref(value):
    return {k:value[k] for k in ("id","revision")}


def test_goal_feature_gate_disabled(client):
    cid=conversation(client)["id"]
    assert client.get(f"/api/v1/conversations/{cid}/goal").status_code==404


@pytest.mark.parametrize("data",[
    {"action":"create","objective":"x","maxGoalRounds":33},
    {"action":"create","objective":"x","maxGoalRounds":True},
    {"action":"create","objective":"  ","maxGoalRounds":2},
    {"action":"create","objective":"x","maxGoalRounds":2,"sessionId":"another"},
    {"action":"edit","ref":{"id":"goal-"+"a"*36,"revision":1}},
    {"action":"resume","ref":{"id":"goal-"+"a"*36,"revision":1},"objective":"x"},
])
def test_strict_goal_request_contract(data):
    with pytest.raises(ValueError):
        GoalChange.model_validate(data)


def test_real_native_goal_lifecycle_restart_and_no_extra_model_calls(tmp_path,model_server):
    node,source=os.getenv("HARNESS_TEST_NODE"),os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:pytest.skip("requires pinned Node Harness")
    base,requests=model_server
    settings=image_fixture_settings(tmp_path,node,source,base)
    settings.harness_goal_state_enabled=True
    app=create_app(settings)
    with TestClient(app) as client:
        service=app.state.service
        service.create_user({"username":"admin","display_name":"admin","password":PASSWORD,"role":"admin"})
        login(client)
        cid=conversation(client)["id"]
        path=f"/api/v1/conversations/{cid}/goal"
        assert client.get(path).status_code==409
        run=send(client,cid,"Goal state fixture")["run_id"]
        assert wait_run(client,run)["status"]=="succeeded"
        baseline=len(requests)
        initial=client.get(path)
        assert initial.status_code==200,initial.text
        assert initial.json()=={"goal":None,"continuation_driver":False}
        created=client.post(path,json={"action":"create","objective":"Verify synthetic milestones","maxGoalRounds":4})
        assert created.status_code==200,created.text
        goal=created.json()["goal"]
        assert goal["phase"]=="active" and goal["activation"]=="disarmed" and goal["roundsStarted"]==0
        assert not created.json()["continuation_driver"]
        again=client.get(path)
        assert again.status_code==200,again.text
        assert again.json()["goal"]==goal
        assert client.post(path,json={"action":"create","objective":"Other","maxGoalRounds":2}).status_code==409
        edited=client.post(path,json={"action":"edit","ref":ref(goal),"objective":"Verify exact synthetic criteria"})
        assert edited.status_code==200,edited.text
        current=edited.json()["goal"]
        assert current["revision"]==goal["revision"]+1
        assert client.post(path,json={"action":"pause","ref":ref(goal)}).status_code==409
        paused=client.post(path,json={"action":"pause","ref":ref(current)})
        assert paused.status_code==200,paused.text
        current=paused.json()["goal"]
        assert current["phase"]=="paused"
        assert service.db.one("SELECT count(*) n FROM runs WHERE conversation_id=?",(cid,))["n"]==1
        assert len(requests)==baseline
        service.create_user({"username":"other","display_name":"other","password":PASSWORD,"role":"member"})
        login(client,"other")
        assert client.get(path).status_code==404
        assert client.post(path,json={"action":"clear","ref":ref(current)}).status_code==404
        login(client)
    # Recreate the full application, not merely the SDK process.
    app2=create_app(settings)
    with TestClient(app2) as client:
        login(client)
        persisted=client.get(path)
        assert persisted.status_code==200,persisted.text
        assert persisted.json()["goal"]==current
        resumed=client.post(path,json={"action":"resume","ref":ref(current)})
        assert resumed.status_code==200,resumed.text
        current=resumed.json()["goal"]
        assert current["phase"]=="active" and current["activation"]=="disarmed"
        assert client.get(path).json()["goal"]==current
        completed=client.post(path,json={"action":"complete","ref":ref(current)})
        assert completed.status_code==200,completed.text
        current=completed.json()["goal"]
        assert current["phase"]=="complete"
        cleared=client.post(path,json={"action":"clear","ref":ref(current)})
        assert cleared.status_code==200,cleared.text
        assert cleared.json()["goal"] is None
        assert client.get(path).json()["goal"] is None
        assert len(requests)==baseline
        assert app2.state.service.db.one("SELECT count(*) n FROM runs WHERE conversation_id=?",(cid,))["n"]==1


def test_real_native_goal_authorization_does_not_run_driver(tmp_path,model_server):
    from app.goal_execution import _run_usage
    node,source=os.getenv("HARNESS_TEST_NODE"),os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:pytest.skip("requires pinned Node Harness")
    base,requests=model_server
    settings=image_fixture_settings(tmp_path,node,source,base)
    settings.harness_goal_state_enabled=True
    settings.harness_goal_execution_enabled=True
    app=create_app(settings)
    with TestClient(app) as client:
        service=app.state.service
        service.create_user({"username":"admin","display_name":"admin","password":PASSWORD,"role":"admin"})
        login(client)
        cid=conversation(client)["id"]
        path=f"/api/v1/conversations/{cid}/goal"
        first=send(client,cid,"Goal execution fixture")["run_id"]
        assert wait_run(client,first)["status"]=="succeeded"
        receipt=service.require("runs",first)
        assert _run_usage(receipt)[2]=="unknown"
        baseline=len(requests)
        goal=client.post(path,json={"action":"create","objective":"Synthetic authorized goal","maxGoalRounds":4}).json()["goal"]
        owner=service.require("conversations",cid)
        payload={"goal_id":goal["id"],"goal_revision":goal["revision"],
                 "harness_session_id":owner["harness_session_id"],"max_rounds":2,
                 "token_budget":10000,"cost_budget_micros":10000,"time_budget_seconds":3600,"confirm":True}
        reply=client.post(path+"/execution",json=payload)
        assert reply.status_code==200,reply.text
        assert reply.json()["authorization"]["status"]=="active"
        # The official driver seam is integrated, but authorization alone never
        # starts a round: execution requires the explicit per-round endpoint.
        assert reply.json()["driver_integrated"] is True
        assert reply.json()["single_round_per_run"] is True
        assert client.get(path).json()["goal"]["activation"]=="disarmed"
        paused=client.post(path,json={"action":"pause","ref":ref(goal)})
        assert paused.status_code==200,paused.text
        assert client.get(path+"/execution").json()["authorization"]["status"]=="revoked"
        assert len(requests)==baseline
        assert service.db.one("SELECT count(*) n FROM runs WHERE conversation_id=?",(cid,))["n"]==1
