from app.token_savings import TokenAccounting, normalize_provider_usage


def test_missing_provider_usage_stays_unknown():
    value = normalize_provider_usage(None)

    assert value["provided"] is False
    assert set(value["unknown_fields"]) == {
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_write_input_tokens",
    }
    assert all(value[field] is None for field in value["unknown_fields"])


def test_partial_provider_usage_does_not_become_zero():
    value = normalize_provider_usage({"input_tokens": 120, "output_tokens": 9})

    assert value["input_tokens"] == 120
    assert value["output_tokens"] == 9
    assert value["cache_read_input_tokens"] is None
    assert value["cache_write_input_tokens"] is None
    assert set(value["unknown_fields"]) == {
        "cache_read_input_tokens",
        "cache_write_input_tokens",
    }


def test_accounting_keeps_stages_separate_and_reports_known_subtotals():
    ledger = TokenAccounting()
    ledger.record(
        "answer",
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 80,
            "cache_write_input_tokens": 20,
        },
    )
    ledger.record(
        "compression",
        {"input_tokens": 40, "output_tokens": 8},
        note="summary request",
    )

    result = ledger.summary()

    assert [item["stage"] for item in result["stages"]] == ["answer", "compression"]
    assert result["known_subtotals"]["input_tokens"] == 140
    assert result["known_subtotals"]["output_tokens"] == 28
    assert result["provider_totals"]["cache_read_input_tokens"] is None
    assert result["provider_usage_complete"] is False
    assert result["unknown_stages"] == [{"stage": "compression", "fields": [
        "cache_read_input_tokens", "cache_write_input_tokens"
    ]}]
    assert result["cache_hit_ratio"] is None


def test_complete_usage_exposes_cache_ratio_without_cost_claim():
    ledger = TokenAccounting()
    ledger.record(
        "answer",
        {
            "input_tokens": 200,
            "output_tokens": 10,
            "cache_read_input_tokens": 150,
            "cache_write_input_tokens": 50,
        },
    )

    result = ledger.summary()

    assert result["provider_usage_complete"] is True
    assert result["provider_totals"] == {
        "input_tokens": 200,
        "output_tokens": 10,
        "cache_read_input_tokens": 150,
        "cache_write_input_tokens": 50,
    }
    assert result["cache_hit_ratio"] == 0.75
    assert result["cost_is_authoritative"] is False


def test_unknown_stage_is_rejected():
    ledger = TokenAccounting()

    try:
        ledger.record("unknown", {"input_tokens": 1})
    except ValueError as error:
        assert "unknown token accounting stage" in str(error)
    else:
        raise AssertionError("unknown stages must be rejected")
