"""DeploymentPolicy boundary tests for normal and controlled-trial modes.

The trial boundary is server-side.  These tests exercise the matched route
template guard, the Service/agent/MCP execution checks, request-field gates and
the shared retrieval policy.  Frontend hiding is intentionally not treated as
authorization evidence.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.db import now
from app.deployment_policy import (
    CAPABILITIES,
    DEFAULT_TRIAL_CAPABILITIES,
    DeploymentPolicy,
    TRIAL_RETRIEVAL_MODES,
    TRIAL_ROUTES,
    TRIAL_TOOLS,
)
from app.harness_bridge import HarnessKnowledgeBridge
from app.harness_tools import HarnessToolBridge
from app.main import create_app
from app.mcp_tools import MCPBusinessTool
from app.runtime import SDKRuntime, WorkspaceRuntime
from app.security import AppError
from app.service import assistant_view
from app.workspace_tools import build_workspace_tools


PASSWORD = "Deployment-policy-test-123"

EXPECTED_TRIAL_ROUTES = {
    ("GET", "/api/v1/health/live"),
    ("GET", "/api/v1/health/ready"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/logout"),
    ("GET", "/api/v1/auth/me"),
    ("GET", "/api/v1/config"),
    ("GET", "/api/v1/models"),
    ("POST", "/api/v1/models/complete"),
    ("GET", "/api/v1/assistants"),
    ("GET", "/api/v1/assistants/{item_id}"),
    ("GET", "/api/v1/conversations"),
    ("POST", "/api/v1/conversations"),
    ("GET", "/api/v1/conversations/{item_id}"),
    ("PATCH", "/api/v1/conversations/{item_id}"),
    ("GET", "/api/v1/conversations/{item_id}/messages"),
    ("POST", "/api/v1/conversations/{item_id}/messages"),
    ("GET", "/api/v1/runs/{item_id}"),
    ("GET", "/api/v1/runs/{item_id}/trace"),
    ("GET", "/api/v1/runs/{item_id}/events"),
    ("POST", "/api/v1/runs/{item_id}/cancel"),
    ("GET", "/api/v1/knowledge-bases"),
    ("GET", "/api/v1/knowledge-bases/{kb_id}"),
    ("GET", "/api/v1/knowledge-bases/{kb_id}/documents"),
    ("POST", "/api/v1/knowledge-bases/{kb_id}/search"),
    ("GET", "/api/v1/knowledge-documents/{document_id}/versions"),
    ("GET", "/api/v1/knowledge-documents/{document_id}/content"),
    ("GET", "/api/v1/knowledge-documents/{document_id}/image"),
    ("GET", "/api/v1/knowledge-documents/{document_id}/regions"),
    ("GET", "/api/v1/knowledge-documents/{document_id}/thumbnail"),
    ("GET", "/api/v1/knowledge-documents/{document_id}/regions/{region_id}/thumbnail"),
    ("GET", "/api/v1/knowledge-documents/{document_id}/chunks"),
    ("GET", "/api/v1/runs/{run_id}/sources"),
    ("GET", "/api/v1/sources/{source_id}"),
}


class FakeRuntime:
    """No-provider runtime used only to exercise the normal API pipeline."""

    connects = 0

    def __init__(self, service, conversation_id, config, approve, emit):
        self.service = service
        self.conversation_id = conversation_id
        self.approve = approve
        self.emit = emit
        self.stopped = asyncio.Event()
        FakeRuntime.connects += 1

    async def connect(self):
        return None

    async def execute(self, run, message):
        if message["text"] == "wait":
            await self.stopped.wait()
            raise asyncio.CancelledError()
        await self.emit("message.delta", {"message_id": run["assistant_message_id"], "text": "trial reply"})

    async def interrupt(self):
        self.stopped.set()

    async def close(self):
        return None


def login(client, username):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response


def assert_feature_disabled(response):
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "FEATURE_DISABLED"


def wait_for_run(client, run_id, terminal=True):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/runs/{run_id}")
        assert response.status_code == 200, response.text
        status = response.json()["status"]
        if (terminal and status in {"succeeded", "failed", "cancelled", "interrupted"}) or (
            not terminal and status == "running"
        ):
            return response.json()
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} did not reach the expected state")


def table_counts(service):
    names = [
        row["name"]
        for row in service.db.all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {
        name: service.db.one(f'SELECT COUNT(*) AS n FROM "{name}"')["n"]
        for name in names
    }


@pytest.fixture
def trial_system(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    settings = Settings(
        data_dir=tmp_path / "trial",
        provider_settings="",
        model="trial-fixture-model",
        runtime_type="harness",
        deployment_mode="trial",
        origins=("http://testserver",),
        mcp_hosts=("localhost", "127.0.0.1"),
        idle_seconds=60,
        approval_seconds=3,
    )
    app = create_app(settings, runtime_factory=FakeRuntime)
    with TestClient(app, follow_redirects=False) as client:
        service = app.state.service
        monkeypatch.setattr(
            service.knowledge.parser,
            "ocr_image",
            lambda _path: "设备铭牌 P-77 压力 8 bar",
        )
        admin = service.create_user(
            {
                "username": "admin",
                "display_name": "管理员",
                "password": PASSWORD,
                "role": "admin",
            }
        )
        member = service.create_user(
            {
                "username": "member",
                "display_name": "普通成员",
                "password": PASSWORD,
                "role": "member",
            }
        )
        kb = service.knowledge.save({"name": "公开示例知识库", "scope": "team"}, admin)
        text_document = service.knowledge.upload(
            kb["id"],
            admin,
            "manual.md",
            "# 设备校验\n\n泵站压力上限为 7 bar，点检周期为每周一次。".encode(),
        )
        image_bytes = io.BytesIO()
        Image.new("RGB", (320, 240), "#dbead4").save(image_bytes, format="PNG")
        image_document = service.knowledge.upload(
            kb["id"],
            admin,
            "nameplate.png",
            image_bytes.getvalue(),
        )
        assistant = service.save_assistant(
            {
                "name": "试用预置助手",
                "scope": "team",
                "knowledge_base_ids": [kb["id"]],
            },
            user=admin,
        )
        admin_conversation = service.create_conversation(
            {"assistant_id": assistant["id"], "title": "管理员会话"},
            admin,
        )
        member_conversation = service.create_conversation(
            {"assistant_id": assistant["id"], "title": "成员会话"},
            member,
        )
        prepared, _ = service.prepare_run(
            member_conversation["id"],
            {
                "text": "泵站压力上限",
                "file_ids": [],
                "client_request_id": "trial-seed-run",
            },
            member,
        )
        run_id = prepared["run_id"]
        found = asyncio.run(
            service.knowledge.search_for_run(
                member,
                service.require("assistants", assistant["id"]),
                run_id,
                "压力上限",
            )
        )
        assert found["results"] and found["results"][0]["source_id"]
        source_id = found["results"][0]["source_id"]
        service.db.execute(
            "UPDATE runs SET status='succeeded',completed_at=? WHERE id=?",
            (now(), run_id),
        )
        service.db.execute(
            "UPDATE conversations SET active_run_id=NULL WHERE id=?",
            (member_conversation["id"],),
        )
        service.db.event(
            run_id,
            "run.completed",
            {"status": "succeeded", "message_id": prepared["message"]["id"]},
        )
        regions = service.knowledge.image_regions(image_document["id"], None, admin)
        assert regions["regions"]
        yield SimpleNamespace(
            app=app,
            client=client,
            service=service,
            admin=admin,
            member=member,
            kb=kb,
            text_document=text_document,
            image_document=image_document,
            assistant=service.require("assistants", assistant["id"]),
            admin_conversation=admin_conversation,
            member_conversation=member_conversation,
            run_id=run_id,
            source_id=source_id,
            region_id=regions["regions"][0]["id"],
        )


def test_deployment_policy_is_exact_read_only_and_fail_closed(tmp_path, monkeypatch):
    assert set(TRIAL_ROUTES) == EXPECTED_TRIAL_ROUTES
    assert set(DEFAULT_TRIAL_CAPABILITIES) == set(CAPABILITIES)
    assert TRIAL_TOOLS == {
        "mcp__workspace__knowledge_search",
        "mcp__workspace__knowledge_read",
    }
    assert TRIAL_RETRIEVAL_MODES == ("bm25", "hybrid")
    assert DeploymentPolicy("trial").retrieval_modes() == ("bm25", "hybrid")
    assert DeploymentPolicy("normal").public_view()["capabilities"] == sorted(CAPABILITIES)

    selected = DeploymentPolicy("trial", ("health",))
    assert selected.route_allowed("GET", "/api/v1/health/live")
    assert not selected.route_allowed("GET", "/api/v1/config")
    assert not selected.tool_allowed("mcp__workspace__knowledge_search")
    with pytest.raises(AttributeError):
        selected.mode = "normal"

    app = create_app(
        Settings(
            data_dir=tmp_path / "routes",
            provider_settings="",
            deployment_mode="trial",
        )
    )
    actual_routes = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    assert EXPECTED_TRIAL_ROUTES <= actual_routes

    monkeypatch.setenv("AGENT_DEPLOYMENT_MODE", "unknown")
    with pytest.raises(ValueError, match="AGENT_DEPLOYMENT_MODE"):
        Settings(provider_settings="")
    monkeypatch.setenv("AGENT_DEPLOYMENT_MODE", "")
    with pytest.raises(ValueError, match="AGENT_DEPLOYMENT_MODE"):
        Settings(provider_settings="")
    monkeypatch.delenv("AGENT_DEPLOYMENT_MODE")

    for mode in (None, 1, True, [], {}):
        with pytest.raises(ValueError, match="AGENT_DEPLOYMENT_MODE"):
            Settings(provider_settings="", deployment_mode=mode)
    for capabilities in ("health", ["health"], ("health", 1), ("not-a-capability",)):
        with pytest.raises(ValueError):
            Settings(
                provider_settings="",
                deployment_mode="trial",
                trial_capabilities=capabilities,
            )
    monkeypatch.setenv("AGENT_DEPLOYMENT_MODE", "trial")
    monkeypatch.setenv("AGENT_TRIAL_CAPABILITIES", "health,not-a-capability")
    with pytest.raises(ValueError, match="unknown capability"):
        Settings(provider_settings="")


@pytest.mark.parametrize("mode", ["", "unknown", "NORMAL", "TRIAL", "Trial", " trial ", "normal ", " normal"])
def test_deployment_mode_explicit_values_are_never_normalized(mode, monkeypatch):
    with pytest.raises(ValueError, match="AGENT_DEPLOYMENT_MODE"):
        Settings(provider_settings="", deployment_mode=mode)
    with pytest.raises(ValueError, match="AGENT_DEPLOYMENT_MODE"):
        DeploymentPolicy(mode)

    monkeypatch.setenv("AGENT_DEPLOYMENT_MODE", mode)
    with pytest.raises(ValueError, match="AGENT_DEPLOYMENT_MODE"):
        Settings(provider_settings="")


def test_exact_deployment_modes_still_start(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_DEPLOYMENT_MODE", raising=False)
    assert Settings(provider_settings="").deployment_mode == "normal"
    for mode in ("normal", "trial"):
        settings = Settings(
            data_dir=tmp_path / mode,
            provider_settings="",
            deployment_mode=mode,
            origins=("http://testserver",),
        )
        with TestClient(create_app(settings)) as client:
            assert client.get("/api/v1/health/live").status_code == 200


def test_normal_mode_keeps_existing_health_auth_assistant_chat_and_knowledge_flows(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "normal",
        provider_settings="",
        model="normal-fixture-model",
        deployment_mode="normal",
        origins=("http://testserver",),
    )
    app = create_app(settings, runtime_factory=FakeRuntime)
    with TestClient(app) as client:
        service = app.state.service
        service.create_user(
            {
                "username": "admin",
                "display_name": "管理员",
                "password": PASSWORD,
                "role": "admin",
            }
        )
        assert client.get("/api/v1/health/live").status_code == 200
        assert client.get("/api/v1/health/ready").status_code == 200
        login(client, "admin")
        config = client.get("/api/v1/config").json()
        assert config["deployment_mode"] == "normal"
        assert config["capabilities"] == sorted(CAPABILITIES)

        assistant = client.post(
            "/api/v1/assistants",
            json={"name": "普通模式助手", "instructions": "fixture"},
        )
        assert assistant.status_code == 201, assistant.text
        assistant = assistant.json()
        assert client.get("/api/v1/assistants").status_code == 200
        assert client.get(f"/api/v1/assistants/{assistant['id']}").status_code == 200

        conversation = client.post(
            "/api/v1/conversations",
            json={"assistant_id": assistant["id"], "auto_model_routing": True},
        )
        assert conversation.status_code == 201, conversation.text
        conversation = conversation.json()
        sent = client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            json={
                "client_request_id": "normal-message",
                "text": "hello",
                "auto_route": False,
            },
        )
        assert sent.status_code == 202, sent.text
        wait_for_run(client, sent.json()["run_id"])
        messages = client.get(f"/api/v1/conversations/{conversation['id']}/messages")
        assert messages.status_code == 200
        assert any(item["text"] == "trial reply" for item in messages.json())

        kb = client.post("/api/v1/knowledge-bases", json={"name": "普通知识库"})
        assert kb.status_code == 201, kb.text
        kb = kb.json()
        uploaded = client.post(
            f"/api/v1/knowledge-bases/{kb['id']}/documents",
            files={"file": ("normal.md", "压力上限为 7 bar。".encode(), "text/markdown")},
        )
        assert uploaded.status_code == 201, uploaded.text
        search = client.post(
            f"/api/v1/knowledge-bases/{kb['id']}/search",
            json={"query": "压力", "mode": "hybrid"},
        )
        assert search.status_code == 200, search.text
        assert search.json()["retrieval"]["mode"] == "hybrid"
        empty_mode = client.post(
            f"/api/v1/knowledge-bases/{kb['id']}/search",
            json={"query": "压力", "mode": ""},
        )
        assert empty_mode.status_code == 422
        assert empty_mode.json()["error"]["code"] == "INVALID_RETRIEVAL_MODE"
        preflight = client.options(
            "/api/v1/users",
            headers={
                "Origin": "http://testserver",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "http://testserver"


def test_trial_exact_allowlist_routes_are_usable(trial_system):
    system = trial_system
    client = system.client
    called = set()

    def hit(method, template, path, **kwargs):
        called.add((method, template))
        response = client.request(method, path, **kwargs)
        return response

    login_response = login(client, "member")
    called.add(("POST", "/api/v1/auth/login"))
    assert login_response.status_code == 200
    assert hit("GET", "/api/v1/health/live", "/api/v1/health/live").status_code == 200
    assert hit("GET", "/api/v1/health/ready", "/api/v1/health/ready").status_code == 200
    assert hit("GET", "/api/v1/auth/me", "/api/v1/auth/me").status_code == 200
    assert hit("GET", "/api/v1/config", "/api/v1/config").status_code == 200
    assert hit("GET", "/api/v1/models", "/api/v1/models").status_code == 200
    completion = hit(
        "POST",
        "/api/v1/models/complete",
        "/api/v1/models/complete",
        json={"text": "试用 Tab"},
    )
    # The endpoint is available in a trial, but provider execution remains
    # fail-closed because this fixture has no authorized 1017 route.
    assert completion.status_code == 200
    assert completion.json()["suggestion"] is None
    assert hit("GET", "/api/v1/assistants", "/api/v1/assistants").status_code == 200
    assert hit(
        "GET",
        "/api/v1/assistants/{item_id}",
        f"/api/v1/assistants/{system.assistant['id']}",
    ).status_code == 200

    conversations = hit("GET", "/api/v1/conversations", "/api/v1/conversations")
    assert conversations.status_code == 200
    created = hit(
        "POST",
        "/api/v1/conversations",
        "/api/v1/conversations",
        json={"assistant_id": system.assistant["id"], "title": "新试用会话"},
    )
    assert created.status_code == 201, created.text
    created = created.json()
    assert hit(
        "GET",
        "/api/v1/conversations/{item_id}",
        f"/api/v1/conversations/{created['id']}",
    ).status_code == 200
    patched = hit(
        "PATCH",
        "/api/v1/conversations/{item_id}",
        f"/api/v1/conversations/{created['id']}",
        json={"title": "重命名后的会话"},
    )
    assert patched.status_code == 200 and patched.json()["title"] == "重命名后的会话"
    assert hit(
        "GET",
        "/api/v1/conversations/{item_id}/messages",
        f"/api/v1/conversations/{created['id']}/messages",
    ).status_code == 200
    sent = hit(
        "POST",
        "/api/v1/conversations/{item_id}/messages",
        f"/api/v1/conversations/{created['id']}/messages",
        json={
            "client_request_id": "trial-allowed-message",
            "text": "基础消息",
            "auto_route": False,
        },
    )
    assert sent.status_code == 202, sent.text
    completed = wait_for_run(client, sent.json()["run_id"])
    called.add(("GET", "/api/v1/runs/{item_id}"))
    assert completed["status"] == "succeeded"

    assert hit(
        "GET",
        "/api/v1/runs/{item_id}",
        f"/api/v1/runs/{system.run_id}",
    ).status_code == 200
    trace = hit(
        "GET",
        "/api/v1/runs/{item_id}/trace",
        f"/api/v1/runs/{system.run_id}/trace",
    )
    assert trace.status_code == 200
    events = hit(
        "GET",
        "/api/v1/runs/{item_id}/events",
        f"/api/v1/runs/{system.run_id}/events",
    )
    assert events.status_code == 200 and "event: run.completed" in events.text
    sources = hit(
        "GET",
        "/api/v1/runs/{run_id}/sources",
        f"/api/v1/runs/{system.run_id}/sources",
    )
    assert sources.status_code == 200
    assert hit(
        "GET",
        "/api/v1/sources/{source_id}",
        f"/api/v1/sources/{system.source_id}",
    ).status_code == 200

    waiting = hit(
        "POST",
        "/api/v1/conversations/{item_id}/messages",
        f"/api/v1/conversations/{created['id']}/messages",
        json={
            "client_request_id": "trial-cancel-message",
            "text": "wait",
            "auto_route": False,
        },
    )
    assert waiting.status_code == 202, waiting.text
    wait_for_run(client, waiting.json()["run_id"], terminal=False)
    called.add(("GET", "/api/v1/runs/{item_id}"))
    cancelled = hit(
        "POST",
        "/api/v1/runs/{item_id}/cancel",
        f"/api/v1/runs/{waiting.json()['run_id']}/cancel",
    )
    assert cancelled.status_code == 200
    wait_for_run(client, waiting.json()["run_id"])

    assert hit(
        "GET",
        "/api/v1/knowledge-bases",
        "/api/v1/knowledge-bases",
    ).status_code == 200
    assert hit(
        "GET",
        "/api/v1/knowledge-bases/{kb_id}",
        f"/api/v1/knowledge-bases/{system.kb['id']}",
    ).status_code == 200
    assert hit(
        "GET",
        "/api/v1/knowledge-bases/{kb_id}/documents",
        f"/api/v1/knowledge-bases/{system.kb['id']}/documents",
    ).status_code == 200
    search = hit(
        "POST",
        "/api/v1/knowledge-bases/{kb_id}/search",
        f"/api/v1/knowledge-bases/{system.kb['id']}/search",
        json={"query": "压力", "mode": "bm25"},
    )
    assert search.status_code == 200 and search.json()["retrieval"]["mode"] == "bm25"

    document_id = system.text_document["id"]
    for template, suffix in (
        ("/api/v1/knowledge-documents/{document_id}/versions", "versions"),
        ("/api/v1/knowledge-documents/{document_id}/content", "content"),
        ("/api/v1/knowledge-documents/{document_id}/chunks", "chunks"),
    ):
        response = hit("GET", template, f"/api/v1/knowledge-documents/{document_id}/{suffix}")
        assert response.status_code == 200, response.text

    image_id = system.image_document["id"]
    for template, suffix in (
        ("/api/v1/knowledge-documents/{document_id}/image", "image"),
        ("/api/v1/knowledge-documents/{document_id}/regions", "regions"),
        ("/api/v1/knowledge-documents/{document_id}/thumbnail", "thumbnail"),
        (
            "/api/v1/knowledge-documents/{document_id}/regions/{region_id}/thumbnail",
            f"regions/{system.region_id}/thumbnail",
        ),
    ):
        response = hit("GET", template, f"/api/v1/knowledge-documents/{image_id}/{suffix}")
        assert response.status_code == 200, response.text

    assert hit("POST", "/api/v1/auth/logout", "/api/v1/auth/logout").status_code == 200
    assert called == EXPECTED_TRIAL_ROUTES


def test_trial_admin_and_member_cannot_reach_disabled_routes_or_create_side_effects(
    trial_system, monkeypatch
):
    system = trial_system
    client = system.client
    provider_calls = {"router": 0, "completion": 0}

    async def router(*_args, **_kwargs):
        provider_calls["router"] += 1
        raise AssertionError("provider router must not run for a disabled route")

    async def completion(*_args, **_kwargs):
        provider_calls["completion"] += 1
        raise AssertionError("model completion must not run for a disabled route")

    monkeypatch.setattr("app.model_recommendation.provider_router", router)
    monkeypatch.setattr("app.model_recommendation.provider_completion", completion)
    for role in ("member", "admin"):
        login(client, role)
        user = system.member if role == "member" else system.admin
        conversation = (
            system.member_conversation if role == "member" else system.admin_conversation
        )
        assistant = system.assistant
        requests = [
            ("GET", "/api/v1/users", {}),
            (
                "POST",
                "/api/v1/users",
                {
                    "json": {
                        "username": f"forbidden-{role}",
                        "display_name": "forbidden",
                        "password": PASSWORD,
                        "role": "member",
                    }
                },
            ),
            ("GET", "/api/v1/connections", {}),
            ("POST", "/api/v1/connections", {"json": {"name": "blocked"}}),
            ("POST", "/api/v1/connections/import", {"json": {"url": "http://localhost/mcp"}}),
            ("POST", "/api/v1/assistants", {"json": {"name": "blocked"}}),
            (
                "PATCH",
                f"/api/v1/assistants/{assistant['id']}",
                {"json": {"name": "blocked"}},
            ),
            ("DELETE", f"/api/v1/assistants/{assistant['id']}", {}),
            (
                "PUT",
                f"/api/v1/assistants/{assistant['id']}/model-approval",
                {"json": {"enabled": True}},
            ),
            (
                "PUT",
                f"/api/v1/assistants/{assistant['id']}/high-risk-approval",
                {"json": {"enabled": True}},
            ),
            ("GET", "/api/v1/skills", {}),
            ("POST", "/api/v1/skills/import", {"json": {"name": "blocked"}}),
            ("GET", "/api/v1/admin/plugins", {}),
            (
                "POST",
                f"/api/v1/knowledge-bases/{system.kb['id']}/documents",
                {"files": {"file": ("blocked.txt", b"blocked", "text/plain")}},
            ),
            (
                "POST",
                f"/api/v1/knowledge-bases/{system.kb['id']}/documents/"
                f"{system.text_document['id']}/versions",
                {"files": {"file": ("blocked.txt", b"blocked", "text/plain")}},
            ),
            (
                "GET",
                f"/api/v1/knowledge-bases/{system.kb['id']}/graph",
                {},
            ),
            ("GET", f"/api/v1/conversations/{conversation['id']}/context", {}),
            (
                "POST",
                f"/api/v1/conversations/{conversation['id']}/context/refresh",
                {},
            ),
            ("POST", f"/api/v1/conversations/{conversation['id']}/compact", {}),
            ("GET", f"/api/v1/conversations/{conversation['id']}/memory", {}),
            (
                "PUT",
                f"/api/v1/conversations/{conversation['id']}/memory/file",
                {"json": {"path": "MEMORY.md", "content": "blocked"}},
            ),
            ("POST", "/api/v1/files", {"files": {"file": ("blocked.txt", b"x", "text/plain")}}),
            ("GET", "/api/v1/models/provider-preflight", {}),
            ("GET", "/api/v1/models/router-status", {}),
            ("POST", "/api/v1/models/recommend", {"json": {"task_summary": "blocked"}}),
            ("GET", "/api/v1/approvals/pending", {}),
            ("POST", f"/api/v1/runs/{system.run_id}/subagents", {"json": {"tasks": ["blocked"]}}),
            ("POST", f"/api/v1/runs/{system.run_id}/subagents/cancel", {}),
            ("POST", "/api/v1/artifacts/xlsx", {"json": {"filename": "blocked.xlsx", "sheets": []}}),
            ("POST", "/api/v1/artifacts/image", {"json": {"prompt": "blocked"}}),
            ("GET", "/api/v1/benchmarks/synthetic/ablation", {}),
            ("GET", "/api/v1/workflow-templates", {}),
            ("GET", "/api/v1/feishu/h5/config", {}),
            ("POST", "/api/v1/feishu/h5/login", {"json": {"code": "blocked"}}),
            ("GET", "/internal/feishu/status", {}),
            ("POST", "/internal/feishu/messages", {"json": {"text": "blocked"}}),
        ]
        before_counts = table_counts(system.service)
        before_approvals = len(system.service.manager.approvals)
        before_connects = FakeRuntime.connects
        for method, path, kwargs in requests:
            response = client.request(method, path, **kwargs)
            assert_feature_disabled(response)
            assert table_counts(system.service) == before_counts, (role, method, path)
            assert len(system.service.manager.approvals) == before_approvals
            assert FakeRuntime.connects == before_connects
        assert user["role"] == role
    assert provider_calls == {"router": 0, "completion": 0}

    client.cookies.clear()
    for method, path, kwargs in requests[:3]:
        assert_feature_disabled(client.request(method, path, **kwargs))


def test_trial_docs_and_path_method_variants_fail_closed(trial_system):
    system = trial_system
    client = system.client
    login(client, "member")

    for path in ("/docs", "/redoc", "/openapi.json"):
        response = client.get(path)
        assert_feature_disabled(response)
        assert "openapi" not in response.text.lower()

    variants = [
        ("GET", "/api/v1/users/"),
        ("GET", "/api/v1//users"),
        ("GET", "/api/v1/users%2F"),
        ("GET", "/api/v1/CONVERSATIONS"),
        ("GET", "/api/v1/health/live/"),
        ("HEAD", "/api/v1/health/live"),
        ("PATCH", "/api/v1/health/live"),
        ("PUT", f"/api/v1/conversations/{system.member_conversation['id']}"),
        ("DELETE", f"/api/v1/conversations/{system.member_conversation['id']}"),
        ("PATCH", f"/api/v1/users/{system.member['id']}"),
        ("DELETE", f"/api/v1/users/{system.member['id']}"),
        ("GET", f"/api/v1/knowledge-bases/{system.kb['id']}/graph/"),
        ("GET", "/api/v1/%75sers"),
        ("GET", "/api/v1/users-extra"),
        ("GET", "/api/v1/unknown-internal-route"),
        ("GET", "/api/v1/users/extra/../health/live"),
    ]
    before_counts = table_counts(system.service)
    for method, path in variants:
        response = client.request(method, path, follow_redirects=False)
        assert response.status_code == 403, (method, path, response.text)
        if method != "HEAD":
            assert response.json()["error"]["code"] == "FEATURE_DISABLED"
            assert path not in response.text
    assert table_counts(system.service) == before_counts


def test_trial_cors_preflight_is_blocked_before_cors_for_disabled_routes(trial_system):
    system = trial_system
    client = system.client
    login(client, "member")
    before = table_counts(system.service)

    for requested_method in ("GET", "POST"):
        response = client.options(
            "/api/v1/users",
            headers={
                "Origin": "http://testserver",
                "Access-Control-Request-Method": requested_method,
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        assert_feature_disabled(response)
        assert "access-control-allow-origin" not in response.headers

    allowed = client.options(
        "/api/v1/conversations",
        headers={
            "Origin": "http://testserver",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.headers["access-control-allow-origin"] == "http://testserver"
    assert "POST" in allowed.headers["access-control-allow-methods"]
    assert table_counts(system.service) == before


def test_trial_disabled_route_rejection_precedes_request_preconditions(trial_system):
    system = trial_system
    client = system.client
    login(client, "member")
    before = table_counts(system.service)

    disabled_requests = (
        client.post(
            "/api/v1/users",
            headers={"Origin": "http://attacker.invalid"},
            json={},
        ),
        client.post(
            "/api/v1/users",
            content=b"x",
            headers={"Content-Type": "application/json", "Content-Length": "999999999"},
        ),
        client.post(
            "/api/v1/approvals/missing-id/decision",
            content=b"{",
            headers={"Content-Type": "application/json"},
        ),
    )
    for response in disabled_requests:
        assert_feature_disabled(response)
    assert table_counts(system.service) == before

    denied_origin = client.post(
        "/api/v1/conversations",
        headers={"Origin": "http://attacker.invalid"},
        json={},
    )
    assert denied_origin.status_code == 403
    assert denied_origin.json()["error"]["code"] == "ORIGIN_DENIED"

    too_large = client.post(
        "/api/v1/conversations",
        content=b"x",
        headers={"Content-Type": "application/json", "Content-Length": "999999999"},
    )
    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "UPLOAD_TOO_LARGE"

    malformed = client.post(
        "/api/v1/conversations",
        content=b"{",
        headers={"Content-Type": "application/json"},
    )
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "VALIDATION_ERROR"

    legal = client.post(
        "/api/v1/conversations",
        headers={"Origin": "http://testserver"},
        json={"assistant_id": system.assistant["id"], "title": "合法预检反例"},
    )
    assert legal.status_code == 201, legal.text
    assert table_counts(system.service)["conversations"] == before["conversations"] + 1


def test_trial_authorize_tool_top_level_and_registration_filtering(trial_system, monkeypatch):
    system = trial_system
    service = system.service
    conversation_id = system.member_conversation["id"]
    assistant = system.assistant
    policy_items = (
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "Bash",
        "mcp__workspace__file_read",
        "mcp__workspace__file_write",
        "mcp__workspace__file_search",
        "mcp__workspace__web_search",
        "mcp__workspace__web_read",
        "mcp__workspace__generate_xlsx",
        "mcp__workspace__generate_image",
        "mcp__workspace__memory_read",
        "mcp__workspace__memory_write",
        "mcp__cforged__lookup",
        "unknown-tool",
    )
    before = table_counts(service)
    for tool_name in policy_items:
        with pytest.raises(AppError) as error:
            service.authorize_tool(conversation_id, tool_name, {})
        assert error.value.status == 403
        assert error.value.code == "FEATURE_DISABLED"
    invalid_names = (None, "", [], {}, 0, False, 1.5, object())
    for tool_name in invalid_names:
        with pytest.raises(AppError) as error:
            service.authorize_tool(conversation_id, tool_name, {})
        assert error.value.status == 403
        assert error.value.code == "FEATURE_DISABLED"
        assert service.policy.tool_allowed(tool_name) is False
    # Deployment denial precedes conversation existence and every special case.
    with pytest.raises(AppError) as error:
        service.authorize_tool("missing-conversation", "Read", {})
    assert error.value.code == "FEATURE_DISABLED"
    for tool_name in sorted(TRIAL_TOOLS):
        assert service.authorize_tool(conversation_id, tool_name, {}) is True
    assert table_counts(service) == before

    runtime = SimpleNamespace(
        service=service,
        conversation_id=conversation_id,
        session_id="trial-session",
        current_run={"id": system.run_id},
        config={
            "assistant_snapshot": {
                **assistant_view(assistant, system.member),
                "connections": [
                    {
                        "connection_id": "legacy-connection",
                        "allowed_tools": ["legacy_lookup"],
                        "auto_approved_tools": ["legacy_lookup"],
                    }
                ],
                "web_search_enabled": True,
            },
            "mcp_servers": {
                "clegacy-connection": {
                    "type": "http",
                    "url": "http://127.0.0.1:8932/mcp",
                }
            },
        },
    )
    names = {tool.name for tool in build_workspace_tools(runtime)}
    assert names == {"knowledge_search", "knowledge_read"}
    bridge = HarnessToolBridge(runtime)
    assert set(bridge.tools) == {"knowledge_search", "knowledge_read"}
    assert bridge.business_tools == {}
    assert bridge.memory.definitions() == []
    assert {item["name"] for item in bridge.definitions()} == {
        "knowledge_search",
        "knowledge_read",
    }

    before_events = len(service.db.events(system.run_id, 0))
    before_approvals = service.db.one("SELECT COUNT(*) AS n FROM approvals")["n"]
    before_evidence = service.db.one("SELECT COUNT(*) AS n FROM evidence_sources")["n"]
    for index, name in enumerate(("file_write", "generate_xlsx", "web_search", "memory_write", "external_legacy")):
        request = {
            "schema": "workspace-tool/v1",
            "sessionId": runtime.session_id,
            "callId": f"forbidden-{index}",
            "toolName": name,
            "arguments": {},
        }
        with pytest.raises(AppError) as error:
            asyncio.run(bridge.invoke(request))
        assert error.value.code == "FEATURE_DISABLED"
    for index, name in enumerate(invalid_names):
        request = {
            "schema": "workspace-tool/v1",
            "sessionId": runtime.session_id,
            "callId": f"invalid-name-{index}",
            "toolName": name,
            "arguments": {},
        }
        with pytest.raises(AppError) as error:
            asyncio.run(bridge.invoke(request))
        assert error.value.status == 403
        assert error.value.code == "FEATURE_DISABLED"
    assert len(service.db.events(system.run_id, 0)) == before_events
    assert service.db.one("SELECT COUNT(*) AS n FROM approvals")["n"] == before_approvals
    assert service.db.one("SELECT COUNT(*) AS n FROM evidence_sources")["n"] == before_evidence

    mcp_tool = object.__new__(MCPBusinessTool)
    mcp_tool.runtime = runtime
    mcp_tool.canonical_name = "mcp__clegacy-connection__legacy_lookup"

    async def forbidden_external(*_args, **_kwargs):
        pytest.fail("deployment-denied MCP tool reached the network")

    monkeypatch.setattr(mcp_tool, "current", forbidden_external)
    with pytest.raises(AppError) as error:
        asyncio.run(mcp_tool.invoke({}, forbidden_external))
    assert error.value.code == "FEATURE_DISABLED"
    with pytest.raises(AppError) as error:
        asyncio.run(bridge.memory.invoke("memory_write", "forbidden-memory", {}))
    assert error.value.code == "FEATURE_DISABLED"

    runtime_config = service.runtime_config(conversation_id)
    workspace_runtime = WorkspaceRuntime(service, conversation_id, runtime_config, lambda *_: True, lambda *_: None)
    knowledge_bridge = HarnessKnowledgeBridge(runtime)
    for name in invalid_names:
        with pytest.raises(AppError) as error:
            workspace_runtime.check_tool(name, {})
        assert error.value.status == 403
        assert error.value.code == "FEATURE_DISABLED"
        with pytest.raises(AppError) as error:
            asyncio.run(knowledge_bridge.invoke(name, {}))
        assert error.value.status == 403
        assert error.value.code == "FEATURE_DISABLED"

    mcp_tool = object.__new__(MCPBusinessTool)
    mcp_tool.runtime = runtime
    for name in invalid_names:
        mcp_tool.canonical_name = name
        with pytest.raises(AppError) as error:
            asyncio.run(mcp_tool.invoke({}, forbidden_external))
        assert error.value.status == 403
        assert error.value.code == "FEATURE_DISABLED"
    assert len(service.db.events(system.run_id, 0)) == before_events
    assert service.db.one("SELECT COUNT(*) AS n FROM approvals")["n"] == before_approvals
    assert service.db.one("SELECT COUNT(*) AS n FROM evidence_sources")["n"] == before_evidence


def test_stale_configuration_policy_denial_precedes_runtime_reads(
    trial_system, monkeypatch
):
    system = trial_system
    service = system.service
    conversation_id = system.member_conversation["id"]
    config = service.runtime_config(conversation_id)

    # Simulate an assistant edit after the run started, making the in-memory
    # runtime configuration stale without regenerating it.
    service.db.execute(
        "UPDATE assistants SET revision=revision+1 WHERE id=?",
        (system.assistant["id"],),
    )

    config_reads, db_reads, authorize_calls = [], [], []
    original_runtime_config = service.runtime_config
    original_one = service.db.one

    def observed_runtime_config(item_id):
        config_reads.append(item_id)
        return original_runtime_config(item_id)

    def observed_one(query, values=()):
        db_reads.append((query, values))
        return original_one(query, values)

    def unexpected_authorize(*_args, **_kwargs):
        authorize_calls.append(True)
        pytest.fail("invalid or stale-config tool reached Service.authorize_tool")

    monkeypatch.setattr(service, "runtime_config", observed_runtime_config)
    monkeypatch.setattr(service.db, "one", observed_one)
    monkeypatch.setattr(service, "authorize_tool", unexpected_authorize)

    class UnhashableToolName:
        __hash__ = None

    invalid_names = (
        None,
        "",
        "   ",
        [],
        {},
        0,
        False,
        1.5,
        object(),
        UnhashableToolName(),
        "unknown-tool",
    )
    before = table_counts(service)
    db_reads.clear()

    for runtime_class in (WorkspaceRuntime, SDKRuntime):
        runtime = runtime_class(
            service,
            conversation_id,
            config,
            lambda *_args, **_kwargs: True,
            lambda *_args, **_kwargs: None,
        )
        runtime.current_run = {"id": system.run_id}
        for tool_name in invalid_names:
            with pytest.raises(AppError) as error:
                runtime.check_tool(tool_name, {})
            assert error.value.status == 403
            assert error.value.code == "FEATURE_DISABLED"

    # The policy is a pure value check: no mutable configuration refresh,
    # database query, authorization path or durable side effect may run first.
    assert config_reads == []
    assert db_reads == []
    assert authorize_calls == []
    assert table_counts(service) == before

    legal_tool = sorted(TRIAL_TOOLS)[0]
    for runtime_class in (WorkspaceRuntime, SDKRuntime):
        config_reads.clear()
        db_reads.clear()
        runtime = runtime_class(
            service,
            conversation_id,
            config,
            lambda *_args, **_kwargs: True,
            lambda *_args, **_kwargs: None,
        )
        runtime.current_run = {"id": system.run_id}
        with pytest.raises(AppError) as error:
            runtime.check_tool(legal_tool, {})
        assert error.value.status == 403
        assert error.value.code == "CONFIG_CHANGED"
        assert config_reads == [conversation_id]
        assert db_reads
    assert authorize_calls == []


def test_trial_historical_snapshot_and_legacy_sdk_cannot_expose_native_tools(
    trial_system, monkeypatch
):
    system = trial_system
    service = system.service
    conversation_id = system.member_conversation["id"]
    assistant = service.require("assistants", system.assistant["id"])
    snapshot = assistant_view(assistant, system.member)
    snapshot["connections"] = [
        {
            "connection_id": "historical-private-connection",
            "allowed_tools": ["read_business"],
            "auto_approved_tools": ["read_business"],
        }
    ]
    service.db.execute(
        "UPDATE runs SET status='running',completed_at=NULL,capability_snapshot=?,skill_snapshots=? WHERE id=?",
        (
            json.dumps(snapshot, ensure_ascii=False),
            json.dumps([{"id": "legacy-skill", "name": "legacy", "revision": 1}]),
            system.run_id,
        ),
    )
    service.db.execute(
        "UPDATE conversations SET active_run_id=? WHERE id=?",
        (system.run_id, conversation_id),
    )
    try:
        config = service.runtime_config(conversation_id)
        assert config["skill_snapshots"] == []
        assert config["mcp_servers"] == {}
        runtime = SimpleNamespace(
            service=service,
            conversation_id=conversation_id,
            session_id="legacy-session",
            current_run={"id": system.run_id},
            config=config,
        )
        assert {item.name for item in build_workspace_tools(runtime)} == {
            "knowledge_search",
            "knowledge_read",
        }
        assert HarnessToolBridge(runtime).business_tools == {}
    finally:
        service.db.execute(
            "UPDATE runs SET status='succeeded',completed_at=? WHERE id=?",
            (now(), system.run_id),
        )
        service.db.execute(
            "UPDATE conversations SET active_run_id=NULL WHERE id=?",
            (conversation_id,),
        )

    settings = Settings(
        data_dir=system.app.state.service.settings.data_dir / "legacy-sdk",
        provider_settings="",
        runtime_type="claude",
        deployment_mode="trial",
    )
    settings.provider_env = lambda: {"ANTHROPIC_API_KEY": "not-a-real-key"}
    legacy_service = type(service)(settings)
    legacy_user = legacy_service.create_user(
        {
            "username": "legacy",
            "display_name": "Legacy",
            "password": PASSWORD,
            "role": "admin",
        }
    )
    legacy_assistant = legacy_service.save_assistant({"name": "Legacy"}, user=legacy_user)
    legacy_conversation = legacy_service.create_conversation(
        {"assistant_id": legacy_assistant["id"], "title": "Legacy"},
        legacy_user,
    )
    legacy_run, _ = legacy_service.prepare_run(
        legacy_conversation["id"],
        {
            "text": "legacy",
            "file_ids": [],
            "client_request_id": "legacy-sdk-options",
        },
        legacy_user,
    )
    captured = {}

    class Client:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self):
            return None

        async def disconnect(self):
            return None

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", Client)
    try:
        async def scenario():
            runtime = SDKRuntime(
                legacy_service,
                legacy_conversation["id"],
                legacy_service.runtime_config(legacy_conversation["id"]),
                None,
                lambda *_args: None,
            )
            await runtime.connect()
            await runtime.close()

        asyncio.run(scenario())
        assert captured["options"].tools == []
        assert "Read" not in captured["options"].tools
        assert legacy_service.db.one("SELECT status FROM runs WHERE id=?", (legacy_run["run_id"],))
    finally:
        legacy_service.db.close()


def test_trial_advanced_fields_and_retrieval_fail_before_provider_or_writes(
    trial_system, monkeypatch
):
    system = trial_system
    service = system.service
    client = system.client
    calls = {"router": 0}

    async def router(*_args, **_kwargs):
        calls["router"] += 1
        return {"model_id": "legacy-model", "reasoning_level": "medium", "reason": "fake"}

    monkeypatch.setattr("app.model_recommendation.provider_router", router)
    login(client, "member")
    conversation_id = system.member_conversation["id"]
    before_runs = service.db.one("SELECT COUNT(*) AS n FROM runs")["n"]
    before_conversations = service.db.one("SELECT COUNT(*) AS n FROM conversations")["n"]

    for payload in (
        {"client_request_id": "trial-auto-route", "text": "x", "auto_route": True},
        {"client_request_id": "trial-files", "text": "x", "file_ids": ["fake-file"]},
        {"client_request_id": "trial-skills", "text": "x", "skill_ids": ["fake-skill"]},
    ):
        assert_feature_disabled(
            client.post(f"/api/v1/conversations/{conversation_id}/messages", json=payload)
        )
    assert calls["router"] == 0
    assert service.db.one("SELECT COUNT(*) AS n FROM runs")["n"] == before_runs

    for data in (
        {"assistant_id": system.assistant["id"], "auto_model_routing": True},
    ):
        assert_feature_disabled(client.post("/api/v1/conversations", json=data))
    assert_feature_disabled(
        client.patch(
            f"/api/v1/conversations/{conversation_id}",
            json={"auto_model_routing": True},
        )
    )
    assert service.db.one("SELECT COUNT(*) AS n FROM conversations")["n"] == before_conversations
    assert calls["router"] == 0

    with pytest.raises(AppError) as error:
        service.prepare_run(
            "missing-conversation",
            {
                "text": "x",
                "file_ids": ["fake-file"],
                "client_request_id": "direct-file-gate",
            },
            system.member,
        )
    assert error.value.code == "FEATURE_DISABLED"
    with pytest.raises(AppError) as error:
        service.prepare_run(
            "missing-conversation",
            {
                "text": "x",
                "file_ids": [],
                "skill_ids": ["fake"],
                "client_request_id": "direct-skill-gate",
            },
            system.member,
        )
    assert error.value.code == "FEATURE_DISABLED"
    with pytest.raises(AppError) as error:
        service.create_conversation(
            {"assistant_id": "missing", "auto_model_routing": True},
            system.member,
        )
    assert error.value.code == "FEATURE_DISABLED"
    with pytest.raises(AppError) as error:
        service.patch_conversation("missing", {"auto_model_routing": True}, system.member)
    assert error.value.code == "FEATURE_DISABLED"

    explicit = client.post(
        "/api/v1/conversations",
        json={
            "assistant_id": system.assistant["id"],
            "title": "显式模型",
            "model_id": "legacy-model",
            "reasoning_level": "medium",
            "auto_model_routing": False,
        },
    )
    assert explicit.status_code == 201, explicit.text
    explicit_conversation = explicit.json()
    sent = client.post(
        f"/api/v1/conversations/{explicit_conversation['id']}/messages",
        json={
            "client_request_id": "explicit-model-message",
            "text": "显式基础模型",
            "model_id": "legacy-model",
            "reasoning_level": "medium",
            "auto_route": False,
        },
    )
    assert sent.status_code == 202, sent.text
    wait_for_run(client, sent.json()["run_id"])
    assert calls["router"] == 0

    kb_id = system.kb["id"]
    for mode in ("bm25", "hybrid"):
        response = client.post(
            f"/api/v1/knowledge-bases/{kb_id}/search",
            json={"query": "压力", "mode": mode},
        )
        assert response.status_code == 200, response.text
        assert response.json()["retrieval"]["mode"] == mode
    for mode in ("vector", "hipporag", "unknown", ""):
        response = client.post(
            f"/api/v1/knowledge-bases/{kb_id}/search",
            json={"query": "压力", "mode": mode},
        )
        assert_feature_disabled(response)

    for mode in ("vector", "hipporag", "unknown", ""):
        with pytest.raises(AppError) as error:
            service.knowledge.search([kb_id], system.member, "压力", mode=mode)
        assert error.value.status == 403
        assert error.value.code == "FEATURE_DISABLED"

    tool_conversation = service.create_conversation(
        {"assistant_id": system.assistant["id"], "title": "tool retrieval"},
        system.member,
    )
    tool_run, _ = service.prepare_run(
        tool_conversation["id"],
        {
            "text": "tool retrieval",
            "file_ids": [],
            "client_request_id": "tool-retrieval-mode",
        },
        system.member,
    )
    monkeypatch.setattr(service.knowledge, "hipporag_backend", object())
    try:
        result = asyncio.run(
            service.knowledge.search_for_run(
                system.member,
                service.require("assistants", system.assistant["id"]),
                tool_run["run_id"],
                "压力",
            )
        )
        assert result["retrieval"]["mode"] == "hybrid"
        assert result["results"]
    finally:
        monkeypatch.setattr(service.knowledge, "hipporag_backend", None)
        service.db.execute(
            "UPDATE runs SET status='succeeded',completed_at=? WHERE id=?",
            (now(), tool_run["run_id"]),
        )
        service.db.execute(
            "UPDATE conversations SET active_run_id=NULL WHERE id=?",
            (tool_conversation["id"],),
        )


def test_config_output_is_deterministic_and_contains_no_credentials(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "config",
        provider_settings="",
        deployment_mode="trial",
        feishu_h5_enabled=True,
        feishu_h5_app_id="cli-public-id",
        feishu_h5_app_secret="h5-super-secret",
        feishu_bridge_token="bridge-super-secret",
        knowledge_vision_base_url="http://127.0.0.1:3425/v1",
        origins=("http://testserver",),
    )
    app = create_app(settings)
    with TestClient(app) as client:
        first = client.get("/api/v1/config")
        second = client.get("/api/v1/config")
        assert first.status_code == 200 and second.status_code == 200
        payload = first.json()
        assert payload == second.json()
        assert payload["deployment_mode"] == "trial"
        assert payload["capabilities"] == sorted(payload["capabilities"])
        assert payload["capabilities"] == sorted(CAPABILITIES)
        assert payload["allowed_retrieval_modes"] == ["bm25", "hybrid"]
        assert payload["feishu_h5"]["enabled"] is False
        assert payload["feishu_h5"]["app_id"] == ""
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        for forbidden in (
            "h5-super-secret",
            "bridge-super-secret",
            "127.0.0.1",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "password_hash",
        ):
            assert forbidden not in serialized
        assert payload["default_model_id"]
        assert set(payload) >= {
            "deployment_mode",
            "capabilities",
            "allowed_retrieval_modes",
        }
