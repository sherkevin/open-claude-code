import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.config import Settings
from app.mcp_import import create_import_router, parse_mcp_config
from app.security import AppError
from app.service import Service


def mcp(name="knowledge", **changes):
    return {"type": "mcp", "server_label": name, "server_url": "http://localhost:8767/mcp", **changes}


@pytest.fixture
def imported(tmp_path):
    service = Service(Settings(data_dir=tmp_path, model="fixture", provider_settings="", mcp_hosts=("localhost",)))
    app = FastAPI()

    @app.exception_handler(AppError)
    async def error(request, exc):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})

    def svc():
        return service

    def user(request: Request):
        role = request.headers.get("x-test-role", "admin")
        return {"id": role, "role": role}

    app.include_router(create_import_router(svc, user))
    with TestClient(app) as client:
        yield client, service
    service.db.close()


def test_preview_redacts_and_never_persists_or_grants(imported):
    client, service = imported
    secret = "test-import-secret-value"
    config = mcp(authorization=secret, allowed_tools=["search_knowledge"], require_approval="never")
    preview = client.post("/api/v1/connections/import/preview", json={"config": config})
    assert preview.status_code == 200
    assert secret not in preview.text and '"authorization"' not in preview.text
    assert preview.json()["connections"][0]["credential_present"] is True
    assert len(preview.json()["warnings"]) >= 2
    assert service.db.all("SELECT * FROM connections") == []
    saved = client.post("/api/v1/connections/import", json={"config": config})
    assert saved.status_code == 201 and saved.json()["binding_required"] is True
    assert secret not in saved.text
    connection = saved.json()["connections"][0]
    assert connection["status"] == "untested" and connection["tools"] == []
    encrypted = service.db.one("SELECT secret FROM connections WHERE id=?", (connection["id"],))["secret"]
    assert secret not in encrypted and service.secrets.decrypt(encrypted) == secret
    assert service.db.all("SELECT * FROM assistants") == []


def test_standard_mcp_servers_and_authorization_case():
    config = {"mcpServers": {"业务知识库": {"type": "http", "url": "http://localhost/mcp", "headers": {"authorization": "Bearer test-secret"}}}}
    plan = parse_mcp_config(config, ("localhost",))
    assert plan.format == "mcp_servers"
    assert plan.connections[0].credential == "test-secret"
    assert "test-secret" not in json.dumps(plan.preview())


def test_approval_filter_is_a_hint_only():
    plan = parse_mcp_config({"tools": [mcp(allowed_tools={"tool_names": ["read"], "read_only": True}, require_approval={"never": {"tool_names": ["read"]}})]}, ("localhost",))
    assert plan.connections[0].allowed_tools["read_only"] is True
    assert plan.connections[0].require_approval["never"]["tool_names"] == ["read"]
    assert set(plan.connections[0].create_fields()) == {"name", "url", "transport", "auth_type", "credential", "enabled"}


@pytest.mark.parametrize("config", [
    {"mcpServers": {"x": {"command": "untrusted", "args": []}}},
    {"mcpServers": {"x": {"type": "sse", "url": "http://localhost/mcp"}}},
    {"mcpServers": {"x": {"type": {}, "url": "http://localhost/mcp"}}},
    {"mcpServers": {"x": {"url": "http://localhost/mcp", "headers": {"X-Key": "do-not-leak"}}}},
    mcp(server_url="http://localhost/mcp?token=do-not-leak"),
    mcp(server_url="http://secret:do-not-leak@localhost/mcp"),
    mcp(server_url="http://unlisted.example/mcp"),
    mcp(authorization="valid\nInjected-header: do-not-leak"),
    mcp(authorization="${SECRET_ENV}"),
    mcp(allowed_tools={"tool_names": "do-not-leak"}),
    mcp(require_approval={"never": "do-not-leak"}),
    [mcp("duplicate"), mcp("duplicate")],
])
def test_invalid_configs_are_rejected_without_values(config):
    with pytest.raises(AppError) as raised:
        parse_mcp_config(config, ("localhost",))
    assert "do-not-leak" not in raised.value.message


def test_permission_and_existing_name_recheck(imported):
    client, service = imported
    body = {"config": mcp(), "scope": "team"}
    for suffix in ("/preview", ""):
        denied = client.post("/api/v1/connections/import" + suffix, headers={"x-test-role": "member"}, json=body)
        assert denied.status_code == 403
    assert client.post("/api/v1/connections/import/preview", json=body).status_code == 200
    assert client.post("/api/v1/connections/import", json=body).status_code == 201
    # A stale successful preview never grants permission to replace existing data.
    assert client.post("/api/v1/connections/import", json=body).status_code == 409
    assert len(service.db.all("SELECT * FROM connections")) == 1


def test_batch_is_atomic_if_persistence_fails(imported, monkeypatch):
    client, service = imported
    original = service.save_connection

    def fail_second(data, **kwargs):
        if data["name"] == "second":
            raise AppError(409, "TEST_CONFLICT", "测试冲突")
        return original(data, **kwargs)

    monkeypatch.setattr(service, "save_connection", fail_second)
    result = client.post("/api/v1/connections/import", json={"config": [mcp("first"), mcp("second")]})
    assert result.status_code == 409
    assert service.db.all("SELECT * FROM connections") == []


def test_import_limits(imported):
    client, service = imported
    too_many = client.post("/api/v1/connections/import", json={"config": [mcp(str(i)) for i in range(17)]})
    assert too_many.status_code == 422
    too_large = client.post("/api/v1/connections/import/preview", json={"config": {"oversized": "x" * 262145}})
    assert too_large.status_code == 413
    assert service.db.all("SELECT * FROM connections") == []
