import pytest

from app.security import AppError
from test_api import client, configured, PASSWORD, login


def graph():
    return {
        "nodes": [
            {"id": "plan", "type": "agent", "label": "规划", "config": {"mode": "synthetic"}},
            {"id": "lookup", "type": "knowledge_search", "label": "检索", "config": {"mode": "synthetic"}},
            {"id": "approval", "type": "approval", "label": "人工审批", "config": {}},
        ],
        "edges": [
            {"source": "plan", "target": "lookup"},
            {"source": "lookup", "target": "approval"},
        ],
    }


def test_workflow_requires_publish_and_replays_a_pinned_synthetic_version(client):
    assistant = client.post("/api/v1/assistants", json={"name": "流程助手", "instructions": "合成流程"}).json()
    created = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "质量审批", "description": "本地演示", "graph": graph()})
    assert created.status_code == 201, created.text
    workflow = created.json()
    assert workflow["synthetic_only"] is True and workflow["published_version"] is None
    assert workflow["versions"][0]["status"] == "draft"

    before = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={})
    assert before.status_code == 409 and before.json()["error"]["code"] == "WORKFLOW_NOT_PUBLISHED"

    published = client.post(f"/api/v1/workflows/{workflow['id']}/publish")
    assert published.status_code == 200, published.text
    pinned = published.json()["published_version"]
    conversation = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"], "title": "流程会话"}).json()
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"conversation_id": conversation["id"], "input": {"batch": "SYNTHETIC-01"}})
    assert run.status_code == 201, run.text
    value = run.json()
    assert value["version"] == pinned and value["synthetic"] is True and value["conversation_id"] == conversation["id"]
    assert len(value["graph_hash"]) == 64 and len(value["permission_digest"]) == 64
    assert value["permission_snapshot"]["conversation_id"] == conversation["id"]
    assert value["permission_snapshot"]["workflow_version"] == pinned
    assert all(item["external_call"] is False for item in value["trace"])
    fetched = client.get(f"/api/v1/workflow-runs/{value['id']}")
    assert fetched.status_code == 200 and fetched.json()["version"] == pinned
    assert fetched.json()["graph_hash"] == value["graph_hash"]
    assert fetched.json()["permission_snapshot"] == value["permission_snapshot"]


def test_workflow_session_binding_rejects_a_different_assistant(client):
    assistant = client.post("/api/v1/assistants", json={"name": "绑定助手"}).json()
    other = client.post("/api/v1/assistants", json={"name": "另一个助手"}).json()
    workflow = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "绑定", "graph": graph()}).json()
    client.post(f"/api/v1/workflows/{workflow['id']}/publish")
    conversation = client.post("/api/v1/conversations", json={"assistant_id": other["id"]}).json()
    response = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"conversation_id": conversation["id"]})
    assert response.status_code == 409 and response.json()["error"]["code"] == "WORKFLOW_SESSION_MISMATCH"


def test_workflow_rejects_cycle_secrets_and_stale_revision(client):
    assistant = client.post("/api/v1/assistants", json={"name": "校验助手"}).json()
    workflow = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "校验", "graph": graph()}).json()
    cycle = graph()
    cycle["edges"].append({"source": "approval", "target": "plan"})
    invalid = client.patch(f"/api/v1/workflows/{workflow['id']}/draft", json={"expected_revision": workflow["revision"], "graph": cycle})
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "WORKFLOW_GRAPH_CYCLE"
    secret = graph()
    secret["nodes"][0]["config"]["api_key"] = "not-stored"
    rejected = client.patch(f"/api/v1/workflows/{workflow['id']}/draft", json={"graph": secret})
    assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "WORKFLOW_SECRET_FORBIDDEN"
    updated = client.patch(f"/api/v1/workflows/{workflow['id']}/draft", json={"expected_revision": workflow["revision"], "graph": graph(), "name": "校验二"})
    assert updated.status_code == 200
    stale = client.patch(f"/api/v1/workflows/{workflow['id']}/draft", json={"expected_revision": workflow["revision"], "graph": graph()})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "WORKFLOW_CONFLICT"


def test_workflow_rollback_creates_unpublished_draft_and_does_not_change_live_version(client):
    assistant = client.post("/api/v1/assistants", json={"name": "版本助手"}).json()
    workflow = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "版本", "graph": graph()}).json()
    first = client.post(f"/api/v1/workflows/{workflow['id']}/publish").json()
    first_version = first["published_version"]
    changed = graph()
    changed["nodes"][0]["label"] = "新版规划"
    draft = client.patch(f"/api/v1/workflows/{workflow['id']}/draft", json={"expected_revision": first["revision"], "graph": changed}).json()
    second = client.post(f"/api/v1/workflows/{workflow['id']}/publish").json()
    assert second["published_version"] != first_version
    rolled = client.post(f"/api/v1/workflows/{workflow['id']}/rollback", json={"version": first_version})
    assert rolled.status_code == 200
    assert rolled.json()["published_version"] == second["published_version"]
    assert rolled.json()["draft_version"] > second["published_version"]
    after = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={}).json()
    assert after["version"] == second["published_version"]


def test_workflow_helper_rejects_non_synthetic_mode():
    from app.workflows import validate_graph

    payload = graph()
    payload["nodes"][0]["config"]["mode"] = "provider"
    with pytest.raises(AppError) as error:
        validate_graph(payload)
    assert error.value.code == "WORKFLOW_EXTERNAL_EXECUTION_FORBIDDEN"


def test_synthetic_finance_templates_are_listed_and_create_versioned_workflows(client):
    templates = client.get("/api/v1/workflow-templates")
    assert templates.status_code == 200
    values = templates.json()
    assert {item["template_id"] for item in values} == {"finance-application-v1", "finance-reimbursement-v1"}
    assert all(item["synthetic"] is True for item in values)
    assistant = client.post("/api/v1/assistants", json={"name": "财务演示助手"}).json()
    created = client.post(f"/api/v1/assistants/{assistant['id']}/workflows/from-template", json={"template_id": "finance-reimbursement-v1"})
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["template_id"] == "finance-reimbursement-v1" and payload["synthetic_template"] is True
    graph = payload["versions"][0]["graph"]
    assert len(graph["nodes"]) == 5
    assert all(node["config"].get("mode") == "synthetic" for node in graph["nodes"])
    assert client.post(f"/api/v1/workflows/{payload['id']}/publish").status_code == 200
    missing = client.post(f"/api/v1/workflows/{payload['id']}/runs", json={})
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "WORKFLOW_FORM_REQUIRED"
    run = client.post(f"/api/v1/workflows/{payload['id']}/runs", json={"input": {"reason": "客户拜访", "amount": 1200, "date": "2026-11-07"}}).json()
    assert run["synthetic"] is True and run["output"]["completed_nodes"] == 5
    assert run["output"]["artifacts"][0]["format"] == "xlsx" and run["output"]["artifacts"][0]["written"] is False


def test_unknown_template_fails_closed(client):
    assistant = client.post("/api/v1/assistants", json={"name": "模板边界助手"}).json()
    response = client.post(f"/api/v1/assistants/{assistant['id']}/workflows/from-template", json={"template_id": "customer-production-v1"})
    assert response.status_code == 404 and response.json()["error"]["code"] == "WORKFLOW_TEMPLATE_NOT_FOUND"


def test_model_agent_node_uses_assistant_catalog_and_records_execution(monkeypatch, client):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-workflow-token")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"content": [{"type": "text", "text": "模型节点已完成审核。"}]}

    class FakeClient:
        calls = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, endpoint, json):
            self.calls.append({"endpoint": endpoint, "json": json})
            return FakeResponse()

    monkeypatch.setattr("app.workflows.httpx.AsyncClient", FakeClient)
    assistant = client.post("/api/v1/assistants", json={"name": "模型流程助手", "instructions": "遵守审核规则"}).json()
    graph = {
        "nodes": [
            {"id": "review", "type": "agent", "label": "审核申请", "config": {"mode": "model", "model_id": "legacy-model", "role": "财务审核员", "system_prompt": "核对金额并说明依据", "prompt": "判断输入是否合规"}},
            {"id": "approval", "type": "approval", "label": "人工确认", "config": {}},
        ],
        "edges": [{"source": "review", "target": "approval", "label": "提交人工确认"}],
    }
    created = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "模型审核流程", "graph": graph})
    assert created.status_code == 201, created.text
    workflow = created.json()
    assert workflow["synthetic_only"] is False and "model" in workflow["execution_modes"]
    client.post(f"/api/v1/workflows/{workflow['id']}/publish")
    response = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {"amount": 1200}})
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["synthetic"] is False and run["execution_mode"] == "model"
    assert run["output"]["agent_outputs"]["review"] == "模型节点已完成审核。"
    assert run["trace"][0]["external_call"] is True
    assert run["trace"][0]["role"] == "财务审核员"
    assert FakeClient.calls[0]["json"]["model"] == "deepseek-v4.1-flash-1017"
    assert FakeClient.calls[0]["json"]["system"].startswith("你是知行工作台工作流中的财务审核员")


def test_model_mode_is_limited_to_agent_nodes():
    from app.workflows import validate_graph

    payload = {"nodes": [{"id": "approval", "type": "approval", "label": "审批", "config": {"mode": "model"}}], "edges": []}
    with pytest.raises(AppError) as error:
        validate_graph(payload)
    assert error.value.code == "WORKFLOW_EXTERNAL_EXECUTION_FORBIDDEN"
