"""No paid model calls: scope checks and real HTTP bootstrap only."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("business_loop_real_model", Path(__file__).with_name("business_loop_real_model.py"))
real = importlib.util.module_from_spec(spec)
spec.loader.exec_module(real)


def test_private_scope_does_not_adopt_existing_data(tmp_path):
    (tmp_path / "existing.sqlite3").write_text("do not adopt")
    with pytest.raises(RuntimeError):
        real.private_directory(tmp_path, create=True)


def test_private_scope_can_be_reused_but_not_follow_symlink(tmp_path):
    directory = real.private_directory(tmp_path / "acceptance", create=True)
    assert real.private_directory(directory) == directory
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    with pytest.raises(RuntimeError):
        real.private_directory(alias)


def test_verify_requires_real_model_authorization_before_io(tmp_path):
    with pytest.raises(RuntimeError, match="--real-model"):
        real.verify(SimpleNamespace(real_model=False, directory=tmp_path))


def test_missing_usage_and_missing_model_events_remain_unknown():
    result = real.evaluate("oversize", {"run": {"status": "succeeded"}, "events": [
        {"type": "tool.completed", "data": {"success": False, "error_code": "MCP_RESULT_TOO_LARGE"}}
    ]}, [{"tool": "oversized_order_rows"}], None, "")
    assert result["real_model"] is False and result["status"] == "failed"
    assert result["usage"] == {} and result["usage_missing"] is True


def test_wrong_actual_parameters_fail_even_when_result_is_plausible():
    expected = {"rows": [{"region": "华北", "amount": 12}]}
    trace = {"run": {"status": "succeeded"}, "events": [
        {"type": "harness.event", "data": {"type": "assistant/message"}},
        {"type": "tool.completed", "data": {"success": True, "business_result": {**expected, "synthetic": True}}},
    ]}
    result = real.evaluate("orders_10k", trace, [{"tool": "order_summary", "dataset_rows": 100000, "region": "华北"}], expected, "12元 [[source:test]]")
    assert result["checks"]["exact_rows"] and not result["checks"]["correct_parameters"]
    assert result["status"] == "failed"


@pytest.mark.parametrize("citation", ["[[source:host_source_1]]", "[1](/api/v1/sources/host_source_1)"])
def test_normalized_citations_require_a_host_registered_source(citation):
    expected = {"rows": [{"region": "华北", "amount": 12}]}
    trace = {"run": {"status": "succeeded"}, "events": [
        {"type": "harness.event", "data": {"type": "assistant/message"}},
        {"type": "tool.completed", "data": {"success": True, "source_ids": ["host_source_1"], "business_result": {**expected, "synthetic": True}}},
    ]}
    calls = [{"tool": "order_summary", "dataset_rows": 10000, "region": "华北"}]
    valid = real.evaluate("orders_10k", trace, calls, expected, "华北12元 " + citation)
    assert valid["checks"]["cited_source"] and valid["status"] == "passed"
    invalid = real.evaluate("orders_10k", trace, calls, expected, "华北12元 " + citation.replace("host_source_1", "untrusted"))
    assert not invalid["checks"]["cited_source"]


def test_shipment_summary_does_not_have_to_duplicate_complete_business_table():
    expected = {"rows": [{"customer": f"合成客户{i}", "quantity": i} for i in range(8)]}
    trace = {"run": {"status": "succeeded"}, "events": [
        {"type": "harness.event", "data": {"type": "assistant/message"}},
        {"type": "tool.completed", "data": {"success": True, "source_ids": ["registered"], "business_result": {**expected, "synthetic": True}}},
    ]}
    calls = [{"tool": "shipment_summary", "dataset_rows": 100000, "region": "全部"}]
    result = real.evaluate("shipments_100k", trace, calls, expected, "发货汇总已显示在结果卡。[1](/api/v1/sources/registered)")
    assert result["status"] == "passed" and result["answer_semantic_review"] == "pending"


@pytest.mark.parametrize("answer", [
    "本次返回行发货合计189万件。[1](/api/v1/sources/registered)",
    "本次返回行发货合计168万件。\n\n本次返回行总计189万件。[1](/api/v1/sources/registered)",
    "|客户|件数（件）|\n|---|---|\n|合成客户01|1680000|\n|**合计**|**1,890,000**|\n来源：[1](/api/v1/sources/registered)",
])
def test_real_negative_wrong_total_cannot_mechanically_pass(answer):
    expected = {"columns": [{"key": "customer", "label": "客户"}, {"key": "quantity", "label": "数量", "unit": "件"}],
        "rows": [{"customer": "合成客户01", "quantity": 1680000}], "aggregates": [{"column": "quantity", "label": "数量合计",
        "claim_labels": ["发货", "发货数量", "数量", "件数"],
        "unit": "件", "value": 1680000, "operation": "sum", "scope": "returned_rows"}]}
    trace = {"run": {"status": "succeeded"}, "events": [
        {"type": "harness.event", "data": {"type": "assistant/message"}},
        {"type": "tool.completed", "data": {"success": True, "source_ids": ["registered"], "business_result": {**expected, "synthetic": True}}},
    ]}
    calls = [{"tool": "shipment_summary", "dataset_rows": 100000, "region": "全部"}]
    result = real.evaluate("shipments_100k", trace, calls, expected, answer)
    assert result["status"] == "failed" and result["answer_semantic_review"] == "failed"
    assert result["checks"]["no_deterministic_fact_conflict"] is False


def test_real_fixture_declares_only_owned_additive_contract(tmp_path):
    from app.business_results import project_business_result
    path = tmp_path / "synthetic.sqlite3"
    real.fixture.seed_database(path)
    raw = real.business_result(path, "shipments", 100000, "全部")
    result = project_business_result({"structuredContent": raw})
    facts = {fact["column"]: fact for fact in result["host_verified_aggregates"]}
    assert facts["quantity"]["value"] == "1680000"
    assert facts["record_count"]["value"] == "80000"
    assert all(fact["scope"] == "returned_rows" for fact in facts.values())


def test_actual_http_bootstrap_preserves_real_runtime_without_calling_model(tmp_path, monkeypatch):
    from app.config import PROVIDER_KEYS
    from app.runtime import configured_runtime
    for key in PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    directory = real.private_directory(tmp_path / "acceptance", create=True)
    catalog = directory / "model-catalog.json"
    real.fixture.secure_json(catalog, {"default_model_id": "authorized", "models": [{"id": "authorized", "label": "Fixture",
        "upstream_model": "not-invoked", "reasoning_levels": {"high": "high"}, "default_reasoning_level": "high",
        "provider_env": {"ANTHROPIC_BASE_URL": "https://tokenhub.tencentmaas.com", "ANTHROPIC_API_KEY": "unused-synthetic-test-key"}}]})
    monkeypatch.setenv("AGENT_MODEL_CATALOG", str(catalog))
    monkeypatch.setenv("AGENT_PROVIDER_SETTINGS", "")
    monkeypatch.setenv("AGENT_HARNESS_NODE_PATH", "/not-invoked/node")
    monkeypatch.setenv("AGENT_HARNESS_SOURCE_DIR", "/not-invoked/source")
    credentials = real.fixture.load_credentials(directory)
    real.fixture.seed_database(directory / "synthetic.sqlite3")
    sockets = [real.fixture.bind_loopback(0), real.fixture.bind_loopback(0)]
    ports = [sock.getsockname()[1] for sock in sockets]
    args = SimpleNamespace(api_port=ports[0], mcp_port=ports[1], model_id="authorized", reasoning_level="high")
    app = real.create_product(args, directory, credentials)
    servers = [real.fixture.LocalServer(real.create_mcp(directory / "synthetic.sqlite3", credentials["mcp_bearer"]), sockets[1]),
               real.fixture.LocalServer(app, sockets[0])]
    try:
        for server in servers:
            server.start()
        real.fixture.secure_json(directory / "instance.json", {"api_url": f"http://127.0.0.1:{ports[0]}",
            "mcp_url": f"http://127.0.0.1:{ports[1]}/mcp", "model_id": "authorized", "reasoning_level": "high"})
        real.bootstrap(directory)
        assert real.private_json(directory / "instance.json")["assistant_id"]
        assert app.state.manager.runtime_factory is configured_runtime
        assert not app.state.service.db.all("SELECT * FROM runs")
    finally:
        for server in reversed(servers):
            server.stop()
