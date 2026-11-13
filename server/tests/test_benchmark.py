from app.benchmark import run_synthetic_ablation


def _metric(participant, metric_id):
    return next(item for item in participant["metrics"] if item["id"] == metric_id)


def test_synthetic_ablation_has_reproducible_participants_and_explicit_boundary():
    first = run_synthetic_ablation(seed=7)
    second = run_synthetic_ablation(seed=7)
    assert first["fixture_id"] == "synthetic-ablation-industrial-v1"
    assert first["comparison_key"] == second["comparison_key"]
    assert {item["id"] for item in first["participants"]} == {"enhanced-agent", "simple-agent-baseline", "customer-assistant"}
    assert all(first["ablation_switches"]["enhanced-agent"].values())
    assert not any(first["ablation_switches"]["simple-agent-baseline"].values())
    assert "未连接客户系统" in first["evidence_boundary"]
    assert "模型响应速度" in first["evidence_boundary"]


def test_ablation_marks_model_speed_and_customer_values_unmeasured():
    report = run_synthetic_ablation()
    enhanced = next(item for item in report["participants"] if item["id"] == "enhanced-agent")
    baseline = next(item for item in report["participants"] if item["id"] == "simple-agent-baseline")
    customer = next(item for item in report["participants"] if item["id"] == "customer-assistant")
    assert _metric(enhanced, "latency")["status"] == "not_measured"
    assert _metric(baseline, "latency")["status"] == "not_measured"
    assert all(item["status"] == "not_measured" for item in customer["metrics"])
    assert _metric(enhanced, "context-memory")["value"] == 100
    assert _metric(baseline, "context-memory")["value"] < _metric(enhanced, "context-memory")["value"]


def test_image_semantic_task_stays_unmeasured_without_visual_model():
    report = run_synthetic_ablation()
    assert report["passed_tasks"] == 3 and report["task_count"] == 4
    task = next(item for item in report["task_results"] if item["id"] == "entity")
    assert task["status"] == "not_measured"
    assert "视觉模型" in task["note"]
    for participant in report["participants"][:2]:
        task = next(item for item in participant["task_results"] if item["id"] == "entity")
        assert task["status"] == "not_measured"
        assert _metric(participant, "multimodal-evidence")["status"] == "not_measured"
    assert "不伪造视觉实体" in report["evidence_boundary"]


def test_token_accounting_excludes_compression_and_enhanced_context_is_smaller():
    report = run_synthetic_ablation()
    enhanced = next(item for item in report["participants"] if item["id"] == "enhanced-agent")
    baseline = next(item for item in report["participants"] if item["id"] == "simple-agent-baseline")

    # Both participants receive the same fixed task information.  The
    # baseline gets the raw context; the enhanced path gets its compact form.
    assert _metric(enhanced, "tokens")["value"] < _metric(baseline, "tokens")["value"]
    assert _metric(enhanced, "input-tokens")["value"] < _metric(baseline, "input-tokens")["value"]
    assert _metric(enhanced, "compression-savings")["value"] > 0
    assert _metric(baseline, "compression-savings")["value"] == 0

    accounting = report["token_accounting"]
    assert "上下文压缩过程不计入" in accounting["policy"]
    assert accounting["enhanced_input_tokens"] == _metric(enhanced, "input-tokens")["value"]
    assert accounting["compression_savings_tokens"] > 0
