"""Synthetic prompts exercise the public chat and native reviewer contracts."""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient

from app.config import DEFAULT_PROMPT_SAFETY_MODEL, Settings
from app.db import dump, now, uid
from app.main import create_app
from app.prompt_safety import UNAVAILABLE_MESSAGE, PromptSafety
from app.runtime import RunManager
from app.security import AppError
from test_api import PASSWORD, FakeRuntime, conversation, login, poll, send


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "server/prompt-safety.example.json"
CASES = json.loads((ROOT / "fixtures/prompt-safety/zh-CN.json").read_text(encoding="utf-8"))
POLICY_BLOCK = "澄 湾啥时候选·议员啊？"
REVIEW_BLOCK = "【合成样本】通用分类待判定"
SAFE = "拟一份安全培训签到表"


@pytest.fixture
def settings(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_bytes(EXAMPLE.read_bytes())
    return Settings(data_dir=tmp_path / "data", provider_settings="", runtime_type="claude",
                    prompt_safety_mode="rules", prompt_safety_policy=str(policy), origins=("http://testserver",),
                    prompt_safety_qwen_model=DEFAULT_PROMPT_SAFETY_MODEL)


class ObservedRuntime(FakeRuntime):
    calls = []

    async def connect(self):
        self.calls.append("model-connect")

    async def execute(self, run, message):
        self.calls.extend(["model-request", "synthetic-tool-call"])
        await super().execute(run, message)


@pytest.fixture
def client(settings):
    ObservedRuntime.calls.clear()
    with TestClient(create_app(settings, runtime_factory=ObservedRuntime)) as client:
        client.app.state.service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
        login(client)
        yield client


def assert_no_run(client, cid):
    assert client.get(f"/api/v1/conversations/{cid}/messages").json() == []
    assert client.get(f"/api/v1/conversations/{cid}").json()["active_run_id"] is None
    assert not client.app.state.service.db.all("SELECT id FROM runs WHERE conversation_id=?", (cid,))
    assert ObservedRuntime.calls == []


@pytest.mark.parametrize("case", [case for case in CASES if case["group"] != "general"], ids=lambda case: case["id"])
def test_chinese_policy_examples(settings, case):
    if case["id"] == "election-no-keyword":
        # Deliberate control measuring what the maintained terms miss.
        return PromptSafety(settings).require_allowed(case["text"])
    if case["decision"] == "allow":
        return PromptSafety(settings).require_allowed(case["text"])
    with pytest.raises(AppError) as rejected:
        PromptSafety(settings).require_allowed(case["text"])
    assert rejected.value.code == "PROMPT_BLOCKED"


def test_policy_blocks_before_routing_and_run_and_does_not_echo(client, caplog, monkeypatch):
    async def unexpected_routing(*args, **kwargs):
        pytest.fail("blocked input reached provider routing")

    monkeypatch.setattr("app.model_recommendation.provider_router", unexpected_routing)
    cid = conversation(client)["id"]
    with caplog.at_level("INFO", logger="app.prompt_safety"):
        response = client.post(f"/api/v1/conversations/{cid}/messages", json={
            "client_request_id": "prompt-unsafe-001", "text": POLICY_BLOCK, "auto_route": True,
        })
    assert response.status_code == 403
    assert response.json()["error"] == {"code": "PROMPT_BLOCKED", "message": "此工作区限制讨论该主题，请调整问题后再发送。"}
    assert "category=fictional_civic_election" in caplog.text
    assert POLICY_BLOCK not in response.text + caplog.text
    assert_no_run(client, cid)


def test_config_update_and_duplicate_preserve_normal_chat(client, settings):
    cid = conversation(client)["id"]
    policy_path = Path(settings.prompt_safety_policy)
    policy = json.loads(policy_path.read_bytes())
    policy["rules"][0]["enabled"] = False
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    original = send(client, cid, POLICY_BLOCK, "prompt-allowed-001")
    run_id = original["run_id"]
    assert poll(client, run_id)["status"] == "succeeded"
    assert "event: message.delta" in client.get(f"/api/v1/runs/{run_id}/events").text
    assert client.get(f"/api/v1/conversations/{cid}/messages").json()[-1]["text"] == "回复：" + POLICY_BLOCK
    policy["rules"][0]["enabled"] = True
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    assert send(client, cid, POLICY_BLOCK, "prompt-allowed-001")["run_id"] == run_id
    assert ObservedRuntime.calls == ["model-connect", "model-request", "synthetic-tool-call"]


@pytest.fixture
def manual_queue(monkeypatch):
    monkeypatch.setattr(RunManager, "start_queue_worker", lambda _manager: None)


@pytest.mark.usefixtures("manual_queue")
def test_queue_reviews_add_edit_and_old_draft_at_dispatch(client, settings):
    cid = conversation(client)["id"]
    base = f"/api/v1/conversations/{cid}/queue"
    bad = client.post(base, json={"client_request_id": "blocked-queue-draft", "text": POLICY_BLOCK})
    assert bad.status_code == 403
    assert client.get(base).json()["items"] == []
    settings.prompt_safety_mode = "off"
    draft = client.post(base, json={"client_request_id": "old-queue-draft", "text": POLICY_BLOCK}).json()
    settings.prompt_safety_mode = "rules"
    edit = client.patch(f"{base}/{draft['id']}", json={"revision": draft["revision"], "text": POLICY_BLOCK})
    assert edit.status_code == 403
    result = client.post(f"{base}/{draft['id']}/dispatch", json={"revision": draft["revision"]}).json()
    assert result["status"] == "paused"
    assert result["queue"]["items"][0]["error"] == bad.json()["error"]["message"]
    assert_no_run(client, cid)
    updated = client.patch(f"{base}/{draft['id']}", json={"revision": draft["revision"], "text": SAFE}).json()
    accepted = client.post(f"{base}/{draft['id']}/dispatch", json={"revision": updated["items"][0]["revision"]}).json()
    assert poll(client, accepted["run_id"])["status"] == "succeeded"


@pytest.mark.usefixtures("manual_queue")
def test_queue_rejects_a_stale_review_when_a_draft_changes(client, monkeypatch):
    cid = conversation(client)["id"]
    base = f"/api/v1/conversations/{cid}/queue"
    draft = client.post(base, json={"client_request_id": "race-queue-draft", "text": SAFE}).json()
    item_url = f"{base}/{draft['id']}"
    reviewed, release = Event(), Event()
    safety = client.app.state.service.prompt_safety
    actual_review = safety.require_allowed

    def hold_original_review(text, user_id=None):
        actual_review(text, user_id)
        if text == SAFE:
            reviewed.set()
            assert release.wait(5)

    monkeypatch.setattr(safety, "require_allowed", hold_original_review)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.post, f"{item_url}/dispatch", json={"revision": draft["revision"]})
        try:
            assert reviewed.wait(5)
            edit = client.patch(item_url, json={"revision": draft["revision"], "text": "拟一份会议议程"})
            assert edit.status_code == 200
        finally:
            release.set()
        assert pending.result().json()["status"] == "changed"
    assert_no_run(client, cid)


def test_scheduled_text_is_reviewed_when_created_and_again_when_due(client, settings):
    cid = conversation(client)["id"]
    schedules = f"/api/v1/conversations/{cid}/schedules"
    scheduled_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    data = {"text": POLICY_BLOCK, "scheduled_at": scheduled_at}
    assert client.post(schedules, json=data).status_code == 403
    assert client.get(schedules).json() == []

    settings.prompt_safety_mode = "off"
    previous = client.post(schedules, json=data)
    assert previous.status_code == 201
    settings.prompt_safety_mode = "rules"
    service = client.app.state.service
    service.db.execute("UPDATE conversation_schedules SET scheduled_at=? WHERE id=?", (now(), previous.json()["id"]))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        item, = client.get(schedules).json()
        if item.get("error"):
            break
        time.sleep(0.05)
    assert item.get("error") and item["run_id"] is None
    assert_no_run(client, cid)


def test_retry_reviews_original_text_before_model(client, settings):
    cid = conversation(client)["id"]
    settings.prompt_safety_mode = "off"
    previous = send(client, cid, POLICY_BLOCK, "legacy-recovery-001")["run_id"]
    assert poll(client, previous)["status"] == "succeeded"
    service = client.app.state.service
    service.db.execute("UPDATE runs SET status='failed' WHERE id=?", (previous,))
    settings.prompt_safety_mode = "rules"
    ObservedRuntime.calls.clear()
    blocked = client.post(f"/api/v1/conversations/{cid}/messages", json={
        "client_request_id": "legacy-recovery-002", "text": SAFE, "recovery_run_id": previous,
    })
    assert blocked.status_code == 403, blocked.text
    assert service.db.all("SELECT id FROM runs WHERE conversation_id=?", (cid,)) == [{"id": previous}]
    assert not ObservedRuntime.calls


def test_steering_is_reviewed_before_reserving_or_delivering(client, monkeypatch):
    async def unexpected_delivery(*args):
        pytest.fail("blocked steering reached the runtime")

    monkeypatch.setattr(ObservedRuntime, "steer", unexpected_delivery, raising=False)
    cid = conversation(client)["id"]
    service = client.app.state.service
    service.db.execute("UPDATE conversations SET runtime_type='harness' WHERE id=?", (cid,))
    run_id = send(client, cid, "wait", "running-prompt-001")["run_id"]
    assert poll(client, run_id, {"running"})["status"] == "running"
    blocked = client.post(f"/api/v1/runs/{run_id}/steering", json={
        "client_request_id": "blocked-steer-001", "text": POLICY_BLOCK,
    })
    assert blocked.status_code == 403, blocked.text
    assert service.db.all("SELECT id FROM run_steers WHERE run_id=?", (run_id,)) == []


def test_policy_applies_to_tab_recommendation_and_compaction(client, monkeypatch):
    async def unexpected_provider(*args, **kwargs):
        pytest.fail("blocked input reached an auxiliary model")

    monkeypatch.setattr("app.model_recommendation.provider_router", unexpected_provider)
    monkeypatch.setattr("app.model_recommendation.provider_completion", unexpected_provider)
    cid = conversation(client)["id"]
    service = client.app.state.service
    service.db.execute("UPDATE conversations SET sdk_session_id=? WHERE id=?", ("synthetic-session", cid))
    for path, data in [
        ("/models/complete", {"text": POLICY_BLOCK}),
        ("/models/recommend", {"task_summary": POLICY_BLOCK}),
        (f"/conversations/{cid}/compact", {"client_request_id": "prompt-compact-001", "instructions": POLICY_BLOCK}),
    ]:
        response = client.post("/api/v1" + path, json=data)
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "PROMPT_BLOCKED"
    assert_no_run(client, cid)


@contextmanager
def reviewer(status, content, *, finish="stop", delay=0):
    payloads = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payloads.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            time.sleep(delay)
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"finish_reason": finish, "message": {"content": content}}]}).encode())
            except BrokenPipeError:
                pass

        def log_message(self, format, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", payloads
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize("content,code", [
    ("Safety: Safe\nCategories: None", 202),
    ("Safety: Unsafe\nCategories: Non-violent Illegal Acts, PII", 403),
    ("Safety: Unsafe\nCategories: Politically Sensitive Topics", 202),
    ("Safety: Controversial\nCategories: Unethical Acts", 202),
])
def test_qwen_review_runs_before_model_and_tools(client, settings, content, code):
    cid = conversation(client)["id"]
    settings.prompt_safety_mode = "qwen"
    with reviewer(200, content) as (settings.prompt_safety_qwen_url, payloads):
        response = client.post(f"/api/v1/conversations/{cid}/messages", json={"client_request_id": "qwen-test-0001", "text": REVIEW_BLOCK})
    assert response.status_code == code, response.text
    path, auth, payload = payloads[0]
    assert len(payloads) == 1 and path == "/v1/chat/completions" and auth is None
    assert payload["model"] == DEFAULT_PROMPT_SAFETY_MODEL
    assert payload["messages"] == [{"role": "user", "content": REVIEW_BLOCK}]
    assert payload["stream"] is False
    if code == 403:
        assert response.json()["error"]["code"] == "PROMPT_BLOCKED"
        assert REVIEW_BLOCK not in response.text
        assert_no_run(client, cid)
    else:
        assert poll(client, response.json()["run_id"])["status"] == "succeeded"
        assert ObservedRuntime.calls == ["model-connect", "model-request", "synthetic-tool-call"]


@pytest.mark.parametrize("status,content,finish,kind", [
    (503, "private-response-token", "stop", "http_status"),
    (200, "private-response-token", "stop", "response"),
    (200, "Safety: Safe\nCategories: None", "length", "response"),
    (200, "Safety: Unsafe\nCategories: unrecognized-label", "stop", "response"),
])
def test_classifier_failure_is_closed(client, settings, status, content, finish, kind, caplog):
    cid = conversation(client)["id"]
    settings.prompt_safety_mode = "qwen"
    with reviewer(status, content, finish=finish) as (settings.prompt_safety_qwen_url, _):
        response = client.post(f"/api/v1/conversations/{cid}/messages", json={"client_request_id": "qwen-error-001", "text": REVIEW_BLOCK})
    assert response.status_code == 503
    assert response.json()["error"] == {"code": "PROMPT_REVIEW_UNAVAILABLE", "message": UNAVAILABLE_MESSAGE}
    assert f"category={kind}" in caplog.text
    assert content not in response.text + caplog.text and REVIEW_BLOCK not in response.text + caplog.text
    assert_no_run(client, cid)


def test_invalid_policy_fails_closed_and_can_be_replaced(client, settings, caplog):
    cid = conversation(client)["id"]
    Path(settings.prompt_safety_policy).write_text('{"rules":["private-policy-term"]}', encoding="utf-8")
    response = client.post(f"/api/v1/conversations/{cid}/messages", json={"client_request_id": "policy-error-001", "text": SAFE})
    assert response.status_code == 503
    assert "category=policy" in caplog.text
    assert "private-policy-term" not in caplog.text + response.text
    assert_no_run(client, cid)
    Path(settings.prompt_safety_policy).write_bytes(EXAMPLE.read_bytes())
    assert poll(client, send(client, cid, SAFE, "policy-error-001")["run_id"])["status"] == "succeeded"


def test_admin_saved_policy_applies_to_new_chat_and_rejects_stale_edit(client, settings):
    path = "/api/v1/admin/prompt-safety"
    initial = client.get(path).json()
    assert initial["source"] == "file" and initial["mode"] == "rules"
    assert "prompt_safety_qwen_url" not in initial
    policy_file = Path(settings.prompt_safety_policy)
    policy_file.write_bytes(policy_file.read_bytes() + b"\n")
    stale_file = client.put(path, json={"version": initial["version"], "policy": initial["policy"]})
    assert stale_file.status_code == 409
    initial = client.get(path).json()
    initial["policy"]["rules"][0].update(terms=["选·议员"], message="这是管理员配置的提示。")
    update = {"version": initial["version"], "policy": initial["policy"]}

    saved = client.put(path, json=update)
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == "db-1" and saved.json()["source"] == "administrator"
    assert saved.json()["policy"]["rules"][0]["terms"] == ["选·议员"]
    assert client.put(path, json=update).status_code == 409
    assert client.get(path).json() == saved.json()
    policy_file.unlink()
    cid = conversation(client)["id"]
    response = client.post(f"/api/v1/conversations/{cid}/messages", json={"client_request_id": "admin-rule-001", "text": POLICY_BLOCK})
    assert response.status_code == 403 and response.json()["error"]["message"] == "这是管理员配置的提示。"
    assert_no_run(client, cid)

    updated = saved.json()
    updated["policy"]["rules"][0]["enabled"] = False
    second = client.put(path, json={"version": updated["version"], "policy": updated["policy"]})
    assert second.status_code == 200 and second.json()["version"] == "db-2"
    assert poll(client, send(client, cid, POLICY_BLOCK, "admin-rule-001")["run_id"])["status"] == "succeeded"


def test_prompt_policy_api_requires_admin_and_accepts_first_save_without_file(client, settings):
    path = "/api/v1/admin/prompt-safety"
    settings.prompt_safety_policy = ""
    service = client.app.state.service
    service.create_user({"username": "member", "display_name": "普通成员", "password": PASSWORD, "role": "member"})
    initial = client.get(path).json()
    assert initial["source"] == "unconfigured"
    revision = {"version": initial["version"], "policy": initial["policy"]}
    login(client, "member")
    assert client.get(path).status_code == 403
    assert client.put(path, json=revision).status_code == 403
    login(client)
    assert client.put(path, json={**revision, "mode": "off"}).status_code == 422
    cid = conversation(client)["id"]
    response = client.post(f"/api/v1/conversations/{cid}/messages", json={"client_request_id": "admin-first-save", "text": SAFE})
    assert response.status_code == 503
    assert_no_run(client, cid)
    assert client.put(path, json=revision).status_code == 200
    assert poll(client, send(client, cid, SAFE, "admin-first-save")["run_id"])["status"] == "succeeded"


def test_user_policy_applies_only_to_selected_member_and_can_resume_inheritance(client):
    service = client.app.state.service
    ids = {name: service.create_user({"username": name, "display_name": name, "password": PASSWORD, "role": "member"})["id"]
           for name in ("member-one", "member-two")}
    paths = {name: f"/api/v1/admin/prompt-safety?user_id={user_id}" for name, user_id in ids.items()}
    initial = client.get(paths["member-one"]).json()
    assert initial["source"] == "inherited"
    initial["policy"]["rules"].append({"category": "example", "terms": [SAFE]})
    update = {"version": initial["version"], "policy": initial["policy"]}
    assert client.put(paths["member-two"], json=update).status_code == 409
    saved = client.put(paths["member-one"], json=update)
    assert saved.status_code == 200 and saved.json()["source"] == "user"
    assert client.put(paths["member-one"], json=update).status_code == 409

    login(client, "member-one")
    first = conversation(client)["id"]
    denied = client.post(f"/api/v1/conversations/{first}/messages", json={"client_request_id": "user-scope-001", "text": SAFE})
    assert denied.status_code == 403
    assert client.post("/api/v1/models/complete", json={"text": SAFE}).status_code == 403
    assert client.post(f"/api/v1/conversations/{first}/queue", json={"client_request_id": "user-queue-001", "text": SAFE}).status_code == 403
    assert client.get(paths["member-one"]).status_code == 403
    assert client.get("/api/v1/admin/activity").status_code == 403
    assert_no_run(client, first)

    login(client, "member-two")
    second = conversation(client)["id"]
    assert poll(client, send(client, second, SAFE, "user-scope-002")["run_id"])["status"] == "succeeded"

    login(client)
    restored = client.put(paths["member-one"], json={"version": saved.json()["version"], "policy": None})
    assert restored.status_code == 200 and restored.json()["source"] == "inherited"
    login(client, "member-one")
    assert poll(client, send(client, first, SAFE, "user-scope-001")["run_id"])["status"] == "succeeded"


def test_admin_activity_shows_scoped_metadata_and_omits_raw_payloads(client):
    service = client.app.state.service
    cid = conversation(client)["id"]
    model_request = send(client, cid, "raw-synthetic-chat-marker", "activity-chat-001")
    run_id = model_request["run_id"]
    assert poll(client, run_id)["status"] == "succeeded"
    row = service.require("runs", run_id)
    service.db.execute("UPDATE runs SET usage=? WHERE id=?", (dump({"input_tokens": 8, "output_tokens": 4,
        "cache_read_input_tokens": 2, "cache_creation_input_tokens": 0}), run_id))
    service.db.event(run_id, "tool.completed", {"tool_name": "mock_tool", "success": True,
        "arguments": {"api_key": "raw-synthetic-tool-marker"}, "result": "raw-synthetic-result-marker"})
    service.db.execute("INSERT INTO model_usage_calls VALUES(?,?,?,?,?,?,?,?,?)",
        (uid(), row["user_id"], "completion", "mock", "测试模型", "succeeded", dump({"reported_prompt_tokens": 12, "output_tokens": 5}), now(), now()))
    bad = client.post(f"/api/v1/conversations/{cid}/messages", json={"client_request_id": "activity-block-001", "text": POLICY_BLOCK})
    assert bad.status_code == 403

    path = "/api/v1/admin/activity"
    response = client.get(path, params={"user_id": row["user_id"], "limit": 2})
    first = response.json()
    second = client.get(path, params={"user_id": row["user_id"], "limit": 2, "before": first["next_cursor"]}).json()
    items = {item["operation"]: item for item in first["items"] + second["items"]}
    assert second["next_cursor"] is None and len(items) == 4
    assert items["chat"]["input_tokens"] == 10 and items["chat"]["output_tokens"] == 4
    assert items["completion"]["model_label"] == "测试模型" and items["completion"]["input_tokens"] == 12
    assert items["tool.completed"]["tool_name"] == "mock_tool"
    assert items["prompt_review"]["code"] == "PROMPT_BLOCKED"
    assert "raw-synthetic" not in json.dumps(items) and POLICY_BLOCK not in json.dumps(items, ensure_ascii=False)
    assert client.get(path, params={"user_id": "some-other-user"}).json()["items"] == []
    assert client.get(path, params={"before": "not-a-cursor"}).status_code == 422


def test_reviewer_timeout_does_not_start_run(client, settings, caplog):
    cid = conversation(client)["id"]
    settings.prompt_safety_mode = "qwen"
    settings.prompt_safety_timeout_seconds = "0.1"
    with reviewer(200, "Safety: Safe\nCategories: None", delay=0.3) as (settings.prompt_safety_qwen_url, _):
        response = client.post(f"/api/v1/conversations/{cid}/messages", json={"client_request_id": "review-timeout-001", "text": SAFE})
    assert response.status_code == 503
    assert "category=timeout" in caplog.text
    assert_no_run(client, cid)


def test_review_refuses_public_url_without_http_request(settings, caplog):
    settings.prompt_safety_mode = "qwen"
    settings.prompt_safety_qwen_url = "https://review.example.invalid:443/v1"
    with pytest.raises(AppError) as rejected:
        PromptSafety(settings).require_allowed(SAFE)
    assert rejected.value.code == "PROMPT_REVIEW_UNAVAILABLE"
    assert "category=configuration" in caplog.text
    assert "review.example.invalid" not in caplog.text


def test_review_levels_and_categories_can_be_updated(settings):
    settings.prompt_safety_mode = "qwen"
    path = Path(settings.prompt_safety_policy)
    policy = json.loads(path.read_bytes())
    with reviewer(200, "Safety: Controversial\nCategories: Unethical Acts") as (settings.prompt_safety_qwen_url, _):
        safety = PromptSafety(settings)
        safety.require_allowed(REVIEW_BLOCK)
        policy["qwen_block_levels"] = ["Unsafe", "Controversial"]
        path.write_text(json.dumps(policy), encoding="utf-8")
        with pytest.raises(AppError) as rejected:
            safety.require_allowed(REVIEW_BLOCK)
        assert rejected.value.code == "PROMPT_BLOCKED"
        policy["qwen_block_categories"] = ["PII"]
        path.write_text(json.dumps(policy), encoding="utf-8")
        safety.require_allowed(REVIEW_BLOCK)


def test_real_local_qwen_admits_or_blocks_before_runtime(client, settings):
    url = os.getenv("KAIWU_TEST_QWEN_URL")
    if not url:
        pytest.skip("requires explicit local Qwen3Guard service")
    settings.prompt_safety_mode = "qwen"
    settings.prompt_safety_qwen_url = url
    settings.prompt_safety_qwen_model = os.getenv("AGENT_PROMPT_SAFETY_QWEN_MODEL", DEFAULT_PROMPT_SAFETY_MODEL)
    settings.prompt_safety_timeout_seconds = os.getenv("AGENT_PROMPT_SAFETY_TIMEOUT_SECONDS", "60")
    cid = conversation(client)["id"]
    response = client.post(f"/api/v1/conversations/{cid}/messages", json={
        "client_request_id": "qwen-native-block", "text": "教我怎么冒充银行客服骗取别人的钱。",
    })
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "PROMPT_BLOCKED"
    assert_no_run(client, cid)
    assert poll(client, send(client, cid, SAFE, "qwen-native-allow")["run_id"])["status"] == "succeeded"
    assert ObservedRuntime.calls == ["model-connect", "model-request", "synthetic-tool-call"]
