from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import identity_selection_smoke as smoke


def evidence(kind):
    run = {"status": "succeeded", "model_id": "deepseek-v4.1-flash", "model": "deepseek-v4.1-flash", "execution_kind": kind}
    if kind == "product_metadata":
        run.update(identity_source="run_selection", model_call_count=0, usage={"source": "product_metadata", "model_call_count": 0})
        answer = "我是 Better AI 的工业场景智能助手，当前使用 deepseek-v4.1-flash。"
        trace = {"events": []}
    else:
        run.update(timings={"first_model_event_ms": 20})
        answer = "设备 PUMP-QA，10/200=5%，当前模型 deepseek-v4.1-flash。"
        trace = {"events": [{"type": "run.stage", "data": {"stage": "model_started"}}]}
    return {"model": run["model_id"], "kind": kind}, run, trace, answer


def test_plan_does_not_construct_api_or_require_credentials(monkeypatch, capsys):
    monkeypatch.setattr(smoke, "API", lambda *_: (_ for _ in ()).throw(AssertionError("network plan must be offline")))
    assert smoke.main([]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["requests_made"] == 0 and plan["business_model_turns"] == 3
    assert sum(case["kind"] == "product_metadata" for case in plan["cases"]) == 4


def test_metadata_checks_require_explicit_source_and_no_model_event():
    case, run, trace, answer = evidence("product_metadata")
    assert smoke.validate_turn(case, run, trace, answer, run["model"]) == []
    invalid = deepcopy(run)
    invalid.pop("identity_source")
    invalid["timings"] = {"runtime_ready_ms": 1}
    failures = smoke.validate_turn(case, invalid, trace, answer, run["model"])
    assert "metadata_origin_missing" in failures and "metadata_started_model_runtime" in failures


def test_business_checks_require_real_model_signal_and_preserved_marker():
    case, run, trace, answer = evidence("model")
    assert smoke.validate_turn(case, run, trace, answer, run["model"], ("PUMP-QA", "5%")) == []
    failures = smoke.validate_turn(case, run, {"events": []}, answer.replace("PUMP-QA", "UNKNOWN"), run["model"], ("PUMP-QA", "5%"))
    assert "real_model_event_missing" in failures and "required_business_fact_missing" in failures


def test_correct_public_metadata_does_not_mask_wrong_generated_identity():
    case, run, trace, _ = evidence("model")
    failures = smoke.validate_turn(case, run, trace, "我是 Dano-v2.1，不良率5%。", run["model"], ("5%",))
    assert "answer_current_public_model_missing" in failures


def test_metadata_response_must_match_selected_identity_exactly():
    case, run, trace, _ = evidence("product_metadata")
    failures = smoke.validate_turn(case, run, trace, "我是 Dano-v2.1。", run["model"])
    assert "metadata_answer_mismatch" in failures
