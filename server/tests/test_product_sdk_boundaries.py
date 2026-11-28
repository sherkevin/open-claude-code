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

@pytest.mark.parametrize("case",["describe","malformed","remote_error","cancel_before_dispatch","revoked_before_dispatch","revoked_during_query"])
def test_product_sdk_boundaries(tmp_path,monkeypatch,mcp_server,local_model_server,case):
    for k in PROVIDER_KEYS:monkeypatch.delenv(k,raising=False)
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL","local-lexical-v1")
    mcp_server.tools=[{"name":n,"description":"Synthetic fixture "+n,"inputSchema":SCHEMA,"annotations":{"readOnlyHint":True}} for n in ("query","describe")]
    value=0 if case=="zero" else 17
    mcp_server.structured_override={**PAYLOAD,"data":[] if case=="empty" else [[value]],"total":0 if case=="empty" else 1}
    if case=="malformed":
        mcp_server.structured_override={**PAYLOAD,"data":[[17]],"columns":[{"name":"other","label":"Other","type":"int"}]}
    if case=="remote_error":mcp_server.fail=True
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
        response=client.post("/api/v1/assistants",json={"name":"Synthetic count fixture","scope":"team","connections":[{"connection_id":connection["id"],"allowed_tools":["query","describe"],"auto_approved_tools":[]}]})
        assert response.status_code==201,response.text
        response=client.post("/api/v1/conversations",json={"assistant_id":response.json()["id"],"permission_mode":"manual"})
        assert response.status_code==201,response.text
        cid=response.json()["id"]
        raw_name="describe" if case=="describe" else "query"
        name=f"mcp__c{connection['id']}__{raw_name}"
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
        if case=="cancel_before_dispatch":
            assert client.post(f"/api/v1/runs/{rid}/cancel").status_code==200
            settled=wait_for(client,rid,{"cancelled"})
            assert settled["status"]=="cancelled"
            assert client.post(f"/api/v1/approvals/{row['id']}/decision",json={"decision":"allow"}).status_code==409
        else:
            if case=="revoked_before_dispatch":
                assert client.patch(f"/api/v1/connections/{connection['id']}",json={"enabled":False}).status_code==200
            if case=="revoked_during_query":mcp_server.delayed=True
            try:
                response=client.post(f"/api/v1/approvals/{row['id']}/decision",json={"decision":"allow"})
                assert response.status_code==200,response.text
                if case=="revoked_during_query":
                    assert mcp_server.started.wait(10),"real MCP call was not dispatched"
                    assert client.patch(f"/api/v1/connections/{connection['id']}",json={"enabled":False}).status_code==200
            finally:
                mcp_server.released.set()
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
        assert not any("count_receipt" in e for e in completions), "non-qualifying result must never become a count receipt"
        if case in {"cancel_before_dispatch","revoked_before_dispatch"}:
            assert not mcp_server.calls
            return
        assert len(mcp_server.calls)==1
        assert mcp_server.calls[0]["arguments"]==QUERY
        if case in {"describe","malformed"}:
            assert settled["status"]=="succeeded" and len(completions)==1 and completions[0]["success"] is True
        else:
            assert not any(e.get("success") and "business_result" in e for e in completions)
