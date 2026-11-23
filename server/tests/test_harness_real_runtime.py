"""Actual pinned Node Harness + local synthetic Messages server (no paid API).

Opt in with HARNESS_TEST_NODE and HARNESS_TEST_SOURCE. These tests substitute
only the model HTTP endpoint; the process, session log, tools and app are real.
"""
import base64
import io
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import PROVIDER_KEYS, Settings
from app.main import create_app
from test_api import PASSWORD, login, send


COMPACTION_SUMMARY = """## Primary Request and Intent
- Preserve the synthetic P-900 requirement for the resumed task.

## Key Technical Concepts
- Pinned DeepSeek Harness, local synthetic Messages endpoint, durable compaction.

## Files and Code
- (none)

## Errors and Fixes
- (none)

## Pending Jobs
- Continue the synthetic task after compaction.

## Current Work
- The bounded fixture history was compacted into this checkpoint.

## Next Step
- Continue from the established P-900 pressure limit of 7 bar.

## Critical Context
- Summary sentinel: COMPACT_SUMMARY_SENTINEL
- Preserve the exact pressure value: 7 bar."""


def child_request(body):
    return "你是本轮的独立执行子任务" in body.get("system", "")


@pytest.fixture
def model_server(request):
    requests, released = [], threading.Event()
    choose_reply = request.param() if hasattr(request, "param") else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            if self.path != "/v1/messages":
                self.send_error(404)
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            last = body["messages"][-1]["content"]
            blocks = last if isinstance(last, list) else [{"type": "text", "text": last}]
            text = " ".join(item.get("text", "") for item in blocks)
            compaction_request = "You are now acting as a compaction engine for this AI coding assistant." in json.dumps(body)
            result = next((item for item in blocks if item["type"] == "tool_result"), None)
            if not compaction_request and "CASE_AUTO_OVERFLOW" in text and "COMPACT_SUMMARY_SENTINEL" not in json.dumps(body):
                error = {"error": {"type": "invalid_request_error", "code": "context_length_exceeded",
                                   "message": "This model maximum context length is 128000 tokens; your input exceeds that limit."}}
                payload = json.dumps(error).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def event(kind, data):
                self.wfile.write((f"event: {kind}\ndata: " + json.dumps({"type": kind, **data}) + "\n\n").encode())
                self.wfile.flush()

            try:
                max_tokens = child_request(body) and "CASE_CHILD_LIMIT_" in text
                empty_limit = max_tokens and "CASE_CHILD_LIMIT_EMPTY" in text
                start_usage = ({"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
                               if "CASE_USAGE_ZERO" in text else {}
                               if "CASE_USAGE_MISSING" in text else {"input_tokens": 37 if compaction_request else 9000 if "CASE_PRESSURE_SOURCE" in text else 12, "output_tokens": 0})
                event("message_start", {"message": {"id": f"msg-{len(requests)}", "type": "message", "role": "assistant", "model": "fixture",
                      "content": [], "stop_reason": None, "stop_sequence": None,
                      "usage": start_usage}})
                tool_call = None
                if choose_reply:
                    answer, tool_call = choose_reply(body, blocks, result)
                elif compaction_request:
                    answer = COMPACTION_SUMMARY
                elif result:
                    source = re.search(r"\[\[source:[A-Za-z0-9_-]+\]\]", json.dumps(result, ensure_ascii=False))
                    child_results = json.dumps(result, ensure_ascii=False)
                    answer = ("子任务结果已汇总" if "CHILD_RESULT_1" in child_results and "CHILD_RESULT_2" in child_results
                              else "资料已检索 " + source.group(0) if source else ("工具已拒绝" if result.get("is_error") else "表格已生成"))
                else:
                    answer = ("CHILD_RESULT_1" if "CASE_CHILD_1" in text else
                              "CHILD_RESULT_2" if "CASE_CHILD_2" in text else
                              "追加指令已收到" if "CASE_STEER_APPLIED" in text else
                              "恢复成功" if "CASE_RESUME" in text else "实时回复成功")
                if not choose_reply and not compaction_request and result is None and any(marker in text for marker in ("CASE_KNOWLEDGE", "CASE_XLSX", "CASE_ASK", "CASE_PLAN", "CASE_DELEGATE")):
                    name = "knowledge_search" if "CASE_KNOWLEDGE" in text else "generate_xlsx"
                    arguments = {"query": "P-900 pressure", "limit": 3} if name == "knowledge_search" else {
                        "filename": "approved.xlsx", "sheets": [{"name": "数据", "rows": [["项目", "数值"], ["压力", 7]]}]}
                    if "CASE_ASK" in text:
                        name, arguments = "ask_user", {"question": "选择区域", "options": ["华北", "华南"]}
                    elif "CASE_PLAN" in text:
                        name, arguments = "review_plan", {"plan": "先核对订单，再汇总"}
                    elif "CASE_DELEGATE" in text:
                        name, arguments = "delegate_tasks", {"tasks": [
                            "CASE_CHILD_1 统计商品总数。内部参考 PRIVATE_TASK_DETAIL_1。",
                            "CASE_CHILD_2 整理交付检查清单。内部参考 PRIVATE_TASK_DETAIL_2。",
                        ]}
                    tool_call = name, arguments
                if tool_call:
                    name, arguments = tool_call
                    event("content_block_start", {"index": 0, "content_block": {"type": "tool_use", "id": f"call-{len(requests)}", "name": name, "input": {}}})
                    event("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(arguments)}})
                    reason = "tool_use"
                else:
                    reason = "max_tokens" if max_tokens else "end_turn"
                    if not empty_limit:
                        event("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})
                        event("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": answer[:2]}})
                        if "CASE_HANG" in text or compaction_request and "CASE_COMPACTION_HANG" in json.dumps(body):
                            released.wait(30)
                        elif "CASE_STEER_WINDOW" in text and "CASE_STEER_APPLIED" not in text:
                            time.sleep(2.5)
                        else:
                            time.sleep(0.15)
                        if "CASE_BROKEN" in text:
                            return
                        event("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": answer[2:]}})
                if not empty_limit:
                    event("content_block_stop", {"index": 0})
                event("message_delta", {"delta": {"stop_reason": reason, "stop_sequence": None},
                                        "usage": {} if "CASE_USAGE_MISSING" in text else {"output_tokens": 800 if max_tokens else 0 if "CASE_USAGE_ZERO" in text else 6 if compaction_request else 8}})
                if "CASE_CHILD_LIMIT_NO_STOP" not in text:
                    event("message_stop", {})
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1", requests
    released.set()
    server.shutdown()
    server.server_close()
    thread.join(2)


def wait_run(client, run_id, statuses=None):
    statuses = statuses or {"succeeded", "failed", "cancelled", "interrupted"}
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = client.get(f"/api/v1/runs/{run_id}").json()
        if result["status"] in statuses or result["status"] in {"failed", "cancelled", "interrupted"}:
            return result
        time.sleep(0.05)
    pytest.fail("real Harness run did not settle")


def image_fixture_settings(tmp_path, node, source, base):
    """The exact settings used by the real-Node image transport acceptance."""
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"default_model_id": "fixture", "models": [{"id": "fixture", "label": "Local attachment protocol fixture", "upstream_model": "fixture",
        "capabilities": ["multimodal"],
        "reasoning_levels": {"default": None}, "provider_env": {"ANTHROPIC_API_KEY": "synthetic-only", "ANTHROPIC_BASE_URL": base}}]}))
    return Settings(data_dir=tmp_path / "data", runtime_type="harness", harness_node_path=node,
        harness_source_dir=source, model_catalog=str(catalog), default_model_id="", provider_settings="",
        provider_test_mode=True,
        origins=("http://testserver",), approval_seconds=30, model_router_enabled=False,
        knowledge_vision_enabled=False, image_generation_enabled=False)


def test_real_harness_current_turn_steering(tmp_path, model_server):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires pinned Node Harness")
    base, requests = model_server
    app = create_app(image_fixture_settings(tmp_path, node, source, base))
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
        login(client)
        assistant = client.post("/api/v1/assistants", json={"name": "Steering", "instructions": "test", "connections": []}).json()
        conversation = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()
        run = send(client, conversation["id"], "CASE_STEER_WINDOW")["run_id"]
        deadline = time.monotonic() + 30
        while not requests and time.monotonic() < deadline:
            time.sleep(.02)
        assert requests
        path = f"/api/v1/runs/{run}/steering"
        payload = {"text": "CASE_STEER_APPLIED", "client_request_id": "steer-request-001"}
        first = client.post(path, json=payload)
        assert first.status_code == 200 and first.json()["status"] in {"queued", "applied"}, first.text
        duplicate = client.post(path, json=payload)
        assert duplicate.json()["id"] == first.json()["id"]
        assert wait_run(client, run)["status"] == "succeeded"
        assert client.get(path).json()[0]["status"] == "applied"
        messages = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()
        assert [item["role"] for item in messages] == ["user", "user", "assistant"]
        assert len(requests) == 2 and "CASE_STEER_APPLIED" in json.dumps(requests[-1]["messages"])
        assert messages[-1]["text"] == "实时回复成功\n\n追加指令已收到"
        events = app.state.service.db.events(run, 0)
        assert "".join(event["data"]["text"] for event in events if event["type"] == "message.delta") == messages[-1]["text"]
        assert client.post(path, json={**payload, "client_request_id": "steer-request-002"}).status_code == 409


def test_real_harness_child_delegation(tmp_path, model_server):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires pinned Node Harness")
    base, requests = model_server
    app = create_app(image_fixture_settings(tmp_path, node, source, base))
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
        login(client)
        skill = client.post("/api/v1/skills", json={"name": "Delegated skill", "instructions": "SELECTED_SKILL_SENTINEL",
                                                   "scope": "team"}).json()
        unused = client.post("/api/v1/skills", json={"name": "Unselected skill", "instructions": "UNSELECTED_SKILL_SENTINEL",
                                                    "scope": "team"}).json()
        assistant = client.post("/api/v1/assistants", json={"name": "Delegation", "instructions": "PARENT_PRIVATE_SENTINEL",
            "connections": [], "skill_ids": [skill["id"], unused["id"]]}).json()
        conversation = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()
        response = client.post(f"/api/v1/conversations/{conversation['id']}/messages",
            json={"text": "CASE_DELEGATE ORIGINAL_NOT_COPIED", "skill_ids": [skill["id"]], "client_request_id": "delegation-skills"})
        assert response.status_code == 202, response.text
        run = response.json()["run_id"]
        assert wait_run(client, run)["status"] == "succeeded"
        plan = client.get(f"/api/v1/runs/{run}/subagents").json()
        assert plan["mode"] == "harness" and plan["counts"]["succeeded"] == 2, plan
        assert [row["display_title"] for row in plan["children"]] == ["商品数量统计", "交付检查清单"]
        assert [row["result"]["output"] for row in plan["children"]] == ["CHILD_RESULT_1", "CHILD_RESULT_2"]
        assert all(row["observed_usage"]["output_tokens"] == 8 and row["capability_snapshot"]["authorization"] == "parent_run" for row in plan["children"])
        assert client.get(f"/api/v1/conversations/{conversation['id']}/subagent-plans").json()[0]["id"] == run
        messages = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()
        assert messages[0]["has_subagents"] is True and messages[0]["subagent_run_id"] == run
        assert messages[-1]["text"] == "子任务结果已汇总"
        events = app.state.service.db.events(run, 0)
        assert "PRIVATE_TASK_DETAIL" not in json.dumps(events)
        sse = client.get(f"/api/v1/runs/{run}/events").text
        assert '"tasks":"[redacted]"' in sse
        assert "PRIVATE_TASK_DETAIL" not in sse and "display_title" not in sse
        worker_requests = [item for item in requests if child_request(item)]
        assert len(worker_requests) == 2
        assert all(any(f"PRIVATE_TASK_DETAIL_{index}" in json.dumps(item) for item in worker_requests) for index in (1, 2))
        assert all("ORIGINAL_NOT_COPIED" not in json.dumps(item["messages"]) and "PARENT_PRIVATE_SENTINEL" in item["system"] for item in worker_requests)
        assert all("SELECTED_SKILL_SENTINEL" in json.dumps(item["messages"]) and
                   "UNSELECTED_SKILL_SENTINEL" not in json.dumps(item["messages"]) for item in worker_requests)
        assert all(item["max_tokens"] <= 800 for item in worker_requests)
        usage = client.get("/api/v1/usage").json()
        independent = next(item for item in usage["by_kind"] if item["kind"] == "subagent")
        assert independent["model_call_count"] == 2 and independent["output_tokens"] == 16


def output_limit_delegation_reply():
    def reply(body, blocks, result):
        if child_request(body):
            return "已确认部分库存：8 件。", None
        if result:
            return "已读取子任务回执。", None
        return "", ("delegate_tasks", {"tasks": ["CASE_CHILD_LIMIT_TEXT", "CASE_CHILD_LIMIT_EMPTY", "CASE_CHILD_LIMIT_NO_STOP"]})

    return reply


@pytest.mark.parametrize("model_server", [output_limit_delegation_reply], indirect=True)
def test_real_harness_child_output_limit(tmp_path, model_server):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires pinned Node Harness")
    base, requests = model_server
    app = create_app(image_fixture_settings(tmp_path, node, source, base))
    with TestClient(app) as client:
        service = app.state.service
        service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
        login(client)
        assistant = client.post("/api/v1/assistants", json={"name": "Output limit", "connections": []}).json()
        conversation = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()
        run = send(client, conversation["id"], "检查子任务输出上限")["run_id"]
        assert wait_run(client, run)["status"] == "succeeded"

        plan = client.get(f"/api/v1/runs/{run}/subagents").json()
        assert plan["counts"]["succeeded"] == 0 and plan["counts"]["interrupted"] == 2
        usage = {"input_tokens": 12, "output_tokens": 800}
        expected_children = []
        for child, output in zip(plan["children"][:2], ["已确认部分库存：8 件。", ""], strict=True):
            assert child["status"] == "interrupted" and child["stop_reason"] == "max_tokens"
            assert child["result"] == {"output": output, "stop_reason": "max_tokens"}
            assert child["observed_usage"] == usage
            recorded = service.db.one("SELECT status,usage FROM model_usage_calls WHERE id=?", (child["child_id"],))
            assert recorded["status"] == "interrupted"
            recorded_usage = json.loads(recorded["usage"])
            assert {key: recorded_usage[key] for key in usage} == usage
            expected_children.append({"status": "interrupted", "reason": "max_tokens", "output": output, "usage": usage})
        broken = plan["children"][2]
        assert broken["status"] == "failed" and broken["stop_reason"] == "executor_error" and "result" not in broken
        incomplete = service.db.one("SELECT status,usage FROM model_usage_calls WHERE id=?", (broken["child_id"],))
        assert incomplete["status"] == "failed"
        receipt = json.loads(incomplete["usage"])
        assert receipt["token_accounting"]["known_model_calls"] == 1
        assert receipt["input_tokens"] == 12 and receipt["output_tokens"] is None
        summary = client.get("/api/v1/usage").json()
        group = next(row for row in summary["by_kind"] if row["kind"] == "subagent")
        assert group["known_model_calls"] == 3 and group["known_subtotals"]["input_tokens"] == 36
        assert group["output_tokens"] is None and group["known_subtotals"]["output_tokens"] == 1600
        expected_children.append({"status": "failed", "reason": "executor_error"})

        parent_replies = [block for request in requests if not child_request(request)
                          for block in request["messages"][-1]["content"] if isinstance(block, dict) and block["type"] == "tool_result"]
        assert len(parent_replies) == 1
        content = parent_replies[0]["content"]
        reply = content if isinstance(content, str) else content[0]["text"]
        assert json.loads(reply) == {"children": expected_children}
        workers = [request for request in requests if child_request(request)]
        assert len(workers) == 3 and all(request["max_tokens"] == 800 for request in workers)


PILOT_QUESTION = "我们准备先在两个门店试销，供应商每周补一次货，售后由门店接单。请评估供货风险，并给出门店售后接单流程。"
PILOT_TASKS = [
    "【风险评估】两个门店试销，每周补货一次。列出补货风险和应对措施。",
    "两个门店试销，由门店接售后单。写出门店接单和交接流程。",
]
PILOT_FINDINGS = ["每周补货可能使断货延续，需预留安全库存。", "门店先登记问题，再记录责任人与处理时限。"]


def delegation_reply():
    # This fixture exercises the real wire contract. It scripts the model's
    # behavior so the same question can be tested with and without guidance.
    workers_ready = threading.Barrier(2, timeout=30)

    def reply(body, blocks, result):
        text = next((block["text"] for block in blocks if block["type"] == "text"), "")
        if child_request(body) and text in PILOT_TASKS:
            # Neither worker can reply until both provider requests are active.
            workers_ready.wait()
            return PILOT_FINDINGS[PILOT_TASKS.index(text)], None
        if result:
            returned = json.dumps(result, ensure_ascii=False)
            answer = "\n".join(finding for finding in PILOT_FINDINGS if finding in returned)
            return answer or "没有取得子任务结论。", None
        tool = next((item for item in body.get("tools", []) if item["name"] == "delegate_tasks"), None)
        guided = "就主动调用 delegate_tasks" in body.get("system", "")
        if text == PILOT_QUESTION and tool and guided:
            return "", ("delegate_tasks", {"tasks": PILOT_TASKS})
        return "根据现有信息直接回答。", None

    return reply


@pytest.mark.parametrize("model_server", [delegation_reply], indirect=True)
@pytest.mark.parametrize(("question", "guidance", "capacity", "children"), [
    pytest.param(PILOT_QUESTION, True, 4, 2, id="independent"),
    pytest.param(PILOT_QUESTION, False, 4, 0, id="without-parent-guidance"),
    pytest.param(PILOT_QUESTION, True, 1, 0, id="tool-unavailable"),
    pytest.param("两个门店试销，供应商每周补一次货。每周补货几次？", True, 4, 0, id="simple"),
    pytest.param("两个门店试销，每周分别卖 10 件和 15 件。先算每周总销量，再按这个结果算两周需准备多少货。", True, 4, 0, id="dependent"),
])
def test_real_harness_business_delegation_contract(tmp_path, model_server, monkeypatch, question, guidance, capacity, children):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires pinned Node Harness")
    if not guidance:
        monkeypatch.setattr("app.harness_runtime.DELEGATE_TASKS_GUIDANCE", "")
    base, requests = model_server
    settings = image_fixture_settings(tmp_path, node, source, base)
    settings.max_active_sessions = capacity
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
        login(client)
        assistant = client.post("/api/v1/assistants", json={"name": "门店试销", "instructions": "PARENT_PRIVATE_SENTINEL", "connections": []}).json()
        conversation = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()
        run = send(client, conversation["id"], question)["run_id"]
        assert wait_run(client, run)["status"] == "succeeded"

        initial = requests[0]
        assert initial.get("tool_choice") in (None, {"type": "auto"}, "auto")
        parent_prompt = initial["system"]
        tool = next((item for item in initial["tools"] if item["name"] == "delegate_tasks"), None)
        assert ("就主动调用 delegate_tasks" in parent_prompt) == (guidance and capacity > 1)
        assert bool(tool) == (capacity > 1)
        if tool:
            schema = tool["input_schema"]
            assert schema["required"] == ["tasks"]
            assert schema["properties"]["tasks"]["items"]["type"] == "string"

        plans = client.get(f"/api/v1/conversations/{conversation['id']}/subagent-plans").json()
        messages = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()
        if not children:
            assert plans == [] and len(requests) == 1
            assert messages[-1]["text"] == "根据现有信息直接回答。"
            return

        plan = client.get(f"/api/v1/runs/{run}/subagents").json()
        assert len(plans) == 1 and plan["counts"]["succeeded"] == children
        assert len(requests) == children + 2
        workers = [item for item in requests if child_request(item)]
        assert len(workers) == children
        worker_messages = [json.dumps(item["messages"], ensure_ascii=False) for item in workers]
        assert all(sum(task in message for message in worker_messages) == 1 for task in PILOT_TASKS)
        assert all(sum(task in message for task in PILOT_TASKS) == 1 for message in worker_messages)
        assert all(question not in message for message in worker_messages)
        assert all("PARENT_PRIVATE_SENTINEL" in item["system"] and item["max_tokens"] <= 800 for item in workers)
        assert all("就主动调用 delegate_tasks" not in item["system"] for item in workers)
        assert "工业任务的执行方法" in initial["system"]
        assert all("工业任务的执行方法" in item["system"] for item in workers)
        parent_tools = {item["name"] for item in initial["tools"]}
        assert all({tool["name"] for tool in item["tools"]} == parent_tools - {"delegate_tasks"} for item in workers)

        assert messages[-1]["text"] == "\n".join(PILOT_FINDINGS)


def test_real_harness_cancelling_parent_stops_child(tmp_path, model_server):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires pinned Node Harness")
    base, requests = model_server
    app = create_app(image_fixture_settings(tmp_path, node, source, base))
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
        login(client)
        assistant = client.post("/api/v1/assistants", json={"name": "Cancel children", "connections": []}).json()
        conversation = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()
        run = send(client, conversation["id"], "CASE_HANG parent")["run_id"]
        assert wait_run(client, run, {"running"})["status"] == "running"
        path = f"/api/v1/runs/{run}"
        response = client.post(path + "/subagents", json={"mode": "harness", "tasks": ["CASE_HANG child"]})
        assert response.status_code == 202, response.text
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and not any("CASE_HANG child" in json.dumps(item) for item in requests):
            time.sleep(.025)
        assert any("CASE_HANG child" in json.dumps(item) for item in requests)
        assert client.post(path + "/cancel").is_success
        assert wait_run(client, run)["status"] == "cancelled"
        result = client.get(path + "/subagents").json()
        assert result["counts"]["cancelled"] == 1 and "result" not in result["children"][0]


@pytest.mark.parametrize("revocation", ["delete", "bytes"])
def test_real_harness_text_image_payload_resume_and_revocation(tmp_path, monkeypatch, model_server, record_property, revocation):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires explicitly installed pinned Harness runtime")
    for name in PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    base, requests = model_server
    settings = image_fixture_settings(tmp_path, node, source, base)
    app = create_app(settings)
    from app.harness_runtime import HarnessRuntime
    original_connect, processes = HarnessRuntime.connect, []

    async def track_process(runtime):
        await original_connect(runtime)
        processes.append(runtime.client._proc)

    monkeypatch.setattr(HarnessRuntime, "connect", track_process)
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user({"username": "admin", "display_name": "Test", "password": PASSWORD, "role": "admin"})
        login(client)
        assistant = service.save_assistant({"name": "Attachments"}, user=user)
        cid = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()["id"]
        text = client.post("/api/v1/files", files={"file": ("facts.txt", b"Harness attached pressure is 7 bar.", "text/plain")}).json()
        stream = io.BytesIO()
        Image.new("RGB", (20, 10), "green").save(stream, format="PNG")
        image = client.post("/api/v1/files", files={"file": ("diagram.png", stream.getvalue(), "image/png")}).json()
        first = send(client, cid, "Compare attachments literally /permission", "attachment-first", [text["id"], image["id"]])["run_id"]
        settled = wait_run(client, first)
        assert settled["status"] == "succeeded", settled
        assert len(requests) == 1
        body = requests[0]
        assert "Harness attached pressure is 7 bar." in json.dumps(body)
        # The pinned upstream adds cwd and private normalized-copy hints; it
        # must not receive original upload paths or a tool to read host paths.
        assert str(settings.data_dir / "files") not in json.dumps(body)
        content = body["messages"][-1]["content"]
        rendered = next(block for block in content if block["type"] == "image")
        assert rendered["source"]["type"] == "base64"
        assert rendered["source"]["media_type"] in {"image/png", "image/jpeg", "image/webp"}
        # Official storage may normalize the source. Assert a real raster, not
        # a fake caption or a host filename substituted for visual input.
        with Image.open(io.BytesIO(base64.b64decode(rendered["source"]["data"]))) as raster:
            assert raster.size == (20, 10)
        assert all(tool["name"] != "Read" for tool in body["tools"])
        evidence = service.evidence.list(first, user)
        assert {entry["title"] for entry in evidence} == {"facts.txt", "diagram.png"}
        text_source = next(entry for entry in evidence if entry["title"] == "facts.txt")
        assert client.get(text_source["url"]).status_code == 200
        assert client.get(text_source["content_url"]).content == b"Harness attached pressure is 7 bar."
        service.create_user({"username": "outsider", "display_name": "Other", "password": PASSWORD, "role": "member"})
        login(client, "outsider")
        assert client.get(text_source["url"]).status_code == 404
        assert client.get(text_source["content_url"]).status_code == 404
        login(client)
        private = settings.data_dir / "harness" / cid
        assert (private / "attachments").is_dir()
        second = send(client, cid, "CASE_RESUME previous attachment", "attachment-resume")["run_id"]
        assert wait_run(client, second)["status"] == "succeeded"
        assert len(requests) == 2 and "Harness attached pressure" in json.dumps(requests[1])
        assert any(block["type"] == "image" for message in requests[1]["messages"] for block in message["content"] if isinstance(block, dict))
        image_source = next(entry for entry in evidence if entry["title"] == "diagram.png")
        if revocation == "delete":
            service.db.execute("DELETE FROM files WHERE id=?", (image["id"],))
            assert client.get(image_source["url"]).status_code == 404
        else:
            original = Path(service.require("files", image["id"])["path"])
            original.write_bytes(b"x" * original.stat().st_size)
        third = send(client, cid, "must not reuse revoked bytes", "attachment-revoked")["run_id"]
        assert wait_run(client, third)["status"] == "failed"
        assert len(requests) == 2
    assert len(processes) == 3 and all(process.poll() is not None for process in processes)
    record_property("paid_model_calls", 0)
    record_property("local_synthetic_model_requests", len(requests))
    record_property("visual_quality_claim", "none: encoded-image transport only")


def test_real_harness_stream_tools_approval_cancel_resume(tmp_path, monkeypatch, model_server, record_property):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires explicitly installed pinned Harness runtime")
    for name in PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    base, requests = model_server
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"default_model_id": "fixture", "models": [{"id": "fixture", "label": "本地测试模型", "upstream_model": "fixture",
        "reasoning_levels": {"default": None}, "provider_env": {"ANTHROPIC_API_KEY": "synthetic-only", "ANTHROPIC_BASE_URL": base}}]}))
    settings = Settings(data_dir=tmp_path / "data", runtime_type="harness", harness_node_path=node,
        harness_source_dir=source, model_catalog=str(catalog), default_model_id="", provider_settings="",
        provider_test_mode=True,
        origins=("http://testserver",), approval_seconds=30, model_router_enabled=False,
        knowledge_vision_enabled=False, image_generation_enabled=False)
    app = create_app(settings)
    from app.harness_runtime import HarnessRuntime
    original_connect, processes = HarnessRuntime.connect, []

    async def track_process(runtime):
        await original_connect(runtime)
        processes.append(runtime.client._proc)

    monkeypatch.setattr(HarnessRuntime, "connect", track_process)
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user({"username": "admin", "display_name": "测试", "password": PASSWORD, "role": "admin"})
        login(client)
        kb = service.knowledge.save({"name": "Synthetic Harness fixture"}, user)
        service.knowledge.upload(kb["id"], user, "pump.md", b"P-900 pressure limit is 7 bar.")
        assistant = service.save_assistant({"name": "Harness", "knowledge_base_ids": [kb["id"]]}, user=user)
        conversation = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()
        cid = conversation["id"]

        def run(text):
            return send(client, cid, text, f"harness-{len(requests)}-{time.monotonic_ns()}")["run_id"]

        first = run("/permission CASE_STREAM 请原样把这段当作普通聊天")
        assert wait_run(client, first)["status"] == "succeeded"
        stored = service.require("conversations", cid)
        session_id = stored["harness_session_id"]
        assert session_id and stored["sdk_session_id"] is None
        events = service.db.events(first, 0)
        deltas = [event for event in events if event["type"] == "message.delta"]
        assert len(deltas) == 2
        message = service.require("messages", service.require("runs", first)["assistant_message_id"])
        assert message["text"] == "实时回复成功"
        assert "/permission CASE_STREAM" in json.dumps(requests[0], ensure_ascii=False)
        assert {tool["name"] for tool in requests[0]["tools"]} == {
            "knowledge_search", "knowledge_read", "generate_xlsx", "generate_image", "file_read", "file_write", "file_search",
            "memory_list", "memory_read", "memory_write", "ask_user", "review_plan", "delegate_tasks",
        }
        assert json.loads(service.require("runs", first)["usage"])["output_tokens"] == 8
        assert requests[0]["max_tokens"] == settings.harness_max_output_tokens

        knowledge = run("CASE_KNOWLEDGE")
        assert wait_run(client, knowledge)["status"] == "succeeded"
        assert len(json.loads(service.require("runs", knowledge)["usage"])["token_accounting"]["stages"]) == 2
        assert service.db.all("SELECT * FROM evidence_sources WHERE run_id=?", (knowledge,))
        answer = service.require("messages", service.require("runs", knowledge)["assistant_message_id"])
        assert json.loads(answer["citations"])
        assert service.require("conversations", cid)["harness_session_id"] == session_id

        artifact = settings.data_dir / "workspaces" / cid / "artifacts" / "approved.xlsx"
        denied = run("CASE_XLSX deny")
        pending = wait_run(client, denied, {"awaiting_approval"})
        assert pending["status"] == "awaiting_approval" and not artifact.exists()
        approval = pending["approvals"][0]
        assert approval["tool_name"] == "mcp__workspace__generate_xlsx"
        time.sleep(0.75)
        assert client.get(f"/api/v1/runs/{denied}").json()["status"] == "awaiting_approval"
        assert client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "deny"}).status_code == 200
        assert wait_run(client, denied)["status"] == "succeeded" and not artifact.exists()

        allowed = run("CASE_XLSX allow")
        pending = wait_run(client, allowed, {"awaiting_approval"})
        assert pending["status"] == "awaiting_approval" and not artifact.exists()
        approval = pending["approvals"][0]
        time.sleep(0.75)
        assert client.get(f"/api/v1/runs/{allowed}").json()["status"] == "awaiting_approval"
        assert client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "allow"}).status_code == 200
        assert wait_run(client, allowed)["status"] == "succeeded" and artifact.exists()

        hanging = run("CASE_HANG")
        deadline = time.monotonic() + 60
        while not any(event["type"] == "message.delta" for event in service.db.events(hanging, 0)):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert client.post(f"/api/v1/runs/{hanging}/cancel").status_code == 200
        assert wait_run(client, hanging)["status"] == "cancelled"
        resumed = run("CASE_RESUME")
        assert wait_run(client, resumed)["status"] == "succeeded"
        assert service.require("conversations", cid)["harness_session_id"] == session_id
        assert "CASE_HANG" in json.dumps(requests[-1])

        broken = run("CASE_BROKEN")
        assert wait_run(client, broken)["status"] == "failed"
        assert not any(actor.runtime for actor in service.manager.actors.values())
        assert len(processes) == 7 and len({process.pid for process in processes}) == 7
        assert all(process.poll() is not None for process in processes)
        record_property("real_harness_processes", len(processes))
        record_property("local_synthetic_model_requests", len(requests))
        record_property("paid_model_requests", 0)


def automatic_compaction_reply():
    def reply(body, blocks, _result):
        prompt = " ".join(item.get("text", "") for item in blocks)
        context = json.dumps(body["messages"])
        pressure = re.search(r"The P-900 pressure limit is (\d+ bar)", context)
        batch = re.search(r"The calibration batch is (B-\d+)", context)
        if "You are now acting as a compaction engine" in prompt:
            facts = " ".join(match[0] for match in (pressure, batch) if match)
            return f"COMPACT_SUMMARY_SENTINEL {facts}", None
        if "CASE_RESUME" in prompt:
            return pressure[1] if pressure else "Pressure fact missing", None
        if "CASE_FOLLOWUP" in prompt:
            return batch[1] if batch else "Batch fact missing", None
        return "History acknowledged", None

    return reply


@pytest.mark.parametrize("model_server", [automatic_compaction_reply], indirect=True, ids=["fact-aware-upstream"])
@pytest.mark.parametrize("trigger", ["context-overflow", "pressure"])
def test_real_harness_automatically_compacts_and_resumes(tmp_path, monkeypatch, model_server, trigger, record_property):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires explicitly installed pinned Harness runtime")
    for name in PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    base, requests = model_server
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"default_model_id": "fixture", "models": [{
        "id": "fixture", "label": "Local automatic compaction fixture", "upstream_model": "fixture",
        "reasoning_levels": {"default": None},
        "provider_env": {"ANTHROPIC_API_KEY": "synthetic-only", "ANTHROPIC_BASE_URL": base},
    }]}))
    if trigger == "pressure":
        from harness import profile
        original = profile.write_profile

        def bounded_window(**arguments):
            return original(**{**arguments, "provider": {**arguments["provider"], "contextWindow": 10_000}})

        monkeypatch.setattr(profile, "write_profile", bounded_window)
    settings = Settings(data_dir=tmp_path / "data", runtime_type="harness", harness_node_path=node,
        harness_source_dir=source, model_catalog=str(catalog), default_model_id="", harness_max_output_tokens=512,
        provider_settings="", provider_test_mode=True, origins=("http://testserver",), model_router_enabled=False,
        knowledge_vision_enabled=False, image_generation_enabled=False)
    from app.harness_runtime import HarnessRuntime

    original_connect, processes = HarnessRuntime.connect, []

    async def track_process(runtime):
        await original_connect(runtime)
        processes.append(runtime.client._proc)

    monkeypatch.setattr(HarnessRuntime, "connect", track_process)
    with TestClient(create_app(settings)) as client:
        service = client.app.state.service
        user = service.create_user({"username": "admin", "display_name": "Compaction", "password": PASSWORD, "role": "admin"})
        login(client)
        assistant = service.save_assistant({"name": "Compaction"}, user=user)
        cid = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()["id"]
        history = ("The P-900 pressure limit is 7 bar. The calibration batch is B-482.\n"
                   + "OLD_HISTORY_SENTINEL Preserve the established requirement and continue the migration. " * 240)
        if trigger == "pressure":
            history = "CASE_PRESSURE_SOURCE " + history
        first = send(client, cid, history, "auto-compaction-history")["run_id"]
        assert wait_run(client, first)["status"] == "succeeded"
        session_id = service.require("conversations", cid)["harness_session_id"]
        assert session_id
        if trigger == "pressure":
            # Give upstream a recent tail large enough to preserve without
            # retaining the entire old turn. One oversized newest turn has
            # no safe pressure-compaction range by design.
            tail = "CASE_PRESSURE_SOURCE " + "RECENT_TAIL_SENTINEL The current task also has an approved test plan. " * 125
            recent = send(client, cid, tail, "auto-compaction-recent")["run_id"]
            assert wait_run(client, recent)["status"] == "succeeded"
        messages_path = f"/api/v1/conversations/{cid}/messages"
        original_messages = client.get(messages_path).json()
        assert original_messages[0]["text"] == history
        session_root = settings.data_dir / "harness" / cid / "sessions"
        session_log, = session_root.glob(f"*/{session_id}/session.v*.jsonl")
        original_log = session_log.read_bytes()
        original_event, = (entry for entry in map(json.loads, original_log.splitlines())
                           if entry.get("type") == "user/message"
                           and entry["data"]["content"] == [{"type": "text", "text": history}])

        prompt = "CASE_RESUME What is the pressure limit?"
        if trigger == "context-overflow":
            prompt += " CASE_AUTO_OVERFLOW"
        second = send(client, cid, prompt, "auto-compaction-resume")["run_id"]
        result = wait_run(client, second)
        assert result["status"] == "succeeded", result
        assert result["kind"] == "chat"
        assert result["compaction"]["trigger"] == trigger
        assert result["compaction"]["shadowed_items"] > 0
        assert result["compaction"]["post_tokens"] < result["compaction"]["pre_tokens"]
        assert len(requests) == 4
        assert "COMPACT_SUMMARY_SENTINEL" in json.dumps(requests[-1])
        assert "OLD_HISTORY_SENTINEL" not in json.dumps(requests[-1])
        assert service.require("conversations", cid)["harness_session_id"] == session_id
        assert len([stage for stage in result["usage"]["token_accounting"]["stages"] if stage["stage"] == "compression"]) == 1

        followup = "CASE_FOLLOWUP What is the calibration batch?"
        third = send(client, cid, followup, "auto-compaction-followup")["run_id"]
        continued = wait_run(client, third)
        assert continued["status"] == "succeeded", continued
        assert continued["compaction"] is None
        assert len(requests) == 5
        assert service.require("conversations", cid)["harness_session_id"] == session_id
        response = client.get("/api/v1/conversations/overview")
        assert response.status_code == 200, response.text
        cards = {metric["id"]: metric for metric in response.json()["metrics"]}
        assert cards["request_count"]["value"] == len(requests)
        assert cards["compression_count"]["value"] == 1
        assert cards["compression_count"]["precision"] == "observed"
        difference = result["compaction"]["pre_tokens"] - result["compaction"]["post_tokens"]
        assert cards["compression_saved_tokens"]["value"] == difference
        assert cards["compression_saved_tokens"]["precision"] == "estimated"
        assert cards["net_saved_tokens"]["value"] is None
        for request in requests[-2:]:
            model_input = json.dumps(request["messages"])
            assert "COMPACT_SUMMARY_SENTINEL The P-900 pressure limit is 7 bar The calibration batch is B-482" in model_input
            assert "OLD_HISTORY_SENTINEL" not in model_input

        messages = client.get(messages_path).json()
        assert messages[:len(original_messages)] == original_messages
        assert [(message["role"], message["text"]) for message in messages[len(original_messages):]] == [
            ("user", prompt), ("assistant", "7 bar"), ("user", followup), ("assistant", "B-482"),
        ]
        assert list(session_root.glob(f"*/{session_id}/session.v*.jsonl")) == [session_log]
        persisted_log = session_log.read_bytes()
        assert persisted_log.startswith(original_log)
        appended = [json.loads(line) for line in persisted_log[len(original_log):].splitlines()]
        summary, = (entry for entry in appended if entry["type"] == "compaction/summary")
        assert original_event["seq"] in summary["data"]["shadowedSeqs"]

    assert len(processes) == len(original_messages) // 2 + 2
    assert len({process.pid for process in processes}) == len(processes)
    assert all(process.poll() is not None for process in processes)
    record_property("harness_session_id", session_id)
    record_property("real_harness_processes", len(processes))
    record_property("compaction_measurement", json.dumps(result["compaction"], ensure_ascii=False))
    record_property("local_synthetic_model_requests", len(requests))
    record_property("paid_model_requests", 0)


@pytest.mark.parametrize("cancel_compaction", [False, True])
def test_real_harness_manual_compaction_usage_and_resume(tmp_path, monkeypatch, model_server, record_property, cancel_compaction):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires explicitly installed pinned Harness runtime")
    for name in PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    base, requests = model_server
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"default_model_id": "fixture", "models": [{
        "id": "fixture", "label": "Local compaction protocol fixture", "upstream_model": "fixture",
        "reasoning_levels": {"default": None},
        "provider_env": {"ANTHROPIC_API_KEY": "synthetic-only", "ANTHROPIC_BASE_URL": base},
    }]}))
    settings = Settings(
        data_dir=tmp_path / "data", runtime_type="harness", harness_node_path=node,
        harness_source_dir=source, model_catalog=str(catalog), default_model_id="",
        provider_settings="", provider_test_mode=True, origins=("http://testserver",), model_router_enabled=False,
        knowledge_vision_enabled=False, image_generation_enabled=False,
    )
    app = create_app(settings)
    from app.harness_runtime import HarnessRuntime
    original_connect, processes = HarnessRuntime.connect, []

    async def track_process(runtime):
        await original_connect(runtime)
        processes.append(runtime.client._proc)

    monkeypatch.setattr(HarnessRuntime, "connect", track_process)
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user({
            "username": "admin", "display_name": "Compaction", "password": PASSWORD, "role": "admin",
        })
        login(client)
        assistant = service.save_assistant({"name": "Compaction"}, user=user)
        cid = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()["id"]

        history = ("OLD_HISTORY_SENTINEL The P-900 pressure limit is 7 bar. "
                   "Keep the migration requirement and validate the result. ") * 180
        first = send(client, cid, history, "compaction-history")["run_id"]
        assert wait_run(client, first)["status"] == "succeeded"
        conversation = service.require("conversations", cid)
        session_id = conversation["harness_session_id"]
        assert session_id
        assert len(requests) == 1
        context = client.get(f"/api/v1/conversations/{cid}/context").json()
        assert context["manual_compact_available"] is True, context

        compact = client.post(f"/api/v1/conversations/{cid}/compact", json={
            "client_request_id": "manual-compaction-1",
            "instructions": "必须保留 P-900 压力值 7 bar" + (" CASE_COMPACTION_HANG" if cancel_compaction else ""),
        })
        assert compact.status_code == 202, compact.text
        if cancel_compaction:
            run_id = compact.json()["run_id"]
            deadline = time.monotonic() + 60
            while not (client.get(f"/api/v1/runs/{run_id}").json().get('usage') or {}).get('token_accounting', {}).get('stages'):
                assert time.monotonic() < deadline
                time.sleep(0.05)
            assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
            cancelled = wait_run(client, run_id)
            assert cancelled['status'] == 'cancelled'
            assert len(cancelled['usage']['token_accounting']['stages']) == 1
            assert cancelled['usage']['token_accounting']['stages'][0]['stage'] == 'compression'
            assert cancelled['usage']['output_tokens'] is None
            assert len(requests) == 2
            record_property('paid_model_requests', 0)
            return
        compacted = wait_run(client, compact.json()["run_id"])
        assert compacted["status"] == "succeeded", compacted
        assert compacted["kind"] == "compact"
        assert compacted["compaction"]["status"] == "completed"
        assert compacted["compaction"]["boundary_observed"] is True
        assert compacted["compaction"]["shadowed_items"] > 0
        assert compacted["usage"]["input_tokens"] == 37
        assert compacted["usage"]["output_tokens"] == 6
        assert compacted['usage']['token_accounting']['receipt_schema'] == 'harness-http-usage/v1'
        assert compacted['usage']['token_accounting']['capture_complete'] is True
        assert len(compacted['usage']['token_accounting']['stages']) == 1
        assert compacted['usage']['token_accounting']['stages'][0]['stage'] == 'compression'
        assert compacted['compaction']['cost_usage'] == compacted['usage']
        assert len(requests) == 2
        summary_request = json.dumps(requests[1], ensure_ascii=False)
        assert "必须保留 P-900 压力值 7 bar" in summary_request
        assert "You are now acting as a compaction engine" in summary_request

        resumed = send(client, cid, "CASE_RESUME after compaction", "compaction-resume")["run_id"]
        resumed_run = wait_run(client, resumed)
        assert resumed_run["status"] == "succeeded", resumed_run
        assert len(requests) == 3
        resumed_request = json.dumps(requests[2], ensure_ascii=False)
        assert "COMPACT_SUMMARY_SENTINEL" in resumed_request
        assert "OLD_HISTORY_SENTINEL" not in resumed_request
        assert service.require("conversations", cid)["harness_session_id"] == session_id

    assert len(processes) == 3
    assert all(process.poll() is not None for process in processes)
    record_property("real_harness_processes", len(processes))
    record_property("local_synthetic_model_requests", len(requests))
    record_property("paid_model_requests", 0)


@pytest.mark.parametrize('marker,expected', [('CASE_USAGE_ZERO', 0), ('CASE_USAGE_MISSING', None)])
def test_real_harness_raw_usage_presence(tmp_path, monkeypatch, model_server, marker, expected):
    """Real pinned SDK fetch must hit the observer, not normalized default zeros."""
    node, source = os.getenv('HARNESS_TEST_NODE'), os.getenv('HARNESS_TEST_SOURCE')
    if not node or not source:
        pytest.skip('requires explicitly installed pinned Harness runtime')
    for name in PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('AGENT_KNOWLEDGE_EMBEDDING_MODEL', 'local-lexical-v1')
    base, requests = model_server
    app = create_app(image_fixture_settings(tmp_path, node, source, base))
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user({'username':'admin', 'display_name':'Usage fixture', 'password':PASSWORD, 'role':'admin'})
        login(client)
        assistant = service.save_assistant({'name':'Usage fixture'}, user=user)
        cid = client.post('/api/v1/conversations', json={'assistant_id':assistant['id']}).json()['id']
        result = wait_run(client, send(client, cid, marker, 'raw-usage-presence')['run_id'])
        assert result['status'] == 'succeeded', result
        assert len(requests) == 1
        usage = result['usage']
        assert usage['input_tokens'] == expected and usage['output_tokens'] == expected
        assert usage['cache_read_input_tokens'] == expected
        assert usage['cache_creation_input_tokens'] == expected
        assert usage['token_accounting']['capture_complete'] is True
        assert usage['token_accounting']['known_model_calls'] == 1
        cards = {metric['id']: metric for metric in client.get('/api/v1/conversations/overview').json()['metrics']}
        assert cards['request_count']['value'] == 1
        assert cards['compression_count']['value'] == 0
        for field in ('input_tokens', 'output_tokens', 'cache_read_tokens'):
            assert cards[field]['value'] == expected and cards[field].get('known_value') is None
        assert cards['cost_usd']['value'] is None and cards['cache_hit_rate']['value'] is None


def test_real_harness_forks_at_completed_turn(tmp_path, model_server):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires explicitly installed pinned Harness runtime")
    base, requests = model_server
    app = create_app(image_fixture_settings(tmp_path, node, source, base))
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user({"username": "admin", "display_name": "Test", "password": PASSWORD, "role": "admin"})
        login(client)
        assistant = service.save_assistant({"name": "Fork test"}, user=user)
        cid = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()["id"]
        image = io.BytesIO()
        Image.new("RGB", (8, 8), "green").save(image, format="PNG")
        upload = client.post("/api/v1/files", files={"file": ("fork.png", image.getvalue(), "image/png")})
        assert upload.status_code == 201, upload.text
        first = send(client, cid, "CASE_FORK_KEEP", "fork-first-request", [upload.json()["id"]])["run_id"]
        assert wait_run(client, first)["status"] == "succeeded"
        second = send(client, cid, "CASE_FORK_OMIT", "fork-second-request")["run_id"]
        assert wait_run(client, second)["status"] == "succeeded"
        boundary = service.require("runs", first)["assistant_message_id"]
        body = {"message_id": boundary, "client_request_id": "fork-regression-001"}
        response = client.post(f"/api/v1/conversations/{cid}/fork", json=body)
        assert response.status_code == 201, response.text
        child = response.json()["id"]
        assert client.post(f"/api/v1/conversations/{cid}/fork", json=body).json()["id"] == child
        messages = client.get(f"/api/v1/conversations/{child}/messages").json()
        assert [message["text"] for message in messages if message["role"] == "user"] == ["CASE_FORK_KEEP"]
        assert client.get(f"/api/v1/conversations/{child}").json()["fork_inherited_through_id"] == messages[-1]["id"]
        run = send(client, child, "CASE_RESUME", "fork-child-request")["run_id"]
        assert wait_run(client, run)["status"] == "succeeded"
        assert "CASE_FORK_KEEP" in json.dumps(requests[-1]["messages"])
        assert "CASE_FORK_OMIT" not in json.dumps(requests[-1]["messages"])
        assert any(block["type"] == "image" for message in requests[-1]["messages"] for block in message["content"])
        before = len(requests)
        service.db.execute("DELETE FROM files WHERE id=?", (upload.json()["id"],))
        revoked = send(client, child, "CASE_RESUME", "fork-revoked-request")["run_id"]
        assert wait_run(client, revoked)["status"] == "failed"
        assert len(requests) == before




@pytest.mark.parametrize(("marker", "kind", "answer"), [
    ("CASE_ASK", "question", "华北"), ("CASE_PLAN", "plan", "修改计划：先核对日期"),
])
def test_real_harness_interaction_resumes_after_answer(tmp_path, model_server, marker, kind, answer):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires explicitly installed pinned Harness runtime")
    base, requests = model_server
    app = create_app(image_fixture_settings(tmp_path, node, source, base))
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user({"username": "admin", "display_name": "Test", "password": PASSWORD, "role": "admin"})
        login(client)
        assistant = service.save_assistant({"name": "Interaction test"}, user=user)
        cid = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()["id"]
        run = send(client, cid, marker, f"interaction-request-{kind}")["run_id"]
        route = f"/api/v1/runs/{run}/interactions"
        deadline = time.monotonic() + 30
        while not (interactions := client.get(route).json()):
            assert time.monotonic() < deadline, client.get(f"/api/v1/runs/{run}").json()
            time.sleep(.1)
        interaction, = interactions
        assert interaction["kind"] == kind
        assert len(requests) == 1
        answer_route = f"{route}/{interaction['id']}"
        assert client.post(answer_route, json={"answer": answer}).status_code == 200
        assert wait_run(client, run)["status"] == "succeeded"
        assert answer in json.dumps(requests[-1]["messages"], ensure_ascii=False)
        assert client.get(route).json() == []
        assert client.post(answer_route, json={"answer": answer}).status_code == 409
