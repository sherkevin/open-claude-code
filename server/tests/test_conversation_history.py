from app.db import now

from test_api import PASSWORD, client, configured, conversation, login


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
