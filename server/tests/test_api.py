import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import AppError, workspace_path


PASSWORD = "Correct-test-password-123"


class FakeRuntime:
    """Explicit test injection: never selectable by a production environment flag."""
    connects = 0

    def __init__(self, service, conversation_id, config, approve, emit):
        self.service, self.conversation_id = service, conversation_id
        self.approve, self.emit = approve, emit
        self.stopped = asyncio.Event()

    async def connect(self):
        FakeRuntime.connects += 1

    async def execute(self, run, message):
        if message["text"] == "wait":
            await self.stopped.wait()
            raise asyncio.CancelledError()
        if message["text"] == "approval":
            allowed = await self.approve("test_business_write", {"ticket": "immutable", "api_token": "do-not-show"})
            text = "已执行" if allowed else "已拒绝"
        else:
            text = "回复：" + message["text"]
        await self.emit("message.delta", {"message_id": run["assistant_message_id"], "text": text})
        await self.emit("usage.updated", {"input_tokens": 12, "output_tokens": 4, "estimated_cost_usd": None, "currency": "USD"})

    async def interrupt(self):
        self.stopped.set()

    async def close(self):
        pass


@pytest.fixture
def configured(tmp_path):
    return Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",), mcp_hosts=("localhost", "127.0.0.1"), idle_seconds=60, approval_seconds=3)


@pytest.fixture
def client(configured):
    app = create_app(configured, runtime_factory=FakeRuntime)
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
        login(client)
        yield client


def login(client, username="admin"):
    response = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    assert "httponly" in response.headers["set-cookie"].lower()
    return response.json()


def assistant(client):
    response = client.post("/api/v1/assistants", json={"name": "QA", "instructions": "业务测试", "connections": []})
    assert response.status_code == 201, response.text
    return response.json()


def conversation(client):
    return client.post("/api/v1/conversations", json={"assistant_id": assistant(client)["id"]}).json()


def send(client, cid, text="hello", request_id="request-0001", files=None):
    response = client.post(f"/api/v1/conversations/{cid}/messages", json={"client_request_id": request_id, "text": text, "file_ids": files or []})
    assert response.status_code == 202, response.text
    return response.json()


def poll(client, run_id, statuses=None):
    statuses = statuses or {"succeeded", "failed", "cancelled", "interrupted"}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get(f"/api/v1/runs/{run_id}").json()
        if result["status"] in statuses:
            return result
        time.sleep(0.02)
    raise AssertionError(f"run did not reach {statuses}: {result}")


def test_login_roles_and_origin(client):
    assert client.get("/api/v1/auth/me").json()["role"] == "admin"
    response = client.post("/api/v1/users", json={"username": "member", "password": PASSWORD, "display_name": "员工", "role": "member"})
    assert response.status_code == 201
    bad = client.post("/api/v1/auth/logout", headers={"Origin": "https://attacker.example"})
    assert bad.status_code == 403
    login(client, "member")
    assert client.get("/api/v1/connections").status_code == 200
    assert client.post("/api/v1/users", json={"username": "bad", "password": PASSWORD, "display_name": "bad"}).status_code == 403
    client.post("/api/v1/auth/logout")
    assert client.get("/api/v1/auth/me").status_code == 401


def test_secret_encryption_and_url_policy(client):
    data = {"name": "MCP", "url": "http://127.0.0.1:8932/mcp", "auth_type": "bearer", "credential": "sensitive-key"}
    response = client.post("/api/v1/connections", json=data)
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["credential_present"] and "sensitive-key" not in response.text
    stored = client.app.state.service.require("connections", item["id"])
    assert "sensitive-key" not in stored["secret"]
    assert client.app.state.service.secrets.decrypt(stored["secret"]) == "sensitive-key"
    updated = client.patch(f"/api/v1/connections/{item['id']}", json={"name": "Updated"})
    assert updated.json()["credential_present"]
    data["url"] = "http://169.254.169.254/latest/meta-data"
    assert client.post("/api/v1/connections", json=data).status_code == 422
    data["url"] = "http://user:secret@localhost/mcp"
    assert client.post("/api/v1/connections", json=data).status_code == 422


def test_mcp_discovery_and_live_permission_revocation(client):
    async def discovery(row):
        return [{"name": "lookup", "description": "Read", "inputSchema": {"type": "object"}}]
    service = client.app.state.service
    service.discover = discovery
    connection = client.post("/api/v1/connections", json={"name": "Business", "url": "http://localhost:8932/mcp"}).json()
    tested = client.post(f"/api/v1/connections/{connection['id']}/test").json()
    assert tested["status"] == "connected" and tested["tools"][0]["name"] == "lookup"
    created = client.post("/api/v1/assistants", json={"name": "Business", "connections": [{"connection_id": connection["id"], "allowed_tools": ["lookup"], "auto_approved_tools": ["lookup"]}]}).json()
    convo = client.post("/api/v1/conversations", json={"assistant_id": created["id"]}).json()
    tool = f"mcp__c{connection['id']}__lookup"
    assert service.authorize_tool(convo["id"], tool, {}) is True
    client.patch(f"/api/v1/connections/{connection['id']}", json={"enabled": False})
    with pytest.raises(AppError):
        service.authorize_tool(convo["id"], tool, {})


def test_replay_idempotency_and_warm_session(client):
    before = FakeRuntime.connects
    cid = conversation(client)["id"]
    first = send(client, cid)
    assert poll(client, first["run_id"])["status"] == "succeeded"
    repeated = send(client, cid)
    assert repeated["run_id"] == first["run_id"]
    second = send(client, cid, "second", "request-0002")
    poll(client, second["run_id"])
    assert FakeRuntime.connects == before + 1
    messages = client.get(f"/api/v1/conversations/{cid}/messages").json()
    assert len(messages) == 4
    assert messages[1]["text"] == "回复：hello"
    stream = client.get(f"/api/v1/runs/{first['run_id']}/events").text
    assert "event: message.delta" in stream and "event: run.completed" in stream
    rows = client.app.state.service.db.events(first["run_id"], 0)
    replay = client.get(f"/api/v1/runs/{first['run_id']}/events?after={rows[-2]['id']}").text
    assert "event: run.completed" in replay and "event: message.delta" not in replay
    usage = client.get("/api/v1/usage").json()
    assert usage["total_runs"] == 2 and usage["estimated_cost_usd"] is None
    trace = client.get(f"/api/v1/runs/{first['run_id']}/trace")
    assert trace.status_code == 200
    value = trace.json()
    assert value["run"]["duration_ms"] >= 0
    assert value["run"]["model"] == client.app.state.service.catalog.defaults["model_label"]
    assert value["usage"]["input_tokens"] == 12
    assert "raw_events" not in value
    assert len(value["messages"]) == 2


def test_approval_immutable_and_cancel(client):
    cid = conversation(client)["id"]
    run = send(client, cid, "approval")
    waiting = poll(client, run["run_id"], {"awaiting_approval"})
    approval = waiting["approvals"][0]
    assert approval["arguments"]["api_token"] == "[redacted]"
    denied_mutation = client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "allow", "arguments": {}})
    assert denied_mutation.status_code == 422
    assert client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "deny"}).status_code == 200
    assert poll(client, run["run_id"])["status"] == "succeeded"
    assert client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "allow"}).status_code == 409
    waiting_run = send(client, cid, "wait", "request-wait")
    poll(client, waiting_run["run_id"], {"running"})
    conflict = client.post(f"/api/v1/conversations/{cid}/messages", json={"text": "other", "client_request_id": "request-conflict"})
    assert conflict.status_code == 409
    assert client.post(f"/api/v1/runs/{waiting_run['run_id']}/cancel").status_code == 200
    assert poll(client, waiting_run["run_id"])["status"] == "cancelled"


def test_ownership_files_and_path_guards(client, configured):
    cid = conversation(client)["id"]
    uploaded = client.post("/api/v1/files", files={"file": ("../../notes.txt", b"business info", "text/plain")})
    assert uploaded.status_code == 201
    file = uploaded.json()
    assert file["name"] == "notes.txt"
    content = client.get(f"/api/v1/files/{file['id']}/content")
    assert content.content == b"business info"
    assert content.headers["x-content-type-options"] == "nosniff"
    client.post("/api/v1/users", json={"username": "member", "password": PASSWORD, "display_name": "员工"})
    login(client, "member")
    assert client.get(f"/api/v1/conversations/{cid}").status_code == 404
    assert client.get(f"/api/v1/files/{file['id']}/content").status_code == 404
    root = configured.data_dir / "workspaces" / cid
    root.mkdir(parents=True, exist_ok=True)
    for path in ("../../app.sqlite3", ".claude/settings.json", str(configured.data_dir / "encryption.key")):
        with pytest.raises(AppError):
            workspace_path(root, path)
    with pytest.raises(AppError):
        client.app.state.service.authorize_tool(cid, "Bash", {"command": "echo hello"})


def test_restart_interrupts_without_replaying(configured):
    app = create_app(configured, FakeRuntime)
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        cid = conversation(client)["id"]
        result, _ = service.prepare_run(cid, {"text": "do not execute", "client_request_id": "restart-case", "file_ids": []}, user)
    with TestClient(create_app(configured, FakeRuntime)) as client:
        login(client)
        run = client.get(f"/api/v1/runs/{result['run_id']}").json()
        assert run["status"] == "interrupted"
        assert "run.completed" in client.get(f"/api/v1/runs/{run['id']}/events").text
        assert client.get(f"/api/v1/conversations/{cid}").json()["active_run_id"] is None


def test_provider_import_only_allowlisted_env(tmp_path, monkeypatch):
    from app.config import PROVIDER_KEYS
    for key in PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "test-only", "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic", "LD_PRELOAD": "bad"}, "hooks": {"SessionStart": "bad"}}))
    settings = Settings(provider_settings=str(path))
    result = settings.provider_env()
    assert result["ANTHROPIC_AUTH_TOKEN"] == "test-only"
    assert "LD_PRELOAD" not in result and "hooks" not in result


def test_cancel_during_startup_never_sends_a_model_query(configured):
    executed = []

    class SlowConnect(FakeRuntime):
        async def connect(self):
            await asyncio.sleep(0.25)

        async def interrupt(self):
            # A runtime that has not connected cannot interrupt a remote turn.
            pass

        async def execute(self, run, message):
            executed.append(run["id"])

    app = create_app(configured, SlowConnect)
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        cid = conversation(client)["id"]
        run = send(client, cid, "must not execute")
        time.sleep(0.05)
        assert client.post(f"/api/v1/runs/{run['run_id']}/cancel").status_code == 200
        assert poll(client, run["run_id"])["status"] == "cancelled"
        assert executed == []


def test_chunked_upload_limited_before_multipart_parsing(configured):
    configured.upload_max_bytes = 64
    app = create_app(configured, FakeRuntime)
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)

        def body():
            yield b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="big.txt"\r\n\r\n'
            for _ in range(20):
                yield b"x" * 65536
            yield b"\r\n--boundary--\r\n"

        response = client.post("/api/v1/files", content=body(), headers={"Content-Type": "multipart/form-data; boundary=boundary"})
        assert "content-length" not in response.request.headers
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "UPLOAD_TOO_LARGE"
        assert app.state.service.db.one("SELECT COUNT(*) n FROM files")["n"] == 0


def test_random_usernames_do_not_bypass_ip_login_limit(client):
    responses = [client.post("/api/v1/auth/login", json={"username": f"random-{index}", "password": "incorrect-password"}).status_code for index in range(42)]
    assert 429 in responses
