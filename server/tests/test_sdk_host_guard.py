"""Actual local SDK round trip for the existing host ticket pattern.
No product modifications. All model and external MCP HTTP traffic is local."""
import asyncio, copy, json
import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, create_sdk_mcp_server, tool, PermissionResultAllow, PermissionResultDeny
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolRequest, CallToolResult, ServerResult, ToolAnnotations
import httpx
from app.runtime import SDKRuntime
from app.capabilities import CAPABILITY_TICKET_FIELD
from harness_mcp_fixture import local_model_server
from test_harness_mcp_tools import mcp_server, SYNTHETIC_CREDENTIAL

QUERY={"dataset":"synthetic_asset","select":[{"field":"*","aggregate":"count","alias":"cnt"}],"rows":20,"page":1}
SCHEMA={"type":"object","properties":{"dataset":{"type":"string"},"select":{"type":"array","items":{"type":"object"}},"rows":{"type":"integer"},"page":{"type":"integer"}},"required":["dataset","select","rows","page"],"additionalProperties":False}
PAYLOAD={"dataset":"synthetic_asset","columns":[{"name":"cnt","label":"Count","type":"int"}],"data":[[17]],"rows":20,"page":1,"has_more":False,"total":1}

@pytest.mark.parametrize("case", ["allow", "deny", "altered_parameters", "revoked_run"])
def test_host_retains_authorized_matrix(tmp_path,mcp_server,local_model_server,case):
    name="mcp__fixture__query_dataset"
    mcp_server.tools=[{"name":"query_dataset","description":"Synthetic count query","inputSchema":SCHEMA,"annotations":{"readOnlyHint":True}}]
    mcp_server.structured_override=PAYLOAD
    local_model_server.actions["HOST_MATRIX_PROBE"]=(name,QUERY)
    rt=object.__new__(SDKRuntime)
    rt.current_run={"id":"synthetic-run"}
    rt._capability_tickets={}
    rt._capability_ticket_cap=32
    receipts=[]; obs=[]
    @tool("query_dataset","Synthetic count query",SCHEMA,annotations=ToolAnnotations(readOnlyHint=True))
    async def placeholder(args):
        raise AssertionError("Expected the existing-style verified host dispatcher")
    config=create_sdk_mcp_server("fixture","1",tools=[placeholder])
    server=config["instance"]
    async def dispatch(request):
        args=dict(request.params.arguments or {})
        ticket=args.pop(CAPABILITY_TICKET_FIELD,None)
        binding=rt.consume_capability_ticket(ticket,name,args)
        obs.append({"phase":"host_dispatch","ticket_present":bool(ticket),"binding_valid":bool(binding),"args_match":args==QUERY,"call_id":binding.get("tool_use_id") if binding else None})
        if not binding:
            return ServerResult(CallToolResult(content=[],isError=True))
        async with httpx.AsyncClient(headers={"Authorization":"Bearer "+SYNTHETIC_CREDENTIAL},trust_env=False) as client:
            async with streamable_http_client(mcp_server.url,http_client=client) as streams:
                async with ClientSession(streams[0],streams[1]) as session:
                    await session.initialize()
                    result=await session.call_tool("query_dataset",args)
        body=result.model_dump(mode="json",exclude_none=True)
        receipts.append({"call_id":binding["tool_use_id"],"run_id":binding["run_id"],"exact_matrix":body.get("structuredContent")==PAYLOAD,"value":body.get("structuredContent",{}).get("data")})
        assert rt.consume_capability_ticket(ticket,name,args) is None
        return ServerResult(CallToolResult.model_validate(body))
    server.request_handlers[CallToolRequest]=dispatch
    async def pre(data,tool_use_id,context):
        if data.get("tool_name")==name:
            obs.append({"phase":"pre","call_id":tool_use_id,"args_match":data.get("tool_input")==QUERY})
        return {"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask"}}
    async def permission(tool_name,args,context):
        if tool_name!=name or args!=QUERY:
            return PermissionResultDeny(message="unexpected synthetic request")
        if case=="deny":
            obs.append({"phase":"permission_denied","call_id":getattr(context,"tool_use_id",None)})
            return PermissionResultDeny(message="synthetic denial")
        ticket=rt.issue_capability_ticket(tool_name,args,context)
        obs.append({"phase":"permission","call_id":getattr(context,"tool_use_id",None),"ticket_issued":bool(ticket)})
        if not ticket:return PermissionResultDeny(message="missing call identity")
        updated={**args,CAPABILITY_TICKET_FIELD:ticket}
        if case=="altered_parameters":updated["rows"]=21
        if case=="revoked_run":rt.current_run={"id":"synthetic-replacement-run"}
        return PermissionResultAllow(updated_input=updated)
    async def post(data,tool_use_id,context):
        if data.get("tool_name")==name:
            obs.append({"phase":"post","call_id":tool_use_id,"response_type":type(data.get("tool_response")).__name__,"host_receipt_present":any(r["call_id"]==tool_use_id for r in receipts)})
        return {}
    async def run():
        opts=ClaudeAgentOptions(cwd=str(tmp_path),env={"ANTHROPIC_API_KEY":"synthetic-local-only","ANTHROPIC_BASE_URL":local_model_server.url.removesuffix("/v1"),"CLAUDE_CONFIG_DIR":str(tmp_path/"config"),"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC":"1","DISABLE_TELEMETRY":"1","DISABLE_ERROR_REPORTING":"1","DISABLE_AUTOUPDATER":"1","CLAUDE_CODE_DISABLE_AUTO_MEMORY":"1"},model="claude-sonnet-4-5",system_prompt="Use the synthetic fixture once.",setting_sources=[],strict_mcp_config=True,tools=[],mcp_servers={"fixture":config},allowed_tools=[],permission_mode="default",max_turns=3,can_use_tool=permission,hooks={"PreToolUse":[HookMatcher(hooks=[pre],on_failure="block")],"PostToolUse":[HookMatcher(hooks=[post])]},stderr=lambda line:print("SDK_STDERR",line[:300],flush=True))
        async with ClaudeSDKClient(options=opts) as client:
            await client.query("HOST_MATRIX_PROBE")
            async for msg in client.receive_response():
                print("SDK_MESSAGE",type(msg).__name__,getattr(msg,"subtype",None),getattr(msg,"is_error",None),flush=True)
    try: asyncio.run(asyncio.wait_for(run(),timeout=50))
    finally:
        print("HOST_PROBE",json.dumps({"case":case,"model_requests":len(local_model_server.requests),"actual_remote_calls":len(mcp_server.calls),"observations":obs,"receipts":receipts},ensure_ascii=False),flush=True)
    if case=="allow":
        assert len(mcp_server.calls)==1
        assert len(receipts)==1 and receipts[0]["exact_matrix"] and receipts[0]["value"]==[[17]]
        assert [o["phase"] for o in obs]==["pre","permission","host_dispatch","post"]
        assert all(o["call_id"]==receipts[0]["call_id"] for o in obs)
        assert obs[-1]["host_receipt_present"]
    else:
        assert not mcp_server.calls and not receipts
        if case=="deny":
            assert any(o["phase"]=="permission_denied" for o in obs)
            assert not any(o["phase"]=="host_dispatch" for o in obs)
        else:
            assert any(o["phase"]=="host_dispatch" and not o["binding_valid"] for o in obs)
