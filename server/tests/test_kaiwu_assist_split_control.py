"""Internal operator authentication and idempotent import on the control side."""

import pytest
from fastapi.testclient import TestClient

from app.kaiwu_assist import create_kaiwu_assist_bridge_router
from app.kaiwu_control import _hash_secret
from test_kaiwu_assist import ADMIN_TOKEN, BASE, SOURCE_ID, TARGET, TOKEN, env

BRIDGE = "/internal/kaiwu-assist/v1"
BRIDGE_SECRET = "synthetic-test-only-bridge-secret"


@pytest.fixture
def split_control(env, monkeypatch):
    app, _, db, device, connect, connector_id, outsider = env
    state = {"secret": BRIDGE_SECRET}
    monkeypatch.setattr(
        type(app.state.service.settings), "kaiwu_assist_bridge_secret", lambda _: state["secret"], raising=False,
    )
    if not any(getattr(route, "path", None) == BRIDGE + "/devices" for route in app.routes):
        app.include_router(create_kaiwu_assist_bridge_router(lambda: app.state.service))
    # The base fixture owns the app lifespan. Additional peer-address clients
    # must not close the database by shutting down that same app again.
    local = TestClient(app, base_url="https://testserver", client=("127.0.0.1", 50000))
    headers = {"Authorization": "Bearer " + BRIDGE_SECRET, "X-Kaiwu-Operator": "test-operator"}
    try:
        yield app, db, device, connect, connector_id, outsider, local, headers, state
    finally:
        local.close()


def test_bridge_requires_local_peer_independent_auth_and_session_ownership(split_control):
    app, db, device, _, connector_id, outsider, local, headers, state = split_control
    path = BRIDGE + "/devices"
    assert local.get(path).status_code == 403
    assert local.get(path, headers={**headers, "Authorization": "Bearer " + ADMIN_TOKEN}).status_code == 403
    assert local.get(path, headers={**headers, "X-Kaiwu-Operator": "workbench:owner"}).status_code == 403
    remote = TestClient(app, base_url="https://testserver", client=("192.0.2.2", 50000))
    try:
        assert remote.get(path, headers=headers).status_code == 403
    finally:
        remote.close()
    assert [r["id"] for r in local.get(path, headers=headers).json()["devices"]] == [connector_id]

    def ask(target):
        return local.post(BRIDGE + "/sessions", headers=headers, json={"connector_id": target, "name": "销售合同", "url": TARGET})

    assert ask(outsider[0]["connector_id"]).status_code == 404
    res = ask(connector_id)
    assert res.status_code == 201, res.text
    sid = res.json()["id"]
    session = {"session_id": sid}
    route = BRIDGE + "/sessions/" + sid
    assert local.get(route, headers={**headers, "X-Kaiwu-Operator": "other-operator"}).status_code == 404
    stored = db.one("SELECT created_by FROM kaiwu_assist_sessions WHERE id=?", (sid,))
    assert stored["created_by"] == "workbench:test-operator"
    assert device("offer").json()["offer"]["id"] == sid
    assert device("start", session).status_code == 200
    queued = local.post(route + "/jobs", headers=headers, json={"action": "discover", "bearer_token": TOKEN})
    assert queued.status_code == 200 and TOKEN not in queued.text
    state["secret"] = ""
    assert local.get(route, headers=headers).status_code == 403
    assert device("poll", session).json() == {"active": False, "job": None}
    assert db.one("SELECT status,sealed_bearer FROM kaiwu_assist_jobs WHERE session_id=?", (sid,)) == {
        "status": "failed", "sealed_bearer": None,
    }


def test_split_control_import_only_returns_same_config_with_live_consent(split_control):
    _, db, device, connect, connector_id, _, local, headers, _ = split_control
    created = local.post(
        BRIDGE + "/sessions", headers=headers, json={"connector_id": connector_id, "name": "销售合同", "url": TARGET},
    )
    assert created.status_code == 201
    sid = created.json()["id"]
    session = {"session_id": sid}
    route = BRIDGE + "/sessions/" + sid
    assert device("start", session).status_code == 200
    discover = local.post(route + "/jobs", headers=headers, json={"action": "discover"}).json()["latest_job"]["id"]
    assert device("poll", session).json()["job"]["id"] == discover
    assert device("result", {**session, "job_id": discover, "status": "succeeded", "code": "tools_loaded",
                            "tools": ["lookup_order"]}).status_code == 200
    connected = local.post(route + "/jobs", headers=headers, json={"action": "connect", "tools": ["lookup_order"]})
    assert connected.status_code == 200, connected.text
    job_id = connected.json()["latest_job"]["id"]
    assert device("poll", session).json()["job"]["id"] == job_id
    source = {"source_id": SOURCE_ID, "display_name": "销售合同", "allowed_tools": ["lookup_order"],
              "tool_schema_sha256": "b" * 64, "target_binding_sha256": "c" * 64, "transport": "streamable_http"}
    assert device(BASE + "/v1/sources", source).status_code == 200
    assert device("result", {**session, "job_id": job_id, "status": "succeeded", "code": "connected",
                            "source_id": SOURCE_ID}).status_code == 200

    def import_source():
        return local.post(route + "/import", headers=headers, json={"scope": "private"})

    assert import_source().status_code == 409
    with connect() as relay:
        assert relay.receive_json() == {"type": "ready"}
        assert device("poll", session).json() == {"active": True, "job": None}
        first = import_source()
        assert first.status_code == 200, first.text
        assert first.headers["Cache-Control"] == "no-store"
        assert set(first.json()) == {"mcp_config"}
        config = first.json()["mcp_config"]["mcpServers"]["销售合同"]
        token = config["headers"]["Authorization"].removeprefix("Bearer ")
        assert token.startswith("kw_int_")
        assert config["url"].endswith(f"/sources/{SOURCE_ID}/mcp")
        assert import_source().json() == first.json()
        issued = db.all("SELECT id,token_hash FROM kaiwu_integrations")
        assert len(issued) == 1 and issued[0]["token_hash"] == _hash_secret(token)
        assert db.one("SELECT count(*) AS n FROM connections")["n"] == 0
        db.execute("UPDATE kaiwu_integrations SET token_hash=? WHERE id=?", (_hash_secret("rotated"), issued[0]["id"]))
        assert import_source().json()["error"]["code"] == "ASSIST_INTEGRATION_CHANGED"
        assert device("stop", session).status_code == 200
        assert import_source().status_code == 409


def test_split_control_local_start_is_claimed_once_by_one_operator(split_control):
    _, db, device, _, connector_id, _, client, headers, _ = split_control
    offer = device("start", {"name": "销售合同", "url": TARGET})
    assert offer.status_code == 200, offer.text
    sid = offer.json()["id"]
    route = BRIDGE + "/sessions/" + sid
    other = {**headers, "X-Kaiwu-Operator": "other-operator"}
    assert client.get(route, headers=headers).status_code == 404
    assert device("poll", {"session_id": sid}).json() == {"active": True, "job": None}
    device_row = client.get(BRIDGE + "/devices", headers=other).json()["devices"][0]
    assert (device_row["id"], device_row["active_session_id"]) == (connector_id, sid)
    assert client.post(route + "/claim", headers=headers).json()["url"] == TARGET
    assert client.post(route + "/claim", headers=other).status_code == 404
    assert client.get(BRIDGE + "/devices", headers=other).json()["devices"][0]["active_session_id"] is None
    assert db.one("SELECT created_by FROM kaiwu_assist_sessions WHERE id=?", (sid,))["created_by"] == "workbench:test-operator"
