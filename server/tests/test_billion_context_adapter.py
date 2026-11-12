from dataclasses import replace

from app.billion_context import (
    SYNTHETIC_LONG_CONTEXT,
    BillionContextAdapter,
    CompressionRequest,
    SyntheticExtractiveCompressor,
)
from app.plugin_catalog import catalog_for
from app.plugin_usage import UsageLedger


MANIFEST = catalog_for(["billion-context"])[0]
REQUEST = CompressionRequest(
    context_id="synthetic-oc-t11",
    text=SYNTHETIC_LONG_CONTEXT,
    facts=("FACT-001", "FACT-002"),
    constraints=("CONSTRAINT-A", "CONSTRAINT-B"),
)


def test_preflight_blocks_invalid_manifest_and_disabled_adapter_is_inert():
    disabled = BillionContextAdapter(MANIFEST)
    assert disabled.preflight()["status"] == "disabled"
    assert disabled.compress(REQUEST, SyntheticExtractiveCompressor())["status"] == "disabled"

    broken = replace(MANIFEST, package_sha256="")
    adapter = BillionContextAdapter(broken, enabled=True)
    preflight = adapter.preflight()
    assert preflight["status"] == "blocked"
    assert preflight["reasons"][0]["code"] == "missing_or_invalid_sha256"
    result = adapter.compress(REQUEST, SyntheticExtractiveCompressor())
    assert result["status"] == "blocked"
    assert result["used_compressed"] is False


def test_synthetic_compression_is_repeatable_and_preserves_facts_and_constraints():
    adapter = BillionContextAdapter(MANIFEST, enabled=True)
    ledger = UsageLedger()
    first = adapter.compress(REQUEST, SyntheticExtractiveCompressor(), ledger=ledger)
    assert first["status"] == "ok", first
    assert first["used_compressed"] is True
    assert first["cache_hit"] is False
    assert len(first["summary"]) < len(REQUEST.text)
    assert set(first["facts_preserved"].values()) == {True}
    assert set(first["constraints_preserved"].values()) == {True}
    assert first["plugin"]["plugin_executed"] is False
    assert first["plugin"]["measurement"] == "synthetic_reference"
    assert first["tokens"]["authoritative"] is False
    assert first["tokens"]["reduction_ratio"] > 0

    second = adapter.compress(REQUEST, SyntheticExtractiveCompressor())
    assert second["status"] == "ok"
    assert second["cache_hit"] is True
    assert second["summary"] == first["summary"]

    stages = [stage["stage"] for stage in ledger.summary()["stages"]]
    assert stages == ["compression"]
    assert ledger.summary()["provider_usage_complete"] is False


def test_failure_is_not_cached_and_context_falls_back_to_original():
    class Failing(SyntheticExtractiveCompressor):
        id = "synthetic-failing"

        def compress(self, request, *, cancel=None, deadline=None):
            raise RuntimeError("synthetic failure")

    adapter = BillionContextAdapter(MANIFEST, enabled=True)
    failed = adapter.compress(REQUEST, Failing())
    assert failed["status"] == "failed"
    assert failed["reason"] == "compressor_error:RuntimeError"
    assert failed["used_compressed"] is False
    assert failed["context"] == REQUEST.text
    assert failed["summary"] is None

    recovered = adapter.compress(REQUEST, SyntheticExtractiveCompressor())
    assert recovered["status"] == "ok"
    assert recovered["cache_hit"] is False


def test_timeout_fails_closed_without_reusing_a_summary():
    import time as _time

    class Slow(SyntheticExtractiveCompressor):
        id = "synthetic-slow"

        def compress(self, request, *, cancel=None, deadline=None):
            _time.sleep(0.2)
            return super().compress(request, cancel=cancel, deadline=deadline)

    adapter = BillionContextAdapter(MANIFEST, enabled=True)
    result = adapter.compress(REQUEST, Slow(), timeout_seconds=0.02)
    assert result["status"] == "timeout"
    assert result["used_compressed"] is False
    assert result["context"] == REQUEST.text
    assert adapter.compress(REQUEST, SyntheticExtractiveCompressor())["cache_hit"] is False


def test_cancellation_before_and_after_compression_is_fail_closed():
    adapter = BillionContextAdapter(MANIFEST, enabled=True)
    before = adapter.compress(REQUEST, SyntheticExtractiveCompressor(), cancel=lambda: True)
    assert before["status"] == "cancelled"
    assert before["used_compressed"] is False

    state = {"cancel": False}

    class CancelAfter(SyntheticExtractiveCompressor):
        id = "synthetic-cancel-after"

        def compress(self, request, *, cancel=None, deadline=None):
            state["cancel"] = True
            return super().compress(request, cancel=None, deadline=deadline)

    after = adapter.compress(REQUEST, CancelAfter(), cancel=lambda: state["cancel"])
    assert after["status"] == "cancelled"
    assert after["reason"] == "cancelled_after_compression"
    assert adapter.compress(REQUEST, SyntheticExtractiveCompressor())["cache_hit"] is False


def test_missing_fact_assertion_discards_compression_result():
    class Forgetful(SyntheticExtractiveCompressor):
        id = "synthetic-forgetful"

        def compress(self, request, *, cancel=None, deadline=None):
            return "summary that dropped every required fact and constraint"

    adapter = BillionContextAdapter(MANIFEST, enabled=True)
    result = adapter.compress(REQUEST, Forgetful())
    assert result["status"] == "assertion_failed"
    assert result["used_compressed"] is False
    assert result["context"] == REQUEST.text
    assert set(result["facts_preserved"].values()) == {False}


def test_version_change_does_not_reuse_old_summary_and_rollback_disables():
    adapter = BillionContextAdapter(MANIFEST, enabled=True)
    first = adapter.compress(REQUEST, SyntheticExtractiveCompressor())
    assert first["status"] == "ok"

    newer = replace(MANIFEST, version="0.1.214", commit="f" * 40)
    adapter.rotate_manifest(newer)
    assert adapter.enabled is False
    assert adapter.compress(REQUEST, SyntheticExtractiveCompressor())["status"] == "disabled"
    assert adapter.enable()["enabled"] is True
    after_upgrade = adapter.compress(REQUEST, SyntheticExtractiveCompressor())
    assert after_upgrade["status"] == "ok"
    assert after_upgrade["cache_hit"] is False

    rolled_back = adapter.rollback()
    assert rolled_back["enabled"] is False
    assert rolled_back["cache_entries"] == 0
    assert adapter.compress(REQUEST, SyntheticExtractiveCompressor())["status"] == "disabled"


def test_invalid_requests_are_rejected_before_compressor_runs():
    adapter = BillionContextAdapter(MANIFEST, enabled=True)
    empty = CompressionRequest(context_id="empty", text="  ")
    assert adapter.compress(empty, SyntheticExtractiveCompressor())["status"] == "invalid_request"
    assert (
        adapter.compress(REQUEST, SyntheticExtractiveCompressor(), timeout_seconds=0)["status"]
        == "invalid_request"
    )


def test_cache_limit_is_enforced():
    adapter = BillionContextAdapter(MANIFEST, enabled=True, cache_limit=1)
    first = CompressionRequest(context_id="one", text="FACT-001\nfiller", facts=("FACT-001",))
    second = CompressionRequest(context_id="two", text="FACT-002\nfiller", facts=("FACT-002",))
    assert adapter.compress(first, SyntheticExtractiveCompressor())["status"] == "ok"
    assert adapter.compress(second, SyntheticExtractiveCompressor())["status"] == "ok"
    assert adapter.compress(first, SyntheticExtractiveCompressor())["cache_hit"] is False
