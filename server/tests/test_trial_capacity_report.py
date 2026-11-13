"""Regression for the capacity acceptance report.

The report must fail closed: an acceptance run only passes with the real
SQLite ``PRAGMA synchronous`` at FULL (2) and a clean shutdown that waited for
every actor task.  A burst that violates the slot expectation must still leave
its accepted/rejected/unexpected detail in ``phases`` before raising.
"""
import asyncio
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import trial_capacity  # noqa: E402


def test_status_requires_fulls_pragma_and_clean_shutdown():
    clean = {"ok": True}
    assert trial_capacity._classify_status(True, 2, clean) == "passed"
    assert trial_capacity._classify_status(True, 1, clean) == "diagnostic_pass"
    assert trial_capacity._classify_status(True, 0, clean) == "diagnostic_pass"
    assert trial_capacity._classify_status(True, None, clean) == "failed"
    assert trial_capacity._classify_status(True, 2, {"ok": False}) == "failed"
    assert trial_capacity._classify_status(False, 2, clean) == "failed"


def test_capacity_config_rejects_off_and_normalizes_case():
    with pytest.raises(ValueError):
        trial_capacity.CapacityConfig(sqlite_synchronous="OFF")
    config = trial_capacity.CapacityConfig(sqlite_synchronous="normal")
    assert config.sqlite_synchronous == "NORMAL"


def test_burst_phase_records_tally_before_assertions(monkeypatch):
    async def fake_conversations(client, assistant_id, count, timeout):
        return [f"c{index}" for index in range(count)]

    async def fake_submit(client, conversations, timeout):
        return [
            {"conversation_id": conversation, "http_status": 202, "run_id": f"r{index}"}
            for index, conversation in enumerate(conversations)
        ]

    async def fake_classify(client, submissions, timeout):
        classified = []
        for index, item in enumerate(submissions):
            outcome = "accepted" if index == 0 else "unexpected"
            classified.append({**item, "outcome": outcome, "status": "running" if outcome == "accepted" else "failed"})
        return classified

    monkeypatch.setattr(trial_capacity, "_create_conversations", fake_conversations)
    monkeypatch.setattr(trial_capacity, "_submit_burst", fake_submit)
    monkeypatch.setattr(trial_capacity, "_classify_all", fake_classify)
    config = trial_capacity.CapacityConfig(sessions=1, bursts=(2,), timeout=1.0, classify_timeout=1.0)
    phases = []
    with pytest.raises(trial_capacity.CheckFailure):
        asyncio.run(
            trial_capacity._burst_phase(None, "assistant", 2, config, "burst_2", phases)
        )
    assert len(phases) == 1
    assert phases[0]["accepted"] == 1
    assert phases[0]["unexpected"] == 1
    assert phases[0]["unexpected_detail"]
    assert phases[0]["submissions"]
