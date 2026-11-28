"""Real product SDK run, real API approval, and local synthetic MCP HTTP.
No monkeypatch of SDK, tool callbacks, approval or execution."""
import json,time
import pytest
from fastapi.testclient import TestClient
from app.config import Settings, PROVIDER_KEYS
from app.main import create_app
from test_api import PASSWORD, login
from test_harness_mcp_tools import mcp_server, SYNTHETIC_CREDENTIAL
from harness_mcp_fixture import local_model_server
from test_sdk_host_guard import QUERY, SCHEMA, PAYLOAD

def wait_for(client, run_id, statuses, timeout=40):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        response=client.get(f"/api/v1/runs/{run_id}")
        assert response.status_code==200
        state=response.json()
        if state["status"] in statuses or state["status"] in {"failed","cancelled","interrupted"}:
            return state
        time.sleep(.05)
    raise AssertionError("product run did not reach expected state")

@pytest.mark.parametrize("case",["allow","zero","empty","deny"])
def test_product_sdk_receipt(tmp_path,monkeypatch,mcp_server,local_model_server,case):
    for k in PROVIDER_KEYS:monkeypatch.delenv(k,raising=False)
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL","local-lexical-v1")
    mcp_server.tools=[{"name":"query","description":"Count synthetic records","inputSchema":SCHEMA,"annotations":{"readOnlyHint":True}}]
    value=0 if case=="zero" else 17
    mcp_server.structured_override={**PAYLOAD,"data":[] if case=="empty" else [[value]],"total":0 if case=="empty" else 1}
    catalog=tmp_path/"catalog.json"
    catalog.write_text(json.dumps({"default_model_id":"fixture","models":[{"id":"fixture","label":"Local SDK probe","upstream_model":"claude-sonnet-4-5","reasoning_levels":{"default":None},"provider_env":{"ANTHROPIC_API_KEY":"synthetic-model-only","ANTHROPIC_BASE_URL":local_model_server.url.removesuffix("/v1")}}]}))
    settings=Settings(data_dir=tmp_path/"data",runtime_type="claude",model_catalog=str(catalog),default_model_id="",provider_settings="",provider_test_mode=True,origins=("http://testserver",),mcp_hosts=("127.0.0.1",),approval_seconds=30,model_router_enabled=False,knowledge_vision_enabled=False,image_generation_enabled=False)
    app=create_app(settings)
    with TestClient(app) as client:
        service=app.state.service
        service.create_user({"username":"admin","display_name":"Fixture","password":PASSWORD,"role":"admin"})
        login(client)
        response=client.post("/api/v1/connections",json={"name":"Synthetic aggregate","scope":"team","url":mcp_server.url,"auth_type":"bearer","credential":SYNTHETIC_CREDENTIAL})
        assert response.status_code==201,response.text
        connection=response.json()
        disc=client.post(f"/api/v1/connections/{connection['id']}/test")
        assert disc.status_code==200 and disc.json()["status"]=="connected",disc.text
        response=client.post("/api/v1/assistants",json={"name":"Synthetic count fixture","scope":"team","connections":[{"connection_id":connection["id"],"allowed_tools":["query"],"auto_approved_tools":[]}]})
        assert response.status_code==201,response.text
        response=client.post("/api/v1/conversations",json={"assistant_id":response.json()["id"],"permission_mode":"manual"})
        assert response.status_code==201,response.text
        cid=response.json()["id"]
        name=f"mcp__c{connection['id']}__query"
        marker="PRODUCT_MATRIX_"+case.upper()
        local_model_server.actions[marker]=(name,QUERY)
        response=client.post(f"/api/v1/conversations/{cid}/messages",json={"client_request_id":"sdk-product-"+case,"text":marker,"file_ids":[],"auto_route":False})
        assert response.status_code==202,response.text
        rid=response.json()["run_id"]
        pending=wait_for(client,rid,{"awaiting_approval","succeeded"})
        assert pending["status"]=="awaiting_approval",pending
        assert not mcp_server.calls
        row=pending["approvals"][0]
        assert row["tool_name"]==name
        response=client.post(f"/api/v1/approvals/{row['id']}/decision",json={"decision":"deny" if case=="deny" else "allow"})
        assert response.status_code==200,response.text
        settled=wait_for(client,rid,{"succeeded"})
        trace=client.get(f"/api/v1/runs/{rid}/trace")
        assert trace.status_code==200
        events=trace.json()["events"]
        completions=[e["data"] for e in events if e["type"]=="tool.completed" and e["data"].get("tool_name")==name]
        started=[e["data"] for e in events if e["type"]=="tool.started" and e["data"].get("tool_name")==name]
        dispatched=[e["data"] for e in events if e["type"]=="tool.dispatched" and e["data"].get("tool_name")==name]
        facts=[{k:e[k] for k in ("tool_call_id","success","count_receipt","business_result","result_facts") if k in e} for e in completions]
        print("PRODUCT_PROBE",json.dumps({"case":case,"status":settled["status"],"model_requests":len(local_model_server.requests),"mcp_calls":len(mcp_server.calls),"approval_count":len(pending["approvals"]),"started":len(started),"dispatched":len(dispatched),"completions":facts},ensure_ascii=False),flush=True)
        assert SYNTHETIC_CREDENTIAL not in trace.text
        assert SYNTHETIC_CREDENTIAL not in json.dumps(local_model_server.requests)
        if case=="deny":
            assert not mcp_server.calls and not any("count_receipt" in e for e in completions)
            return
        assert settled["status"]=="succeeded",settled
        assert len(mcp_server.calls)==1 and len(completions)==1
        assert mcp_server.calls[0]["arguments"]==QUERY
        assert len(started)==len(dispatched)==1
        assert started[0]["tool_call_id"]==dispatched[0]["tool_call_id"]==completions[0]["tool_call_id"]
        if case=="empty":
            assert "count_receipt" not in completions[0]
        else:
            receipt=completions[0].get("count_receipt")
            assert receipt is not None,"approved real SDK call has no public aggregate receipt"
            assert receipt["counts"][0]["value"]==value
            assert completions[0].get("business_result") is not None
