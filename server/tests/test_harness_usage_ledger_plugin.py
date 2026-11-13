from app.plugin_usage import UsageLedger, estimate_text_tokens, normalize_usage


def test_unknown_provider_usage_is_not_zero_filled():
    ledger = UsageLedger()
    ledger.record("answer", {"input_tokens": None, "output_tokens": 5})
    summary = ledger.summary()
    stage = summary["stages"][0]
    assert stage["provider_usage"]["input_tokens"] is None
    assert stage["provider_usage"]["output_tokens"] == 5
    assert summary["provider_totals"] == {"input_tokens": None, "output_tokens": 5}
    assert summary["known_subtotals"] == {"input_tokens": 0, "output_tokens": 5}
    assert summary["provider_usage_complete"] is False
    assert summary["unknown_stages"] == [{"stage": "answer", "fields": ["input_tokens"]}]


def test_missing_usage_object_keeps_every_field_unknown():
    ledger = UsageLedger()
    ledger.record("answer", None)
    summary = ledger.summary()
    assert summary["stages"][0]["provider_usage"]["input_tokens"] is None
    assert summary["stages"][0]["provider_usage"]["output_tokens"] is None
    assert summary["provider_usage_complete"] is False
    assert summary["provider_billed_stages"] == []
    assert summary["warnings"]


def test_invalid_usage_values_are_reported_not_coerced():
    for invalid in (-1, 1.5, True, "12"):
        normalized = normalize_usage({"input_tokens": invalid, "output_tokens": 2})
        assert normalized["input_tokens"] is None
        assert "input_tokens" in normalized["invalid_fields"]
    ledger = UsageLedger()
    ledger.record("answer", {"input_tokens": -1, "output_tokens": 2})
    assert ledger.summary()["provider_usage_complete"] is False
    assert "invalid provider usage fields" in ledger.summary()["warnings"][0]


def test_compression_stage_and_answer_stage_are_separate():
    compression_text = "FACT-001\nCONSTRAINT-A"
    ledger = UsageLedger()
    ledger.record(
        "compression",
        None,
        source="synthetic-extractive-v1",
        provider_call=False,
        local_estimate=estimate_text_tokens(compression_text),
        note="local reference; no provider call",
    )
    ledger.record(
        "answer",
        {"input_tokens": 120, "output_tokens": 30},
        source="provider",
        provider_call=True,
    )
    summary = ledger.summary()
    assert [stage["stage"] for stage in summary["stages"]] == ["compression", "answer"]
    compression, answer = summary["stages"]
    assert compression["provider_call"] is False
    assert compression["provider_usage"]["input_tokens"] is None
    assert compression["local_estimate"]["estimated_tokens"] > 0
    assert compression["local_estimate"]["authoritative"] is False
    assert answer["provider_usage"] == {
        "input_tokens": 120,
        "output_tokens": 30,
        "unknown_fields": [],
        "invalid_fields": [],
    }
    assert summary["provider_totals"] == {"input_tokens": None, "output_tokens": None}
    assert summary["known_subtotals"] == {"input_tokens": 120, "output_tokens": 30}
    assert summary["provider_billed_stages"] == ["answer"]
    assert summary["provider_usage_complete"] is False  # compression stage unknown


def test_known_usage_reports_complete_booleans_and_reset():
    ledger = UsageLedger()
    ledger.record("answer", {"input_tokens": 3, "output_tokens": 4})
    summary = ledger.summary()
    assert summary["provider_usage_complete"] is True
    assert summary["provider_billed_stages"] == ["answer"]
    assert summary["estimate_is_authoritative"] is False
    ledger.reset()
    assert ledger.summary()["stages"] == []


def test_local_estimate_is_explicitly_non_authoritative():
    estimate = estimate_text_tokens("abcd" * 10)
    assert estimate == {
        "characters": 40,
        "estimated_tokens": 10,
        "method": "chars_div_4",
        "authoritative": False,
    }
    ledger = UsageLedger()
    try:
        ledger.record("compression", None, local_estimate={"estimated_tokens": 1, "authoritative": True})
    except ValueError as error:
        assert "authoritative=false" in str(error)
    else:
        raise AssertionError("authoritative local estimate must be rejected")
