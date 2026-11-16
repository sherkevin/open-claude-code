"""Missing/partial runs cannot appear as complete account usage."""
import json

import pytest

from test_tool_constraints import env


def runs(env, values):
    for i, value in enumerate(values):
        result, _ = env.service.prepare_run(env.conversation["id"], {
            "text": "Synthetic usage", "file_ids": [], "client_request_id": f"usage-{i}"}, env.user)
        env.service.db.execute("UPDATE runs SET status=?,usage=? WHERE id=?", (
            "cancelled" if value is None else "succeeded", json.dumps(value) if value is not None else None, result["run_id"]))
        env.service.db.execute("UPDATE conversations SET active_run_id=NULL WHERE id=?", (env.conversation["id"],))
    return env.service.usage(env.user, scope="all")


def test_unknown_run_keeps_totals_unknown_and_subtotals_visible(env):
    view = runs(env, [{"input_tokens": 100, "output_tokens": 5, "estimated_cost_usd": .5}, None])
    for result in (view, view["users"][0]):
        assert result["input_tokens"] is None and result["output_tokens"] is None and result["estimated_cost_usd"] is None
        assert {key: result["known_subtotals"][key] for key in ("input_tokens", "output_tokens", "estimated_cost_usd")} == {"input_tokens": 100, "output_tokens": 5, "estimated_cost_usd": .5}
        assert result["total_runs"] == 2 and result["missing_usage_runs"] == 1
        assert result["field_coverage"]["input_tokens"] == {"known_runs": 1, "unknown_runs": 1}
        assert result["usage_complete"] is False


def test_missing_token_field_is_not_coerced_to_zero(env):
    view = runs(env, [{"input_tokens": 12, "output_tokens": None, "estimated_cost_usd": 0}])
    assert view["input_tokens"] == 12 and view["output_tokens"] is None
    assert view["known_subtotals"]["output_tokens"] == 0
    assert view["estimated_cost_usd"] == 0


def test_stage_partial_usage_overrides_legacy_known_counter(env):
    view = runs(env, [{"input_tokens": 100, "output_tokens": 5, "estimated_cost_usd": None,
        "token_accounting": {"provider_totals": {"input_tokens": None, "output_tokens": 5},
                             "known_subtotals": {"input_tokens": 100, "output_tokens": 5}}}])
    assert view["input_tokens"] is None and view["known_subtotals"]["input_tokens"] == 100
    assert view["output_tokens"] == 5


@pytest.mark.parametrize("invalid", [True, -1, "12", float("nan")])
def test_invalid_usage_is_unknown(env, invalid):
    view = runs(env, [{"input_tokens": invalid, "output_tokens": invalid, "estimated_cost_usd": invalid}])
    assert view["input_tokens"] is None and view["output_tokens"] is None and view["estimated_cost_usd"] is None


def test_explicit_total_prompt_survives_missing_cache_buckets(env):
    view = runs(env, [{"input_tokens": None, "output_tokens": 5, "reported_prompt_tokens": 100}])
    assert view['total_input_tokens'] == 100 and view['input_tokens'] is None
    assert view['cache_read_input_tokens'] is None
    assert view['total_input_coverage'] == {'known_runs': 1, 'unknown_runs': 0}


def test_new_http_capture_counts_attempts_without_claiming_receipt_on_cancel(env):
    from app.harness_usage import HarnessUsageLedger
    from test_harness_usage_capture import frame
    ledger = HarnessUsageLedger('session', 'run')
    ledger.observe(frame())
    view = runs(env, [ledger.receipt()])
    assert view['model_call_count'] is None and view['known_model_calls'] == 1
    assert view['input_tokens'] is None and view['total_input_tokens'] is None


def test_verified_empty_http_scope_is_distinct_from_legacy_empty_ledger(env):
    from app.harness_usage import HarnessUsageLedger
    ledger = HarnessUsageLedger('session', 'run')
    ledger.settle([])
    view = runs(env, [ledger.receipt()])
    assert view['model_call_count'] == view['input_tokens'] == view['total_input_tokens'] == 0
    assert view['estimated_cost_usd'] is None
