"""Pure host protocol fixtures, no model or socket calls."""
from app.harness_usage import HarnessUsageLedger


def frame(identity="one", revision=0, phase="started", **extra):
    return {"schema": "workspace-usage/v1", "sessionId": "session", "runId": "run",
            "attemptId": identity, "revision": revision, "purpose": "answer", "phase": phase,
            "terminalObserved": phase == "finished", "outcome": "succeeded" if phase == "finished" else None,
            "usage": {}, **extra}


def test_revisions_overwrite_attempt_and_retries_have_separate_unknown_cost():
    ledger = HarnessUsageLedger("session", "run")
    first = frame(revision=1, phase="finished", outcome="transport_error", terminalObserved=False)
    second = frame("two", 1, "finished", usage={"input_tokens": 10, "output_tokens": 4,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
    ledger.observe(frame())
    ledger.observe(frame("two"))
    ledger.observe(second)
    ledger.observe(second)
    ledger.settle([first, second])
    receipt = ledger.receipt()
    assert len(receipt["token_accounting"]["stages"]) == 2
    assert receipt["input_tokens"] is None
    assert receipt["token_accounting"]["known_subtotals"]["input_tokens"] == 10
    assert receipt["token_accounting"]["capture_complete"] is True
    assert receipt["token_accounting"]["stages"][0]["outcome"] == "transport_error"


def test_explicit_zero_is_measured_and_missing_cache_stays_unknown():
    ledger = HarnessUsageLedger("session", "run")
    ledger.settle([frame(phase="finished", usage={"input_tokens": 0, "output_tokens": 0, "reported_prompt_tokens": 100})])
    receipt = ledger.receipt()
    assert receipt["input_tokens"] == receipt["output_tokens"] == 0
    assert receipt["cache_read_input_tokens"] is None
    assert receipt["total_input_tokens"] == 100


def test_cancel_keeps_started_attempt_without_fabricating_final_output():
    ledger = HarnessUsageLedger("session", "run")
    ledger.observe(frame(usage={"input_tokens": 20, "output_tokens": 0}))
    receipt = ledger.receipt()
    assert receipt["token_accounting"]["capture_complete"] is False
    assert receipt["token_accounting"]["known_model_calls"] == 1
    assert receipt["output_tokens"] is None
    assert receipt["token_accounting"]["known_subtotals"]["input_tokens"] == 20


def test_foreign_run_and_old_revision_cannot_replace_bound_receipt():
    ledger = HarnessUsageLedger("session", "run")
    final = frame(revision=2, phase="finished", purpose="compaction", usage={"input_tokens": 7, "output_tokens": 2})
    ledger.observe(frame(runId="foreign", usage={"input_tokens": 99999}))
    ledger.observe(final)
    ledger.observe(frame(revision=1, usage={"input_tokens": 99999}))
    ledger.settle([final])
    assert ledger.receipt()["input_tokens"] == 7
    assert ledger.receipt()["token_accounting"]["stages"][0]["stage"] == "compression"


def test_complete_empty_scope_proves_no_model_request_but_incomplete_scope_does_not():
    ledger = HarnessUsageLedger("session", "run")
    assert ledger.receipt()["total_input_tokens"] is None
    ledger.settle([])
    assert ledger.receipt()["total_input_tokens"] == 0
    assert ledger.receipt()["token_accounting"]["capture_complete"] is True


def test_inconsistent_total_and_bucket_sum_is_not_an_exact_total():
    ledger = HarnessUsageLedger("session", "run")
    ledger.settle([frame(phase="finished", usage={"input_tokens": 60, "cache_read_input_tokens": 60,
        "output_tokens": 1, "reported_prompt_tokens": 100})])
    assert ledger.receipt()["total_input_tokens"] is None


def test_excess_attempts_or_inconsistent_snapshot_never_claim_complete(monkeypatch):
    monkeypatch.setattr('app.harness_usage.MAX_ATTEMPTS', 1)
    ledger = HarnessUsageLedger("session", "run")
    ledger.observe(frame())
    ledger.observe(frame('two'))
    ledger.settle([frame(revision=1, phase='finished')])
    assert len(ledger.attempts) == 1 and not ledger.complete


def test_usage_projection_keeps_total_independent_and_empty_capture_is_known_zero():
    from app.usage_accounting import _entry, _aggregate
    def project(usage):
        return _aggregate([_entry({'id':'r', 'user_id':'u', 'created_at':'2026-01-01T00:00:00+00:00',
            'status':'succeeded', 'usage':usage})])
    ledger = HarnessUsageLedger('session', 'run')
    ledger.settle([])
    assert project(ledger.receipt())['model_call_count'] == 0
    assert project(ledger.receipt())['input_tokens'] == 0
    ledger = HarnessUsageLedger('session', 'run')
    ledger.settle([frame(phase='finished', usage={'input_tokens':7, 'output_tokens':2, 'reported_prompt_tokens':100})])
    view = project(ledger.receipt())
    assert view['total_input_tokens'] == 100 and view['cache_read_input_tokens'] is None
    assert view['model_call_count'] == 1
    ledger.complete = False
    view = project(ledger.receipt())
    assert view['model_call_count'] is None and view['known_model_calls'] == 1
