import json

import httpx
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
    assert value["status"] == "awaiting_approval" and value["approval"]["status"] == "pending"
    resumed = client.post(f"/api/v1/workflow-runs/{value['id']}/approvals/{value['approval']['node_id']}/decision", json={"decision": "approve", "comment": "测试通过"})
    assert resumed.status_code == 200, resumed.text
    value = resumed.json()
    assert value["status"] == "succeeded"
    assert all(item["external_call"] is False for item in value["trace"])
    fetched = client.get(f"/api/v1/workflow-runs/{value['id']}")
    assert fetched.status_code == 200 and fetched.json()["version"] == pinned
    assert fetched.json()["graph_hash"] == value["graph_hash"]
    assert fetched.json()["permission_snapshot"] == value["permission_snapshot"]
    assert fetched.json()["approvals"][0]["decision"] == "approve"
    history = client.get(f"/api/v1/workflows/{workflow['id']}/runs?limit=5")
    assert history.status_code == 200
    assert history.json()[0]["id"] == value["id"]
    assert history.json()[0]["trace_count"] == len(value["trace"])
    assert "output" not in history.json()[0]


def test_input_node_defines_the_runtime_form_contract(client):
    assistant = client.post("/api/v1/assistants", json={"name": "开始节点助手"}).json()
    workflow_graph = {
        "nodes": [
            {"id": "start", "type": "input", "label": "开始 / 输入", "config": {"mode": "synthetic", "form_fields": [{"id": "topic", "type": "text", "label": "主题", "required": True}]}},
            {"id": "plan", "type": "agent", "label": "整理主题", "config": {"mode": "synthetic"}},
        ],
        "edges": [{"source": "start", "target": "plan", "label": "提交"}],
    }
    created = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "开始输入", "graph": workflow_graph})
    assert created.status_code == 201, created.text
    workflow = created.json()
    assert client.post(f"/api/v1/workflows/{workflow['id']}/publish").status_code == 200
    missing = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={})
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "WORKFLOW_FORM_REQUIRED"
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {"topic": "质量审查"}})
    assert run.status_code == 201, run.text
    value = run.json()
    assert value["status"] == "succeeded"
    assert value["output"]["input"] == {"topic": "质量审查"}
    assert [item["node_id"] for item in value["trace"]] == ["start", "plan"]


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
    assert len(graph["nodes"]) == 6
    assert any(node["type"] == "input" for node in graph["nodes"])
    assert all(node["config"].get("mode") == "synthetic" for node in graph["nodes"])
    assert client.post(f"/api/v1/workflows/{payload['id']}/publish").status_code == 200
    missing = client.post(f"/api/v1/workflows/{payload['id']}/runs", json={})
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "WORKFLOW_FORM_REQUIRED"
    run = client.post(f"/api/v1/workflows/{payload['id']}/runs", json={"input": {"reason": "客户拜访", "amount": 1200, "date": "2026-11-07"}}).json()
    assert run["synthetic"] is True and run["status"] == "awaiting_approval"
    resumed = client.post(f"/api/v1/workflow-runs/{run['id']}/approvals/{run['approval']['node_id']}/decision", json={"decision": "approve"}).json()
    assert resumed["status"] == "succeeded" and resumed["output"]["completed_nodes"] == 6
    assert resumed["output"]["artifacts"][0]["format"] == "xlsx" and resumed["output"]["artifacts"][0]["written"] is False


def test_condition_node_selects_only_the_matching_branch_in_a_synthetic_run(client):
    assistant = client.post("/api/v1/assistants", json={"name": "条件分支助手"}).json()
    graph = {
        "nodes": [
            {"id": "form", "type": "skill", "label": "填写金额", "config": {"form_fields": [{"id": "amount", "type": "number", "label": "金额", "required": True}]}},
            {"id": "gate", "type": "condition", "label": "金额判断", "config": {"field": "amount", "operator": "gt", "value": 5000, "true_label": "high", "false_label": "low"}},
            {"id": "high", "type": "artifact", "label": "高金额产物", "config": {"format": "xlsx"}},
            {"id": "low", "type": "artifact", "label": "普通金额产物", "config": {"format": "xlsx"}},
        ],
        "edges": [
            {"source": "form", "target": "gate", "label": "判断"},
            {"source": "gate", "target": "high", "label": "high"},
            {"source": "gate", "target": "low", "label": "low"},
        ],
    }
    workflow = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "金额分支", "graph": graph}).json()
    client.post(f"/api/v1/workflows/{workflow['id']}/publish")
    response = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {"amount": 8000}})
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["output"]["condition_outputs"]["gate"]["result"] is True
    assert run["trace"][1]["selected_targets"] == ["high"]
    assert run["trace"][2]["status"] == "simulated"
    assert run["trace"][3]["status"] == "skipped"
    assert run["output"]["node_outputs"]["low"].startswith('{"status":"skipped"')


def test_synthetic_condition_can_read_an_upstream_node_output(client):
    assistant = client.post("/api/v1/assistants", json={"name": "输出分支助手"}).json()
    graph = {
        "nodes": [
            {"id": "plan", "type": "agent", "label": "准备结果", "config": {"mode": "synthetic"}},
            {"id": "gate", "type": "condition", "label": "检查结果", "config": {"value_source": "node.plan.output", "operator": "contains", "value": "simulated", "true_label": "通过", "false_label": "不通过"}},
            {"id": "pass", "type": "artifact", "label": "通过产物", "config": {}},
            {"id": "fail", "type": "artifact", "label": "不通过产物", "config": {}},
        ],
        "edges": [
            {"source": "plan", "target": "gate", "label": "检查"},
            {"source": "gate", "target": "pass", "label": "通过"},
            {"source": "gate", "target": "fail", "label": "不通过"},
        ],
    }
    workflow = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "输出分支", "graph": graph}).json()
    client.post(f"/api/v1/workflows/{workflow['id']}/publish")
    response = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={})
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["output"]["condition_outputs"]["gate"]["actual"].startswith('{"status":"simulated"')
    assert run["output"]["condition_outputs"]["gate"]["result"] is True
    assert run["trace"][1]["selected_targets"] == ["pass"]
    trace_by_node = {item["node_id"]: item for item in run["trace"]}
    assert trace_by_node["pass"]["status"] == "simulated"
    assert trace_by_node["fail"]["status"] == "skipped"


def test_workflow_approval_rejection_is_terminal_and_audited(client):
    assistant = client.post("/api/v1/assistants", json={"name": "拒绝审批助手"}).json()
    workflow = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "拒绝流程", "graph": graph()}).json()
    client.post(f"/api/v1/workflows/{workflow['id']}/publish")
    waiting = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {"batch": "reject"}}).json()
    approval = waiting["approval"]
    rejected = client.post(f"/api/v1/workflow-runs/{waiting['id']}/approvals/{approval['node_id']}/decision", json={"decision": "reject", "comment": "风险未确认"})
    assert rejected.status_code == 200, rejected.text
    result = rejected.json()
    assert result["status"] == "failed" and result["error"] == "工作流审批被拒绝"
    assert result["approvals"][0]["status"] == "rejected"
    assert result["approvals"][0]["comment"] == "风险未确认"
    assert result["trace"][-1]["status"] == "rejected"
    repeated = client.post(f"/api/v1/workflow-runs/{waiting['id']}/approvals/{approval['node_id']}/decision", json={"decision": "approve"})
    assert repeated.status_code == 409 and repeated.json()["error"]["code"] == "WORKFLOW_APPROVAL_DECIDED"


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
            {"id": "review", "type": "agent", "label": "审核申请", "config": {"mode": "model", "model_id": "legacy-model", "role": "财务审核员", "system_prompt": "核对金额并说明依据", "prompt": "判断输入是否合规", "input_bindings": [{"key": "amount", "source": "input.amount"}], "temperature": 0.7, "max_tokens": 640}},
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
    waiting = response.json()
    assert waiting["status"] == "awaiting_approval" and len(FakeClient.calls) == 1
    response = client.post(f"/api/v1/workflow-runs/{waiting['id']}/approvals/{waiting['approval']['node_id']}/decision", json={"decision": "approve", "comment": "已核对"})
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["synthetic"] is False and run["execution_mode"] == "model"
    assert run["output"]["agent_outputs"]["review"] == "模型节点已完成审核。"
    assert run["trace"][0]["external_call"] is True
    assert run["trace"][0]["role"] == "财务审核员"
    assert FakeClient.calls[0]["json"]["model"] == "deepseek-v4.1-flash-1017"
    assert FakeClient.calls[0]["json"]["temperature"] == 0.7
    assert FakeClient.calls[0]["json"]["max_tokens"] == 640
    assert FakeClient.calls[0]["json"]["system"].startswith("你是知行工作台工作流中的财务审核员")
    assert '"mapped_inputs":{"amount":1200}' in FakeClient.calls[0]["json"]["messages"][0]["content"]
    assert run["trace"][0]["input_bindings"] == [{"key": "amount", "source": "input.amount"}]



def test_model_agent_retries_transient_provider_failure(monkeypatch, client):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-retry-token")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"content": [{"type": "text", "text": "重试后完成。"}]}

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
            if len(self.calls) == 1:
                raise httpx.ReadTimeout("temporary provider timeout")
            return FakeResponse()

    monkeypatch.setattr("app.workflows.httpx.AsyncClient", FakeClient)
    assistant = client.post("/api/v1/assistants", json={"name": "重试助手"}).json()
    graph = {
        "nodes": [{"id": "review", "type": "agent", "label": "重试审核", "config": {"mode": "model", "model_id": "legacy-model", "max_retries": 1, "retry_backoff_ms": 0}}],
        "edges": [],
    }
    created = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "重试流程", "graph": graph})
    assert created.status_code == 201, created.text
    workflow = created.json()
    assert client.post(f"/api/v1/workflows/{workflow['id']}/publish").status_code == 200
    response = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}})
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "succeeded"
    assert len(FakeClient.calls) == 2
    assert run["trace"][0]["model_attempts"] == 2
    assert run["trace"][0]["model_retries"] == 1
    assert isinstance(run["trace"][0]["duration_ms"], int)


def test_workflow_rejects_invalid_agent_retry_config():
    from app.workflows import validate_graph

    payload = {"nodes": [{"id": "review", "type": "agent", "label": "审核", "config": {"mode": "model", "max_retries": 4}}], "edges": []}
    with pytest.raises(AppError) as error:
        validate_graph(payload)
    assert error.value.code == "WORKFLOW_AGENT_RETRY_INVALID"

def test_model_mode_is_limited_to_agent_nodes():
    from app.workflows import validate_graph

    payload = {"nodes": [{"id": "approval", "type": "approval", "label": "审批", "config": {"mode": "model"}}], "edges": []}
    with pytest.raises(AppError) as error:
        validate_graph(payload)
    assert error.value.code == "WORKFLOW_EXTERNAL_EXECUTION_FORBIDDEN"


def test_model_agent_structured_output_can_feed_a_downstream_field(monkeypatch, client):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-structured-token")

    class FakeResponse:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

        def json(self):
            return {"content": [{"type": "text", "text": self.text}]}

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
            return FakeResponse('{"decision":"approve","score":0.92}' if len(self.calls) == 1 else "已根据决定生成说明")

    monkeypatch.setattr("app.workflows.httpx.AsyncClient", FakeClient)
    assistant = client.post("/api/v1/assistants", json={"name": "结构化输出助手"}).json()
    graph = {
        "nodes": [
            {"id": "review", "type": "agent", "label": "审核", "config": {"mode": "model", "model_id": "legacy-model", "output_schema_json": '{"fields":[{"name":"decision","type":"text","required":true},{"name":"score","type":"number","required":true}]}' }},
            {"id": "explain", "type": "agent", "label": "说明", "config": {"mode": "model", "model_id": "legacy-model", "input_bindings": [{"key": "decision", "source": "node.review.output.decision"}]}},
        ],
        "edges": [{"source": "review", "target": "explain", "label": "传递决定"}],
    }
    created = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "结构化流程", "graph": graph})
    assert created.status_code == 201, created.text
    workflow = created.json()
    assert client.post(f"/api/v1/workflows/{workflow['id']}/publish").status_code == 200
    response = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {"topic": "合同"}})
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "succeeded"
    assert run["output"]["agent_structured_outputs"]["review"] == {"decision": "approve", "score": 0.92}
    assert run["trace"][0]["structured_output"] is True
    assert run["trace"][0]["structured_fields"] == ["decision", "score"]
    assert '"mapped_inputs":{"decision":"approve"}' in FakeClient.calls[1]["json"]["messages"][0]["content"]


def test_workflow_rejects_invalid_agent_output_schema(client):
    assistant = client.post("/api/v1/assistants", json={"name": "输出结构边界助手"}).json()
    graph = {"nodes": [{"id": "review", "type": "agent", "label": "审核", "config": {"mode": "model", "output_schema_json": '{"fields":[{"name":"score","type":"money"}]}'}}], "edges": []}
    response = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "非法输出结构", "graph": graph})
    assert response.status_code == 422 and response.json()["error"]["code"] == "WORKFLOW_AGENT_OUTPUT_SCHEMA_INVALID"


def test_workflow_marks_invalid_agent_output_as_failed(monkeypatch, client):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-invalid-structured-token")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"content": [{"type": "text", "text": "这不是 JSON"}]}

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, endpoint, json):
            return FakeResponse()

    monkeypatch.setattr("app.workflows.httpx.AsyncClient", FakeClient)
    assistant = client.post("/api/v1/assistants", json={"name": "输出校验助手"}).json()
    graph = {"nodes": [{"id": "review", "type": "agent", "label": "审核", "config": {"mode": "model", "model_id": "legacy-model", "output_schema_json": '{"fields":[{"name":"decision","type":"text","required":true}]}'}}], "edges": []}
    workflow = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "输出校验流程", "graph": graph}).json()
    assert client.post(f"/api/v1/workflows/{workflow['id']}/publish").status_code == 200
    response = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "WORKFLOW_AGENT_OUTPUT_INVALID"


def test_condition_can_branch_on_a_structured_agent_field(monkeypatch, client):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-condition-output-token")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"content": [{"type": "text", "text": '{"score":0.92}'}]}

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, endpoint, json):
            return FakeResponse()

    monkeypatch.setattr("app.workflows.httpx.AsyncClient", FakeClient)
    assistant = client.post("/api/v1/assistants", json={"name": "结构化条件助手"}).json()
    graph = {
        "nodes": [
            {"id": "review", "type": "agent", "label": "评分", "config": {"mode": "model", "model_id": "legacy-model", "output_schema_json": '{"fields":[{"name":"score","type":"number","required":true}]}' }},
            {"id": "gate", "type": "condition", "label": "判断分数", "config": {"value_source": "node.review.output.score", "operator": "gte", "value": 0.8, "true_label": "通过", "false_label": "不通过"}},
            {"id": "pass", "type": "artifact", "label": "通过产物", "config": {}},
            {"id": "fail", "type": "artifact", "label": "不通过产物", "config": {}},
        ],
        "edges": [
            {"source": "review", "target": "gate", "label": "评分完成"},
            {"source": "gate", "target": "pass", "label": "通过"},
            {"source": "gate", "target": "fail", "label": "不通过"},
        ],
    }
    workflow = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "结构化条件流程", "graph": graph}).json()
    assert client.post(f"/api/v1/workflows/{workflow['id']}/publish").status_code == 200
    response = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={})
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "succeeded"
    assert run["trace"][1]["condition_result"] is True
    assert run["output"]["condition_outputs"]["gate"]["actual"] == 0.92
    trace_by_node = {item["node_id"]: item for item in run["trace"]}
    assert trace_by_node["fail"]["status"] == "skipped"
    assert trace_by_node["pass"]["status"] == "simulated"


def test_condition_rejects_an_undeclared_structured_field(client):
    assistant = client.post("/api/v1/assistants", json={"name": "条件字段边界助手"}).json()
    graph = {
        "nodes": [
            {"id": "review", "type": "agent", "label": "评分", "config": {"mode": "model", "model_id": "legacy-model", "output_schema_json": '{"fields":[{"name":"score","type":"number","required":true}]}' }},
            {"id": "gate", "type": "condition", "label": "判断", "config": {"value_source": "node.review.output.decision", "operator": "exists"}},
        ],
        "edges": [{"source": "review", "target": "gate", "label": "完成"}],
    }
    response = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "非法条件字段", "graph": graph})
    assert response.status_code == 422 and response.json()["error"]["code"] == "WORKFLOW_CONDITION_CONFIG_INVALID"


def test_live_mcp_read_node_calls_bound_readonly_tool_and_persists_evidence(monkeypatch, client):
    class FakeResult:
        isError = False

        def model_dump(self, mode="json"):
            return {
                "structuredContent": {"rows": [{"id": "R-7", "status": "ready"}], "sources": [{"title": "质量台账", "excerpt": "R-7 已完成复核", "locator": {"database": "qa", "table": "records", "fields": ["id", "status"], "queried_at": "2026-11-08T00:00:00Z"}}]},
                "content": [{"type": "text", "text": "R-7 已完成复核"}],
                "isError": False,
            }

    class FakeSession:
        calls = []

        def __init__(self, *streams):
            self.streams = streams

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments=None, read_timeout_seconds=None):
            self.calls.append({"name": name, "arguments": arguments})
            return FakeResult()

    class FakeStreams:
        async def __aenter__(self):
            return (object(), object(), lambda: None)

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("mcp.ClientSession", FakeSession)
    monkeypatch.setattr("mcp.client.streamable_http.streamable_http_client", lambda *args, **kwargs: FakeStreams())
    connection = client.post("/api/v1/connections", json={"name": "质量 MCP", "url": "http://localhost:8932/mcp"}).json()
    service = client.app.state.service
    service.db.execute("UPDATE connections SET status='connected',tools=? WHERE id=?", (json.dumps([{"name": "read_records"}]), connection["id"]))
    assistant = client.post("/api/v1/assistants", json={"name": "工具流程助手", "connections": [{"connection_id": connection["id"], "allowed_tools": ["read_records"], "auto_approved_tools": ["read_records"]}]}).json()
    conversation = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"], "title": "工具运行会话"}).json()
    tool_name = f"mcp__c{connection['id']}__read_records"
    graph = {
        "nodes": [
            {"id": "read", "type": "mcp_read", "label": "读取质量记录", "config": {"mode": "live", "tool_name": tool_name, "arguments_json": '{"query":"{{input.topic}}"}'}},
            {"id": "approval", "type": "approval", "label": "人工确认", "config": {}},
        ],
        "edges": [{"source": "read", "target": "approval", "label": "提交确认"}],
    }
    workflow = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "实时工具流程", "graph": graph}).json()
    assert client.post(f"/api/v1/workflows/{workflow['id']}/publish").status_code == 200
    response = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"conversation_id": conversation["id"], "input": {"topic": "设备验收"}})
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "awaiting_approval" and run["execution_mode"] == "tool" and run["synthetic"] is False
    assert run["trace"][0]["tool_call"] is True and run["trace"][0]["evidence_count"] == 1
    assert run["output"]["tool_outputs"]["read"]["result"]["structuredContent"]["rows"][0]["id"] == "R-7"
    assert run["output"]["tool_evidence"]["read"][0]["title"] == "质量台账"
    assert FakeSession.calls == [{"name": "read_records", "arguments": {"query": "设备验收"}}]
    evidence = service.db.one("SELECT * FROM workflow_tool_evidence WHERE workflow_run_id=?", (run["id"],))
    assert evidence and evidence["node_id"] == "read"
    resumed = client.post(f"/api/v1/workflow-runs/{run['id']}/approvals/{run['approval']['node_id']}/decision", json={"decision": "approve"})
    assert resumed.status_code == 200 and resumed.json()["status"] == "succeeded" and resumed.json()["execution_mode"] == "tool"
    assert service.db.one("SELECT COUNT(*) n FROM workflow_tool_evidence WHERE workflow_run_id=?", (run["id"],))["n"] == 1
    assert FakeSession.calls == [{"name": "read_records", "arguments": {"query": "设备验收"}}]


def test_live_mcp_read_requires_a_bound_session_and_auto_approval(client):
    assistant = client.post("/api/v1/assistants", json={"name": "工具会话边界"}).json()
    graph = {"nodes": [{"id": "read", "type": "mcp_read", "label": "实时读取", "config": {"mode": "live", "tool_name": "mcp__cfixture__read", "arguments_json": "{}"}}], "edges": []}
    workflow = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "实时边界", "graph": graph}).json()
    assert client.post(f"/api/v1/workflows/{workflow['id']}/publish").status_code == 200
    response = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={})
    assert response.status_code == 409 and response.json()["error"]["code"] == "WORKFLOW_TOOL_SESSION_REQUIRED"


def test_model_workflow_resumes_after_approval_without_repeating_completed_nodes(monkeypatch, client):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-resume-token")

    class FakeResponse:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

        def json(self):
            return {"content": [{"type": "text", "text": self.text}]}

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
            return FakeResponse("第一步结论" if len(self.calls) == 1 else "审批后结果")

    monkeypatch.setattr("app.workflows.httpx.AsyncClient", FakeClient)
    assistant = client.post("/api/v1/assistants", json={"name": "审批恢复助手"}).json()
    graph = {
        "nodes": [
            {"id": "first", "type": "agent", "label": "先判断", "config": {"mode": "model", "model_id": "legacy-model"}},
            {"id": "approval", "type": "approval", "label": "人工确认", "config": {"role": "审核人"}},
            {"id": "approval2", "type": "approval", "label": "部门复核", "config": {"role": "部门负责人"}},
            {"id": "second", "type": "agent", "label": "再输出", "config": {"mode": "model", "model_id": "legacy-model", "input_bindings": [{"key": "prior", "source": "node.first.output"}]}},
        ],
        "edges": [{"source": "first", "target": "approval", "label": "提交"}, {"source": "approval", "target": "approval2", "label": "部门复核"}, {"source": "approval2", "target": "second", "label": "继续"}],
    }
    workflow = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "审批恢复", "graph": graph}).json()
    client.post(f"/api/v1/workflows/{workflow['id']}/publish")
    waiting = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {"topic": "合同"}}).json()
    assert waiting["status"] == "awaiting_approval" and len(FakeClient.calls) == 1
    assert waiting["trace"][1]["status"] == "awaiting_approval"
    next_waiting = client.post(f"/api/v1/workflow-runs/{waiting['id']}/approvals/approval/decision", json={"decision": "approve"})
    assert next_waiting.status_code == 200, next_waiting.text
    next_payload = next_waiting.json()
    assert next_payload["status"] == "awaiting_approval" and [item["status"] for item in next_payload["approvals"]] == ["approved", "pending"]
    resumed = client.post(f"/api/v1/workflow-runs/{waiting['id']}/approvals/approval2/decision", json={"decision": "approve", "comment": "部门复核完成"})
    assert resumed.status_code == 200, resumed.text
    result = resumed.json()
    assert result["status"] == "succeeded" and len(FakeClient.calls) == 2
    assert result["output"]["agent_outputs"]["second"] == "审批后结果"
    assert [item["status"] for item in result["trace"]] == ["completed", "approved", "approved", "completed"]
    assert '"status":"approved"' in result["trace"][1]["output_preview"]
    assert '"mapped_inputs":{"prior":"第一步结论"}' in FakeClient.calls[1]["json"]["messages"][0]["content"]
    assert [item["status"] for item in result["approvals"]] == ["approved", "approved"]
    assert result["approvals"][0]["decided_by"]



def test_workflow_rejects_invalid_or_downstream_agent_input_bindings(client):
    assistant = client.post("/api/v1/assistants", json={"name": "变量边界助手"}).json()
    invalid = {
        "nodes": [{"id": "review", "type": "agent", "label": "审核", "config": {"mode": "model", "input_bindings": [{"key": "金额", "source": "input.amount"}]}}],
        "edges": [],
    }
    response = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "非法变量", "graph": invalid})
    assert response.status_code == 422 and response.json()["error"]["code"] == "WORKFLOW_AGENT_BINDING_INVALID"

    downstream = {
        "nodes": [
            {"id": "first", "type": "agent", "label": "第一步", "config": {"mode": "model"}},
            {"id": "second", "type": "agent", "label": "第二步", "config": {"mode": "model", "input_bindings": [{"key": "prior", "source": "node.first.output"}]}},
        ],
        "edges": [{"source": "first", "target": "second"}],
    }
    valid = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "合法变量", "graph": downstream})
    assert valid.status_code == 201, valid.text

    future_reference = {
        "nodes": [
            {"id": "first", "type": "agent", "label": "第一步", "config": {"mode": "model", "input_bindings": [{"key": "prior", "source": "node.second.output"}]}},
            {"id": "second", "type": "agent", "label": "第二步", "config": {"mode": "model"}},
        ],
        "edges": [{"source": "first", "target": "second"}],
    }
    reversed = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "后置变量", "graph": future_reference})
    assert reversed.status_code == 422 and reversed.json()["error"]["code"] == "WORKFLOW_AGENT_BINDING_ORDER"


def test_model_agent_node_receives_a_previous_node_binding(monkeypatch, client):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-binding-token")

    class FakeResponse:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

        def json(self):
            return {"content": [{"type": "text", "text": self.text}]}

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
            return FakeResponse("第一节点结论。" if len(self.calls) == 1 else "第二节点已核验。")

    monkeypatch.setattr("app.workflows.httpx.AsyncClient", FakeClient)
    assistant = client.post("/api/v1/assistants", json={"name": "变量运行助手"}).json()
    graph = {
        "nodes": [
            {"id": "first", "type": "agent", "label": "先判断", "config": {"mode": "model", "model_id": "legacy-model"}},
            {"id": "second", "type": "agent", "label": "再核验", "config": {"mode": "model", "model_id": "legacy-model", "input_bindings": [{"key": "prior", "source": "node.first.output"}]}},
        ],
        "edges": [{"source": "first", "target": "second", "label": "传递结论"}],
    }
    workflow = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "变量运行", "graph": graph}).json()
    client.post(f"/api/v1/workflows/{workflow['id']}/publish")
    response = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {"amount": 1200}})
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["output"]["agent_outputs"]["second"] == "第二节点已核验。"
    second_prompt = FakeClient.calls[1]["json"]["messages"][0]["content"]
    assert '"mapped_inputs":{"prior":"第一节点结论。"}' in second_prompt
    assert run["trace"][1]["input_bindings"] == [{"key": "prior", "source": "node.first.output"}]


def test_model_workflow_runs_authorized_knowledge_node_and_passes_source_bound_context(monkeypatch, client):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-knowledge-token")

    class FakeResponse:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

        def json(self):
            return {"content": [{"type": "text", "text": self.text}]}

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
            return FakeResponse('{"topic":"设备验收"}' if len(self.calls) == 1 else "依据已核验。")

    monkeypatch.setattr("app.workflows.httpx.AsyncClient", FakeClient)
    service = client.app.state.service
    kb = client.post("/api/v1/knowledge-bases", json={"name": "工作流授权手册"}).json()
    assistant = client.post("/api/v1/assistants", json={"name": "知识流程助手", "knowledge_base_ids": [kb["id"]]}).json()
    search_calls = []

    def fake_search(kb_ids, user, query, limit=6, run_id=None, mode="hybrid", context_chars=12000):
        search_calls.append({"kb_ids": kb_ids, "query": query, "limit": limit, "run_id": run_id, "mode": mode, "context_chars": context_chars})
        return {
            "results": [{
                "document_id": "doc-policy",
                "version": 3,
                "name": "quality-policy.md",
                "title": "quality-policy.md",
                "kb_id": kb["id"],
                "knowledge_base": "工作流授权手册",
                "sha256": "a" * 64,
                "page": None,
                "section": "质量边界",
                "chunk_id": "chunk-policy",
                "score": 0.91,
                "rank": 1,
                "retrieval_mode": mode,
                "evidence_type": "source_chunk",
                "excerpt": "授权规则：金额超过 5000 元必须人工复核。",
                "text": "授权规则：金额超过 5000 元必须人工复核。",
                "content_url": "/api/v1/knowledge-documents/doc-policy/content?version=3",
            }],
            "retrieval": {"mode": mode, "engine": "fake-local"},
            "context": {"text": "[1] quality-policy.md v3\\n授权规则：金额超过 5000 元必须人工复核。", "characters": 35, "truncated": False},
        }

    monkeypatch.setattr(service.knowledge, "search", fake_search)
    graph = {
        "nodes": [
            {"id": "plan", "type": "agent", "label": "先规划", "config": {"mode": "model", "model_id": "legacy-model", "output_schema_json": '{"fields":[{"name":"topic","type":"text","required":true}]}' }},
            {"id": "policy", "type": "knowledge_search", "label": "检索质量规则", "config": {"mode": "synthetic", "query": "请检索 {{node.plan.output.topic}} 的质量规则", "top_k": 4, "retrieval_mode": "bm25", "context_chars": 300}},
            {"id": "review", "type": "agent", "label": "依据核验", "config": {"mode": "model", "model_id": "legacy-model", "input_bindings": [{"key": "evidence", "source": "node.policy.output"}]}},
        ],
        "edges": [{"source": "plan", "target": "policy"}, {"source": "policy", "target": "review"}],
    }
    workflow = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "授权知识流程", "graph": graph}).json()
    client.post(f"/api/v1/workflows/{workflow['id']}/publish")
    response = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {"topic": "设备验收"}})
    assert response.status_code == 201, response.text
    run = response.json()
    assert search_calls == [{"kb_ids": [kb["id"]], "query": "请检索 设备验收 的质量规则", "limit": 4, "run_id": None, "mode": "bm25", "context_chars": 300}]
    assert run["output"]["knowledge_outputs"]["policy"]["source_count"] == 1
    assert run["trace"][1]["knowledge_retrieval"] is True and run["trace"][1]["source_count"] == 1
    assert run["permission_snapshot"]["knowledge_bases"][0]["id"] == kb["id"]
    assert run["permission_snapshot"]["knowledge_bases"][0]["revision"] == kb["revision"]
    assert "授权规则" in FakeClient.calls[1]["json"]["messages"][0]["content"]


def test_workflow_rejects_knowledge_query_references_to_future_nodes(client):
    assistant = client.post("/api/v1/assistants", json={"name": "知识变量边界助手"}).json()
    graph = {
        "nodes": [
            {"id": "lookup", "type": "knowledge_search", "label": "检索", "config": {"query": "{{node.review.output}}"}},
            {"id": "review", "type": "agent", "label": "核验", "config": {"mode": "synthetic"}},
        ],
        "edges": [{"source": "lookup", "target": "review"}],
    }
    response = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "后置知识变量", "graph": graph})
    assert response.status_code == 422 and response.json()["error"]["code"] == "WORKFLOW_KNOWLEDGE_QUERY_ORDER"


def test_workflow_tool_arguments_can_read_structured_agent_fields(client):
    from app.workflows import _tool_arguments

    node = {"id": "read", "type": "mcp_read", "config": {"arguments_json": '{"query":"{{node.review.output.decision}}","payload":"结论：{{node.review.output.decision}}"}'} }
    rendered = _tool_arguments(node, {}, {}, {"review": {"decision": "approve"}})
    assert rendered == {"query": "approve", "payload": "结论：approve"}

    assistant = client.post("/api/v1/assistants", json={"name": "工具字段边界助手"}).json()
    graph = {
        "nodes": [
            {"id": "review", "type": "agent", "label": "审核", "config": {"mode": "model", "model_id": "legacy-model", "output_schema_json": '{"fields":[{"name":"decision","type":"text","required":true}]}' }},
            {"id": "read", "type": "mcp_read", "label": "读取", "config": {"mode": "synthetic", "arguments_json": '{"query":"{{node.review.output.missing}}"}'}},
        ],
        "edges": [{"source": "review", "target": "read", "label": "读取"}],
    }
    response = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "非法工具字段", "graph": graph})
    assert response.status_code == 422 and response.json()["error"]["code"] == "WORKFLOW_TOOL_ARGUMENTS_INVALID"


def test_model_agent_node_debug_runs_draft_and_records_audited_trace(monkeypatch, client):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-debug-token")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"content": [{"type": "text", "text": "草稿节点调试结果。"}]}

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
    assistant = client.post(f"/api/v1/assistants", json={"name": "节点调试助手", "instructions": "只输出可核验结论"}).json()
    graph = {
        "nodes": [
            {"id": "review", "type": "agent", "label": "审核草稿", "config": {"mode": "model", "model_id": "legacy-model", "role": "审核员", "system_prompt": "只依据输入回答", "prompt": "判断是否通过"}},
            {"id": "approval", "type": "approval", "label": "人工确认", "config": {}},
        ],
        "edges": [{"source": "review", "target": "approval", "label": "交给人工"}],
    }
    workflow = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "节点调试", "graph": graph}).json()

    response = client.post(f"/api/v1/workflows/{workflow['id']}/nodes/review/debug", json={"input": {"amount": 1200}})
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["version"] == workflow["draft_version"]
    assert run["synthetic"] is False and run["output"]["debug"] is True
    assert run["output"]["node_output"] == "草稿节点调试结果。"
    assert run["permission_snapshot"]["debug_node_id"] == "review"
    assert run["trace"][0]["model_label"]
    assert isinstance(run["trace"][0]["duration_ms"], int)
    assert FakeClient.calls[0]["json"]["model"] == "deepseek-v4.1-flash-1017"
    fetched = client.get(f"/api/v1/workflow-runs/{run['id']}")
    assert fetched.status_code == 200 and fetched.json()["output"]["debug"] is True

    rejected = client.post(f"/api/v1/workflows/{workflow['id']}/nodes/approval/debug", json={})
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "WORKFLOW_NODE_NOT_MODEL"
