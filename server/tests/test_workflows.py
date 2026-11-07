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


def test_model_mode_is_limited_to_agent_nodes():
    from app.workflows import validate_graph

    payload = {"nodes": [{"id": "approval", "type": "approval", "label": "审批", "config": {"mode": "model"}}], "edges": []}
    with pytest.raises(AppError) as error:
        validate_graph(payload)
    assert error.value.code == "WORKFLOW_EXTERNAL_EXECUTION_FORBIDDEN"


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
            return FakeResponse("规划完成。" if len(self.calls) == 1 else "依据已核验。")

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
            {"id": "plan", "type": "agent", "label": "先规划", "config": {"mode": "model", "model_id": "legacy-model"}},
            {"id": "policy", "type": "knowledge_search", "label": "检索质量规则", "config": {"mode": "synthetic", "query": "请检索 {{input.topic}} 的质量规则", "top_k": 4, "retrieval_mode": "bm25", "context_chars": 300}},
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
    assert FakeClient.calls[0]["json"]["model"] == "deepseek-v4.1-flash-1017"
    fetched = client.get(f"/api/v1/workflow-runs/{run['id']}")
    assert fetched.status_code == 200 and fetched.json()["output"]["debug"] is True

    rejected = client.post(f"/api/v1/workflows/{workflow['id']}/nodes/approval/debug", json={})
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "WORKFLOW_NODE_NOT_MODEL"
