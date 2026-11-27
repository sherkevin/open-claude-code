"""Conversation titles observed through the owner API and the real Codex host admission path."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.codex_appserver import CodexHostUnavailable, _Session
from app.conversation_title import DEFAULT_TITLE
from test_api import PASSWORD, conversation, login
from test_codex_appserver_host import log_events, requests
from test_conversation_codex import codex_env


PWD = "请说明 pwd 如何显示当前工作目录。不要执行命令或修改文件。"
CALENDAR = "请说明如何列出下周日历安排。不要执行命令或修改文件。"
BASE = "/api/v1/conversations"


def turn(client, cid, request_id, text, *, prepare=True):
    return client.post(f"{BASE}/{cid}/codex/turn", json={
        "client_request_id": request_id, "text": text, "prepare_if_needed": prepare,
    })


def home_title(client, cid):
    detail = client.get(f"{BASE}/{cid}")
    listing = client.get(BASE)
    assert detail.status_code == listing.status_code == 200
    title = detail.json()["title"]
    assert [item["title"] for item in listing.json() if item["id"] == cid] == [title]
    return title


def test_distinct_accepted_topics_reach_home_and_survive_second_turn_and_replay(codex_env, tmp_path):
    client, _, _ = codex_env
    pwd, calendar = (conversation(client)["id"] for _ in range(2))
    assert client.post(f"{BASE}/{calendar}/codex/thread").status_code == 200
    assert home_title(client, calendar) == DEFAULT_TITLE
    assert turn(client, pwd, "pwd-first", PWD).status_code == 200
    assert turn(client, calendar, "calendar-first", CALENDAR).status_code == 200
    p, c = home_title(client, pwd), home_title(client, calendar)
    assert p != c
    assert "pwd" in p.lower() or "目录" in p
    assert "日历" in c or "日程" in c
    assert p != PWD and c != CALENDAR
    assert turn(client, pwd, "later-calendar", CALENDAR).status_code == 200
    replay = turn(client, pwd, "pwd-first", PWD)
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert home_title(client, pwd) == p
    # Listing and replay did not start extra native turns for titles.
    assert sum(len(requests(log_events(log), "turn/start")) for log in tmp_path.glob("peer-*.jsonl")) == 3


@pytest.mark.parametrize("manual", ["我的目录手册", DEFAULT_TITLE])
def test_manual_rename_wins_while_the_first_engineering_request_is_active(codex_env, monkeypatch, manual):
    client, _, _ = codex_env
    cid = conversation(client)["id"]
    admitted, finish = Event(), Event()
    wait_turn = _Session.wait_turn

    def hold_outcome(session, *args, **kwargs):
        admitted.set()
        assert finish.wait(5)
        return wait_turn(session, *args, **kwargs)

    monkeypatch.setattr(_Session, "wait_turn", hold_outcome)
    with ThreadPoolExecutor(max_workers=1) as worker:
        first = worker.submit(turn, client, cid, "rename-first", PWD)
        try:
            assert admitted.wait(5)
            assert client.patch(f"{BASE}/{cid}", json={"title": "人工设定"}).status_code == 200
            assert client.patch(f"{BASE}/{cid}", json={"title": manual}).status_code == 200
        finally:
            finish.set()
        assert first.result(timeout=5).status_code == 200
    assert turn(client, cid, "rename-next", CALENDAR).status_code == 200
    assert turn(client, cid, "rename-first", PWD).json()["replayed"]
    assert home_title(client, cid) == manual


def test_busy_conversation_database_cannot_delay_native_output_or_its_durable_receipt(codex_env, monkeypatch):
    client, service, host = codex_env
    cid = conversation(client)["id"]
    started, allow_admission, first_output, recorded = (Event() for _ in range(4))
    start_turn, wait_turn, save = _Session.start_turn, _Session.wait_turn, host._save
    request_id = "title-contention"

    def wait_for_database_owner(session, *args):
        native_id = start_turn(session, *args)
        started.set()
        assert allow_admission.wait(5)
        return native_id

    def observe_output(session, thread_id, turn_id, **kwargs):
        publish = kwargs["first_output"]

        def forward(text):
            publish(text)
            first_output.set()

        return wait_turn(session, thread_id, turn_id, first_output=forward)

    def observe_saved_receipt(key, binding):
        save(key, binding)
        if binding.get("last_request_id") == request_id:
            recorded.set()

    monkeypatch.setattr(_Session, "start_turn", wait_for_database_owner)
    monkeypatch.setattr(_Session, "wait_turn", observe_output)
    monkeypatch.setattr(host, "_save", observe_saved_receipt)
    with ThreadPoolExecutor(max_workers=1) as worker:
        first = worker.submit(turn, client, cid, request_id, PWD)
        try:
            assert started.wait(5)
            with service.db.lock:
                allow_admission.set()
                assert first_output.wait(5), "title storage blocked native first output"
                assert recorded.wait(5), "title storage blocked the durable terminal receipt"
        finally:
            allow_admission.set()
        assert first.result(timeout=5).status_code == 200
    assert home_title(client, cid) != DEFAULT_TITLE


@pytest.mark.parametrize("legacy", ["explicit_old_flag", "already_bound_and_used"])
def test_legacy_topics_are_not_guessed_from_new_input_or_historical_replay(codex_env, legacy):
    client, service, host = codex_env
    cid = conversation(client)["id"]
    if legacy == "explicit_old_flag":
        # Existing rows from before automatic titles migrated with this value.
        service.db.execute("UPDATE conversations SET auto_title_pending=0 WHERE id=?", (cid,))
    else:
        # The earlier product API committed host admissions without changing the conversation title.
        owner = service.db.one("SELECT user_id FROM conversations WHERE id=?", (cid,))["user_id"]
        host.run_turn(owner, cid, "original-legacy", PWD, True)
        key = host._key(owner, cid)
        with host._state_lock(key):
            binding = host._load(key)
            # Old bindings kept digests but did not identify the first accepted turn.
            binding.pop("first_turn_request_id", None)
            host._save(key, binding)
        replay = turn(client, cid, "original-legacy", PWD)
        assert replay.status_code == 200 and replay.json()["replayed"]
    assert turn(client, cid, "next-legacy", CALENDAR).status_code == 200
    assert home_title(client, cid) == DEFAULT_TITLE


def test_only_accepted_owner_input_can_consume_the_default_title(codex_env, monkeypatch):
    client, service, _ = codex_env
    cid = conversation(client)["id"]
    service.create_user({"username": "second", "display_name": "second", "password": PASSWORD, "role": "member"})
    login(client, "second")
    assert turn(client, cid, "foreign-first", CALENDAR).status_code == 404
    login(client)
    assert turn(client, cid, "needs-binding", CALENDAR, prepare=False).status_code == 409
    assert turn(client, cid, "short", CALENDAR).status_code == 422
    with monkeypatch.context() as patch:
        def fail_at_init(_session):
            raise CodexHostUnavailable("synthetic initialization failure")
        patch.setattr(_Session, "initialize", fail_at_init)
        assert turn(client, cid, "failed-at-init", CALENDAR).status_code == 503
    assert home_title(client, cid) == DEFAULT_TITLE
    assert turn(client, cid, "valid-owner", PWD).status_code == 200
    assert home_title(client, cid) != DEFAULT_TITLE


def test_an_uncertain_start_does_not_publish_an_unconfirmed_topic(codex_env, monkeypatch):
    client, _, _ = codex_env
    cid = conversation(client)["id"]

    def lose_start_receipt(_session, _thread_id, _text):
        raise CodexHostUnavailable("synthetic lost turn/start reply")

    monkeypatch.setattr(_Session, "start_turn", lose_start_receipt)
    assert turn(client, cid, "uncertain-admit", CALENDAR).status_code == 503
    receipt = client.get(f"{BASE}/{cid}/codex/requests/uncertain-admit")
    assert receipt.json()["state"] == "pending"
    assert turn(client, cid, "uncertain-admit", CALENDAR).status_code == 409
    assert home_title(client, cid) == DEFAULT_TITLE


def test_receipt_survives_failed_title_write_and_only_original_replay_repairs_it(codex_env):
    client, service, _ = codex_env
    cid = conversation(client)["id"]
    service.db.execute(
        "CREATE TRIGGER temporary_title_failure BEFORE UPDATE OF title ON conversations "
        "BEGIN SELECT RAISE(ABORT, 'synthetic title write failure'); END"
    )
    try:
        first = turn(client, cid, "recover-first", PWD)
        assert first.status_code == 200 and first.json()["status"] == "completed"
        assert home_title(client, cid) == DEFAULT_TITLE
    finally:
        service.db.execute("DROP TRIGGER temporary_title_failure")

    assert turn(client, cid, "later-admission", CALENDAR).status_code == 200
    assert turn(client, cid, "recover-first", CALENDAR).status_code == 409
    assert home_title(client, cid) == DEFAULT_TITLE
    replay = turn(client, cid, "recover-first", PWD)
    assert replay.status_code == 200 and replay.json()["turn_id"] == first.json()["turn_id"]
    assert replay.json()["replayed"]
    assert home_title(client, cid) != DEFAULT_TITLE
