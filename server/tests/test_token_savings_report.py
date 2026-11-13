from scripts.token_savings_report import build_report


def test_report_groups_runs_and_keeps_unknown_costs_unknown():
    report = build_report({
        "runs": [
            {
                "user_id": "u1",
                "assistant_id": "a1",
                "model": "deepseek",
                "runtime": "harness",
                "stages": [
                    {"stage": "answer", "usage": {"input_tokens": 100, "output_tokens": 12}},
                    {"stage": "compression", "usage": {"input_tokens": 40, "output_tokens": 5}},
                ],
            },
            {
                "user_id": "u1",
                "assistant_id": "a1",
                "model": "deepseek",
                "runtime": "harness",
                "usage": {"input_tokens": 20, "output_tokens": 3, "cache_read_input_tokens": 10},
            },
        ]
    })

    assert report["read_only"] is True
    assert report["record_count"] == 2
    assert len(report["groups"]) == 1
    total = report["total"]
    assert total["known_subtotals"]["input_tokens"] == 160
    assert total["known_subtotals"]["output_tokens"] == 20
    assert total["provider_totals"]["cache_read_input_tokens"] is None
    assert total["provider_usage_complete"] is False
    assert total["cost_is_authoritative"] is False


def test_report_accepts_legacy_usage_list():
    report = build_report([{"user_id": "u", "usage": {"input_tokens": 7, "output_tokens": 2}}])

    assert report["record_count"] == 1
    assert report["total"]["provider_totals"]["input_tokens"] == 7
    assert report["groups"][0]["runtime"] == "unknown"
