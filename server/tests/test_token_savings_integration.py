from app.plugin_usage import UsageLedger


def test_existing_usage_ledger_exposes_additive_cache_aware_accounting():
    ledger = UsageLedger()
    ledger.record(
        "answer",
        {
            "input_tokens": 100,
            "output_tokens": 8,
            "cache_read_input_tokens": 75,
            "cache_write_input_tokens": 25,
        },
    )

    summary = ledger.summary()

    # Historical fields remain unchanged for existing callers.
    assert summary["provider_totals"] == {"input_tokens": 100, "output_tokens": 8}
    # New reports can use the complete cache-aware view.
    assert summary["token_accounting"]["provider_totals"] == {
        "input_tokens": 100,
        "output_tokens": 8,
        "cache_read_input_tokens": 75,
        "cache_write_input_tokens": 25,
    }
    assert summary["token_accounting"]["cache_hit_ratio"] == 0.75
