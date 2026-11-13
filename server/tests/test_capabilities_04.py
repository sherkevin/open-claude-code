import asyncio
from types import SimpleNamespace

from app.capabilities import create_workspace_tools
from app.security import AppError


def test_workspace_tools_keep_submitted_scope_and_refresh_user_before_read(monkeypatch):
    captured = {}
    class Knowledge:
        async def search_for_run(self, user, assistant, run_id, query, limit):
            captured.update(user=user, assistant=assistant, run_id=run_id)
            return {"results": []}
    class Service:
        knowledge = Knowledge()
        role = "admin"
        def require(self, table, item_id):
            return {"id": "conversation", "user_id": "user", "assistant_id": "assistant"} if table == "conversations" else {"id": "user", "role": self.role}
        def usable_assistant(self, assistant_id, user):
            return {"id": "assistant", "knowledge_base_ids": ["old-kb", "new-kb"], "web_search_enabled": True}
    service = Service()
    snapshot = {"id": "assistant", "knowledge_base_ids": ["old-kb"], "web_search_enabled": False}
    runtime = SimpleNamespace(service=service, conversation_id="conversation", config={"assistant_snapshot": snapshot}, current_run={"id": "run"}, check_tool=lambda *args: True)
    monkeypatch.setattr("claude_agent_sdk.create_sdk_mcp_server", lambda *args, **kwargs: kwargs["tools"])
    tools = create_workspace_tools(runtime)
    assert {tool.name for tool in tools} == {
        "knowledge_search", "knowledge_read", "file_read", "file_write", "file_search",
    }
    service.role = "member"
    result = asyncio.run(next(tool for tool in tools if tool.name == "knowledge_search").handler({"query": "company", "limit": 2}))
    assert captured["assistant"]["knowledge_base_ids"] == ["old-kb"]
    assert captured["user"]["role"] == "member"
    assert captured["run_id"] == "run"
    assert not result.get("is_error")


def test_workspace_failure_uses_sdk_handler_error_contract(monkeypatch):
    class Service:
        def require(self, table, item_id):
            return {"user_id": "u", "assistant_id": "a"} if table == "conversations" else {"id": "u"}
    def denied(*args):
        raise AppError(403, "DENIED", "授权已撤回")
    runtime = SimpleNamespace(service=Service(), conversation_id="c", config={"assistant_snapshot": {"id": "a", "knowledge_base_ids": ["kb"]}}, current_run={"id": "r"}, check_tool=denied)
    monkeypatch.setattr("claude_agent_sdk.create_sdk_mcp_server", lambda *args, **kwargs: kwargs["tools"])
    tools = create_workspace_tools(runtime)
    result = asyncio.run(tools[0].handler({"query": "company", "limit": 2}))
    # SDK converts handler `is_error` into MCP wire `isError`.
    assert result.get("is_error") is True
    assert "授权已撤回" in result["content"][0]["text"]
