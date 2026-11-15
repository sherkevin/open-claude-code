"""Reproductions for consent replay, identity, revocation and redirect defects."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from app.capability_credentials import CapabilityCredentials
from app.capability_execution import CapabilityExecutor
from app.capability_receipts import issue_approval_receipt
from app.capability_settings import CAPABILITY_KINDS, CapabilitySettings, CapabilityUpdate
from app.config import Settings
from app.db import dump, uid
from app.security import AppError
from app.service import Service
from app.web_tools import WebTools
from test_capability_settings import context_for, make_run


@pytest.fixture
def system(tmp_path):
    service = Service(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",)))
    try:
        yield make_run(service)
    finally:
        service.db.close()


def configure(service, user, capability, **changes):
    api = CapabilitySettings(service)
    data = {key: api.effective()[key] for key in ("revision", *CAPABILITY_KINDS)}
    data[capability].update(changes)
    return api.save(user, CapabilityUpdate(**data))


@pytest.mark.parametrize("human", [False, True])
@pytest.mark.parametrize("concurrent", [False, True])
@pytest.mark.parametrize("shared_executor", [False, True])
def test_one_host_call_dispatches_once(system, human, concurrent, shared_executor):
    service, user, assistant, run_id = system
    arguments = {"query": "public", "limit": 5}
    if human:
        configure(service, user, "search", egress={"query": "approval", "url": "allow", "upload": "deny"})
        issue_approval_receipt(service, run_id=run_id, tool_call_id="call-one",
                               tool_name="mcp__workspace__web_search", arguments=arguments,
                               decision="allowed", source="user")
    context = context_for(service, user, assistant, run_id, "call-one")
    executor = CapabilityExecutor(service)
    dispatched = []

    async def operation(provider, deadline, recheck):
        dispatched.append(provider)
        recheck()
        await asyncio.sleep(0.02)
        recheck()
        return []

    async def invoke():
        selected = executor if shared_executor else CapabilityExecutor(service)
        try:
            await selected.execute_for_run(context, "search", arguments, operation)
            return "accepted"
        except AppError as exc:
            return exc.code

    async def run():
        if concurrent:
            return await asyncio.gather(invoke(), invoke())
        return [await invoke(), await invoke()]

    outcomes = asyncio.run(run())
    assert outcomes.count("accepted") == 1
    assert set(outcomes) <= {"accepted", "APPROVAL_REPLAY", "CAPABILITY_REPLAY"}
    assert dispatched == ["bing_rss"]
    assert service.db.one("SELECT COUNT(*) AS n FROM capability_attempts")["n"] == 1


@pytest.mark.parametrize("field", ["assistant", "run_user", "snapshot"])
def test_inconsistent_identity_is_denied_before_dispatch(system, field):
    service, user, assistant, run_id = system
    context = context_for(service, user, assistant, run_id)
    if field == "assistant":
        context = replace(context, assistant_id="unrelated-assistant")
    elif field == "run_user":
        other = service.create_user({"username": "other", "display_name": "Other", "role": "member",
                                     "password": "Synthetic-pass-for-test-123"})
        service.db.execute("UPDATE runs SET user_id=? WHERE id=?", (other["id"], run_id))
    else:
        service.db.execute("UPDATE runs SET capability_snapshot=? WHERE id=?",
                           (dump({"id": "unrelated-assistant"}), run_id))
    dispatched = []

    async def operation(*_):
        dispatched.append(True)

    with pytest.raises(AppError) as error:
        asyncio.run(CapabilityExecutor(service).execute_for_run(context, "search", {"query": "q"}, operation))
    assert error.value.code == "CAPABILITY_CONTEXT_DENIED"
    assert dispatched == []
    assert service.db.one("SELECT COUNT(*) AS n FROM capability_attempts")["n"] == 0


@pytest.mark.parametrize("delete", [False, True])
@pytest.mark.parametrize("inside_recheck", [False, True])
def test_credential_change_rejects_inflight_results(system, delete, inside_recheck):
    service, user, assistant, run_id = system
    credentials = CapabilityCredentials(service)
    first = credentials.put(user, "brave", 0, "synthetic-before")
    configure(service, user, "search", preferred="brave", fallbacks=[], max_attempts=1)
    context = context_for(service, user, assistant, run_id)

    async def operation(provider, deadline, recheck):
        if delete:
            credentials.delete(user, provider, first["revision"])
        else:
            credentials.put(user, provider, first["revision"], "synthetic-after")
        if inside_recheck:
            recheck()
        return [{"title": "stale result"}]

    with pytest.raises(AppError) as error:
        asyncio.run(CapabilityExecutor(service).execute_for_run(context, "search", {"query": "q"}, operation))
    assert error.value.code == "CAPABILITY_CREDENTIAL_CHANGED"
    attempt = service.db.one("SELECT * FROM capability_attempts")
    assert attempt["state"] == ("failed" if inside_recheck else "discarded")
    assert attempt["provider_revision"] == first["revision"]
    service.evidence.register.assert_not_called()


def test_same_host_id_in_distinct_runs_does_not_collide(system):
    service, user, assistant, run_id = system
    original = service.require("runs", run_id)
    conversation2, run2 = uid(), uid()
    service.db.execute(
        "INSERT INTO conversations(id,user_id,assistant_id,title,created_at,updated_at,active_run_id) VALUES(?,?,?,?,?,?,?)",
        (conversation2, user["id"], assistant["id"], "second", "2026-01-01", "2026-01-01", run2))
    service.db.execute(
        "INSERT INTO runs(id,conversation_id,user_id,client_request_id,message_id,status,created_at,capability_snapshot) VALUES(?,?,?,?,?,?,?,?)",
        (run2, conversation2, user["id"], uid(), uid(), "running", "2026-01-01", original["capability_snapshot"]))

    async def operation(provider, deadline, recheck):
        recheck()
        return []

    for current_run in [run_id, run2]:
        context = context_for(service, user, assistant, current_run, "same-host-id")
        asyncio.run(CapabilityExecutor(service).execute_for_run(context, "search", {"query": "q"}, operation))
    assert service.db.one("SELECT COUNT(*) AS n FROM capability_dispatch_claims")["n"] == 2
    assert service.db.one("SELECT COUNT(DISTINCT call_id) AS n FROM capability_attempts")["n"] == 2


@pytest.mark.parametrize("return_to_allowed", [False, True])
def test_redirect_denied_before_any_disallowed_request(system, monkeypatch, return_to_allowed):
    service, user, assistant, run_id = system
    configure(service, user, "fetch", domains={"mode": "allowlist", "hosts": ["allowed.example"]})
    requests = []

    def handler(request):
        requests.append(request.url.host)
        if request.url.host == "allowed.example":
            return httpx.Response(302, headers={"location": "https://denied.example/hop"})
        if return_to_allowed:
            return httpx.Response(302, headers={"location": "https://allowed.example/final"})
        return httpx.Response(200, text="should never be read")

    def transport(**kwargs):
        mocked = httpx.MockTransport(handler)
        mocked._pool = SimpleNamespace(_network_backend=None)
        return mocked

    monkeypatch.setattr("app.web_tools.httpx.AsyncHTTPTransport", transport)
    with pytest.raises(AppError) as error:
        asyncio.run(WebTools(service).read_for_run(user, assistant, run_id, "https://allowed.example/start"))
    assert error.value.code == "CAPABILITY_DOMAIN_DENIED"
    assert requests == ["allowed.example"]
    service.evidence.register.assert_not_called()


def test_final_provider_url_is_checked_even_when_transport_is_replaced(system, monkeypatch):
    service, user, assistant, run_id = system
    configure(service, user, "fetch", domains={"mode": "allowlist", "hosts": ["allowed.example"]})

    async def replaced_transport(*args, **kwargs):
        return "https://denied.example/final", "text/plain", b"untrusted result"

    monkeypatch.setattr("app.web_tools.fetch_public", replaced_transport)
    with pytest.raises(AppError) as error:
        asyncio.run(WebTools(service).read_for_run(user, assistant, run_id, "https://allowed.example/start"))
    assert error.value.code == "CAPABILITY_DOMAIN_DENIED"
    service.evidence.register.assert_not_called()


@pytest.mark.parametrize("result,passes", [
    ({"pages": [{"text": "A1"}], "empty": False, "partial": False}, True),
    ({"pages": [{"text": ""}], "empty": True, "partial": False}, False),
    ({"pages": [{"text": "A1"}], "empty": False, "partial": True}, False),
])
def test_diagnostics_check_ocr_content_not_just_http_success(system, monkeypatch, result, passes):
    import time
    from unittest.mock import AsyncMock
    from app.capability_diagnostics import CapabilityDiagnostics
    service = system[0]
    diagnostics = CapabilityDiagnostics(service)
    client = SimpleNamespace(ocr_bytes=AsyncMock(return_value=result))
    monkeypatch.setattr(diagnostics, "_deepseek_client", lambda: client)
    if passes:
        assert asyncio.run(diagnostics._probe("deepseek_api", "ocr", time.monotonic() + 10)) == "verified"
    else:
        with pytest.raises(AppError) as error:
            asyncio.run(diagnostics._probe("deepseek_api", "ocr", time.monotonic() + 10))
        assert error.value.code == "CAPABILITY_DIAGNOSTIC_QUALITY_FAILED"
    assert client.ocr_bytes.await_count == 1
    assert client.ocr_bytes.call_args.kwargs["mime_type"] == "image/png"


def test_diagnostics_do_not_continue_after_credential_revocation(system, monkeypatch):
    from app.capability_diagnostics import CapabilityDiagnostics
    service, user, _, _ = system
    service.settings.deepseek_api_enabled = True
    credentials = CapabilityCredentials(service)
    credentials.put(user, "deepseek_api", 0, "synthetic-key")
    diagnostics = CapabilityDiagnostics(service)
    called = []

    async def probe(provider, capability, deadline):
        called.append(capability)
        credentials.delete(user, provider, credentials.revision(provider))
        return "verified"

    monkeypatch.setattr(diagnostics, "_probe", probe)
    result = asyncio.run(diagnostics.run("deepseek_api"))
    assert called == ["search"]
    assert all(item["status"] == "failed" and item["error_code"] == "CAPABILITY_CHANGED"
               for item in result["results"])
    assert service.db.one("SELECT COUNT(*) AS n FROM capability_provider_verification")["n"] == 0


def test_diagnostic_cancellation_stops_the_batch(system, monkeypatch):
    from app.capability_diagnostics import CapabilityDiagnostics
    service, user, _, _ = system
    service.settings.deepseek_api_enabled = True
    CapabilityCredentials(service).put(user, "deepseek_api", 0, "synthetic-key")
    diagnostics = CapabilityDiagnostics(service)
    called = []

    async def probe(provider, capability, deadline):
        called.append(capability)
        raise asyncio.CancelledError()

    monkeypatch.setattr(diagnostics, "_probe", probe)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(diagnostics.run("deepseek_api"))
    assert called == ["search"]
    assert service.db.one("SELECT * FROM capability_provider_verification")["status"] == "failed"


def test_verification_is_invalidated_by_policy_revision(system):
    from app.capability_diagnostics import CapabilityDiagnostics
    service, user, _, _ = system
    diagnostics = CapabilityDiagnostics(service)
    assert diagnostics._record("bing_rss", "search", "verified", latency_ms=1, sample_id="synthetic",
                               error_code=None, provider_revision=0, settings_revision=0)
    assert CapabilitySettings(service)._verification()["bing_rss"]["search"] == "verified"
    configure(service, user, "search", timeout_seconds=30)
    assert "bing_rss" not in CapabilitySettings(service)._verification()


def test_host_ticket_is_redacted_from_progress_arguments():
    from app.security import redact
    assert redact({"__oc_capability_ticket": "private-host-nonce", "query": "public"}) == {
        "__oc_capability_ticket": "[redacted]", "query": "public"}


def test_explicit_image_modes_route_to_their_own_authorized_provider(system):
    from app.service import assistant_view
    service, user, assistant, run_id = system
    service.settings.deepseek_api_enabled = True
    credentials = CapabilityCredentials(service)
    credentials.put(user, "deepseek_webtools", 0, "synthetic-reverse")
    credentials.put(user, "brave_images", 0, "synthetic-text")
    configure(service, user, "image_search", enabled=True, preferred="deepseek_webtools",
              mode_providers={"text_search": "brave_images", "reverse_image": "deepseek_webtools"},
              egress={"query": "allow", "url": "allow", "upload": "allow"})
    service.save_assistant({"capabilities": {"image_search": {"enabled": True, "auto_approved": True}}},
                           assistant["id"], user=user)
    snapshot = assistant_view(service.require("assistants", assistant["id"]), user)
    service.db.execute("UPDATE runs SET capability_snapshot=? WHERE id=?", (dump(snapshot), run_id))
    called = []

    async def operation(provider, deadline, recheck):
        called.append(provider)
        recheck()
        return provider

    for mode in ["text_search", "reverse_image"]:
        arguments = {"mode": mode, **({"query": "public"} if mode == "text_search" else {"file_id": "fixture"})}
        context = context_for(service, user, assistant, run_id, mode)
        context = replace(context, recheck=lambda: service.authorize_tool(
            context.conversation_id, "mcp__workspace__image_search", arguments))
        result = asyncio.run(CapabilityExecutor(service).execute_for_run(
            context, "image_search", arguments, operation))
        assert result.attempts == 1
    assert called == ["brave_images", "deepseek_webtools"]
    from app.capability_policy import authorize_source_access
    configure(service, user, "image_search", domains={"mode": "allowlist", "hosts": ["example.com"]})
    with pytest.raises(AppError) as error:
        authorize_source_access(service, {"run_id": run_id, "kind": "web", "original_url": "https://example.com/page",
            "locator": {"capability": "image_search", "image_url": "https://denied.example/image.jpg"}}, user)
    assert error.value.code == "SOURCE_UNAVAILABLE"


def test_incompatible_image_mode_provider_cannot_be_saved(system):
    service, user, _, _ = system
    with pytest.raises(AppError) as error:
        configure(service, user, "image_search", mode_providers={"text_search": "deepseek_webtools"})
    assert error.value.code == "CAPABILITY_PROVIDER_INVALID"
