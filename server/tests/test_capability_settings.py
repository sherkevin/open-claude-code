import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.capability_credentials import CapabilityCredentials, secure_transport
from app.capability_execution import CapabilityExecutor, RunContext, new_call_id
from app.capability_policy import authorize_source_access, classify_error
from app.capability_settings import CAPABILITY_KINDS, CapabilitySettings, CapabilityUpdate
from app.capability_usage import CapabilityUsage
from app.config import Settings
from app.db import dump, now
from app.main import create_app
from app.security import AppError
from app.service import Service, assistant_view
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
    return {key: value[key] for key in ("revision", *CAPABILITY_KINDS)}


def make_run(service, *, capabilities=None, web_search_enabled=True, assistant_extra=None):
    user = service.create_user({"username": "owner", "display_name": "Owner", "role": "admin", "password": PASSWORD})
    row = service.db.one("SELECT * FROM users WHERE id=?", (user["id"],))
    data = {"name": "Synthetic", "instructions": "Test", "web_search_enabled": web_search_enabled}
    if capabilities:
        data["capabilities"] = capabilities
    if assistant_extra:
        data.update(assistant_extra)
    assistant = service.save_assistant(data, user=row)
    conversation_id, run_id = f"conv-{new_call_id()}", f"run-{new_call_id()}"
    service.db.execute(
        "INSERT INTO conversations(id,user_id,assistant_id,title,created_at,updated_at,active_run_id) "
        "VALUES(?,?,?,?,?,?,?)", (conversation_id, user["id"], assistant["id"], "T", "2026-01-01", "2026-01-01", run_id))
    snapshot = assistant_view(service.db.one("SELECT * FROM assistants WHERE id=?", (assistant["id"],)), row)
    service.db.execute(
        "INSERT INTO runs(id,conversation_id,user_id,client_request_id,message_id,status,created_at,capability_snapshot) "
        "VALUES(?,?,?,?,?,?,?,?)", (run_id, conversation_id, user["id"], new_call_id(), new_call_id(), "running",
                                     "2026-01-01", dump(snapshot)))
    service.evidence = Mock()
    service.evidence.register.return_value = {"id": "source-test"}
    return service, row, assistant, run_id


def context_for(service, user, assistant, run_id, tool_call_id=None):
    run = service.require("runs", run_id)
    return RunContext(run_id=run_id, conversation_id=run["conversation_id"], user_id=user["id"],
                      assistant_id=assistant["id"], tool_call_id=tool_call_id or new_call_id(),
                      recheck=lambda: service.authorize_tool(run["conversation_id"], "mcp__workspace__web_search", {}))


# --- configuration surface -------------------------------------------------

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


def test_defaults_preserve_environment_and_disable_new_capabilities(client, settings):
    value = client.get("/api/v1/capability-settings").json()
    assert value["revision"] == 0 and value["origin"] == "environment" and value["schema_version"] == 2
    assert value["search"]["preferred"] == "bing_rss"
    assert value["fetch"]["preferred"] == "public_http"
    assert value["ocr"]["enabled"] is False and value["ocr"]["egress"]["upload"] == "deny"
    assert value["image_search"]["enabled"] is False and value["image_search"]["egress"]["upload"] == "deny"
    assert all(p["verification"] == "not_tested" for p in value["providers"])
    assert all("modes" in p for p in value["providers"])
    settings.web_search_provider = "searxng"
    value = client.get("/api/v1/capability-settings").json()
    assert value["search"]["preferred"] == "searxng" and value["search"]["fallbacks"] == ["bing_rss"]
    settings.web_search_provider = "deepseek_api"
    settings.web_search_fallback_bing = False
    value = client.get("/api/v1/capability-settings").json()
    assert value["fetch"]["preferred"] == "deepseek_api" and value["fetch"]["fallbacks"] == []


def test_v1_document_migrates_ocr_and_image_off(client):
    service = client.app.state.service
    v1 = {"search": {"enabled": True, "preferred": "bing_rss", "fallbacks": [], "timeout_seconds": 30},
          "fetch": {"enabled": True, "preferred": "public_http", "fallbacks": [], "timeout_seconds": 30}}
    service.db.execute("INSERT INTO capability_settings(id,revision,config,updated_at,updated_by) VALUES(1,7,?,?,?)",
                       (dump(v1), now(), "admin"))
    value = client.get("/api/v1/capability-settings").json()
    assert value["revision"] == 7
    assert value["search"]["timeout_seconds"] == 30 and value["search"]["max_attempts"] == 1
    assert value["ocr"]["enabled"] is False and value["image_search"]["enabled"] is False


def test_ocr_enable_requires_configured_provider(client, settings):
    data = payload(client)
    data["ocr"]["enabled"] = True
    data["ocr"]["egress"]["upload"] = "allow"
    assert client.put("/api/v1/capability-settings", json=data).status_code == 422
    settings.deepseek_api_enabled = True
    settings.deepseek_api_key = "synthetic-key-not-for-display"
    response = client.put("/api/v1/capability-settings", json=data)
    assert response.status_code == 200, response.text
    assert response.json()["ocr"]["enabled"] is True
    assert "synthetic-key-not-for-display" not in response.text


def test_save_reload_conflict_and_secret_free_audit(client, settings):
    settings.deepseek_api_enabled = True
    settings.deepseek_api_key = "synthetic-key-not-for-display"
    settings.web_search_searxng_url = "https://search.example/private-endpoint?q=not-for-display"
    data = payload(client)
    data["search"].update(preferred="deepseek_api", fallbacks=["bing_rss"], timeout_seconds=12, max_attempts=2)
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
    {"domains": {"mode": "allowlist", "hosts": ["*.example.com"]}},
    {"egress": {"query": "sometimes"}}, {"limits": {"calls_per_run": True}},
])
def test_invalid_unconfigured_or_secret_inputs_rejected(client, change):
    data = payload(client)
    data["search"].update(change)
    response = client.put("/api/v1/capability-settings", json=data)
    assert response.status_code == 422
    assert client.app.state.service.db.one("SELECT COUNT(*) AS n FROM capability_settings")["n"] == 0
    assert "never-store" not in response.text and "credential.example" not in response.text


def test_unimplemented_image_provider_is_not_selectable(client):
    data = payload(client)
    data["image_search"]["preferred"] = "bing_rss"
    assert client.put("/api/v1/capability-settings", json=data).status_code == 422


def test_stale_admin_object_cannot_write_after_demotion(client):
    service = client.app.state.service
    user = service.db.one("SELECT * FROM users WHERE username='admin'")
    data = CapabilityUpdate(**payload(client))
    service.db.execute("UPDATE users SET role='member' WHERE id=?", (user["id"],))
    with pytest.raises(AppError) as result:
        CapabilitySettings(service).save(user, data)
    assert result.value.status == 403
    assert service.db.one("SELECT COUNT(*) AS n FROM capability_settings")["n"] == 0


# --- execution -------------------------------------------------------------

def test_search_route_is_applied_and_failure_falls_back(tmp_path, monkeypatch):
    service = Service(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",),
                               web_search_provider="bing_rss", web_search_fallback_bing=True,
                               web_search_searxng_url="https://search.example/search"))
    service, user, assistant, run_id = make_run(service)
    user_row = service.db.one("SELECT * FROM users WHERE username='owner'")
    data = {key: CapabilitySettings(service).effective()[key] for key in ("revision", *CAPABILITY_KINDS)}
    data["search"].update(preferred="searxng", fallbacks=["bing_rss"], max_attempts=2)
    CapabilitySettings(service).save(service.db.one("SELECT * FROM users WHERE username='admin'") if service.db.one("SELECT * FROM users WHERE username='admin'") else user_row, CapabilityUpdate(**data))
    tools = WebTools(service)
    searx = AsyncMock(return_value=[{"url": "https://example.com", "title": "Public", "excerpt": "Evidence"}])
    bing = AsyncMock(return_value=[])
    monkeypatch.setattr(tools, "_searxng", searx)
    monkeypatch.setattr(tools, "_bing", bing)
    result = asyncio.run(tools.search_for_run(user, assistant, run_id, "public query"))
    assert result["provider"] == "searxng" and not result["fallback"]
    bing.assert_not_awaited()
    searx.side_effect = AppError(503, "UNAVAILABLE", "Unavailable")
    result = asyncio.run(tools.search_for_run(user, assistant, run_id, "public query"))
    assert result["provider"] == "bing_rss" and result["fallback"]
    assert bing.await_count == 1


@pytest.mark.parametrize("status", [403, 422])
def test_permission_and_safety_errors_do_not_fallback(tmp_path, monkeypatch, status):
    service = Service(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",),
                               web_search_provider="bing_rss", web_search_fallback_bing=True,
                               web_search_searxng_url="https://search.example/search"))
    service, user, assistant, run_id = make_run(service)
    admin = service.db.one("SELECT * FROM users WHERE username='owner'")
    data = {key: CapabilitySettings(service).effective()[key] for key in ("revision", *CAPABILITY_KINDS)}
    data["search"].update(preferred="searxng", fallbacks=["bing_rss"], max_attempts=2)
    CapabilitySettings(service).save(admin, CapabilityUpdate(**data))
    tools = WebTools(service)
    monkeypatch.setattr(tools, "_searxng", AsyncMock(side_effect=AppError(status, "DENIED", "Denied")))
    fallback = AsyncMock()
    monkeypatch.setattr(tools, "_bing", fallback)
    with pytest.raises(AppError) as result:
        asyncio.run(tools.search_for_run(user, assistant, run_id, "public query"))
    assert result.value.status == status
    fallback.assert_not_awaited()


def test_domain_allowlist_filters_results_not_upstream(tmp_path, monkeypatch):
    service = Service(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",),
                               web_search_provider="bing_rss", web_search_fallback_bing=False))
    service, user, assistant, run_id = make_run(service)
    admin = service.db.one("SELECT * FROM users WHERE username='owner'")
    data = {key: CapabilitySettings(service).effective()[key] for key in ("revision", *CAPABILITY_KINDS)}
    data["search"]["domains"] = {"mode": "allowlist", "hosts": ["example.com"]}
    CapabilitySettings(service).save(admin, CapabilityUpdate(**data))
    tools = WebTools(service)
    monkeypatch.setattr(tools, "_bing", AsyncMock(return_value=[
        {"url": "https://example.com/ok", "title": "A", "excerpt": "a"},
        {"url": "https://evil.example.org/no", "title": "B", "excerpt": "b"},
    ]))
    result = asyncio.run(tools.search_for_run(user, assistant, run_id, "public query"))
    assert [item["url"] for item in result["results"]] == ["https://example.com/ok"]


def test_timeout_and_cancellation_do_not_create_sources(tmp_path, monkeypatch):
    service = Service(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",),
                               web_search_provider="bing_rss", web_search_fallback_bing=False))
    service, user, assistant, run_id = make_run(service)
    admin = service.db.one("SELECT * FROM users WHERE username='owner'")
    data = {key: CapabilitySettings(service).effective()[key] for key in ("revision", *CAPABILITY_KINDS)}
    data["search"]["timeout_seconds"] = 1
    CapabilitySettings(service).save(admin, CapabilityUpdate(**data))
    tools = WebTools(service)
    async def slow(*args):
        await asyncio.sleep(10)
    monkeypatch.setattr(tools, "_bing", slow)
    with pytest.raises(AppError) as result:
        asyncio.run(tools.search_for_run(user, assistant, run_id, "query"))
    assert result.value.status == 504
    service.evidence.register.assert_not_called()


def test_each_actual_attempt_is_counted_and_retry_budget_is_not_multiplied(tmp_path):
    service = Service(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",),
                               web_search_provider="bing_rss", web_search_fallback_bing=True,
                               web_search_searxng_url="https://search.example/search"))
    service, user, assistant, run_id = make_run(service)
    admin = service.db.one("SELECT * FROM users WHERE username='owner'")
    data = {key: CapabilitySettings(service).effective()[key] for key in ("revision", *CAPABILITY_KINDS)}
    data["search"].update(preferred="searxng", fallbacks=["bing_rss"], max_attempts=2)
    CapabilitySettings(service).save(admin, CapabilityUpdate(**data))
    context = RunContext(run_id=run_id, conversation_id=service.require("runs", run_id)["conversation_id"],
                         user_id=user["id"], assistant_id=assistant["id"], tool_call_id="call-budget",
                         recheck=lambda: None)
    calls = []
    async def operation(provider, deadline, recheck):
        calls.append(provider)
        raise AppError(503, "UNAVAILABLE", "offline")
    with pytest.raises(AppError):
        asyncio.run(CapabilityExecutor(service).execute_for_run(context, "search", {"query": "q"}, operation))
    assert calls == ["searxng", "bing_rss"]  # two attempts, not two providers x two retries
    rows = service.db.all("SELECT * FROM capability_attempts WHERE call_id='call-budget' ORDER BY attempt_index")
    assert [row["attempt_index"] for row in rows] == [0, 1]
    assert all(row["state"] == "failed" for row in rows)


def test_quota_last_unit_has_exactly_one_winner(tmp_path):
    service = Service(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",)))
    usage = CapabilityUsage(service)
    def reserve(_):
        try:
            usage.reserve_attempt(run_id="r", assistant_id="a", user_id="u", capability="search",
                                  call_id=new_call_id(), attempt_index=0, provider="bing_rss",
                                  settings_revision=1, calls_per_run=1, calls_per_day=10)
            return "ok"
        except AppError as exc:
            return str(exc.status)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, range(2))) == ["429", "ok"]


def test_ocr_has_no_retry_and_no_fallback(tmp_path):
    service = Service(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",),
                               deepseek_api_enabled=True, deepseek_api_key="synthetic"))
    # Global OCR must be enabled with upload egress allowed before an assistant
    # grant can narrow to the same value.
    created = service.create_user({"username": "rootadmin", "display_name": "R", "role": "admin", "password": PASSWORD})
    root_admin = service.db.one("SELECT * FROM users WHERE id=?", (created["id"],))
    data = {key: CapabilitySettings(service).effective()[key] for key in ("revision", *CAPABILITY_KINDS)}
    data["ocr"].update(enabled=True, preferred="deepseek_api", fallbacks=[], max_attempts=3)
    data["ocr"]["egress"]["upload"] = "allow"
    CapabilitySettings(service).save(root_admin, CapabilityUpdate(**data))
    service, user, assistant, run_id = make_run(service, capabilities={"ocr": {"enabled": True, "egress": {"upload": "allow"}}})
    calls = []
    async def operation(provider, deadline, recheck):
        calls.append(provider)
        raise AppError(503, "UNAVAILABLE", "offline")
    context = RunContext(run_id=run_id, conversation_id=service.require("runs", run_id)["conversation_id"],
                         user_id=user["id"], assistant_id=assistant["id"], tool_call_id="call-ocr",
                         recheck=lambda: None)
    with pytest.raises(AppError):
        asyncio.run(CapabilityExecutor(service).execute_for_run(
            context, "ocr", {"file_id": "f", "purpose": "text_recognition"}, operation, external_upload=True))
    assert calls == ["deepseek_api"]


def test_runtime_authorization_checks_saved_global_policy(tmp_path):
    service = Service(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",)))
    service, user, assistant, run_id = make_run(service)
    conversation_id = service.require("runs", run_id)["conversation_id"]
    assert service.authorize_tool(conversation_id, "mcp__workspace__web_search", {}) is True
    data = {key: CapabilitySettings(service).effective()[key] for key in ("revision", *CAPABILITY_KINDS)}
    data["search"]["enabled"] = False
    CapabilitySettings(service).save(service.db.one("SELECT * FROM users WHERE username='owner'"), CapabilityUpdate(**data))
    with pytest.raises(AppError) as result:
        service.authorize_tool(conversation_id, "mcp__workspace__web_search", {})
    assert result.value.code == "CAPABILITY_DISABLED"
    assert service.authorize_tool(conversation_id, "mcp__workspace__web_read", {}) is True


def test_unknown_tool_is_not_auto_authorized(tmp_path):
    service = Service(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",)))
    service, user, assistant, run_id = make_run(service)
    conversation_id = service.require("runs", run_id)["conversation_id"]
    with pytest.raises(AppError) as result:
        service.authorize_tool(conversation_id, "mcp__workspace__unknown_external", {})
    assert result.value.code == "TOOL_DENIED"


def test_legacy_boolean_cannot_overwrite_v2_grant(tmp_path):
    service = Service(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",)))
    service, user, assistant, run_id = make_run(service, capabilities={"search": {"enabled": False}})
    with pytest.raises(AppError) as result:
        service.save_assistant({"web_search_enabled": True}, item_id=assistant["id"], user=user)
    assert result.value.code == "CAPABILITY_GRANT_CONFLICT"


# --- source authorization and credentials ----------------------------------

def test_source_access_honours_current_capability(tmp_path):
    service = Service(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",)))
    service, user, assistant, run_id = make_run(service)
    source = {"run_id": run_id, "kind": "web", "original_url": "https://example.com/a",
              "locator": {"capability": "search", "content_type": "search_excerpt"}, "tool_name": "mcp__workspace__web_search",
              "resource_type": "web", "resource_id": assistant["id"]}
    authorize_source_access(service, source, user)  # allowed while enabled
    data = {key: CapabilitySettings(service).effective()[key] for key in ("revision", *CAPABILITY_KINDS)}
    data["search"]["enabled"] = False
    CapabilitySettings(service).save(service.db.one("SELECT * FROM users WHERE username='owner'"), CapabilityUpdate(**data))
    with pytest.raises(AppError) as result:
        authorize_source_access(service, source, user)
    assert result.value.status == 404


def test_credential_is_write_only_revisioned_and_tombstoned(tmp_path):
    service = Service(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",),
                               deepseek_api_key="environment-secret"))
    admin = service.db.one("SELECT * FROM users WHERE username='admin'") if service.db.one("SELECT * FROM users WHERE username='admin'") else None
    if admin is None:
        created = service.create_user({"username": "admin2", "display_name": "A", "role": "admin", "password": PASSWORD})
        admin = service.db.one("SELECT * FROM users WHERE id=?", (created["id"],))
    credentials = CapabilityCredentials(service)
    assert credentials.get("deepseek_api") == "environment-secret"
    public = credentials.put(admin, "deepseek_api", credentials.revision("deepseek_api"), "database-secret")
    assert public == {"provider_id": "deepseek_api", "present": True, "revision": 2, "verified": False}
    assert credentials.get("deepseek_api") == "database-secret"
    assert "database-secret" not in json.dumps(public)
    with pytest.raises(AppError) as conflict:
        credentials.put(admin, "deepseek_api", 0, "stale")
    assert conflict.value.status == 409
    deleted = credentials.delete(admin, "deepseek_api", credentials.revision("deepseek_api"))
    assert deleted["present"] is False
    # The tombstone is authoritative; the environment key is not reactivated.
    assert credentials.get("deepseek_api") is None
    assert credentials.present("deepseek_api") is False


def test_secure_transport_requires_https_or_explicit_local_mode():
    class Url:
        scheme = "http"
        hostname = "example.com"
    class Request:
        url = Url()
        headers = {"x-forwarded-proto": "https"}
        client = type("C", (), {"host": "10.0.0.5"})()
    # A remote HTTP request cannot prove transport by forging a header or Host.
    assert secure_transport(Request()) is False
    assert secure_transport(Request(), trusted_proxy=True) is True
    Request.headers = {}
    Request.client = type("C", (), {"host": "127.0.0.1"})()
    Request.url = type("U", (), {"scheme": "http", "hostname": "127.0.0.1"})()
    assert secure_transport(Request()) is False
    assert secure_transport(Request(), local_dev=True) is True


def test_approval_receipt_is_bound_to_exact_call_and_arguments(tmp_path):
    from app.capability_receipts import (
        claim_approval_receipt, issue_approval_receipt, verify_approval_receipt,
    )
    service = Service(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",)))
    tool = "mcp__workspace__web_search"
    args = {"query": "public", "limit": 5}
    receipt = issue_approval_receipt(
        service, run_id="r", tool_call_id="call-A", tool_name=tool, arguments=args,
        decision="allowed", source="user")
    assert claim_approval_receipt(service, run_id="r", tool_call_id="call-A", tool_name=tool,
                                  arguments=args, call_scope_id="call-A") == receipt
    # Same logical call can re-verify its own receipt without consuming again.
    verify_approval_receipt(service, run_id="r", tool_call_id="call-A", tool_name=tool,
                            arguments=args, call_scope_id="call-A")
    # A different call with identical arguments has no receipt and cannot borrow it.
    with pytest.raises(AppError) as missing:
        claim_approval_receipt(service, run_id="r", tool_call_id="call-B", tool_name=tool,
                               arguments=args, call_scope_id="call-B")
    assert missing.value.code == "APPROVAL_REQUIRED"
    # Mutated arguments do not match the stored digest.
    with pytest.raises(AppError) as mutated:
        verify_approval_receipt(service, run_id="r", tool_call_id="call-A", tool_name=tool,
                                arguments={**args, "limit": 9}, call_scope_id="call-A")
    assert mutated.value.code == "APPROVAL_REQUIRED"
    # A consumed receipt cannot be reissued for another invocation.
    with pytest.raises(AppError) as replay:
        issue_approval_receipt(service, run_id="r", tool_call_id="call-A", tool_name=tool,
                               arguments=args, decision="allowed", source="user")
    assert replay.value.code == "APPROVAL_REPLAY"


def test_external_egress_requires_human_receipt(tmp_path):
    from app.capability_receipts import claim_approval_receipt, issue_approval_receipt
    service = Service(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",)))
    tool = "mcp__workspace__ocr"
    args = {"file_id": "f1", "purpose": "text_recognition"}
    issue_approval_receipt(service, run_id="r", tool_call_id="c1", tool_name=tool, arguments=args,
                           decision="allowed", source="model")
    with pytest.raises(AppError) as error:
        claim_approval_receipt(service, run_id="r", tool_call_id="c1", tool_name=tool,
                               arguments=args, call_scope_id="c1", require_human=True)
    assert error.value.code == "APPROVAL_HUMAN_REQUIRED"


def test_executor_rejects_false_without_receipt_and_accepts_exact_receipt(tmp_path):
    from app.capability_receipts import issue_approval_receipt
    service = Service(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",)))
    service, user, assistant, run_id = make_run(service)
    admin = service.db.one("SELECT * FROM users WHERE username='owner'")
    data = {key: CapabilitySettings(service).effective()[key] for key in ("revision", *CAPABILITY_KINDS)}
    data["search"]["egress"]["query"] = "approval"
    CapabilitySettings(service).save(admin, CapabilityUpdate(**data))
    conversation_id = service.require("runs", run_id)["conversation_id"]
    arguments = {"query": "public", "limit": 5}
    context = RunContext(run_id=run_id, conversation_id=conversation_id, user_id=user["id"],
                         assistant_id=assistant["id"], tool_call_id="call-needs-approval",
                         recheck=lambda: None)
    async def operation(provider, deadline, recheck):
        recheck()
        return [{"url": "https://example.com", "title": "A", "excerpt": "a"}]
    with pytest.raises(AppError) as error:
        asyncio.run(CapabilityExecutor(service).execute_for_run(context, "search", arguments, operation))
    assert error.value.code == "APPROVAL_REQUIRED"
    issue_approval_receipt(service, run_id=run_id, tool_call_id="call-needs-approval",
                           tool_name="mcp__workspace__web_search", arguments=arguments,
                           decision="allowed", source="user")
    result = asyncio.run(CapabilityExecutor(service).execute_for_run(context, "search", arguments, operation))
    assert result.provider == "bing_rss"


def test_knowledge_ocr_egress_gate_defaults_closed(tmp_path):
    service = Service(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",),
                               deepseek_api_enabled=True, deepseek_api_key="synthetic"))
    assert CapabilitySettings(service).knowledge_ocr_egress_allowed() is False


def test_error_classification_never_retries_permission():
    assert classify_error(AppError(403, "DENIED", "x")) == "permission"
    assert classify_error(AppError(503, "UNAVAILABLE", "x")) == "transient"
    assert classify_error(AppError(422, "INVALID", "x")) == "input"
    assert classify_error(AppError(409, "CHANGED", "x")) == "input"
