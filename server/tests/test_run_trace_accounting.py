"""HTTP trace exports must retain measured usage without opening secret fields."""
import json

import pytest

from app.run_measurements import build_report
from app.token_savings import TokenAccounting
from app.config import Settings
from test_api import PASSWORD, FakeRuntime, client, conversation, login, poll, send  # noqa: F401


@pytest.fixture
def configured(tmp_path):
    # Trace export is runtime-independent. Avoid requiring POSIX-only Harness
    # snapshots for a local HTTP test of the usage serialization boundary.
    return Settings(data_dir=tmp_path, provider_settings="", runtime_type="claude",
                    origins=("http://testserver",), idle_seconds=60)


@pytest.mark.parametrize("partial", [False, True])
def test_http_trace_retains_accounting_for_measurements_and_scrubs_secrets(client, monkeypatch, partial):
    ledger = TokenAccounting()
    ledger.record("answer", {"input_tokens": 100, "output_tokens": 12,
                              "cache_read_input_tokens": 0, "cache_write_input_tokens": 0})
    if partial:
        ledger.record("retry", None)
    usage = {"input_tokens": None if partial else 100,
             "output_tokens": None if partial else 12,
             "token_accounting": ledger.summary(),
             "api_token": "synthetic-usage-secret"}
    usage["token_accounting"]["extension"] = {
        "api_key": "synthetic-ledger-secret", "label": "safe extension",
        "token_accounting": {"note": "nested ledger is not a public usage boundary"},
    }
    usage["token_accounting"]["stages"][0]["credential"] = "synthetic-stage-secret"

    async def execute(runtime, run, message):
        await runtime.emit("message.delta", {"message_id": run["assistant_message_id"], "text": "Local accounting fixture"})
        await runtime.emit("usage.updated", usage)
        # A tool cannot opt out of redaction by choosing the same wrapper key.
        await runtime.emit("tool.completed", {"success": False,
            "token_accounting": {"value": "synthetic-tool-secret"}})

    monkeypatch.setattr(FakeRuntime, "execute", execute)
    run_id = send(client, conversation(client)["id"])["run_id"]
    assert poll(client, run_id)["status"] == "succeeded"
    response = client.get(f"/api/v1/runs/{run_id}/trace")
    assert response.status_code == 200
    trace = response.json()
    exported = trace["usage"]["token_accounting"]
    assert isinstance(exported, dict), "the public usage ledger was mistaken for an authentication token"
    assert exported["provider_totals"] == usage["token_accounting"]["provider_totals"]
    assert exported["known_subtotals"] == usage["token_accounting"]["known_subtotals"]
    assert exported["provider_usage_complete"] is (not partial)
    assert trace["run"]["usage"]["token_accounting"] == exported
    updated = next(event["data"] for event in trace["events"] if event["type"] == "usage.updated")
    completed = next(event["data"] for event in trace["events"] if event["type"] == "run.completed")
    assert updated["token_accounting"] == completed["usage"]["token_accounting"] == exported
    assert exported["extension"]["label"] == "safe extension"
    assert exported["extension"]["api_key"] == "[redacted]"
    assert exported["extension"]["token_accounting"] == "[redacted]"
    assert exported["stages"][0]["credential"] == "[redacted]"
    tool = next(event["data"] for event in trace["events"] if event["type"] == "tool.completed")
    assert tool["token_accounting"] == "[redacted]"
    assert "synthetic-usage-secret" not in response.text
    assert "synthetic-ledger-secret" not in response.text
    assert "synthetic-stage-secret" not in response.text
    assert "synthetic-tool-secret" not in response.text

    # Exercise the supported export -> measurement workflow, not an in-memory
    # ledger-only fixture that bypasses HTTP export redaction.
    report = build_report([trace], experiment_id="local-trace-accounting", source_kind="synthetic")
    tokens = report["all_runs"]["provider_tokens"]
    assert tokens["input_tokens"]["known_subtotal"] == 100
    assert tokens["input_tokens"]["complete_total"] == (None if partial else 100)
    assert tokens["input_tokens"]["known_count"] == (0 if partial else 1)
    assert tokens["output_tokens"]["known_subtotal"] == 12
    assert tokens["cache_read_input_tokens"]["known_subtotal"] == 0
    assert tokens["cache_read_input_tokens"]["complete_total"] == (None if partial else 0)


def test_malformed_accounting_wrapper_stays_redacted_in_http_export(client):
    run_id = send(client, conversation(client)["id"])["run_id"]
    poll(client, run_id)
    client.app.state.service.db.execute("UPDATE runs SET usage=? WHERE id=?", (
        json.dumps({"input_tokens": 12, "token_accounting": "synthetic-malformed-secret"}), run_id))
    response = client.get(f"/api/v1/runs/{run_id}/trace")
    assert response.status_code == 200
    assert response.json()["usage"]["token_accounting"] == "[redacted]"
    assert "synthetic-malformed-secret" not in response.text


@pytest.mark.parametrize("role", ["member", "admin"])
def test_trace_accounting_remains_private_to_run_owner(client, role):
    run_id = send(client, conversation(client)["id"])["run_id"]
    poll(client, run_id)
    created = client.post("/api/v1/users", json={"username": "other-user", "display_name": "Other",
                                              "password": PASSWORD, "role": role})
    assert created.status_code == 201
    login(client, "other-user")
    assert client.get(f"/api/v1/runs/{run_id}/trace").status_code == 404
