"""The split workbench authenticates operators and owns imported connections."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.db import now
from app.kaiwu_assist_workbench import BRIDGE_PATH
from app.main import create_app
from app.security import password_hash
from test_kaiwu_enterprise import settings

PORTAL = "/api/v1/connections/kaiwu-assist"
SESSION = "asst_" + "5" * 32
TEST_PASSWORD = "synthetic-assist-workbench-password"
RELAY_TOKEN = "synthetic-integration-credential"


def test_workbench_forwards_only_signed_in_admin_and_imports_locally(tmp_path, monkeypatch):
    from app import kaiwu_assist_workbench as portal

    requests = []

    def forward(service, operator, method, path, payload=None):
        requests.append((operator, method, path, payload))
        if path.endswith("/import"):
            return {"mcp_config": {"mcpServers": {"内部测试": {
                "type": "http", "url": "https://testserver/projects/kaiwu/mcp/sources/src_example/mcp",
                "headers": {"Authorization": "Bearer " + RELAY_TOKEN},
            }}}}
        return {"devices": []} if path == "/devices" else {"id": SESSION}

    monkeypatch.setattr(portal, "_forward", forward)
    config = replace(settings(tmp_path), kaiwu_assist_control_origin="http://127.0.0.1:18093",
                     kaiwu_control_enabled=False, mcp_hosts=("testserver",))
    app = create_app(config)
    with TestClient(app, base_url="https://testserver") as client:
        db = app.state.service.db
        for user, role in (("owner", "admin"), ("reader", "member")):
            db.execute(
                "INSERT INTO users(id,username,display_name,password_hash,role,enabled,created_at) VALUES(?,?,?,?,?,1,?)",
                (user, user, user, password_hash(TEST_PASSWORD), role, now()),
            )
        assert client.get(PORTAL + "/devices").status_code == 401
        assert client.post("/api/v1/auth/login", json={"username":"reader","password":TEST_PASSWORD}).is_success
        assert client.get(PORTAL + "/devices").status_code == 403
        assert not requests
        assert client.post("/api/v1/auth/login", json={"username":"owner","password":TEST_PASSWORD}).is_success
        assert client.get(PORTAL + "/devices", headers={"X-Kaiwu-Operator":"forged"}).json() == {"devices":[]}
        assert requests[-1] == ("owner", "GET", "/devices", None)
        assert client.get(BRIDGE_PATH + "/devices").status_code == 404
        assert client.get(PORTAL + "/sessions/not-a-valid-session").status_code == 422
        assert len(requests) == 1
        claim_path = f"/sessions/{SESSION}/claim"
        assert client.post(PORTAL + claim_path, headers={"X-Kaiwu-Operator": "forged"}).json() == {"id": SESSION}
        assert requests[-1] == ("owner", "POST", claim_path, None)

        path = PORTAL + f"/sessions/{SESSION}/import"
        first = client.post(path, json={"scope":"private"})
        assert first.status_code == 200, first.text
        assert RELAY_TOKEN not in first.text
        connection = first.json()["connection"]
        assert connection["scope"] == "private"
        assert client.post(path, json={"scope":"private"}).json() == first.json()
        assert requests.count(("owner", "POST", f"/sessions/{SESSION}/import", {"scope":"private"})) == 1
        assert client.get(PORTAL + f"/sessions/{SESSION}").json()["imported_connection_id"] == connection["id"]
        assert db.one("SELECT count(*) AS n FROM connections")["n"] == 1
        assert RELAY_TOKEN not in db.one("SELECT secret FROM connections")["secret"]
        assert db.one("SELECT count(*) AS n FROM kaiwu_connectors")["n"] == 0


def test_bridge_origin_allows_only_loopback_http(tmp_path):
    original = settings(tmp_path)
    for origin in ("http://localhost:18093", "http://127.0.0.1:99999", "https://example.com",
                   "http://127.0.0.1:18093/internal", "http://127.0.0.1@remote.example:80"):
        with pytest.raises(ValueError, match="loopback"):
            replace(original, kaiwu_assist_control_origin=origin)
