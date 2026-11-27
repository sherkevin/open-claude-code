"""Steering uses the already-running, owner-bound native turn and a durable admission."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

import pytest

from app import codex_appserver as app
from test_codex_appserver_host import (
    CID, FAKE, OTHER, USER, log_events, make_host, requests, state_path, wait_for_log,
)

RID = "steer-request-001"
SECRET = "fixture text that must never enter a host steer receipt"

# Extend the existing synthetic stdio peer only inside this test's temporary
# directory. Normal start, stream, interrupt and terminal handling stay intact.
PEER = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
import codex_appserver_fake_peer as peer
gate = Path(__file__).parent / "release"
mode = sys.argv.pop(1)
original = peer.handle
def handle(message):
    method = message.get("method")
    if method == "turn/interrupt" and mode in ("reject-interrupt", "ambiguous-interrupt") and not gate.exists():
        return peer.error(message["id"], -32602 if mode == "reject-interrupt" else -32603, "interrupt refused")
    if method == "turn/interrupt" and mode == "slow-interrupt":
        peer.log({"event":"interrupt-pending"})
        while not gate.exists(): time.sleep(.01)
    if method != "turn/steer":
        return original(message)
    rid, params = message["id"], message["params"]
    peer.log({"event":"request", "method":method, "params":params, "id":rid})
    if mode in ("defer", "late"):
        while not gate.exists(): time.sleep(.01)
    if mode == "rejected":
        return peer.error(rid, -32602, "private rejection detail")
    if mode == "internal-error":
        return peer.error(rid, -32603, "outcome uncertain")
    if mode == "mixed":
        return peer.send({"id":rid,"result":{"turnId":params["expectedTurnId"]},"error":{}})
    if mode == "bad-error":
        return peer.send({"id":rid,"error":None})
    if mode == "malformed":
        return peer.send({"id":rid,"result":[]})
    if mode == "wrong-turn":
        return peer.result(rid, {"turnId":"unrelated-turn"})
    if mode == "exit":
        import os
        os._exit(0)
    peer.result(rid, {"turnId":params["expectedTurnId"]})
peer.handle = handle
peer.main()
"""


def setup_peer(path, mode="ok"):
    template, log = make_host(path, "interrupt", response_timeout=0.65 if mode == "late" else 3)
    peer = path / "steer_peer.py"
    peer.write_text(PEER)
    cmd = (sys.executable, str(peer), str(FAKE.parent), mode, *template.config.command[2:])
    return app.CodexHost(replace(template.config, command=cmd)), log


def active_turn(host):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        active = host.status(USER, CID)["active_turn"]
        if active:
            return active["turn_id"]
        time.sleep(.01)
    pytest.fail("the synthetic turn did not start")


@contextmanager
def running(host):
    with ThreadPoolExecutor() as pool:
        run = pool.submit(host.run_turn, USER, CID, "original-001", "original", True)
        turn = active_turn(host)
        try:
            yield turn, run
        finally:
            if not run.done() and host.status(USER, CID)["active_turn"]:
                host.interrupt(USER, CID, turn)
            assert run.result(timeout=5)["status"] == "interrupted"


@pytest.mark.parametrize(("mode", "state"), [
    ("ok", "delivered"), ("rejected", "rejected"), ("wrong-turn", "uncertain"),
    ("mixed", "uncertain"), ("bad-error", "uncertain"), ("malformed", "uncertain"),
    ("internal-error", "uncertain"),
])
def test_wire_and_durable_projection(tmp_path, mode, state):
    host, log = setup_peer(tmp_path, mode)
    assert host.steer_history(USER, CID) == {"receipts": []}
    assert not (tmp_path/"codex-appserver").exists()
    with running(host) as (turn, _):
        result = host.steer(USER, CID, turn, RID, SECRET)
        assert result == {"client_request_id": RID, "turn_id": turn, "state": state}
        assert host.steer(USER, CID, turn, RID, SECRET) == result
        assert host.steer_history(OTHER, CID) == {"receipts": []}
        with pytest.raises(app.CodexHostConflict):
            host.steer(OTHER, CID, turn, RID, SECRET)
        for wrong_turn, text in [(turn, "different"), ("other-turn", SECRET)]:
            with pytest.raises(app.CodexHostConflict):
                host.steer(USER, CID, wrong_turn, RID, text)
    assert host.steer(USER, CID, turn, RID, SECRET) == result
    restarted = app.CodexHost(host.config)
    assert restarted.steer_history(USER, CID) == {"receipts": [result]}
    assert SECRET not in state_path(tmp_path, USER, CID).read_text()
    assert "private rejection detail" not in str(result)
    wire = log_events(log)
    assert len(requests(wire, "turn/start")) == 1
    assert len([e for e in wire if e["event"]=="startup"]) == 1
    assert [r["params"] for r in requests(wire, "turn/steer")] == [{
        "threadId": host.status(USER,CID)["thread_id"], "expectedTurnId": turn,
        "clientUserMessageId": RID, "input": [{"type":"text","text":SECRET}]}]


@pytest.mark.parametrize("mode", ["defer", "late"])
def test_concurrent_and_timed_out_admission(tmp_path, mode):
    host, log = setup_peer(tmp_path, mode)
    with running(host) as (turn,_), ThreadPoolExecutor() as pool:
        attempt = pool.submit(host.steer, USER,CID,turn,RID,SECRET)
        wait_for_log(log, lambda events: requests(events,"turn/steer"))
        waiting = host.steer_history(USER,CID)["receipts"][0]
        assert waiting["state"]=="uncertain"
        assert app.CodexHost(host.config).steer(USER,CID,turn,RID,SECRET)==waiting
        if mode=="late":
            assert attempt.result(timeout=3)==waiting
        (tmp_path/"release").touch()
        assert attempt.result(timeout=3)["state"]==("delivered" if mode=="defer" else "uncertain")
    assert len(requests(log_events(log), "turn/steer"))==1


def test_lost_runtime_never_resubmits(tmp_path):
    host, log = setup_peer(tmp_path,"exit")
    with ThreadPoolExecutor() as pool:
        run = pool.submit(host.run_turn,USER,CID,"original-001","original",True)
        turn = active_turn(host)
        receipt = host.steer(USER,CID,turn,RID,SECRET)
        assert receipt["state"]=="uncertain"
        with pytest.raises(app.CodexHostUnavailable):
            run.result(timeout=5)
    reopened = app.CodexHost(host.config)
    assert reopened.steer_history(USER,CID) == {"receipts":[receipt]}
    assert reopened.steer(USER,CID,turn,RID,SECRET) == receipt
    assert reopened.steer(USER,CID,turn,"steer-fresh-id",SECRET)["state"]=="rejected"
    assert len(requests(log_events(log),"turn/steer")) == 1


def test_steer_after_interrupt_begins_is_closed_before_ack(tmp_path):
    host, log = setup_peer(tmp_path, "slow-interrupt")
    with running(host) as (turn, run), ThreadPoolExecutor() as pool:
        stop = pool.submit(host.interrupt,USER,CID,turn)
        try:
            wait_for_log(log,lambda rows:any(e['event']=='interrupt-pending' for e in rows))
            receipt = host.steer(USER,CID,turn,RID,SECRET)
            assert receipt["state"]=="closed"
            assert not requests(log_events(log),"turn/steer")
        finally:
            (tmp_path/"release").touch()
        stop.result(timeout=3)
        run.result(timeout=5)


def test_persistence_failure_before_and_after_wire(tmp_path, monkeypatch):
    host, log = setup_peer(tmp_path)
    original_save = host._save
    refused_state = "uncertain"
    def fail(key, binding):
        if binding.get("steer_requests",{}).get(RID,{}).get("state")==refused_state:
            raise app.CodexHostUnavailable("test persistence failure")
        original_save(key,binding)
    with running(host) as (turn, _):
        with monkeypatch.context() as patch:
            patch.setattr(host,"_save",fail)
            with pytest.raises(app.CodexHostUnavailable):
                host.steer(USER,CID,turn,RID,SECRET)
            assert not requests(log_events(log),"turn/steer")
            assert not host.steer_history(USER,CID)["receipts"]
            refused_state = "delivered"
            assert host.steer(USER,CID,turn,RID,SECRET)["state"] == "uncertain"
        assert host.steer(USER,CID,turn,RID,SECRET)["state"] == "uncertain"
    assert len(requests(log_events(log),"turn/steer"))==1


def test_bounded_tail_keeps_old_ids_and_owner_guard(tmp_path,monkeypatch):
    host,log=setup_peer(tmp_path)
    host.ensure_thread(USER,CID)
    monkeypatch.setattr(app,"MAX_STEER_REQUESTS",10)
    ids=[f"request-{index:02}" for index in range(10)]
    for rid in ids:
        assert host.steer(USER,CID,"finished-turn",rid,SECRET)["state"]=="closed"
    assert [x["client_request_id"] for x in host.steer_history(USER,CID)["receipts"]]==ids[-8:][::-1]
    assert host.steer(USER,CID,"finished-turn",ids[0],SECRET)["state"]=="closed"
    with pytest.raises(app.CodexHostConflict): host.steer(USER,CID,"finished-turn",ids[0],"changed")
    with pytest.raises(app.CodexHostConflict): host.steer(USER,CID,"finished-turn","request-10",SECRET)
    assert not requests(log_events(log),"turn/steer")
    path=state_path(tmp_path,USER,CID)
    stored=json.loads(path.read_text())
    stored["user_id"]=OTHER
    path.write_text(json.dumps(stored))
    with pytest.raises(app.CodexHostUnavailable): host.steer_history(USER,CID)
    with pytest.raises(app.CodexHostUnavailable): host.steer(USER,CID,"finished-turn",ids[0],SECRET)


@pytest.mark.parametrize(("mode", "expected"), [("reject-interrupt", "delivered"), ("ambiguous-interrupt", "closed")])
def test_interrupt_error_preserves_known_effect(tmp_path, mode, expected):
    host, log = setup_peer(tmp_path, mode)
    with running(host) as (turn, _):
        try:
            with pytest.raises(app.CodexHostRejected):
                host.interrupt(USER, CID, turn)
            assert host.steer(USER, CID, turn, RID, SECRET)["state"] == expected
            assert len(requests(log_events(log), "turn/steer")) == int(expected == "delivered")
        finally:
            (tmp_path / "release").touch()


def test_terminal_saved_between_native_steer_ack_and_receipt_merge(tmp_path, monkeypatch):
    import threading
    host, log = setup_peer(tmp_path)
    ack, resume = threading.Event(), threading.Event()
    original = app._Session.steer_turn
    def defer_receipt(*args):
        state = original(*args)
        ack.set()
        assert resume.wait(4)
        return state
    monkeypatch.setattr(app._Session, "steer_turn", defer_receipt)
    with running(host) as (turn, run), ThreadPoolExecutor() as pool:
        pending = pool.submit(host.steer, USER, CID, turn, RID, SECRET)
        try:
            assert ack.wait(3)
            host.interrupt(USER, CID, turn)
            assert run.result(timeout=3)["status"] == "interrupted"
        finally:
            resume.set()
        assert pending.result(timeout=3)["state"] == "delivered"
    assert host.steer_history(USER, CID)["receipts"][0]["state"] == "delivered"
    assert host.status(USER, CID)["last_turn"]["status"] == "interrupted"
    assert len(requests(log_events(log), "turn/steer")) == 1
