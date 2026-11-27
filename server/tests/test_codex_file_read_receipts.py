"""Keep reported read counts grounded in the native item and its exact turn."""
import json

import pytest

from app.codex_appserver import CodexHostUnavailable, _Session
from test_api import conversation
from test_codex_appserver_host import USER, log_events, make_host, requests
from test_conversation_codex import codex_env


THREAD = "thread-receipt"
TURN = "turn-receipt"
PRIVATE_PATH = "/private-fixture-a/receipt-input.md"
PRIVATE_OUTPUT = "synthetic-only-content-that-must-stay-out-of-the-read-receipt"
READ = {"type": "read", "name": "receipt-input.md", "path": PRIVATE_PATH,
        "command": f"cat {PRIVATE_PATH}"}
ZERO = {"succeeded": 0, "failed": 0, "unconfirmed": 0}


def command(method, item_id, *, status="inProgress", code=None, actions=None,
            thread=THREAD, turn=TURN, kind="commandExecution"):
    return {"method": f"item/{method}", "params": {
        "threadId": thread, "turnId": turn, "item": {
            "id": item_id, "type": kind, "command": READ["command"], "cwd": PRIVATE_PATH,
            "commandActions": [READ] if actions is None else actions,
            "status": status, "exitCode": code, "aggregatedOutput": PRIVATE_OUTPUT,
        },
    }}


def result_for(tmp_path, notices):
    host, _ = make_host(tmp_path)
    # A real idle stdio session receives the same notification shape as _read().
    with host._initialized_session(tmp_path) as session:
        for event in notices:
            session._note_notification(event)
        # Even a transient queued read record must not retain file names or output.
        queued = json.dumps(list(session.events.queue))
        assert PRIVATE_PATH not in queued and PRIVATE_OUTPUT not in queued
        session._note_notification({"method": "turn/completed", "params": {
            "threadId": THREAD, "turn": {"id": TURN, "status": "completed"},
        }})
        return session.wait_turn(THREAD, TURN)


def test_read_result_counts_each_native_item_once_across_duplicates_and_late_starts(tmp_path):
    notices = [
        command("started", "a"), command("started", "a"),
        command("completed", "a", status="completed", code=0),
        command("completed", "a", status="completed", code=0),
        command("completed", "b", status="completed", code=0), command("started", "b"),
        command("started", "no-finish"),
        command("completed", "nonzero", status="completed", code=7),
        command("completed", "failure", status="failed", code=0),
        command("completed", "denied", status="declined"),
    ]
    got = result_for(tmp_path, notices)
    assert got["file_reads"] == {"succeeded": 2, "failed": 3, "unconfirmed": 1}
    assert got["text"] == ""
    assert PRIVATE_PATH not in str(got) and PRIVATE_OUTPUT not in str(got)


@pytest.mark.parametrize("exit_code", [None, False, "0"])
def test_success_requires_an_explicit_integer_zero_exit(tmp_path, exit_code):
    got = result_for(tmp_path, [command("completed", "not-confirmed",
                                        status="completed", code=exit_code)])
    assert got["file_reads"] == {**ZERO, "unconfirmed": 1}


def test_terminal_status_and_conflicting_completions_need_consistent_evidence(tmp_path):
    got = result_for(tmp_path, [
        command("completed", "unfinished", status="inProgress", code=0),
        command("completed", "conflict", status="completed", code=0),
        command("completed", "conflict", status="failed", code=1),
        command("started", "conflict"),
    ])
    assert got["file_reads"] == {**ZERO, "unconfirmed": 2}


def test_model_claims_and_unclassified_or_mixed_tool_actions_do_not_count(tmp_path):
    claim = "I have read two files."
    notices = [
        {"method": "item/agentMessage/delta", "params": {
            "threadId": THREAD, "turnId": TURN, "delta": claim}},
        command("completed", "mixed", status="completed", code=0,
                actions=[READ, {"type": "search", "command": "search"}]),
        command("completed", "two-reads", status="completed", code=0, actions=[READ, READ]),
        command("completed", "empty", status="completed", code=0, actions=[]),
        command("completed", "unknown", status="completed", code=0,
                actions=[{"type": "unknown", "command": READ["command"]}]),
        command("completed", "wrong-kind", status="completed", code=0, kind="fileChange"),
    ]
    got = result_for(tmp_path, notices)
    assert got["text"] == claim
    assert got["file_reads"] == ZERO


@pytest.mark.parametrize(("thread", "turn"), [
    ("thread-other", TURN), (THREAD, "turn-other"), (None, TURN), (THREAD, None),
])
def test_reads_without_both_current_identities_cannot_be_attributed(tmp_path, thread, turn):
    notice = command("completed", "unrelated", status="completed", code=0,
                     thread=thread, turn=turn)
    try:
        got = result_for(tmp_path, [notice])
    except CodexHostUnavailable:
        # The host also rejects incompatible notifications at the protocol boundary.
        return
    assert got["file_reads"] == ZERO


def test_saved_counts_round_trip_through_owner_api_without_command_payload(codex_env, monkeypatch):
    client, _, host = codex_env
    cid = conversation(client)["id"]
    url = f"/api/v1/conversations/{cid}/codex"
    original = _Session._note_notification

    def include_read_before_completion(session, message):
        if message["method"] == "turn/completed":
            params = message["params"]
            original(session, command("completed", "source-a", status="completed", code=0,
                                      thread=params["threadId"], turn=params["turn"]["id"]))
        return original(session, message)

    monkeypatch.setattr(_Session, "_note_notification", include_read_before_completion)
    req = {"client_request_id": "file-read-one", "text": "synthetic", "prepare_if_needed": True}
    posted = client.post(url + "/turn", json=req)
    assert posted.status_code == 200, posted.text
    second = client.post(url + "/turn", json={**req, "client_request_id": "file-read-two"})
    assert second.status_code == 200, second.text
    replay = client.post(url + "/turn", json=req)
    assert replay.status_code == 200 and replay.json()["replayed"]
    owner = client.get("/api/v1/auth/me").json()["id"]
    key = host._key(owner, cid)
    saved = host._load(key)
    check = [posted.json(), replay.json(), client.get(url).json()["last_turn"],
             client.get(url + "/requests/file-read-one").json()["turn"],
             *client.get(url + "/history").json()["receipts"]]
    assert len(check) == 6
    for entry in check:
        assert entry["file_reads"] == {**ZERO, "succeeded": 1}
    for body in (saved, check):
        assert PRIVATE_PATH not in str(body) and PRIVATE_OUTPUT not in str(body)
        assert "source-a" not in str(body)


def test_older_receipts_do_not_invent_observed_counts(tmp_path):
    host, log = make_host(tmp_path)
    cid = "legacy-read"
    req = "before-file-read-receipts"
    host.run_turn(USER, cid, req, "synthetic", prepare_if_needed=True)
    key = host._key(USER, cid)
    binding = host._load(key)
    binding.pop("last_file_reads", None)
    for recent in binding["recent_requests"]:
        recent.pop("file_reads", None)
    host._save(key, binding)
    count = len(requests(log_events(log), "turn/start"))
    restored = [host.status(USER, cid)["last_turn"], host.request_status(USER, cid, req)["turn"],
                host.receipt_history(USER, cid)["receipts"][0],
                host.run_turn(USER, cid, req, "synthetic")]
    assert all(record.get("file_reads") is None for record in restored)
    assert len(requests(log_events(log), "turn/start")) == count
