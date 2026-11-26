"""End-to-end product API tests for the opt-in Mem0 semantic index.

These run the real FastAPI app, real native memory snapshots, real SQLite
authority and the product router. The SDK is replaced by a synthetic injected
backend (the same contract as the adapter probe); no network, provider, model
or customer data is used. The real pinned Mem0 SDK is still covered separately
by the existing probe/evidence, not here.

The tests pin the product invariants:
* disabled by default and fail-closed when no backend is injected;
* owner-only sync and verified search with authoritative source coordinates;
* read-only lookup never creates a scope or writes the index;
* provider failure falls back to native memory instead of "empty memory";
* authorized-run binding and revocation stop retrieval;
* logical retirement needs an exact committed clear and does not delete bytes;
* an unconfirmed SDK close refuses to reopen the scope.
"""
from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.mem0_index import TrustedScope
from app.security import AppError

from test_mem0_index_adapter import FakeMem0

PASSWORD = "synthetic-mem0-product-123"
CONTENT = "approved inventory threshold 12"


class ScopedFakeMem0(FakeMem0):
    """Persistent synthetic scope backend with the scoped close contract."""

    def __init__(self):
        super().__init__()
        self.closes = 0
        self.fail_close = False

    def close(self):
        self.closes += 1
        if self.fail_close:
            raise RuntimeError("synthetic unconfirmed SDK close")


def _new_app(tmp_path, monkeypatch, *, enabled):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    settings = Settings(
        data_dir=tmp_path, provider_settings="", model="synthetic",
        runtime_type="harness", mem0_product_enabled=enabled,
        origins=("http://testserver",),
    )
    def forbidden_model_runtime(*args, **kwargs):
        raise AssertionError("product memory test must never start a model runtime")
    return create_app(settings, runtime_factory=forbidden_model_runtime)


def _seed(service):
    owner = service.create_user({"username": "owner", "display_name": "Owner",
                                 "password": PASSWORD, "role": "admin"})
    other = service.create_user({"username": "other", "display_name": "Other",
                                 "password": PASSWORD, "role": "admin"})
    assistant = service.save_assistant(
        {"name": "Mem0 product fixture", "connections": [], "access": {"all_members": True, "user_ids": []}},
        user=owner,
    )
    cid = service.create_conversation(
        {"assistant_id": assistant["id"], "title": "Synthetic Mem0 product"}, owner)["id"]
    return owner, other, assistant, cid


def _login(client, username):
    response = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response


def _url(cid, suffix=""):
    return f"/api/v1/conversations/{cid}/memory/semantic{suffix}"


@pytest.fixture
def product(tmp_path, monkeypatch):
    app = _new_app(tmp_path, monkeypatch, enabled=True)
    with TestClient(app) as client:
        service = app.state.service
        owner, other, assistant, cid = _seed(service)
        backend = ScopedFakeMem0()
        opened = []

        def factory(paths):
            opened.append(paths)
            if not paths.history.exists():
                paths.history.write_text("synthetic history", encoding="utf-8")
            return backend

        service.mem0_product.configure(scoped_factory=factory)
        _login(client, "owner")
        yield {
            "client": client, "service": service, "owner": owner, "other": other,
            "assistant": assistant, "cid": cid, "backend": backend,
            "opened": opened,
        }


def _edit_committed(service, owner, cid, content=CONTENT, operation_id="native-edit"):
    revision = service.memory.snapshot_for_run(owner["id"], cid)["revision"]
    return service.memory.edit(owner, cid, revision, "MEMORY.md", content, operation_id)


def test_disabled_by_default_returns_stable_403_without_side_effects(tmp_path, monkeypatch):
    app = _new_app(tmp_path, monkeypatch, enabled=False)
    with TestClient(app) as client:
        service = app.state.service
        owner, _, _, cid = _seed(service)
        _login(client, "owner")
        for method, suffix, extras in (
            ("post", "/query", {"json": {"q": "threshold"}}),
            ("get", "/status", {}),
            ("post", "/sync", {}),
            ("post", "/retire", {"json": {"operation_id": "clear"}}),
        ):
            response = getattr(client, method)(_url(cid, suffix), **extras)
            assert response.status_code == 403, response.text
            assert response.json()["error"]["code"] == "MEM0_DISABLED"
        assert service.mem0_product.enabled is False
        assert service.mem0_product.host is None


def test_enabled_without_injected_backend_fails_closed(tmp_path, monkeypatch):
    app = _new_app(tmp_path, monkeypatch, enabled=True)
    with TestClient(app) as client:
        service = app.state.service
        owner, _, _, cid = _seed(service)
        _login(client, "owner")
        status = client.get(_url(cid, "/status"))
        assert status.status_code == 200
        assert status.json()["enabled"] is True and status.json()["configured"] is False
        assert status.json()["mode"] is None
        response = client.post(_url(cid, "/query"), json={"q": "threshold"})
        assert response.status_code == 403 and response.json()["error"]["code"] == "MEM0_DISABLED"
        assert client.post(_url(cid, "/sync")).status_code == 403


def test_lookup_is_read_only_and_never_provisions_a_scope(product):
    client, service, owner, cid = product["client"], product["service"], product["owner"], product["cid"]
    before = client.post(_url(cid, "/query"), json={"q": "threshold"})
    assert before.status_code == 200
    assert before.json()["status"] == "fallback" and before.json()["hits"] == []
    assert product["opened"] == []
    assert not service.mem0_product.host.scope_store.root.exists()
    assert product["backend"].calls == []


def test_owner_sync_then_verified_query_with_source_coordinates(product):
    client, service, owner, cid = product["client"], product["service"], product["owner"], product["cid"]
    _edit_committed(service, owner, cid)
    synced = client.post(_url(cid, "/sync"))
    assert synced.status_code == 200, synced.text
    body = synced.json()
    assert body["status"] == "indexed" and body["added"] == 1
    assert body["authoritative_source"] == "native_memory"
    assert len(product["opened"]) == 1
    product["backend"].calls.clear()

    response = client.post(_url(cid, "/query"), json={"q": "inventory threshold"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "verified" and payload["fallback"] is False
    assert [hit["text"] for hit in payload["hits"]] == [CONTENT]
    hit = payload["hits"][0]
    binding = service.memory.snapshot_for_run(owner["id"], cid)
    assert hit["source_path"] == "MEMORY.md"
    assert hit["content_sha256"] == hashlib.sha256(CONTENT.encode()).hexdigest()
    assert hit["revision"] == binding["revision"]
    assert hit["native_epoch"] == binding["native_epoch"]
    assert payload["context_bytes"] == len(CONTENT.encode())
    # A read-only query opens the scope, performs one search and no index write.
    assert [call[0] for call in product["backend"].calls] == ["search"]


def test_query_failure_falls_back_to_native_memory(product):
    client, service, owner, cid = product["client"], product["service"], product["owner"], product["cid"]
    _edit_committed(service, owner, cid)
    assert client.post(_url(cid, "/sync")).status_code == 200
    product["backend"].failures.add("search")
    response = client.post(_url(cid, "/query"), json={"q": "threshold"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "fallback" and payload["hits"] == []
    assert payload["reason"] == "Mem0IndexUnavailable"
    # The authoritative memory is intact and readable through the normal route.
    native = client.get(f"/api/v1/conversations/{cid}/memory/file", params={"path": "MEMORY.md"})
    assert native.status_code == 200 and native.json()["content"] == CONTENT


def test_other_owner_cannot_query_sync_or_retire(product):
    client, cid = product["client"], product["cid"]
    _login(client, "other")
    assert client.post(_url(cid, "/query"), json={"q": "threshold"}).status_code == 404
    assert client.post(_url(cid, "/sync")).status_code == 404
    assert client.post(_url(cid, "/retire"), json={"operation_id": "clear"}).status_code == 404


def test_invalid_query_is_rejected_before_backend_access(product):
    client, cid, backend = product["client"], product["cid"], product["backend"]
    backend.calls.clear()
    response = client.post(_url(cid, "/query"), json={"q": "   "})
    assert response.status_code == 422 and response.json()["error"]["code"] == "MEM0_QUERY_INVALID"
    assert backend.calls == []


def test_authorized_run_scoped_lookup_and_run_change_falls_back(product):
    from test_harness_memory_storage import make_run
    client, service, owner, cid = product["client"], product["service"], product["owner"], product["cid"]
    _edit_committed(service, owner, cid)
    assert client.post(_url(cid, "/sync")).status_code == 200

    rid, _binding = make_run((service, owner, cid))
    allowed = client.post(_url(cid, "/query"), json={"q": "threshold", "run_id": rid})
    assert allowed.status_code == 200 and allowed.json()["status"] == "verified"
    assert allowed.json()["run_id"] == rid

    service.db.execute("UPDATE runs SET status='interrupted' WHERE id=?", (rid,))
    denied = client.post(_url(cid, "/query"), json={"q": "threshold", "run_id": rid})
    assert denied.status_code == 200
    assert denied.json()["status"] == "fallback" and denied.json()["hits"] == []
    assert denied.json()["reason"] == "Mem0IndexSourceChanged"


def test_archived_conversation_stops_lookup_and_sync(product):
    client, service, owner, cid = product["client"], product["service"], product["owner"], product["cid"]
    _edit_committed(service, owner, cid)
    assert client.post(_url(cid, "/sync")).status_code == 200
    service.db.execute("UPDATE conversations SET archived=1 WHERE id=?", (cid,))
    lookup = client.post(_url(cid, "/query"), json={"q": "threshold"})
    assert lookup.status_code == 200 and lookup.json()["status"] == "fallback"
    assert lookup.json()["reason"] == "Mem0IndexScopeInvalid"
    denied = client.post(_url(cid, "/sync"))
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "MEM0_SCOPE_DENIED"


def test_disabled_owner_denies_service_level_lookup_and_sync(product):
    service, owner, cid = product["service"], product["owner"], product["cid"]
    _edit_committed(service, owner, cid)
    assert service.mem0_product.sync(owner, cid)["status"] == "indexed"
    service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (owner["id"],))
    result = service.mem0_product.lookup(owner, cid, "threshold")
    assert result["status"] == "fallback" and result["hits"] == []
    with pytest.raises(AppError) as error:
        service.mem0_product.sync(owner, cid)
    assert error.value.status == 403 and error.value.code == "MEM0_SCOPE_DENIED"


def test_logical_retirement_requires_committed_clear_and_keeps_bytes(product):
    client, service, owner, cid = product["client"], product["service"], product["owner"], product["cid"]
    _edit_committed(service, owner, cid)
    assert client.post(_url(cid, "/sync")).status_code == 200

    scope = TrustedScope.from_binding(service.memory.snapshot_for_run(owner["id"], cid))
    with service.mem0_product.host.scope_store.lease(scope) as paths:
        history = paths.history
        assert history.exists()

    # No committed clear yet: retirement is refused and opens no provider.
    opens = len(product["opened"])
    refused = client.post(_url(cid, "/retire"), json={"operation_id": "not-a-clear"})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "MEM0_SCOPE_DENIED"
    assert len(product["opened"]) == opens

    revision = service.memory.snapshot_for_run(owner["id"], cid)["revision"]
    cleared = client.delete(f"/api/v1/conversations/{cid}/memory",
                            params={"revision": revision, "operation_id": "clear-op"})
    assert cleared.status_code == 200, cleared.text

    receipt = client.post(_url(cid, "/retire"), json={"operation_id": "clear-op"})
    assert receipt.status_code == 200, receipt.text
    payload = receipt.json()
    assert payload["state"] == "retired"
    assert payload["cleanup_status"] == "not_performed"
    assert payload["physical_deletion"] is False
    assert len(payload["retirement_proof"]) == 64
    # Logical retirement writes a metadata receipt; it never removes bytes.
    assert history.exists()

    # A retired scope never reopens and retrieval falls back.
    after = client.post(_url(cid, "/query"), json={"q": "threshold"})
    assert after.status_code == 200 and after.json()["status"] == "fallback"
    assert client.post(_url(cid, "/sync")).status_code == 503


def test_unconfirmed_sdk_close_refuses_reopen(product):
    client, service, owner, cid = product["client"], product["service"], product["owner"], product["cid"]
    _edit_committed(service, owner, cid)
    product["backend"].fail_close = True
    failed = client.post(_url(cid, "/sync"))
    assert failed.status_code == 503 and failed.json()["error"]["code"] == "MEM0_UNAVAILABLE"

    opens = len(product["opened"])
    product["backend"].fail_close = False
    # The persisted sdk_open marker rejects any reopen of this scope, across
    # product requests, while leaving the authoritative path readable.
    fallback = client.post(_url(cid, "/query"), json={"q": "threshold"})
    assert fallback.status_code == 200 and fallback.json()["status"] == "fallback"
    assert len(product["opened"]) == opens
    native = client.get(f"/api/v1/conversations/{cid}/memory/file", params={"path": "MEMORY.md"})
    assert native.status_code == 200 and native.json()["content"] == CONTENT


def test_product_requires_scoped_factory_and_immutable_switch(product):
    from app.mem0_index import Mem0IndexConfig
    from app.mem0_product import Mem0ProductService
    service = product["service"]
    with pytest.raises(ValueError, match="isolated scoped factory"):
        Mem0ProductService(service, backend=product["backend"])
    with pytest.raises(ValueError, match="configured once"):
        service.mem0_product.configure(scoped_factory=lambda paths: product["backend"])
    with pytest.raises(ValueError, match="deployment switch"):
        Mem0ProductService(service, config=Mem0IndexConfig(enabled=False))

def test_query_is_not_in_url_or_echoed(product):
    client, cid = product["client"], product["cid"]
    response = client.post(_url(cid, "/query"), json={"q": "private synthetic query"})
    assert response.status_code == 200
    assert "query" not in response.json()
    assert "private synthetic query" not in str(response.request.url)
