import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from app.capability_settings import CapabilitySettings, CapabilityUpdate
from app.config import Settings
from app.main import create_app
from app.security import AppError
from app.web_tools import WebTools

PASSWORD = "Synthetic-pass-for-test-123"


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",),
                    web_search_provider="bing_rss", web_search_fallback_bing=True,
                    web_search_searxng_url="https://search.example/search", web_search_brave_api_key="",
                    deepseek_api_enabled=False, deepseek_api_key="")


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        service = app.state.service
        for role in ("admin", "member"):
            service.create_user({"username": role, "display_name": role, "role": role, "password": PASSWORD})
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 200
        yield client


def payload(client):
    value = client.get("/api/v1/capability-settings").json()
    return {key: value[key] for key in ("revision", "search", "fetch")}


def setup_tools(client):
    service = client.app.state.service
    user = service.db.one("SELECT * FROM users WHERE username='admin'")
    assistant = service.save_assistant({"name": "Synthetic", "instructions": "Test", "web_search_enabled": True}, user=user)
    service.evidence = Mock()
    service.evidence.register.return_value = {"id": "source-test"}
    return service, user, assistant, WebTools(service)


def test_admin_only_and_unauthenticated(client):
    before = payload(client)
    client.post("/api/v1/auth/logout")
    assert client.get("/api/v1/capability-settings").status_code == 401
    assert client.put("/api/v1/capability-settings", json=before).status_code == 401
    client.post("/api/v1/auth/login", json={"username": "member", "password": PASSWORD})
    assert client.get("/api/v1/capability-settings").status_code == 403
    assert client.put("/api/v1/capability-settings", json=before).status_code == 403
    assert client.app.state.service.db.one("SELECT COUNT(*) AS n FROM capability_settings")["n"] == 0


def test_trial_routes_fail_closed(settings):
    settings.deployment_mode = "trial"
    with TestClient(create_app(settings)) as client:
        for method in ("get", "put"):
            result = getattr(client, method)("/api/v1/capability-settings")
            assert result.status_code == 403
            assert result.json()["error"]["code"] == "FEATURE_DISABLED"
        assert client.app.state.service.db.one("SELECT COUNT(*) AS n FROM capability_settings")["n"] == 0


def test_defaults_preserve_environment_and_do_not_claim_verified(client, settings):
    value = client.get("/api/v1/capability-settings").json()
    assert value["revision"] == 0 and value["origin"] == "environment"
    assert value["search"]["preferred"] == "bing_rss"
    assert value["fetch"]["preferred"] == "public_http"
    assert value["ocr"]["configurable"] is False
    assert value["image_search"]["configurable"] is False
    assert all(p["verification"] == "not_tested" for p in value["providers"])
    settings.web_search_provider = "searxng"
    value = client.get("/api/v1/capability-settings").json()
    assert value["search"]["preferred"] == "searxng"
    assert value["search"]["fallbacks"] == ["bing_rss"]
    settings.web_search_provider = "deepseek_api"
    settings.web_search_fallback_bing = False
    value = client.get("/api/v1/capability-settings").json()
    assert value["fetch"]["preferred"] == "deepseek_api" and value["fetch"]["fallbacks"] == []


def test_save_reload_conflict_and_secret_free_audit(client, settings):
    settings.deepseek_api_enabled = True
    settings.deepseek_api_key = "synthetic-key-not-for-display"
    settings.web_search_searxng_url = "https://search.example/private-endpoint?q=not-for-display"
    data = payload(client)
    data["search"].update(preferred="deepseek_api", fallbacks=["bing_rss"], timeout_seconds=12)
    response = client.put("/api/v1/capability-settings", json=data)
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 1 and response.json()["origin"] == "administrator"
    assert client.get("/api/v1/capability-settings").json()["search"] == data["search"]
    assert client.put("/api/v1/capability-settings", json=data).status_code == 409
    database = client.app.state.service.db
    audit = database.all("SELECT * FROM capability_settings_audit")
    assert len(audit) == 1 and audit[0]["revision"] == 1 and audit[0]["updated_by"]
    assert "not-for-display" not in response.text + json.dumps(audit)
    assert "private-endpoint" not in response.text + json.dumps(audit)


def test_persistence_across_service_restart(settings):
    with TestClient(create_app(settings)) as first:
        first.app.state.service.create_user({"username": "admin", "display_name": "Admin", "role": "admin", "password": PASSWORD})
        first.post("/api/v1/auth/login", json={"username": "admin", "password": PASSWORD})
        data = payload(first)
        data["fetch"]["enabled"] = False
        assert first.put("/api/v1/capability-settings", json=data).status_code == 200
    with TestClient(create_app(settings)) as second:
        second.post("/api/v1/auth/login", json={"username": "admin", "password": PASSWORD})
        value = second.get("/api/v1/capability-settings").json()
        assert value["revision"] == 1 and value["fetch"]["enabled"] is False


def test_concurrent_saves_have_one_winner(client):
    service = client.app.state.service
    user = service.db.one("SELECT * FROM users WHERE username='admin'")
    update = CapabilityUpdate(**payload(client))
    def save():
        try:
            return CapabilitySettings(service).save(user, update)["revision"]
        except AppError as exc:
            return exc.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: save(), range(2))) == [1, 409]


@pytest.mark.parametrize("change", [
    {"preferred": "public_http"}, {"preferred": "brave"}, {"preferred": "deepseek_api"},
    {"fallbacks": ["bing_rss"]}, {"timeout_seconds": 0}, {"timeout_seconds": 61},
    {"timeout_seconds": True}, {"timeout_seconds": 1.5}, {"api_key": "never-store"},
    {"enabled": "false"}, {"preferred": "https://credential.example/?key=secret"},
])
def test_invalid_unconfigured_or_secret_inputs_rejected(client, change):
    data = payload(client)
    data["search"].update(change)
    response = client.put("/api/v1/capability-settings", json=data)
    assert response.status_code == 422
    assert client.app.state.service.db.one("SELECT COUNT(*) AS n FROM capability_settings")["n"] == 0
    assert "never-store" not in response.text and "credential.example" not in response.text


def test_unsupported_capabilities_are_not_writable(client):
    data = payload(client)
    data["ocr"] = {"enabled": True}
    assert client.put("/api/v1/capability-settings", json=data).status_code == 422


def test_search_route_is_applied_and_failure_falls_back(client, monkeypatch):
    service, user, assistant, tools = setup_tools(client)
    data = payload(client)
    data["search"].update(preferred="searxng", fallbacks=["bing_rss"])
    assert client.put("/api/v1/capability-settings", json=data).status_code == 200
    searx = AsyncMock(return_value=[{"url": "https://example.com", "title": "Public", "excerpt": "Evidence"}])
    bing = AsyncMock(return_value=[])
    monkeypatch.setattr(tools, "_searxng", searx)
    monkeypatch.setattr(tools, "_bing", bing)
    result = asyncio.run(tools.search_for_run(user, assistant, "run", "public query"))
    assert result["provider"] == "searxng" and not result["fallback"]
    assert result["results"][0]["source_id"] == "source-test"
    bing.assert_not_awaited()
    searx.side_effect = AppError(503, "UNAVAILABLE", "Unavailable")
    result = asyncio.run(tools.search_for_run(user, assistant, "run", "public query"))
    assert result["provider"] == "bing_rss" and result["fallback"]
    assert bing.await_count == 1


@pytest.mark.parametrize("status", [403, 422])
def test_permission_and_safety_errors_do_not_fallback(client, monkeypatch, status):
    service, user, assistant, tools = setup_tools(client)
    data = payload(client)
    data["search"].update(preferred="searxng", fallbacks=["bing_rss"])
    client.put("/api/v1/capability-settings", json=data)
    monkeypatch.setattr(tools, "_searxng", AsyncMock(side_effect=AppError(status, "DENIED", "Denied")))
    fallback = AsyncMock()
    monkeypatch.setattr(tools, "_bing", fallback)
    with pytest.raises(AppError) as result:
        asyncio.run(tools.search_for_run(user, assistant, "run", "public query"))
    assert result.value.status == status
    fallback.assert_not_awaited()


@pytest.mark.parametrize("revoke", ["global", "assistant", "user", "revision"])
@pytest.mark.parametrize("fail", [True, False])
def test_inflight_revocation_never_falls_back_or_registers_sources(client, monkeypatch, revoke, fail):
    service, user, assistant, tools = setup_tools(client)
    data = payload(client)
    data["search"].update(preferred="searxng", fallbacks=["bing_rss"])
    saved = client.put("/api/v1/capability-settings", json=data).json()
    async def primary(*args):
        if revoke in {"global", "revision"}:
            changed = {key: saved[key] for key in ("revision", "search", "fetch")}
            changed["search"]["enabled"] = revoke == "revision"
            CapabilitySettings(service).save(user, CapabilityUpdate(**changed))
        elif revoke == "assistant":
            service.db.execute("UPDATE assistants SET web_search_enabled=0 WHERE id=?", (assistant["id"],))
        else:
            service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (user["id"],))
        if fail:
            raise AppError(503, "UNAVAILABLE", "Unavailable")
        return [{"url": "https://example.com", "title": "Public", "excerpt": "Evidence"}]
    monkeypatch.setattr(tools, "_searxng", primary)
    fallback = AsyncMock()
    monkeypatch.setattr(tools, "_bing", fallback)
    with pytest.raises(AppError) as result:
        asyncio.run(tools.search_for_run(user, assistant, "run", "query"))
    assert result.value.status in {403, 409}
    fallback.assert_not_awaited()
    service.evidence.register.assert_not_called()


def test_fetch_is_independent_and_uses_configured_provider(client, settings, monkeypatch):
    service, user, assistant, tools = setup_tools(client)
    settings.deepseek_api_enabled = True
    settings.deepseek_api_key = "synthetic"
    data = payload(client)
    data["search"]["enabled"] = False
    data["fetch"].update(preferred="deepseek_api", fallbacks=["public_http"])
    assert client.put("/api/v1/capability-settings", json=data).status_code == 200
    external = Mock(fetch=AsyncMock(return_value={"url": "https://example.com", "content": "Body", "title": "Public"}))
    monkeypatch.setattr(tools, "_deepseek_client", lambda: external)
    fetch = AsyncMock(return_value=("https://example.com", "text/plain", b"Fallback"))
    monkeypatch.setattr("app.web_tools.fetch_public", fetch)
    result = asyncio.run(tools.read_for_run(user, assistant, "run", "https://example.com"))
    assert result["provider"] == "managed_search" and result["fallback"] is False
    external.fetch.side_effect = AppError(503, "UNAVAILABLE", "Unavailable")
    result = asyncio.run(tools.read_for_run(user, assistant, "run", "https://example.com"))
    assert result["provider"] == "public_http" and result["fallback"] is True
    with pytest.raises(AppError) as result:
        asyncio.run(tools.search_for_run(user, assistant, "run", "query"))
    assert result.value.code == "CAPABILITY_DISABLED"


def test_timeout_and_cancellation_do_not_create_sources(client, monkeypatch):
    service, user, assistant, tools = setup_tools(client)
    data = payload(client)
    data["search"]["timeout_seconds"] = 1
    client.put("/api/v1/capability-settings", json=data)
    async def slow(*args):
        await asyncio.sleep(10)
    monkeypatch.setattr(tools, "_bing", slow)
    with pytest.raises(AppError) as result:
        asyncio.run(tools.search_for_run(user, assistant, "run", "query"))
    assert result.value.status == 504
    async def cancel():
        task = asyncio.create_task(tools.search_for_run(user, assistant, "run", "query"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel())
    service.evidence.register.assert_not_called()


def test_runtime_authorization_checks_saved_global_policy(client):
    service, user, assistant, _ = setup_tools(client)
    service.db.execute("INSERT INTO conversations(id,user_id,assistant_id,title,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                       ("runtime-test", user["id"], assistant["id"], "Test", "2026-01-01", "2026-01-01"))
    assert service.authorize_tool("runtime-test", "mcp__workspace__web_search", {}) is True
    data = payload(client)
    data["search"]["enabled"] = False
    client.put("/api/v1/capability-settings", json=data)
    with pytest.raises(AppError) as result:
        service.authorize_tool("runtime-test", "mcp__workspace__web_search", {})
    assert result.value.code == "CAPABILITY_DISABLED"
    assert service.authorize_tool("runtime-test", "mcp__workspace__web_read", {}) is True


def test_stale_admin_object_cannot_write_after_demotion(client):
    service = client.app.state.service
    user = service.db.one("SELECT * FROM users WHERE username='admin'")
    data = CapabilityUpdate(**payload(client))
    service.db.execute("UPDATE users SET role='member' WHERE id=?", (user["id"],))
    with pytest.raises(AppError) as result:
        CapabilitySettings(service).save(user, data)
    assert result.value.status == 403
    assert service.db.one("SELECT COUNT(*) AS n FROM capability_settings")["n"] == 0
