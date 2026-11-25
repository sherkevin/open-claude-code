"""Owner-only full-content review against real storage/bindings; no models."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import dump, now, uid
from app.harness_memory import MEMORY_WRITE, content_digest, policy_binding
from app.main import create_app
from app.service import approval_view


@pytest.fixture
def memory_review(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    # Explicit local-only synthetic provider; no real request is made by these storage tests.
    monkeypatch.setenv("ANTHROPIC_API_KEY","synthetic-memory-fixture")
    monkeypatch.setenv("ANTHROPIC_BASE_URL","http://127.0.0.1:9/v1")
    app = create_app(Settings(data_dir=tmp_path, provider_settings="", model="fixture", runtime_type="harness"))
    with TestClient(app) as client:
        service = app.state.service
        password = "synthetic-memory-review-123"
        owner = service.create_user({"username": "owner", "display_name": "Owner", "password": password, "role": "admin"})
        other = service.create_user({"username": "other", "display_name": "Other", "password": password, "role": "admin"})
        assistant = service.save_assistant({"name": "Memory review", "access": {"all_members": True, "user_ids": []}}, user=owner)
        cid = service.create_conversation({"assistant_id": assistant["id"], "title": "Memory review"}, owner)["id"]
        run, _ = service.prepare_run(cid, {"text": "remember synthetic note", "file_ids": [], "client_request_id": "memory-review"}, owner)
        rid = run["run_id"]
        service.db.execute("UPDATE runs SET status='awaiting_approval' WHERE id=?", (rid,))
        binding = service.memory.snapshot_for_run(owner["id"], cid, rid)
        # Exercise the real immutable proposal, not an API-only fake helper.
        op_id = uid()
        content = "Private synthetic text\n" + "a" * 17000 + "\nSYNTHETIC-END-OF-CONTENT"
        operation = service.memory.stage_for_run(binding, rid, op_id, "MEMORY.md", content, binding["revision"])
        args = {"path": "MEMORY.md", "content": content, "expected_revision": binding["revision"],
                "operation_id": operation["operation_id"], "native_epoch": binding["native_epoch"],
                "policy_binding": policy_binding(service, cid, service.runtime_config(cid))}
        aid = uid()
        expiry = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        service.db.execute("INSERT INTO approvals(id,run_id,user_id,tool_name,arguments,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                           (aid, rid, owner["id"], MEMORY_WRITE, dump(args), "pending", now(), expiry))

        async def install_future():
            service.manager.approvals[aid] = asyncio.get_running_loop().create_future()

        client.portal.call(install_future)
        assert client.post("/api/v1/auth/login", json={"username": "owner", "password": password}).status_code == 200
        yield {"client": client, "service": service, "owner": owner, "other": other, "assistant": assistant,
               "cid": cid, "rid": rid, "aid": aid, "content": content, "args": args,
               "url": f"/api/v1/approvals/{aid}", "password": password}


def test_full_body_is_owner_only_untruncated_and_never_in_public_preview(memory_review):
    f = memory_review
    result = f["client"].get(f["url"] + "/memory-content")
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["content"] == f["content"]
    assert result.json()["content_digest"] == content_digest(f["content"])
    preview = approval_view(f["service"].require("approvals", f["aid"]))
    assert "content" not in preview["arguments"]
    assert "SYNTHETIC-END-OF-CONTENT" not in json.dumps(preview)
    assert preview["arguments"]["requires_full_content_review"] is True
    f["client"].post("/api/v1/auth/login", json={"username": "other", "password": f["password"]})
    assert f["client"].get(f["url"] + "/memory-content").status_code == 404
    assert f["client"].post(f["url"] + "/decision", json={"decision": "allow", "memory_content_digest": content_digest(f["content"])}).status_code == 404


@pytest.mark.parametrize("digest", [None, "0" * 64])
def test_allow_requires_digest_of_exact_full_body(memory_review, digest):
    f = memory_review
    result = f["client"].post(f["url"] + "/decision", json={"decision": "allow", "memory_content_digest": digest})
    assert result.status_code == 409 and result.json()["error"]["code"] == "MEMORY_REVIEW_REQUIRED"
    assert not f["service"].manager.approvals[f["aid"]].done()
    assert f["service"].require("approvals", f["aid"])["decided_by"] is None


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_human_decision_records_owner_but_does_not_publish(memory_review, decision):
    f = memory_review
    data = {"decision": decision}
    if decision == "allow":
        data["memory_content_digest"] = f["client"].get(f["url"] + "/memory-content").json()["content_digest"]
    result = f["client"].post(f["url"] + "/decision", json=data)
    assert result.status_code == 200
    row = f["service"].require("approvals", f["aid"])
    assert row["decision_source"] == "user" and row["decided_by"] == f["owner"]["id"]
    assert f["service"].manager.approvals[f["aid"]].result() == decision
    assert f["service"].memory.view(f["owner"], f["cid"])["files"] == []
    assert f["client"].get(f["url"] + "/memory-content").status_code == 409


@pytest.mark.parametrize("change", ["assistant_disabled", "user_disabled", "cancelled", "epoch", "expired", "body", "dual", "finished", "operation"])
def test_revalidate_on_allow_after_content_was_read(memory_review, change):
    f = memory_review
    response = f["client"].get(f["url"] + "/memory-content")
    assert response.status_code == 200
    db = f["service"].db
    if change == "assistant_disabled":
        db.execute("UPDATE assistants SET enabled=0 WHERE id=?", (f["assistant"]["id"],))
    elif change == "user_disabled":
        db.execute("UPDATE users SET enabled=0 WHERE id=?", (f["owner"]["id"],))
    elif change == "cancelled":
        db.execute("UPDATE runs SET status='cancelling' WHERE id=?", (f["rid"],))
    elif change == "epoch":
        db.execute("UPDATE conversations SET native_epoch=native_epoch+1 WHERE id=?", (f["cid"],))
    elif change == "expired":
        db.execute("UPDATE approvals SET expires_at=? WHERE id=?", ("2020-01-01T00:00:00+00:00", f["aid"]))
    elif change == "body":
        db.execute("UPDATE approvals SET arguments=? WHERE id=?", (dump({**f["args"], "content": "replacement"}), f["aid"]))
    elif change == "dual":
        db.execute("UPDATE approvals SET approval_mode='dual' WHERE id=?", (f["aid"],))
    elif change == "finished":
        f["service"].manager.approvals.pop(f["aid"])
    elif change == "operation":
        f["service"].memory.discard_for_run(f["rid"], "synthetic cancellation")
    result = f["client"].post(f["url"] + "/decision", json={"decision": "allow", "memory_content_digest": response.json()["content_digest"]})
    assert result.status_code in {401, 403, 404, 409}
    assert f["service"].require("approvals", f["aid"])["decided_by"] is None
    assert f["client"].get(f["url"] + "/memory-content").status_code in {401, 403, 404, 409}


def test_nonmemory_approval_has_no_full_memory_body_route(memory_review):
    f = memory_review
    f["service"].db.execute("UPDATE approvals SET tool_name='mcp__workspace__generate_xlsx' WHERE id=?", (f["aid"],))
    assert f["client"].get(f["url"] + "/memory-content").status_code == 404


def test_invalid_digest_is_rejected_before_decision(memory_review):
    f = memory_review
    result = f["client"].post(f["url"] + "/decision", json={"decision": "allow", "memory_content_digest": "not-a-digest"})
    assert result.status_code == 422
    assert not f["service"].manager.approvals[f["aid"]].done()
