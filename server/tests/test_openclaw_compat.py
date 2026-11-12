"""Synthetic acceptance tests for the tenant-scoped OpenClaw ``/v1`` facade.

Everything here is local and synthetic: a fake executor, temporary SQLite
databases and ``TestClient``.  No customer data, credentials, network MCP
server or paid model is used.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import now, uid
from app.openai_compat import NormalizedResult, NormalizedToolCall, NormalizedUsage
from app.openclaw_compat import (
    AGENT_HARNESS_V2_CONTRACT,
    CANDIDATE_OPENCLAW_VERSION,
    OpenClawError,
    OpenClawFacade,
    UnavailableExecutor,
    create_openclaw_sidecar_app,
    harness_contract_manifest,
)
from app.security import password_hash
from app.service import Service

PASSWORD = "Correct-test-password-123"
PASSWORD_HASH = password_hash(PASSWORD)


def make_user(service: Service, username: str, display_name: str, role: str) -> dict:
    """Insert a synthetic user without re-running scrypt for every fixture."""
    user_id = uid()
    service.db.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)",
                       (user_id, username, display_name, PASSWORD_HASH, role, 1, now()))
    return service.db.one("SELECT * FROM users WHERE id=?", (user_id,))


class UpstreamFailure(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"upstream-secret-detail-{status_code}")
        self.status_code = status_code


class ScriptedExecutor:
    """Deterministic synthetic executor; records the requests it receives."""

    def __init__(self):
        self.calls: list[tuple[Any, Any]] = []

    async def execute(self, request, context, cancelled):
        self.calls.append((request, context))
        tool_text = None
        for message in reversed(request.messages):
            if message.role == "tool" and message.text is not None:
                tool_text = message.text
                break
        user_text = ""
        for message in reversed(request.messages):
            if message.role == "user" and message.text is not None:
                user_text = message.text
                break
        if tool_text == "42":
            return NormalizedResult(text="answer 42", usage=NormalizedUsage(5, 3, 8))
        if user_text == "tool":
            return NormalizedResult(
                tool_calls=(NormalizedToolCall("call_1", "lookup", {"q": "x"}),),
                finish_reason="tool_calls",
                usage=NormalizedUsage(4, 1, 5),
            )
        if user_text == "upstream-429":
            raise UpstreamFailure(429)
        if user_text == "upstream-500":
            raise UpstreamFailure(500)
        return NormalizedResult(
            text=f"echo: {user_text}",
            usage=NormalizedUsage(3, 2, 5),
            chunks=("echo: ", user_text),
        )


class SlowExecutor:
    async def execute(self, request, context, cancelled):
        await asyncio.sleep(2)
        return NormalizedResult(text="too late")


@dataclass
class Env:
    service: Service
    facade: OpenClawFacade
    client: TestClient
    executor: ScriptedExecutor
    qa: dict
    sales: dict
    tenant_a: dict
    tenant_b: dict
    token_a: str
    token_b: str
    user_a: dict
    user_b: dict


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        provider_settings="",
        origins=("http://testserver",),
        mcp_hosts=("localhost", "127.0.0.1"),
        idle_seconds=60,
        approval_seconds=3,
    )
    service = Service(settings)
    admin = make_user(service, "admin", "管理员", "admin")
    user_a = make_user(service, "openclaw-a", "租户A", "member")
    user_b = make_user(service, "openclaw-b", "租户B", "member")
    qa = service.save_assistant({"name": "问答助手", "description": "", "instructions": "", "connections": [], "scope": "team"}, user=admin)
    sales = service.save_assistant({"name": "销售助手", "description": "", "instructions": "", "connections": [], "scope": "team"}, user=admin)
    executor = ScriptedExecutor()
    facade = OpenClawFacade(service, executor=executor, executor_timeout_seconds=1.0)
    tenant_a = facade.store.create_tenant("tenant-a", user_a["id"], {"default": qa["id"], "sales": sales["id"]})
    tenant_b = facade.store.create_tenant("tenant-b", user_b["id"], {"default": qa["id"]})
    token_a = facade.store.issue_token(tenant_a["id"], "a", allowed_agents=["default", "sales"])["token"]
    token_b = facade.store.issue_token(tenant_b["id"], "b")["token"]
    app = create_openclaw_sidecar_app(facade)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield Env(service, facade, client, executor, qa, sales, tenant_a, tenant_b,
                  token_a, token_b, user_a, user_b)
    service.db.close()


def auth(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def chat(client: TestClient, token: str, *, model: str = "openclaw/default", text: str = "hello",
         user: str | None = "u-1", headers: dict[str, str] | None = None, **fields: Any):
    payload: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": text}]}
    if user is not None:
        payload["user"] = user
    payload.update(fields)
    return client.post("/v1/chat/completions", headers={**auth(token), **(headers or {})}, json=payload)


def responses(client: TestClient, token: str, *, text: str = "hello", user: str | None = "r-1",
              headers: dict[str, str] | None = None, **fields: Any):
    payload: dict[str, Any] = {"model": "openclaw/default", "input": text}
    if user is not None:
        payload["user"] = user
    payload.update(fields)
    return client.post("/v1/responses", headers={**auth(token), **(headers or {})}, json=payload)


def sse_data(response) -> list[dict[str, Any]]:
    return [json.loads(line[6:]) for line in response.text.splitlines()
            if line.startswith("data: ") and line[6:] != "[DONE]"]


def error_code(response) -> str:
    return response.json()["error"]["code"]


# ----------------------------------------------------------------------- auth
def test_auth_rejects_cookie_management_and_invalid_tokens(env):
    client = env.client
    cookie = client.get("/v1/models", headers={"Cookie": "zhixing_session=employee-session"})
    assert cookie.status_code == 401
    assert error_code(cookie) == "EMPLOYEE_SESSION_NOT_ALLOWED"

    both = client.get("/v1/models", headers={**auth(env.token_a), "Cookie": "zhixing_session=employee-session"})
    assert both.status_code == 401
    assert error_code(both) == "EMPLOYEE_SESSION_NOT_ALLOWED"

    for token in ("openclaw-admin-management-token", "zxoc_not-a-real-token"):
        response = client.get("/v1/models", headers=auth(token))
        assert response.status_code == 401
        assert error_code(response) == "AUTHENTICATION_FAILED"

    missing = client.get("/v1/models")
    assert missing.status_code == 401
    assert error_code(missing) == "AUTHENTICATION_FAILED"

    revoked = env.facade.store.issue_token(env.tenant_a["id"], "revoked")
    assert env.facade.store.revoke_token(revoked["token_id"]) is True
    response = client.get("/v1/models", headers=auth(revoked["token"]))
    assert response.status_code == 401
    assert error_code(response) == "TOKEN_REVOKED"

    expired = env.facade.store.issue_token(env.tenant_a["id"], "expired", ttl_seconds=60)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    env.service.db.execute("UPDATE openclaw_tenant_tokens SET expires_at=? WHERE id=?", (past, expired["token_id"]))
    response = client.get("/v1/models", headers=auth(expired["token"]))
    assert response.status_code == 401
    assert error_code(response) == "TOKEN_EXPIRED"

    tenant_c = env.facade.store.create_tenant("tenant-c", env.user_a["id"], {"default": env.qa["id"]})
    token_c = env.facade.store.issue_token(tenant_c["id"], "c")["token"]
    assert env.facade.store.disable_tenant(tenant_c["id"]) is True
    response = client.get("/v1/models", headers=auth(token_c))
    assert response.status_code == 401
    assert error_code(response) == "TENANT_DISABLED"

    audit_text = json.dumps(env.facade.audit_events(), ensure_ascii=False)
    for secret in (env.token_a, env.token_b, revoked["token"], expired["token"]):
        assert secret not in audit_text


# --------------------------------------------------------- models and agents
def test_model_whitelist_and_agent_mapping(env):
    client = env.client
    response = client.get("/v1/models", headers=auth(env.token_a))
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["data"]}
    assert ids == {"openclaw/default", "openclaw/sales"}
    assert not any("claude" in item or "gpt" in item for item in ids)

    assert client.get("/v1/models/openclaw/default", headers=auth(env.token_a)).status_code == 200
    unknown = client.get("/v1/models/openclaw/ghost", headers=auth(env.token_a))
    assert unknown.status_code == 404
    assert error_code(unknown) == "MODEL_NOT_ALLOWED"

    provider_model = chat(client, env.token_a, model="claude-sonnet-4", user="provider-model")
    assert provider_model.status_code == 404
    assert error_code(provider_model) == "MODEL_NOT_ALLOWED"

    header_agent = chat(client, env.token_a, text="agent", user="agent-header",
                        headers={"x-openclaw-agent-id": "sales"})
    assert header_agent.status_code == 200, header_agent.text
    session_id = header_agent.headers["x-openclaw-session-id"]
    row = env.service.db.one(
        """SELECT sessions.agent_id, conversations.assistant_id, conversations.user_id
           FROM openclaw_sessions sessions JOIN conversations ON conversations.id=sessions.conversation_id
           WHERE sessions.id=?""",
        (session_id,),
    )
    assert row["agent_id"] == "sales"
    assert row["assistant_id"] == env.sales["id"]
    assert row["user_id"] == env.user_a["id"]

    override = chat(client, env.token_a, model="openclaw", text="override", user="override",
                    headers={"x-openclaw-model": "openclaw/sales"})
    assert override.status_code == 200, override.text
    assert override.json()["model"] == "openclaw/sales"
    override_row = env.service.db.one(
        """SELECT sessions.agent_id FROM openclaw_sessions sessions WHERE sessions.id=?""",
        (override.headers["x-openclaw-session-id"],),
    )
    assert override_row["agent_id"] == "sales"

    conflict = chat(client, env.token_a, model="openclaw/sales", text="conflict", user="conflict",
                    headers={"x-openclaw-agent-id": "default"})
    assert conflict.status_code == 400
    assert error_code(conflict) == "AGENT_CONFLICT"

    override_conflict = chat(client, env.token_a, model="openclaw/sales", text="conflict", user="conflict",
                             headers={"x-openclaw-model": "openclaw/default"})
    assert override_conflict.status_code == 400
    assert error_code(override_conflict) == "MODEL_OVERRIDE_CONFLICT"

    scoped = env.facade.store.issue_token(env.tenant_a["id"], "default-only", allowed_agents=["default"])
    scoped_models = client.get("/v1/models", headers=auth(scoped["token"])).json()["data"]
    assert [item["id"] for item in scoped_models] == ["openclaw/default"]
    denied_agent = chat(client, scoped["token"], model="openclaw/sales", text="denied", user="denied")
    assert denied_agent.status_code == 404
    assert error_code(denied_agent) == "AGENT_NOT_ALLOWED"

    with pytest.raises(OpenClawError) as exc:
        env.facade.store.issue_token(env.tenant_a["id"], "bad", allowed_agents=["ghost"])
    assert exc.value.code == "INVALID_TOKEN_SCOPE"


# -------------------------------------------------------- sessions/isolation
def test_session_mapping_continuation_and_isolation(env):
    client = env.client
    first = chat(client, env.token_a, text="one", user="same-user")
    assert first.status_code == 200
    sid = first.headers["x-openclaw-session-id"]
    second = chat(client, env.token_a, text="two", user="same-user")
    assert second.headers["x-openclaw-session-id"] == sid

    other_tenant = chat(client, env.token_b, text="one", user="same-user")
    assert other_tenant.headers["x-openclaw-session-id"] != sid

    shared_a = chat(client, env.token_a, text="key", user=None, headers={"x-openclaw-session-key": "shared-key"})
    shared_b = chat(client, env.token_b, text="key", user=None, headers={"x-openclaw-session-key": "shared-key"})
    assert shared_a.status_code == 200 and shared_b.status_code == 200
    assert shared_a.headers["x-openclaw-session-id"] != shared_b.headers["x-openclaw-session-id"]

    sales_session = chat(client, env.token_a, model="openclaw/sales", text="key", user=None,
                         headers={"x-openclaw-session-key": "shared-key"})
    assert sales_session.status_code == 200
    assert sales_session.headers["x-openclaw-session-id"] != shared_a.headers["x-openclaw-session-id"]

    cross_tenant = chat(client, env.token_b, text="cross", user=None,
                        headers={"x-openclaw-session-id": shared_a.headers["x-openclaw-session-id"]})
    assert cross_tenant.status_code == 404
    assert error_code(cross_tenant) == "SESSION_NOT_FOUND"

    cross_agent = chat(client, env.token_a, model="openclaw/sales", text="cross", user=None,
                       headers={"x-openclaw-session-id": shared_a.headers["x-openclaw-session-id"]})
    assert cross_agent.status_code == 404
    assert error_code(cross_agent) == "SESSION_NOT_FOUND"

    mismatch = chat(client, env.token_a, text="mismatch", user=None, headers={
        "x-openclaw-session-id": shared_a.headers["x-openclaw-session-id"],
        "x-openclaw-session-key": "another-key",
    })
    assert mismatch.status_code == 400
    assert error_code(mismatch) == "SESSION_CONFLICT"

    for key, code in (("admin:one", "RESERVED_SESSION_KEY"), ("system:root", "RESERVED_SESSION_KEY"),
                      ("bad/key", "INVALID_SESSION_KEY")):
        response = chat(client, env.token_a, text="key", user=None, headers={"x-openclaw-session-key": key})
        assert response.status_code == 400
        assert error_code(response) == code

    no_identity = chat(client, env.token_a, text="none", user=None)
    assert no_identity.status_code == 400
    assert error_code(no_identity) == "SESSION_KEY_REQUIRED"

    random_id = chat(client, env.token_a, text="random", user=None, headers={"x-openclaw-session-id": "not-a-session"})
    assert random_id.status_code == 404
    assert error_code(random_id) == "SESSION_NOT_FOUND"

    rotated = env.facade.store.issue_token(env.tenant_a["id"], "rotated", allowed_agents=["default", "sales"])
    before = chat(client, env.token_a, text="rotate", user="rotating-user")
    after = chat(client, rotated["token"], text="rotate", user="rotating-user")
    assert after.status_code == 200
    assert after.headers["x-openclaw-session-id"] == before.headers["x-openclaw-session-id"]

    count = env.service.db.one(
        "SELECT COUNT(*) n FROM openclaw_sessions WHERE tenant_id=? AND principal_id=? AND agent_id='default' AND user_digest IS NOT NULL",
        (env.tenant_a["id"], env.facade.store.authenticate(auth(env.token_a)).principal_id),
    )["n"]
    assert count >= 1
    raw_user = env.service.db.one(
        "SELECT session_key, user_digest FROM openclaw_sessions WHERE id=?", (sid,)
    )
    assert raw_user["session_key"].startswith("user:")
    assert "same-user" not in raw_user["session_key"]
    assert raw_user["user_digest"] != "same-user"


# ------------------------------------------------------ chat / responses wire
def test_chat_non_stream_and_sse(env):
    client = env.client
    response = chat(client, env.token_a, text="hi", user="wire-user")
    assert response.status_code == 200, response.text
    assert response.headers["x-openclaw-compat-version"]
    assert response.headers["x-openclaw-session-id"]
    assert response.headers["x-openclaw-run-id"]
    assert response.headers["x-openclaw-response-id"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "openclaw/default"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "echo: hi"}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}

    stream = chat(client, env.token_a, text="hi", user="wire-user", stream=True,
                  stream_options={"include_usage": True})
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert stream.headers["cache-control"] == "no-cache"
    assert stream.text.rstrip().endswith("data: [DONE]")
    events = sse_data(stream)
    assert all(event["object"] == "chat.completion.chunk" for event in events)
    text = "".join(event["choices"][0]["delta"].get("content", "") for event in events)
    assert text == "echo: hi"
    assert events[-1]["choices"][0]["finish_reason"] == "stop"
    assert events[-1]["usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    assert events[0]["choices"][0]["delta"]["role"] == "assistant"


def test_responses_non_stream_previous_and_sse(env):
    client = env.client
    first = responses(client, env.token_a, text="hello", user="resp-user",
                      instructions="be brief", metadata={"case": "synthetic"})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["output_text"] == "echo: hello"
    assert body["output"][0]["type"] == "message"
    assert body["output"][0]["content"][0]["text"] == "echo: hello"
    assert body["metadata"] == {"case": "synthetic"}
    assert body["usage"] == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
    request, _ = env.executor.calls[-1]
    assert request.messages[0].role == "system"
    assert request.messages[0].text == "be brief"

    response_id = body["id"]
    session_id = first.headers["x-openclaw-session-id"]
    continued = responses(client, env.token_a, text="again", user=None,
                          previous_response_id=response_id)
    assert continued.status_code == 200, continued.text
    assert continued.headers["x-openclaw-session-id"] == session_id
    assert continued.json()["previous_response_id"] == response_id

    cross = responses(client, env.token_b, text="again", user=None, previous_response_id=response_id)
    assert cross.status_code == 404
    assert error_code(cross) == "RESPONSE_NOT_FOUND"

    env.service.db.execute(
        "UPDATE openclaw_responses SET created_at=? WHERE response_id=?",
        ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(), response_id),
    )
    expired = responses(client, env.token_a, text="again", user=None, previous_response_id=response_id)
    assert expired.status_code == 404
    assert error_code(expired) == "RESPONSE_NOT_FOUND"

    stream = responses(client, env.token_a, text="hello", user="resp-stream", stream=True)
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    events = sse_data(stream)
    types = [event["type"] for event in events]
    assert types[0] == "response.created"
    assert "response.output_text.delta" in types
    assert "response.output_text.done" in types
    assert types[-1] == "response.completed"
    assert "".join(event["delta"] for event in events if event["type"] == "response.output_text.delta") == "echo: hello"
    assert events[-1]["response"]["output_text"] == "echo: hello"


# --------------------------------------------------------- tool round trips
def test_tool_call_round_trip_chat_and_responses(env):
    client = env.client
    first = chat(client, env.token_a, text="tool", user="tool-user")
    assert first.status_code == 200, first.text
    message = first.json()["choices"][0]["message"]
    assert first.json()["choices"][0]["finish_reason"] == "tool_calls"
    assert message["content"] is None
    assert message["tool_calls"][0]["id"] == "call_1"
    assert message["tool_calls"][0]["function"]["name"] == "lookup"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"q": "x"}

    second = chat(client, env.token_a, text="ignored", user="tool-user", messages=[
        {"role": "user", "content": "tool"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "42"},
    ])
    assert second.status_code == 200, second.text
    assert second.json()["choices"][0]["message"]["content"] == "answer 42"

    fabricated = chat(client, env.token_a, text="ignored", user="tool-user", messages=[
        {"role": "user", "content": "tool"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_fake", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_fake", "content": "42"},
    ])
    assert fabricated.status_code == 400
    assert error_code(fabricated) == "TOOL_CALL_UNKNOWN"

    resp_first = responses(client, env.token_a, text="tool", user="resp-tool")
    assert resp_first.status_code == 200, resp_first.text
    function_item = [item for item in resp_first.json()["output"] if item["type"] == "function_call"][0]
    assert function_item["call_id"] == "call_1"
    resp_second = responses(
        client, env.token_a, text="ignored", user=None,
        previous_response_id=resp_first.json()["id"],
        input=[{"type": "function_call_output", "call_id": "call_1", "output": "42"}],
    )
    assert resp_second.status_code == 200, resp_second.text
    assert resp_second.json()["output_text"] == "answer 42"

    unknown = responses(
        client, env.token_a, text="ignored", user=None,
        previous_response_id=resp_first.json()["id"],
        input=[{"type": "function_call_output", "call_id": "call_missing", "output": "42"}],
    )
    assert unknown.status_code == 400
    assert error_code(unknown) == "TOOL_CALL_UNKNOWN"

    stored = env.service.db.all("SELECT call_id FROM openclaw_tool_calls ORDER BY call_id")
    assert any(row["call_id"] == "call_1" for row in stored)


# ---------------------------------------------------------------- idempotency
def test_idempotency_replay_conflict_and_stream_replay(env):
    client = env.client
    headers = {"Idempotency-Key": "idem-1"}
    first = chat(client, env.token_a, text="idem", user="idem-user", headers=headers)
    assert first.status_code == 200, first.text
    second = chat(client, env.token_a, text="idem", user="idem-user", headers=headers)
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.headers["x-openclaw-session-id"] == first.headers["x-openclaw-session-id"]
    turns = env.service.db.all("SELECT * FROM openclaw_turns WHERE idempotency_key='idem-1'")
    assert len(turns) == 1
    assert any(event["action"] == "turn.replayed" for event in env.facade.audit_events())

    conflict = chat(client, env.token_a, text="different", user="idem-user", headers=headers)
    assert conflict.status_code == 409
    assert error_code(conflict) == "IDEMPOTENCY_CONFLICT"

    mismatch = chat(client, env.token_a, text="idem", user="idem-user", headers={
        "Idempotency-Key": "one", "x-openclaw-idempotency-key": "two",
    })
    assert mismatch.status_code == 400
    assert error_code(mismatch) == "INVALID_IDEMPOTENCY_KEY"

    stream_headers = {"Idempotency-Key": "idem-stream"}
    stream_first = chat(client, env.token_a, text="stream-idem", user="idem-stream-user",
                        headers=stream_headers, stream=True)
    stream_second = chat(client, env.token_a, text="stream-idem", user="idem-stream-user",
                         headers=stream_headers, stream=True)
    assert stream_first.status_code == 200 and stream_second.status_code == 200
    assert stream_first.headers["content-type"].startswith("text/event-stream")
    assert stream_second.headers["content-type"].startswith("text/event-stream")
    first_events = sse_data(stream_first)
    second_events = sse_data(stream_second)
    assert first_events == second_events
    assert stream_first.text.rstrip().endswith("data: [DONE]")
    assert stream_second.text.rstrip().endswith("data: [DONE]")

    failed_headers = {"Idempotency-Key": "idem-failed"}
    failed = chat(client, env.token_a, text="upstream-429", user="idem-fail-user", headers=failed_headers)
    assert failed.status_code == 429
    replay_failed = chat(client, env.token_a, text="upstream-429", user="idem-fail-user", headers=failed_headers)
    assert replay_failed.status_code == 429
    assert error_code(replay_failed) == "MODEL_RATE_LIMITED"


def test_idempotency_concurrent_request_is_reported_in_progress(env):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingExecutor:
            async def execute(self, request, context, cancelled):
                started.set()
                await release.wait()
                return NormalizedResult(text="done")

        facade = OpenClawFacade(env.service, executor=BlockingExecutor(), executor_timeout_seconds=5)
        principal = facade.store.authenticate(auth(env.token_a))
        payload = {"model": "openclaw/default", "messages": [{"role": "user", "content": "block"}],
                   "user": "blocking-user"}
        headers = {**auth(env.token_a), "Idempotency-Key": "idem-blocking"}
        task = asyncio.create_task(facade.execute(principal, "chat.completions", payload, headers))
        await asyncio.wait_for(started.wait(), 2)
        with pytest.raises(OpenClawError) as exc:
            await facade.execute(principal, "chat.completions", payload, headers)
        assert exc.value.status == 409
        assert exc.value.code == "REQUEST_IN_PROGRESS"
        release.set()
        outcome = await asyncio.wait_for(task, 2)
        assert outcome.body["choices"][0]["message"]["content"] == "done"
        replay = await facade.execute(principal, "chat.completions", payload, headers)
        assert replay.response_id == outcome.response_id

    asyncio.run(scenario())


# ------------------------------------------------------------- validation
def test_validation_and_upstream_error_envelopes(env):
    client = env.client
    long_text = "x" * 100_001
    cases = [
        ({"model": "openclaw/default", "messages": [{"role": "user", "content": "x"}], "n": 2},
         400, "UNSUPPORTED_PARAMETER"),
        ({"model": "openclaw/default", "messages": [{"role": "user", "content": "x"}], "input": "x"},
         400, "UNSUPPORTED_PARAMETER"),
        ({"model": "openclaw/default", "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://example.invalid/x.png"}}]}]},
         400, "UNSUPPORTED_CONTENT"),
        ({"model": "openclaw/default", "messages": [{"role": "user", "content": "x"}] * 201},
         400, "INVALID_REQUEST"),
        ({"model": "openclaw/default", "messages": [{"role": "user", "content": long_text}]},
         413, "REQUEST_TOO_LARGE"),
        ({"model": "openclaw/default", "messages": [{"role": "user", "content": "x"}], "tools": [
            {"type": "function", "function": {"name": "same", "parameters": {}}},
            {"type": "function", "function": {"name": "same", "parameters": {}}},
        ]}, 400, "INVALID_REQUEST"),
        ({"model": "openclaw/default", "messages": [{"role": "user", "content": "x"}],
          "tools": [{"type": "function", "function": {"name": "a", "parameters": {}}}],
          "tool_choice": {"type": "function", "function": {"name": "b"}}}, 400, "INVALID_TOOL_CHOICE"),
        ({"model": "openclaw/default", "messages": [{"role": "user", "content": "x"}], "stream": "yes"},
         400, "INVALID_REQUEST"),
        ({"model": "openclaw/default", "messages": [{"role": "user", "content": "x"}],
          "metadata": {"k": ["not", "a", "string"]}}, 400, "INVALID_REQUEST"),
        ({"model": "openclaw/default", "messages": [{"role": "user", "content": "x"}], "temperature": 3},
         400, "INVALID_REQUEST"),
        ({"model": "openclaw/default", "messages": [{"role": "user", "content": "x"}], "max_tokens": 100_000},
         400, "INVALID_REQUEST"),
        ({"model": "openclaw/default", "messages": [{"role": "user", "content": "x"}],
          "previous_response_id": "resp_x"}, 400, "UNSUPPORTED_PARAMETER"),
    ]
    for payload, status, code in cases:
        response = client.post("/v1/chat/completions", headers=auth(env.token_a), json=payload)
        assert response.status_code == status, (payload, response.text)
        assert error_code(response) == code
        assert set(response.json()["error"]) == {"message", "type", "param", "code"}

    empty_responses = client.post("/v1/responses", headers=auth(env.token_a),
                                  json={"model": "openclaw/default", "input": []})
    assert empty_responses.status_code == 400
    assert error_code(empty_responses) == "INVALID_REQUEST"

    bad_json = client.post("/v1/chat/completions", headers={**auth(env.token_a), "Content-Type": "application/json"},
                           content=b"{")
    assert bad_json.status_code == 400
    assert error_code(bad_json) == "INVALID_JSON"

    huge = client.post("/v1/chat/completions", headers={**auth(env.token_a), "Content-Type": "application/json"},
                       content=json.dumps({"model": "openclaw/default",
                                           "messages": [{"role": "user", "content": "x" * 1_100_000}]}).encode())
    assert huge.status_code == 413

    rate_limited = chat(client, env.token_a, text="upstream-429", user="upstream-user")
    assert rate_limited.status_code == 429
    assert error_code(rate_limited) == "MODEL_RATE_LIMITED"
    assert "upstream-secret" not in rate_limited.text

    unavailable = chat(client, env.token_a, text="upstream-500", user="upstream-user")
    assert unavailable.status_code == 503
    assert error_code(unavailable) == "MODEL_SERVICE_UNAVAILABLE"
    assert "upstream-secret" not in unavailable.text

    embeddings = client.post("/v1/embeddings", headers=auth(env.token_a),
                             json={"model": "openclaw/default", "input": "x"})
    assert embeddings.status_code == 501
    assert error_code(embeddings) == "EMBEDDINGS_UNSUPPORTED"


# ------------------------------------------------ timeout / cancel / terminals
def test_timeout_and_cancellation_are_terminal(env):
    async def timeout_case():
        facade = OpenClawFacade(env.service, executor=SlowExecutor(), executor_timeout_seconds=0.05)
        principal = facade.store.authenticate(auth(env.token_a))
        payload = {"model": "openclaw/default", "messages": [{"role": "user", "content": "slow"}],
                   "user": "slow-user"}
        with pytest.raises(OpenClawError) as exc:
            await facade.execute(principal, "chat.completions", payload, auth(env.token_a))
        assert exc.value.status == 504
        assert exc.value.code == "MODEL_TIMEOUT"
        row = env.service.db.one(
            "SELECT * FROM openclaw_turns WHERE principal_id=? ORDER BY rowid DESC LIMIT 1",
            (principal.principal_id,),
        )
        assert row["status"] == "failed"
        assert json.loads(row["error_json"])["code"] == "MODEL_TIMEOUT"
        assert any(event["action"] == "turn.failed" for event in env.facade.audit_events())

    asyncio.run(timeout_case())

    async def cancel_case():
        started = asyncio.Event()

        class WaitingExecutor:
            async def execute(self, request, context, cancelled):
                started.set()
                await cancelled.wait()
                raise asyncio.CancelledError

        facade = OpenClawFacade(env.service, executor=WaitingExecutor(), executor_timeout_seconds=5)
        principal = facade.store.authenticate(auth(env.token_a))
        payload = {"model": "openclaw/default", "messages": [{"role": "user", "content": "wait"}],
                   "user": "cancel-user"}
        task = asyncio.create_task(facade.execute(principal, "chat.completions", payload, auth(env.token_a)))
        await asyncio.wait_for(started.wait(), 2)
        for _ in range(100):
            if facade._active:
                break
            await asyncio.sleep(0.01)
        run_id = next(iter(facade._active))
        result = facade.cancel(principal, run_id)
        assert result["status"] == "cancelling"
        with pytest.raises(OpenClawError) as exc:
            await task
        assert exc.value.status == 499
        assert exc.value.code == "REQUEST_CANCELLED"
        row = env.service.db.one("SELECT * FROM openclaw_turns WHERE run_id=?", (run_id,))
        assert row["status"] == "cancelled"
        assert any(event["action"] == "turn.cancelled" for event in env.facade.audit_events())

    asyncio.run(cancel_case())


def test_cancel_endpoints_and_terminal_states(env):
    client = env.client
    completed = chat(client, env.token_a, text="cancel-me", user="terminal-user")
    assert completed.status_code == 200
    run_id = completed.headers["x-openclaw-run-id"]
    cancelled = client.post(f"/v1/runs/{run_id}/cancel", headers=auth(env.token_a))
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "succeeded"
    missing_run = client.post("/v1/runs/not-a-run/cancel", headers=auth(env.token_a))
    assert missing_run.status_code == 404
    assert error_code(missing_run) == "RUN_NOT_FOUND"
    cross_tenant = client.post(f"/v1/runs/{run_id}/cancel", headers=auth(env.token_b))
    assert cross_tenant.status_code == 404
    assert error_code(cross_tenant) == "RUN_NOT_FOUND"

    response = responses(client, env.token_a, text="cancel-response", user="terminal-response")
    assert response.status_code == 200
    response_id = response.json()["id"]
    cancelled_response = client.post(f"/v1/responses/{response_id}/cancel", headers=auth(env.token_a))
    assert cancelled_response.status_code == 200
    assert cancelled_response.json()["status"] == "succeeded"
    missing_response = client.post("/v1/responses/not-a-response/cancel", headers=auth(env.token_a))
    assert missing_response.status_code == 404
    assert error_code(missing_response) == "RESPONSE_NOT_FOUND"


# ----------------------------------------------------------- contract / safety
def test_harness_contract_and_default_executor_fail_closed(env):
    manifest = harness_contract_manifest()
    assert manifest["locked_openclaw_version"] == CANDIDATE_OPENCLAW_VERSION == "v2026.11.3"
    assert manifest["lock_state"] == "candidate_pending_customer_version_confirmation"
    assert "turn.execute" in manifest["supported"]
    assert "gateway.websocket_rpc" in manifest["unsupported"]
    assert "unknown_tool" in manifest["deny_by_default"]
    manifest["supported"].append("mutated")
    assert "mutated" not in AGENT_HARNESS_V2_CONTRACT["supported"]

    async def scenario():
        facade = OpenClawFacade(env.service, executor=UnavailableExecutor())
        principal = facade.store.authenticate(auth(env.token_a))
        payload = {"model": "openclaw/default", "messages": [{"role": "user", "content": "hello"}],
                   "user": "unavailable-user"}
        with pytest.raises(OpenClawError) as exc:
            await facade.execute(principal, "chat.completions", payload, auth(env.token_a))
        assert exc.value.status == 503
        assert exc.value.code == "MODEL_UNAVAILABLE"
        assert "echo" not in exc.value.message

    asyncio.run(scenario())


def test_audit_and_persistence_do_not_store_prompts_or_credentials(env):
    prompt = "SECRET-PROMPT-XYZ"
    user_alias = "secret-user-alias-XYZ"
    response = chat(client := env.client, env.token_a, text=prompt, user=user_alias)
    assert response.status_code == 200
    audit_text = json.dumps(env.facade.audit_events(), ensure_ascii=False)
    assert prompt not in audit_text
    assert user_alias not in audit_text
    assert env.token_a not in audit_text
    actions = {event["action"] for event in env.facade.audit_events()}
    assert {"auth.succeeded", "session.created", "turn.started", "turn.completed"} <= actions

    session = env.service.db.one("SELECT * FROM openclaw_sessions WHERE id=?",
                                 (response.headers["x-openclaw-session-id"],))
    assert user_alias not in json.dumps(session, ensure_ascii=False)
    turn = env.service.db.one("SELECT * FROM openclaw_turns WHERE run_id=?",
                              (response.headers["x-openclaw-run-id"],))
    assert prompt not in json.dumps({key: turn[key] for key in ("request_digest", "idempotency_key", "error_json")},
                                     ensure_ascii=False)
    assert len(turn["request_digest"]) == 64


# ------------------------------------------- session namespace and user binding
def test_user_namespace_is_reserved_and_sessions_are_user_bound(env):
    client = env.client
    derived = "user:" + hashlib.sha256(b"alice").hexdigest()[:40]
    for key in (derived, "user:anything", "User:mixed", "USER:upper"):
        response = chat(client, env.token_a, text="hijack", user=None,
                        headers={"x-openclaw-session-key": key})
        assert response.status_code == 400, key
        assert error_code(response) == "RESERVED_SESSION_KEY"

    alice = chat(client, env.token_a, text="one", user="alice")
    assert alice.status_code == 200, alice.text
    guess = chat(client, env.token_a, text="hijack", user=None,
                 headers={"x-openclaw-session-key": derived})
    assert guess.status_code == 400
    assert error_code(guess) == "RESERVED_SESSION_KEY"

    explicit = chat(client, env.token_a, text="one", user="alice",
                    headers={"x-openclaw-session-key": "ops-shared"})
    assert explicit.status_code == 200, explicit.text
    session_id = explicit.headers["x-openclaw-session-id"]
    again = chat(client, env.token_a, text="two", user="alice",
                 headers={"x-openclaw-session-key": "ops-shared"})
    assert again.status_code == 200
    assert again.headers["x-openclaw-session-id"] == session_id
    row = env.service.db.one("SELECT * FROM openclaw_sessions WHERE id=?", (session_id,))
    assert row["session_key"] == "ops-shared"
    assert row["user_digest"] == hashlib.sha256(b"alice").hexdigest()

    for headers in ({"x-openclaw-session-key": "ops-shared"},
                    {"x-openclaw-session-id": session_id}):
        hijack = chat(client, env.token_a, text="hijack", user="bob", headers=headers)
        assert hijack.status_code == 409, headers
        assert error_code(hijack) == "SESSION_CONFLICT"

    remembered = responses(client, env.token_a, text="remember", user="alice")
    assert remembered.status_code == 200, remembered.text
    response_id = remembered.json()["id"]
    hijack = responses(client, env.token_a, text="hijack", user="bob",
                       previous_response_id=response_id)
    assert hijack.status_code == 409
    assert error_code(hijack) == "SESSION_CONFLICT"
    same = responses(client, env.token_a, text="continue", user="alice",
                     previous_response_id=response_id)
    assert same.status_code == 200
    assert same.headers["x-openclaw-session-id"] == remembered.headers["x-openclaw-session-id"]


# ------------------------------- multi-instance lease / ownership (F3 rework)
def test_second_facade_startup_does_not_recover_another_instances_live_turn(env):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingExecutor:
            async def execute(self, request, context, cancelled):
                started.set()
                await release.wait()
                return NormalizedResult(text="done")

        first = OpenClawFacade(env.service, executor=BlockingExecutor(),
                               executor_timeout_seconds=5, lease_seconds=60)
        principal = first.store.authenticate(auth(env.token_a))
        payload = {"model": "openclaw/default",
                   "messages": [{"role": "user", "content": "lease"}], "user": "lease-user"}
        headers = {**auth(env.token_a), "Idempotency-Key": "lease-live"}
        task = asyncio.create_task(first.execute(principal, "chat.completions", payload, headers))
        await asyncio.wait_for(started.wait(), 2)
        run_id = next(iter(first._active))
        row = env.service.db.one("SELECT * FROM openclaw_turns WHERE run_id=?", (run_id,))
        assert row["status"] == "running"
        assert row["owner_instance"] == first.instance_id
        assert row["lease_expires_at"]

        second = OpenClawFacade(env.service, executor=ScriptedExecutor(),
                                executor_timeout_seconds=5, lease_seconds=60)
        row = env.service.db.one("SELECT * FROM openclaw_turns WHERE run_id=?", (run_id,))
        assert row["status"] == "running"
        assert not any(event["action"] == "turn.interrupted" for event in second.audit_events())

        second_principal = second.store.authenticate(auth(env.token_a))
        with pytest.raises(OpenClawError) as exc:
            await second.execute(second_principal, "chat.completions", payload, headers)
        assert (exc.value.status, exc.value.code) == (409, "REQUEST_IN_PROGRESS")

        release.set()
        outcome = await asyncio.wait_for(task, 2)
        assert outcome.body["choices"][0]["message"]["content"] == "done"
        row = env.service.db.one("SELECT * FROM openclaw_turns WHERE run_id=?", (run_id,))
        assert row["status"] == "succeeded"
        assert row["lease_expires_at"] is None

    asyncio.run(scenario())


def test_expired_lease_recovery_and_live_heartbeat(env):
    client = env.client
    seed = chat(client, env.token_a, text="seed", user="lease-recovery")
    assert seed.status_code == 200
    session = env.service.db.one("SELECT * FROM openclaw_sessions WHERE id=?",
                                 (seed.headers["x-openclaw-session-id"],))
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    leases = {"expired": past, "live": future, "legacy": None}
    run_ids: dict[str, str] = {}
    for label, lease in leases.items():
        turn_id, run_id = uid(), uid()
        run_ids[label] = run_id
        env.service.db.execute(
            "INSERT INTO openclaw_turns(id,tenant_id,principal_id,session_id,conversation_id,run_id,"
            "protocol,model_alias,request_digest,status,owner_instance,lease_expires_at,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (turn_id, session["tenant_id"], session["principal_id"], session["id"],
             session["conversation_id"], run_id, "chat.completions", "openclaw/default",
             "0" * 64, "running", f"{label}-instance", lease, now()),
        )

    facade = OpenClawFacade(env.service, executor=ScriptedExecutor(), lease_seconds=60)
    for label in ("expired", "legacy"):
        row = env.service.db.one("SELECT * FROM openclaw_turns WHERE run_id=?", (run_ids[label],))
        assert row["status"] == "failed", label
        assert row["http_status"] == 503
        assert json.loads(row["error_json"])["code"] == "INTERRUPTED"
        assert row["lease_expires_at"] is None
    live = env.service.db.one("SELECT * FROM openclaw_turns WHERE run_id=?", (run_ids["live"],))
    assert live["status"] == "running"
    assert live["lease_expires_at"] == future
    interrupted = [event for event in facade.audit_events() if event["action"] == "turn.interrupted"]
    assert len(interrupted) == 2
    assert all(event["detail"]["lease_expired"] is True for event in interrupted)

    async def heartbeat_case():
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingExecutor:
            async def execute(self, request, context, cancelled):
                started.set()
                await release.wait()
                return NormalizedResult(text="done")

        active = OpenClawFacade(env.service, executor=BlockingExecutor(),
                                executor_timeout_seconds=5, lease_seconds=0.3)
        principal = active.store.authenticate(auth(env.token_a))
        payload = {"model": "openclaw/default",
                   "messages": [{"role": "user", "content": "heartbeat"}],
                   "user": "heartbeat-user"}
        task = asyncio.create_task(active.execute(principal, "chat.completions", payload,
                                                  auth(env.token_a)))
        await asyncio.wait_for(started.wait(), 2)
        run_id = next(iter(active._active))
        before = env.service.db.one("SELECT lease_expires_at FROM openclaw_turns WHERE run_id=?",
                                    (run_id,))["lease_expires_at"]
        await asyncio.sleep(0.7)
        after = env.service.db.one("SELECT lease_expires_at FROM openclaw_turns WHERE run_id=?",
                                   (run_id,))["lease_expires_at"]
        assert after > before

        second = OpenClawFacade(env.service, executor=ScriptedExecutor(), lease_seconds=60)
        row = env.service.db.one("SELECT * FROM openclaw_turns WHERE run_id=?", (run_id,))
        assert row["status"] == "running"
        assert not any(event["action"] == "turn.interrupted"
                       for event in second.audit_events() if event["run_id"] == run_id)

        release.set()
        await asyncio.wait_for(task, 2)
        row = env.service.db.one("SELECT * FROM openclaw_turns WHERE run_id=?", (run_id,))
        assert row["status"] == "succeeded"
        assert row["lease_expires_at"] is None

    asyncio.run(heartbeat_case())


# -------------------------------- contract boundary versus delivered fields (F4)
def test_harness_manifest_separates_supported_from_target_only(env):
    manifest = harness_contract_manifest()
    supported = set(manifest["supported"])
    target = set(manifest["target_only"])
    assert supported.isdisjoint(target)
    for capability in ("context.summary", "memory.read", "memory.propose_write", "approval.resume",
                       "event.run.stage", "event.tool", "event.approval", "event.message",
                       "event.usage", "event.run.completed"):
        assert capability in target
        assert capability not in supported
    assert set(manifest["capability_scope"]) == supported
    assert manifest["slice_scope"] == "synthetic_first_slice_no_real_harness_executor_or_mcp_transport"
    assert manifest["recovery_model"] == "owner_instance_plus_lease_expiry_heartbeat"

    assert set(manifest["turn_field_status"]) == set(manifest["required_turn_fields"])
    not_delivered = {name for name, status in manifest["turn_field_status"].items()
                     if status.startswith("not_delivered")}
    assert not_delivered == set(manifest["turn_fields_not_yet_delivered"])
    assert not_delivered == {"context_digest", "tool_policy_digest", "event_stream_id"}
    for name in ("turn_id", "session_id", "agent_id", "messages"):
        assert manifest["turn_field_status"][name] == "delivered_to_executor"

    response = chat(env.client, env.token_a, text="fields", user="field-user")
    assert response.status_code == 200
    request, context = env.executor.calls[-1]
    for field_name in not_delivered:
        assert field_name not in request.__dataclass_fields__
        assert field_name not in context.__dataclass_fields__
    assert context.turn_id == request.request_id
    assert context.session_id == response.headers["x-openclaw-session-id"]
    assert context.agent_id == "default"
    assert request.messages[-1].text == "fields"


# ------------------------- size, retention and binding-only continuation (F5)
def test_request_size_retention_and_binding_only_continuation(env):
    client = env.client
    long_text = chat(client, env.token_a, text="x" * 100_001, user="size-user")
    assert long_text.status_code == 413
    assert error_code(long_text) == "REQUEST_TOO_LARGE"

    huge = client.post("/v1/chat/completions",
                       headers={**auth(env.token_a), "Content-Type": "application/json"},
                       content=json.dumps({"model": "openclaw/default", "messages": [
                           {"role": "user", "content": "x" * 1_100_000}]}).encode())
    assert huge.status_code == 413
    assert error_code(huge) == "UPLOAD_TOO_LARGE"

    first = responses(client, env.token_a, text="retained", user="retention-user")
    assert first.status_code == 200
    response_id = first.json()["id"]
    turn = env.service.db.one("SELECT id FROM openclaw_turns WHERE run_id=?",
                              (first.headers["x-openclaw-run-id"],))
    session_id = first.headers["x-openclaw-session-id"]
    env.service.db.execute(
        "UPDATE openclaw_responses SET created_at=? WHERE response_id=?",
        ((datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(), response_id),
    )
    expired = responses(client, env.token_a, text="again", user=None,
                        previous_response_id=response_id)
    assert expired.status_code == 404
    assert error_code(expired) == "RESPONSE_NOT_FOUND"
    # 24h is a retrieval TTL, not a deletion job: the stored rows remain.
    assert env.service.db.one("SELECT response_id FROM openclaw_responses WHERE response_id=?", (response_id,))
    assert env.service.db.one("SELECT id FROM openclaw_turns WHERE id=?", (turn["id"],))
    assert env.service.db.one("SELECT id FROM openclaw_sessions WHERE id=?", (session_id,))

    remembered = responses(client, env.token_a, text="first-message", user="binding-user")
    assert remembered.status_code == 200, remembered.text
    continued = responses(client, env.token_a, text="second-message", user=None,
                          previous_response_id=remembered.json()["id"])
    assert continued.status_code == 200, continued.text
    request, _ = env.executor.calls[-1]
    texts = [message.text for message in request.messages]
    assert "first-message" not in texts
    assert texts[-1] == "second-message"
