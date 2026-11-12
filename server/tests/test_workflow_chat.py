import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import httpx
import pytest
from fastapi.testclient import TestClient

from app.artifacts import xlsx_preview
from app.config import Settings
from app.image_generation import generate_image, image_generation_capability
from app.main import create_app
from test_api import PASSWORD, client, configured, login


# A 1x1 PNG is enough to prove the adapter validates a real image payload.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z4XcAAAAASUVORK5CYII="
)
PNG_B64 = base64.b64encode(PNG_BYTES).decode()


def chat_graph():
    """A synthetic workflow that exercises input, node references and xlsx."""

    return {
        "nodes": [
            {
                "id": "start",
                "type": "input",
                "label": "开始 / 输入",
                "config": {
                    "mode": "synthetic",
                    "form_fields": [
                        {"id": "topic", "type": "text", "label": "主题", "required": True},
                        {"id": "note", "type": "text", "label": "备注", "required": False},
                    ],
                },
            },
            {
                "id": "plan",
                "type": "agent",
                "label": "整理主题",
                "config": {"mode": "synthetic", "input_bindings": [{"key": "topic", "source": "input.topic"}]},
            },
            {
                "id": "reference",
                "type": "condition",
                "label": "引用上游节点结果",
                # This is a node-output reference, not a re-read of user input.
                "config": {"value_source": "node.plan.output", "operator": "contains", "value": "simulated", "true_label": "有结果", "false_label": "无结果"},
            },
            {"id": "approval", "type": "approval", "label": "人工审批", "config": {"role": "owner"}},
            {"id": "export", "type": "artifact", "label": "生成 Excel", "config": {"mode": "synthetic", "format": "xlsx", "columns": ["topic", "note"]}},
        ],
        "edges": [
            {"source": "start", "target": "plan", "label": "提交"},
            {"source": "plan", "target": "reference", "label": "核对"},
            {"source": "reference", "target": "approval", "label": "有结果"},
            {"source": "approval", "target": "export", "label": "通过"},
        ],
    }


def publish_workflow(client):
    assistant = client.post("/api/v1/assistants", json={"name": "聊天工作流助手", "instructions": "合成流程"}).json()
    conversation = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"], "title": "聊天工作流会话"}).json()
    created = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "质量审查", "description": "聊天内合成流程", "graph": chat_graph()})
    assert created.status_code == 201, created.text
    workflow = created.json()
    assert client.post(f"/api/v1/workflows/{workflow['id']}/publish").status_code == 200
    return assistant, conversation, workflow


def start_chat_workflow(client, conversation, workflow, request_id="chat-workflow-0001", note='=HYPERLINK("http://evil.example")'):
    return client.post(
        f"/api/v1/conversations/{conversation['id']}/workflows/{workflow['id']}/runs",
        json={"client_request_id": request_id, "input": {"topic": "合成质量审查", "note": note}},
    )


def test_chat_workflow_runs_through_approval_and_produces_interactive_excel(client):
    assistant, conversation, workflow = publish_workflow(client)
    started = start_chat_workflow(client, conversation, workflow)
    assert started.status_code == 201, started.text
    value = started.json()
    assert value["run"]["status"] == "awaiting_approval"
    assert value["run"]["conversation_id"] == conversation["id"]
    assert value["run"]["workflow_name"] == "质量审查"
    assert value["message"]["workflow_run_id"] == value["run_id"]
    assert value["assistant_message"]["workflow_run_id"] == value["run_id"]
    assert "等待审批节点" in value["assistant_message"]["text"]
    assert value["run"]["chat"]["assistant_message_id"] == value["assistant_message"]["id"]

    trace = {item["node_id"]: item for item in value["run"]["trace"]}
    assert trace["reference"]["condition_result"] is True
    assert trace["approval"]["status"] == "awaiting_approval"

    # Duplicate submission is idempotent and does not create a second run.
    duplicate = start_chat_workflow(client, conversation, workflow)
    assert duplicate.status_code == 201
    assert duplicate.json()["run_id"] == value["run_id"]
    assert len(client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()) == 2

    approval = value["run"]["approval"]
    resumed = client.post(
        f"/api/v1/workflow-runs/{value['run_id']}/approvals/{approval['node_id']}/decision",
        json={"decision": "approve", "comment": "证据齐全"},
    )
    assert resumed.status_code == 200, resumed.text
    finished = resumed.json()
    assert finished["status"] == "succeeded"
    artifacts = finished["output"]["artifacts"]
    assert len(artifacts) == 1 and artifacts[0]["written"] is True
    artifact = artifacts[0]
    assert artifact["file_name"].endswith(".xlsx")
    assert artifact["download_path"] and artifact["preview_path"]

    # Download and reopen: the workbook is a real Office Open XML file.
    download = client.get(artifact["download_path"])
    assert download.status_code == 200
    # Spreadsheets are served as an attachment; the client previews them from
    # the bounded JSON preview endpoint instead of executing workbook content.
    assert download.headers["content-type"] == "application/octet-stream"
    assert "attachment" in download.headers["content-disposition"]
    with ZipFile(BytesIO(download.content)) as archive:
        sheet_xml = archive.read("xl/worksheets/sheet1.xml").decode()
        assert "'=" in sheet_xml and "<f>" not in sheet_xml

    preview = client.get(artifact["preview_path"])
    assert preview.status_code == 200
    first = preview.json()
    assert first["evaluated_formulas"] is False
    assert first["escaped_text_cells"] >= 1
    sheet = first["sheets"][0]
    assert sheet["columns"] == ["topic", "note"]
    assert sheet["rows"][0][0] == "合成质量审查"
    # The leading apostrophe is removed for display, but the text is never a formula.
    assert sheet["rows"][0][1] == '=HYPERLINK("http://evil.example")'
    assert sheet["formula_cells"] == 0
    # Reopening the preview is deterministic and does not mutate the file.
    assert client.get(artifact["preview_path"]).json() == first

    messages = client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()
    assistant_message = next(item for item in messages if item["role"] == "assistant")
    assert assistant_message["workflow_run_id"] == value["run_id"]
    assert "已完成" in assistant_message["text"] and "已生成产物" in assistant_message["text"]
    assert artifact["file_id"] in assistant_message["file_ids"]
    assert assistant_message["text"].strip()

    history = client.get(f"/api/v1/conversations/{conversation['id']}/workflow-runs").json()
    assert history[0]["id"] == value["run_id"]
    assert history[0]["status"] == "succeeded"


def test_chat_workflow_cancel_stops_pending_approval_and_keeps_audit(client):
    assistant, conversation, workflow = publish_workflow(client)
    started = start_chat_workflow(client, conversation, workflow, request_id="chat-workflow-cancel")
    assert started.status_code == 201, started.text
    value = started.json()
    assert value["run"]["status"] == "awaiting_approval"
    approval = value["run"]["approval"]

    cancelled = client.post(f"/api/v1/workflow-runs/{value['run_id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    stopped = cancelled.json()
    assert stopped["status"] == "cancelled"
    assert stopped["approvals"][0]["status"] == "cancelled"
    assert any(item["status"] == "cancelled" for item in stopped["trace"])
    assert stopped["output"]["artifacts"][0]["written"] is False

    assistant_message = next(
        item for item in client.get(f"/api/v1/conversations/{conversation['id']}/messages").json()
        if item["role"] == "assistant"
    )
    assert assistant_message["workflow_run_id"] == value["run_id"]
    assert "已取消" in assistant_message["text"]

    # A cancelled approval cannot be decided afterwards.
    decision = client.post(
        f"/api/v1/workflow-runs/{value['run_id']}/approvals/{approval['node_id']}/decision",
        json={"decision": "approve"},
    )
    assert decision.status_code == 409
    assert client.get(f"/api/v1/workflow-runs/{value['run_id']}").json()["status"] == "cancelled"


def test_chat_workflow_rejects_cross_assistant_binding(client):
    assistant, conversation, workflow = publish_workflow(client)
    other = client.post("/api/v1/assistants", json={"name": "其他助手"}).json()
    other_conversation = client.post("/api/v1/conversations", json={"assistant_id": other["id"]}).json()
    response = client.post(
        f"/api/v1/conversations/{other_conversation['id']}/workflows/{workflow['id']}/runs",
        json={"client_request_id": "cross-assistant-01", "input": {"topic": "不应运行"}},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WORKFLOW_SESSION_MISMATCH"


def test_image_generation_failure_is_explicit_and_recorded(client):
    response = client.post("/api/v1/artifacts/image", json={"prompt": "合成工业质量看板"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "IMAGE_MODEL_UNAVAILABLE"
    jobs = client.get("/api/v1/artifacts/image-jobs").json()
    assert jobs
    job = jobs[0]
    assert job["status"] == "unavailable"
    assert job["error_code"] == "IMAGE_MODEL_UNAVAILABLE"
    assert len(job["prompt_digest"]) == 64
    assert "prompt" not in job and "合成工业质量看板" not in json.dumps(jobs, ensure_ascii=False)


def test_image_adapter_accepts_only_a_reviewed_base64_contract():
    settings = SimpleNamespace(
        image_generation_enabled=True,
        image_model_id="synthetic-image-v1",
        image_generator_url="http://127.0.0.1:9/v1/images/generations",
        image_generator_contract="openai-images-v1",
        image_generator_api_key="test-key",
        image_generation_timeout_seconds=5,
        image_generation_max_bytes=1024 * 1024,
        mcp_hosts=("127.0.0.1",),
    )
    catalog = SimpleNamespace(models={"synthetic-image-v1": {"id": "synthetic-image-v1", "capabilities": ["image_generation"]}})
    assert image_generation_capability(settings, catalog)["available"] is True

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"b64_json": PNG_B64}], "usage": {"images": 1}})

    result = asyncio.run(generate_image(settings, catalog, "合成质量看板", transport=httpx.MockTransport(handler)))
    assert result["mime_type"] == "image/png"
    assert result["image_bytes"] == PNG_BYTES
    assert result["usage"]["images"] == 1 and result["usage"]["bytes"] == len(PNG_BYTES)
    assert captured["payload"]["response_format"] == "b64_json"
    assert captured["payload"]["size"] == "1024x1024"
    assert captured["authorization"] == "Bearer test-key"

    def url_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"url": "https://example.invalid/image.png"}]})

    with pytest.raises(Exception) as error:
        asyncio.run(generate_image(settings, catalog, "合成外链图片", transport=httpx.MockTransport(url_handler)))
    assert getattr(error.value, "code", "") == "IMAGE_RESPONSE_UNSUPPORTED"


class _ImageHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - stdlib handler name
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        assert body["response_format"] == "b64_json"
        payload = {"created": 0, "data": [{"b64_json": PNG_B64}], "usage": {"images": 1}}
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        return


def test_image_endpoint_stores_real_image_and_usage_with_local_adapter(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ImageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        catalog_path = tmp_path / "catalog.json"
        catalog_path.write_text(json.dumps({
            "default_model_id": "synthetic-chat",
            "models": [
                {"id": "synthetic-chat", "label": "合成聊天模型", "upstream_model": "synthetic-chat", "enabled": True, "capabilities": ["general"], "reasoning_levels": {"default": None}, "default_reasoning_level": "default"},
                {"id": "synthetic-image-v1", "label": "合成图片模型", "upstream_model": "", "enabled": True, "capabilities": ["image_generation"], "reasoning_levels": {"default": None}, "default_reasoning_level": "default"},
            ],
        }), encoding="utf-8")
        settings = Settings(
            data_dir=tmp_path / "data",
            provider_settings="",
            origins=("http://testserver",),
            mcp_hosts=("127.0.0.1",),
            model_catalog=str(catalog_path),
            image_generation_enabled=True,
            image_model_id="synthetic-image-v1",
            image_generator_url=f"http://127.0.0.1:{server.server_port}/v1/images/generations",
            image_generator_api_key="test-key",
        )
        app = create_app(settings)
        with TestClient(app) as local:
            app.state.service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
            login(local)
            response = local.post("/api/v1/artifacts/image", json={"prompt": "合成质量看板"})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["format"] == "png"
            assert body["usage"]["images"] == 1 and body["usage"]["bytes"] == len(PNG_BYTES)
            content = local.get(body["download_path"])
            assert content.status_code == 200
            assert content.headers["content-type"] == "image/png"
            assert content.content == PNG_BYTES
            jobs = local.get("/api/v1/artifacts/image-jobs").json()
            assert jobs[0]["status"] == "succeeded"
            assert jobs[0]["file_id"] == body["file"]["id"]
            assert jobs[0]["usage"]["images"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_xlsx_preview_never_evaluates_formula_cells(tmp_path):
    from app.artifacts import write_xlsx

    path, _ = write_xlsx(tmp_path / "conversation", "公式核对", [{"name": "结果", "columns": ["备注"], "rows": [["=1+1"], ["@cmd"]]}])
    preview = xlsx_preview(path)
    sheet = preview["sheets"][0]
    assert preview["evaluated_formulas"] is False
    assert sheet["rows"] == [["=1+1"], ["@cmd"]]
    assert sheet["escaped_text_cells"] == 2
    assert sheet["formula_cells"] == 0
