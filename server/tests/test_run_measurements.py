from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from app.run_measurements import MeasurementError, build_report, nearest_rank
from app.token_savings import TokenAccounting


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "report_run_measurements.py"


def trace(run_id="run-1", status="succeeded", timings=None, usage=None, caches=()):
    return {
        "schema_version": 1,
        "run": {"id": run_id, "status": status, "timings": timings or {}, "duration_ms": 99999},
        "usage": usage,
        "messages": [{"text": "SECRET_MESSAGE", "api_key": "SECRET_CREDENTIAL"}],
        "events": [{"type": "tool.completed", "data": {
            "success": True, "result_facts": {"cache_hit": cache}, "arguments": {"password": "SECRET_ARGUMENT"},
        }} for cache in caches],
    }


def report(*traces, **kwargs):
    return build_report(list(traces), experiment_id="huawi-readonly-A-v1", source_kind="synthetic", **kwargs)


def reported_usage(**fields):
    ledger = TokenAccounting()
    ledger.record("answer", fields)
    return {"token_accounting": ledger.summary()}


def test_nearest_rank_is_exact_and_known_only():
    assert nearest_rank([], 95) is None
    assert nearest_rank([100, 10], 50) == 10
    assert nearest_rank([100, 10], 95) == 100
    value = report(*(trace(f"run-{i}", timings={"first_text_ms": i}) for i in range(1, 21)), trace("missing"))
    assert value["all_runs"]["timings_ms"]["first_text_ms"] == {
        "sample_count": 21, "known_count": 20, "missing_count": 1, "p50": 10, "p95": 19, "max": 20,
    }


def test_failures_cancellations_and_unfinished_runs_are_retained():
    value = report(
        trace("a", "succeeded", {"total_ms": 10}),
        trace("b", "failed", {"total_ms": 900}),
        trace("c", "cancelled", {"total_ms": 50}),
        trace("d", "interrupted", {"total_ms": 100}),
        trace("e", "running", {"total_ms": 5000, "first_text_ms": 20}),
        trace("f", "unrecognized-sensitive-status", {"total_ms": 8000}),
    )
    denominator = value["status_denominator"]
    assert denominator["all_runs"] == 6
    assert denominator["terminal_runs"] == 4 and denominator["nonterminal_runs"] == 2
    assert denominator["by_status"]["succeeded"] == {"count": 1, "fraction_of_all_runs": 1 / 6}
    assert value["all_runs"]["timings_ms"]["total_ms"] == {
        "sample_count": 6, "known_count": 4, "missing_count": 2, "p50": 50, "p95": 900, "max": 900,
    }
    assert value["by_status"]["running"]["timings_ms"]["first_text_ms"]["known_count"] == 1
    assert value["by_status"]["failed"]["run_count"] == 1
    assert "unrecognized-sensitive-status" not in json.dumps(value)


@pytest.mark.parametrize("invalid", [-1, True, "42", float("nan"), float("inf"), {}, None])
def test_invalid_timings_are_missing_not_zero(invalid):
    value = report(trace(timings={"first_text_ms": invalid}))
    metric = value["all_runs"]["timings_ms"]["first_text_ms"]
    assert metric["known_count"] == 0 and metric["missing_count"] == 1 and metric["p50"] is None


def test_old_and_semantically_different_timings_never_substitute():
    item = trace(timings={"first_feedback_ms": 1, "first_structure_ms": 2, "first_valid_result_ms": 3})
    item["events"].extend([
        {"type": "run.stage", "data": {"stage": "routing", "first_server_progress_ms": 10}},
        {"type": "tool.completed", "data": {"success": True, "duration_ms": 20}},
    ])
    value = report(item)
    assert all(metric["known_count"] == 0 for metric in value["all_runs"]["timings_ms"].values())
    assert "first_feedback_ms" not in value["all_runs"]["timings_ms"]


def test_cache_is_per_run_separated_from_provider_cache_tokens():
    value = report(
        trace("hit", caches=[True, True]), trace("miss", caches=[False]),
        trace("unknown", usage={"cache_read_input_tokens": 800}),
        trace("mixed", caches=[True, False]), trace("partial", caches=[True, "unknown"]),
        trace("string", caches=["true"]),
    )
    assert {key: group["run_count"] for key, group in value["by_business_cache"].items()} == {
        "true": 1, "false": 1, "unknown": 2, "mixed": 2,
    }
    assert value["business_cache_observations"] == {"true": 4, "false": 2, "unknown": 2}
    assert value["by_business_cache"]["unknown"]["provider_tokens"]["cache_read_input_tokens"]["known_subtotal"] == 800


@pytest.mark.parametrize("success", [False, None, 1, "true", "missing"])
def test_cache_flag_from_failed_or_unconfirmed_completion_stays_unknown(success):
    item = trace(caches=[True])
    if success == "missing":
        del item["events"][0]["data"]["success"]
    else:
        item["events"][0]["data"]["success"] = success
    value = report(item)
    assert value["by_business_cache"]["unknown"]["run_count"] == 1
    assert value["business_cache_observations"] == {"true": 0, "false": 0, "unknown": 1}


def test_event_run_id_must_match_trace_when_provided():
    item = trace(caches=[True])
    item["events"][0]["run_id"] = "run-1"
    assert report(item)["by_business_cache"]["true"]["run_count"] == 1
    item["events"][0]["run_id"] = "SECRET_OTHER_RUN"
    with pytest.raises(MeasurementError, match="different run") as error:
        report(item)
    assert "SECRET_OTHER_RUN" not in str(error.value)


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted", "queued", "running", "awaiting_approval", "cancelling", "unknown"])
def test_unsuccessful_and_nonterminal_tokens_are_subtotals_not_complete_run_bills(status):
    value = report(trace("finished", usage=reported_usage(input_tokens=100)),
                   trace("unfinished", status=status, usage={"input_tokens": 50, "output_tokens": 0}))
    tokens = value["all_runs"]["provider_tokens"]
    assert tokens["input_tokens"]["known_subtotal"] == 150
    assert tokens["input_tokens"]["subtotal_reporting_count"] == 2
    assert tokens["input_tokens"]["known_count"] == 1
    assert tokens["input_tokens"]["missing_count"] == 1
    assert tokens["input_tokens"]["complete_total"] is None
    assert tokens["output_tokens"]["known_subtotal"] == 0
    assert tokens["output_tokens"]["complete_total"] is None
    assert value["by_status"][status]["provider_tokens"]["input_tokens"]["complete_total"] is None


def test_token_unknown_and_partial_are_not_a_complete_bill():
    ledger = TokenAccounting()
    ledger.record("answer", {"input_tokens": 200, "output_tokens": 30})
    ledger.record("retry", {"output_tokens": 20})
    partial = {"input_tokens": 200, "token_accounting": ledger.summary()}
    value = report(trace("a", usage=reported_usage(input_tokens=100, output_tokens=0, cache_write_input_tokens=25)),
                   trace("b", usage=partial), trace("c", status="failed"))
    tokens = value["all_runs"]["provider_tokens"]
    assert tokens["input_tokens"]["known_count"] == 1
    assert tokens["input_tokens"]["missing_count"] == 2
    assert tokens["input_tokens"]["known_subtotal"] == 300
    assert tokens["input_tokens"]["subtotal_reporting_count"] == 2
    assert tokens["input_tokens"]["complete_total"] is None
    assert tokens["input_tokens"]["p50"] == 100
    assert tokens["output_tokens"]["known_count"] == 2
    assert tokens["output_tokens"]["known_subtotal"] == 50
    assert tokens["cache_write_input_tokens"]["known_subtotal"] == 25
    assert tokens["cache_read_input_tokens"]["known_count"] == 0
    assert tokens["cache_read_input_tokens"]["known_subtotal"] is None


def test_real_ledger_with_unreported_usage_does_not_report_zero():
    ledger = TokenAccounting()
    ledger.record("answer", None)
    # The runtime ledger intentionally initializes its subtotal to zero. The
    # report must check observations instead of trusting that zero as measured.
    assert ledger.summary()["known_subtotals"]["input_tokens"] == 0
    tokens = report(trace(usage={"token_accounting": ledger.summary()}))["all_runs"]["provider_tokens"]
    assert all(metric["known_subtotal"] is None and metric["subtotal_reporting_count"] == 0
               and metric["complete_total"] is None for metric in tokens.values())


def test_real_ledger_preserves_explicit_zero_and_unknown_retry():
    ledger = TokenAccounting()
    ledger.record("answer", {"input_tokens": 0, "output_tokens": 0,
                             "cache_read_input_tokens": 0, "cache_write_input_tokens": 0})
    tokens = report(trace(usage={"token_accounting": ledger.summary()}))["all_runs"]["provider_tokens"]
    assert all(metric["known_subtotal"] == 0 and metric["subtotal_reporting_count"] == 1
               and metric["complete_total"] == 0 for metric in tokens.values())
    ledger.record("retry", None)
    partial = report(trace(usage={"token_accounting": ledger.summary()}))["all_runs"]["provider_tokens"]
    assert all(metric["known_subtotal"] == 0 and metric["subtotal_reporting_count"] == 1
               and metric["complete_total"] is None for metric in partial.values())


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
def test_terminal_failure_with_real_complete_ledger_remains_subtotal_only(status):
    ledger = TokenAccounting()
    ledger.record("answer", {"input_tokens": 90, "output_tokens": 10,
                             "cache_read_input_tokens": 0, "cache_write_input_tokens": 0})
    value = report(trace(status=status, usage={"token_accounting": ledger.summary()}))
    tokens = value["all_runs"]["provider_tokens"]
    assert tokens["input_tokens"]["known_subtotal"] == 90
    assert tokens["output_tokens"]["known_subtotal"] == 10
    assert all(metric["complete_total"] is None and metric["known_count"] == 0
               and metric["missing_count"] == 1 for metric in tokens.values())


def test_final_usage_snapshot_is_not_added_to_cumulative_events():
    item = trace(usage={"input_tokens": 100, "output_tokens": 0})
    item["events"] += [
        {"type": "usage.updated", "data": {"input_tokens": 40}},
        {"type": "usage.updated", "data": {"input_tokens": 100}},
    ]
    tokens = report(item)["all_runs"]["provider_tokens"]
    assert tokens["input_tokens"]["known_subtotal"] == 100
    assert tokens["input_tokens"]["subtotal_reporting_count"] == 1
    assert tokens["output_tokens"]["known_subtotal"] == 0
    assert tokens["input_tokens"]["complete_total"] is None
    assert tokens["output_tokens"]["complete_total"] is None
    assert tokens["cache_read_input_tokens"]["complete_total"] is None


def test_successful_legacy_flat_usage_is_subtotal_only_even_with_all_fields():
    tokens = report(trace(usage={"input_tokens": 80, "output_tokens": 20,
                                "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 0}))["all_runs"]["provider_tokens"]
    assert tokens["input_tokens"]["known_subtotal"] == 80
    assert tokens["output_tokens"]["known_subtotal"] == 20
    assert tokens["cache_write_input_tokens"]["known_subtotal"] == 0
    assert all(metric["complete_total"] is None and metric["known_count"] == 0
               and metric["subtotal_reporting_count"] == 1 for metric in tokens.values())


def test_invalid_provider_counts_remain_unknown():
    tokens = report(trace(usage={"input_tokens": True, "output_tokens": 2.5,
                                "cache_read_input_tokens": -1}))["all_runs"]["provider_tokens"]
    assert all(metric["known_count"] == 0 and metric["known_subtotal"] is None for metric in tokens.values())


def test_empty_ledger_and_conflicting_provider_alias_remain_unknown():
    value = report(trace("a", usage={"input_tokens": 80, "token_accounting": {
        "stages": [], "provider_totals": {"input_tokens": 0}, "known_subtotals": {"input_tokens": 0},
    }}), trace("b", usage={"cache_creation_input_tokens": 12, "cache_write_input_tokens": 13}))
    assert value["all_runs"]["provider_tokens"]["input_tokens"]["known_count"] == 0
    assert value["all_runs"]["provider_tokens"]["cache_write_input_tokens"]["known_count"] == 0


def test_duplicate_run_ids_are_rejected_not_deduplicated_silently():
    with pytest.raises(MeasurementError, match="duplicate run id"):
        report(trace(), trace(status="failed"))


@pytest.mark.parametrize("invalid", [{}, {"schema_version": True}, {"schema_version": 2},
                                      {"schema_version": 1, "run": {}, "events": []}])
def test_malformed_trace_fails_closed(invalid):
    with pytest.raises(MeasurementError):
        report(invalid)


def test_report_excludes_body_arguments_errors_ids_and_credentials():
    item = trace(run_id="SECRET_RUN_ID", usage={"input_tokens": 5, "password": "SECRET_USAGE"})
    item["run"]["error"] = "SECRET_ERROR"
    value = report(item)
    assert "SECRET_" not in json.dumps(value)
    assert value["baseline"] == {"status": "not_supplied", "reference": None, "savings": None}
    assert value["source_kind_verified"] is False
    supplied = report(item, baseline_reference="baseline-A-v1")
    assert supplied["baseline"]["status"] == "external_reference_unverified"
    assert supplied["baseline"]["savings"] is None


def test_cli_is_offline_usable_and_creates_private_output(tmp_path):
    source, output = tmp_path / "trace.json", tmp_path / "report.json"
    source.write_text(json.dumps(trace()), encoding="utf-8")
    command = [sys.executable, str(SCRIPT), "--experiment-id", "test-v1", "--source-kind", "synthetic",
               "--output", str(output), str(source)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    value = json.loads(output.read_text())
    assert value["input_run_count"] == 1
    assert len(value["input_fingerprints"][0]["sha256"]) == 64
    assert output.stat().st_mode & 0o777 == 0o600
    repeated = subprocess.run(command, capture_output=True, text=True)
    assert repeated.returncode == 2 and "must not already exist" in repeated.stderr
    help_result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
    assert help_result.returncode == 0 and "--source-kind" in help_result.stdout


def test_cli_rejects_duplicate_files_and_does_not_echo_private_paths(tmp_path):
    source = tmp_path / "SECRET_FILENAME.json"
    source.write_text(json.dumps(trace()), encoding="utf-8")
    command = [sys.executable, str(SCRIPT), "--experiment-id", "test-v1", "--source-kind", "customer"]
    duplicate = subprocess.run(command + [str(source), str(source)], capture_output=True, text=True)
    assert duplicate.returncode == 2 and duplicate.stdout == ""
    source.write_text("SECRET_NOT_JSON", encoding="utf-8")
    invalid = subprocess.run(command + [str(source)], capture_output=True, text=True)
    assert invalid.returncode == 2 and "input position 1" in invalid.stderr
    assert "SECRET" not in invalid.stderr
