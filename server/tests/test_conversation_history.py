from app.db import now

from test_api import PASSWORD, client, configured, conversation, login, poll, send


def test_message_windows_and_search_hits_are_bounded_and_conversation_scoped(client):
    service = client.app.state.service
    own = conversation(client)["id"]
    other = conversation(client)["id"]
    latest_run = "linked-run"
    timestamp = now()
    texts = [f"历史消息 {index:03d}" for index in range(360)]
    texts[10] = "十月发货明细：AB_50%\\北区，等待签收。"
    rows = [(f"msg-{index:03d}", own, "user", message, latest_run if index == 359 else None, timestamp)
            for index, message in enumerate(texts)]
    service.db.executemany("INSERT INTO messages(id,conversation_id,role,text,run_id,created_at) VALUES(?,?,?,?,?,?)",
                           [*rows, ("other-target", other, "user", "不同会话", None, timestamp)])
    route = f"/api/v1/conversations/{own}/messages"

    latest = client.get(route, params={"limit": 20}).json()
    assert latest["before"] == "msg-340"
    assert latest["after"] is None
    assert [item["id"] for item in latest["messages"]] == [f"msg-{index:03d}" for index in range(340, 360)]
    assert latest["latest_run_id"] == latest_run
    # Legacy callers still receive a plain list when no paging option is specified.
    assert len(client.get(route).json()) == 360

    older = client.get(route, params={"limit": 20, "before": latest["before"]}).json()
    assert older["before"] == "msg-320"
    assert older["after"] == "msg-339"
    around = client.get(route, params={"limit": 3, "around": "msg-010"}).json()
    assert [item["id"] for item in around["messages"]] == ["msg-009", "msg-010", "msg-011"]
    assert around["latest_run_id"] == latest_run
    newer = client.get(route, params={"limit": 2, "after": around["after"]}).json()
    assert [item["id"] for item in newer["messages"]] == ["msg-012", "msg-013"]
    assert client.get(route, params={"around": "other-target"}).status_code == 404
    assert client.get(route, params={"before": "msg-010", "after": "msg-020"}).status_code == 422
    assert client.get(route, params={"limit": 101}).status_code == 422

    search = client.get("/api/v1/conversations", params={"q": "ab_50%\\北区"}).json()
    assert [item["id"] for item in search] == [own]
    assert search[0]["search_hit"] == {"message_id": "msg-010", "before": "十月发货明细：", "match": "AB_50%\\北区", "after": "，等待签收。"}
    assert client.get("/api/v1/conversations", params={"q": "AB%北区"}).json() == []
    service.create_user({"username": "member", "display_name": "另一个用户", "password": PASSWORD, "role": "member"})
    login(client, "member")
    assert client.get(route, params={"limit": 20}).status_code == 404
    assert client.get("/api/v1/conversations", params={"q": "AB_50%\\北区"}).json() == []


def test_history_links_only_the_question_that_started_the_plan(client):
    service = client.app.state.service
    own = conversation(client)["id"]
    route = f"/api/v1/conversations/{own}/messages"
    parent = send(client, own, "wait", request_id="history-parent")
    parent_id, question_id = parent["run_id"], parent["message"]["id"]
    assert client.post(f"/api/v1/runs/{parent_id}/subagents", json={"tasks": ["统计商品数量"]}).status_code == 202
    assert client.post(f"/api/v1/runs/{parent_id}/cancel").status_code == 200
    poll(client, parent_id)
    plain = send(client, own, "ordinary question", request_id="history-plain")
    poll(client, plain["run_id"])
    plain_id = plain["message"]["id"]

    other = conversation(client)["id"]
    foreign = send(client, other, "wait", request_id="history-other")
    assert client.post(f"/api/v1/runs/{foreign['run_id']}/subagents", json={"tasks": ["核对库存"]}).status_code == 202
    # Simulate legacy missing backlinks and reject a different conversation's
    # run even if its forward link names a question in this conversation.
    service.db.execute("UPDATE messages SET run_id=NULL WHERE id=?", (question_id,))
    service.db.execute("UPDATE runs SET message_id=? WHERE id=?", (plain_id, foreign["run_id"]))
    service.db.execute(
        "INSERT INTO messages(id,conversation_id,role,text,run_id,created_at) VALUES(?,?,'user',?,?,?)",
        ("steering-message", own, "follow-up for the active run", parent_id, now()),
    )

    for params in ({}, {"limit": 20}, {"limit": 1, "around": question_id}, {"limit": 20, "before": plain_id}):
        data = client.get(route, params=params).json()
        messages = data if isinstance(data, list) else data["messages"]
        question = next(message for message in messages if message["id"] == question_id)
        assert question["run_id"] is None
        assert question["has_subagents"] is True
        assert question["subagent_run_id"] == parent_id
        for message in messages:
            if message["role"] == "assistant":
                assert "has_subagents" not in message
            elif message["id"] != question_id:
                assert message["has_subagents"] is False
                assert "subagent_run_id" not in message

    after_question = client.get(route, params={"limit": 20, "after": question_id}).json()
    assert {message["id"]: message["has_subagents"] for message in after_question["messages"] if message["role"] == "user"} == {
        plain_id: False, "steering-message": False,
    }


def test_only_canonical_settled_answers_get_their_own_partial_plan_link(client):
    service = client.app.state.service
    cid = conversation(client)["id"]
    owner = service.db.one("SELECT * FROM users WHERE username='admin'")
    route = f"/api/v1/conversations/{cid}/messages"
    parent, _ = service.prepare_run(cid, {"client_request_id": "partial-run", "text": "分项核对", "file_ids": []}, owner)
    run_id = parent["run_id"]
    service.subagents.create_plan(run_id, owner["id"], ["第一项", "第二项"])
    service.db.execute("UPDATE subagent_children SET status='failed' WHERE parent_run_id=?", (run_id,))
    for message_id in ("partial-answer", "unrelated-answer"):
        service.db.execute("INSERT INTO messages(id,conversation_id,role,text,run_id,created_at) VALUES(?,?,'assistant',?,?,?)",
                           (message_id, cid, "两项都已完成。", run_id, now()))
    service.db.execute("UPDATE runs SET assistant_message_id='partial-answer' WHERE id=?", (run_id,))

    def only_message(message_id):
        response = client.get(route, params={"limit": 1, "around": message_id})
        assert response.status_code == 200
        records = response.json()["messages"]
        assert len(records) == 1
        return records[0]

    assert "subagent_run_id" not in only_message("partial-answer")
    service.db.execute("UPDATE runs SET status='succeeded' WHERE id=?", (run_id,))
    assert only_message("partial-answer")["subagent_run_id"] == run_id
    assert "subagent_run_id" not in only_message("unrelated-answer")
    service.db.execute("UPDATE messages SET run_id='incorrect' WHERE id='partial-answer'")
    assert "subagent_run_id" not in only_message("partial-answer")
    service.db.execute("UPDATE messages SET run_id=NULL WHERE id='partial-answer'")
    assert only_message("partial-answer")["subagent_run_id"] == run_id
    service.db.execute("UPDATE subagent_children SET status='succeeded' WHERE parent_run_id=?", (run_id,))
    assert "subagent_run_id" not in only_message("partial-answer")
