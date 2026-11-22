"""Exercise the web callback and actual MCP HTTP discovery against a local issuer."""
import asyncio
import base64
import hashlib
import json
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.mcp_tools import build_mcp_tools


@pytest.fixture
def issuer():
    state = SimpleNamespace(grants={}, calls=[], methods=[], refreshes=0, revoked=[],
                            access="loopback-expiring-token-v1", refresh="loopback-refresh-token-v1", tool_name="lookup",
                            untrusted_endpoint=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status, body=None, headers=None):
            content = json.dumps(body).encode() if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            path = urlsplit(self.path)
            if path.path == "/.well-known/oauth-protected-resource/mcp":
                self.reply(200, {"resource": state.resource, "authorization_servers": [state.url], "scopes_supported": ["lookup:read"]})
            elif path.path == "/.well-known/oauth-authorization-server":
                self.reply(200, {"issuer": state.url, "authorization_endpoint": state.url + "/authorize",
                    "token_endpoint": "http://127.0.0.1:1/untrusted" if state.untrusted_endpoint else state.url + "/token",
                    "revocation_endpoint": state.url + "/revoke", "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"]})
            elif path.path == "/authorize":
                request = {key: values[0] for key, values in parse_qs(path.query).items()}
                assert request["client_id"] == "kaiwu-loopback" and request["resource"] == state.resource
                assert request["scope"] == "lookup:read" and request["code_challenge_method"] == "S256"
                state.grants["one-use-local-code"] = request
                callback = urlencode({"code": "one-use-local-code", "state": request["state"], "iss": state.url})
                self.reply(302, headers={"Location": request["redirect_uri"] + "?" + callback})
            else:
                self.reply(404)

        def do_DELETE(self):
            self.reply(200)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/mcp":
                self.mcp(raw)
                return
            request = {key: values[0] for key, values in parse_qs(raw.decode()).items()}
            assert request["client_id"] == "kaiwu-loopback"
            if self.path == "/token":
                assert request["resource"] == state.resource
                if request["grant_type"] == "authorization_code":
                    grant = state.grants.pop(request["code"])
                    assert request["redirect_uri"] == grant["redirect_uri"]
                    digest = hashlib.sha256(request["code_verifier"].encode()).digest()
                    assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == grant["code_challenge"]
                else:
                    assert request["grant_type"] == "refresh_token" and request["refresh_token"] == state.refresh
                    state.refreshes += 1
                    state.access = "loopback-refreshed-token-v2"
                    state.refresh = "loopback-refresh-token-v2"
                self.reply(200, {"access_token": state.access, "refresh_token": state.refresh, "token_type": "Bearer",
                                 "expires_in": 120, "scope": "lookup:read"})
            elif self.path == "/revoke":
                assert request["token"] == state.refresh and request["token_type_hint"] == "refresh_token"
                state.revoked.append(request["token"])
                state.access = None
                self.reply(200)
            else:
                self.reply(404)

        def mcp(self, raw):
            if not state.access or self.headers.get("Authorization") != f"Bearer {state.access}":
                self.reply(401)
                return
            request = json.loads(raw)
            method = request["method"]
            state.methods.append(method)
            if "id" not in request:
                self.reply(202)
                return
            if method == "initialize":
                value = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}},
                         "serverInfo": {"name": "loopback-oauth", "version": "1"}}
            elif method == "tools/list":
                value = {"tools": [{"name": state.tool_name, "description": "Read the local fixture",
                         "inputSchema": {"type": "object", "additionalProperties": False}}]}
            else:
                assert method == "tools/call"
                state.calls.append(request["params"])
                value = {"content": [{"type": "text", "text": f"Pipe result {state.access}"}]}
            self.reply(200, {"jsonrpc": "2.0", "id": request["id"], "result": value})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    state.resource = state.url + "/mcp"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_oauth_link_refresh_revoke_and_mcp_tool_refresh(tmp_path, monkeypatch, issuer):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    profile_path = tmp_path / "oauth.json"
    callback = "/api/v1/connections/oauth/callback/local"
    profile_path.write_text(json.dumps({"local": {"resource": issuer.resource, "issuer": issuer.url,
        "client_id": "kaiwu-loopback", "redirect_uri": "http://127.0.0.1" + callback, "scope": "lookup:read"}}))
    settings = Settings(data_dir=tmp_path / "data", provider_settings="", runtime_type="harness", mcp_hosts=("127.0.0.1",),
                        mcp_oauth_profiles=str(profile_path), mcp_oauth_loopback=True)
    app = create_app(settings, runtime_factory=lambda *_: pytest.fail("OAuth flow did not request a model run"))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        service = app.state.service
        password = "Synthetic-oauth-test-123"
        owner = service.create_user({"username": "owner", "display_name": "Owner", "role": "admin", "password": password})
        service.create_user({"username": "other", "display_name": "Other", "role": "member", "password": password})
        assert client.post("/api/v1/auth/login", json={"username": "owner", "password": password}).status_code == 200
        assert client.get("/api/v1/connections/oauth/profiles").json() == [{"url": issuer.resource}]
        connection = {"name": "OAuth MCP", "url": issuer.resource, "auth_type": "oauth"}
        assert client.post("/api/v1/connections", json={**connection, "scope": "team"}).status_code == 422
        assert client.post("/api/v1/connections", json={**connection, "url": issuer.resource + "/unknown"}).status_code == 403
        response = client.post("/api/v1/connections", json=connection)
        assert response.status_code == 201, response.text
        registered = response.json()
        assert registered["status"] == "needs_auth" and not registered["credential_present"]
        connection_id = registered["id"]
        path = f"/api/v1/connections/{connection_id}"

        issuer.untrusted_endpoint = True
        assert client.post(path + "/oauth/start").json()["error"]["code"] == "MCP_OAUTH_METADATA_INVALID"
        issuer.untrusted_endpoint = False
        authorization = client.post(path + "/oauth/start")
        assert authorization.status_code == 200, authorization.text
        response = httpx.get(authorization.json()["authorization_url"], follow_redirects=False, trust_env=False)
        assert response.status_code == 302
        target = response.headers["Location"]
        with closing(TestClient(app, base_url="http://127.0.0.1")) as other:
            assert other.post("/api/v1/auth/login", json={"username": "other", "password": password}).status_code == 200
            assert other.get(path).status_code == 404
            assert other.get(target, follow_redirects=False).headers["Location"] == "/?mcp_oauth=failed"
        returned = client.get(target, follow_redirects=False)
        assert returned.status_code == 303 and returned.headers["Location"].endswith(connection_id)
        assert returned.headers["Referrer-Policy"] == "no-referrer"
        assert client.get(target, follow_redirects=False).headers["Location"] == "/?mcp_oauth=failed"
        connected = client.get(path)
        assert connected.json()["status"] == "connected" and connected.json()["tools"][0]["name"] == "lookup"
        assert issuer.access not in connected.text and issuer.refresh not in connected.text
        assert not issuer.calls and "initialize" in issuer.methods and "tools/list" in issuer.methods

        row = service.require("connections", connection_id)
        grant = service.db.one("SELECT * FROM mcp_oauth_grants WHERE connection_id=?", (connection_id,))
        assert issuer.access not in row["secret"] and issuer.refresh not in grant["refresh_token"]
        service.db.execute("UPDATE mcp_oauth_grants SET expires=0 WHERE connection_id=?", (connection_id,))

        async def refresh_simultaneously():
            return await asyncio.gather(service.mcp_oauth.access_token(row), service.mcp_oauth.access_token(row))

        assert asyncio.run(refresh_simultaneously()) == [issuer.access, issuer.access]
        assert issuer.refreshes == 1

        assistant = service.save_assistant({"name": "OAuth consumer", "connections": [{"connection_id": connection_id,
            "allowed_tools": ["lookup"], "auto_approved_tools": []}]}, user=owner)
        conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "Local OAuth"}, owner)
        configuration = service.runtime_config(conversation["id"])
        assert "headers" not in configuration["mcp_servers"]["c" + connection_id]
        runtime = SimpleNamespace(service=service, config=configuration, conversation_id=conversation["id"])
        tool = next(iter(build_mcp_tools(runtime).values()))
        result = asyncio.run(tool.invoke({}, lambda: None))
        assert "Pipe result" in str(result) and issuer.access not in str(result)
        assert issuer.calls == [{"name": "lookup", "arguments": {}}]

        issuer.tool_name = "lookup_new"
        updated = client.post(path + "/test").json()
        assert updated["status"] == "connected" and updated["tools"][0]["name"] == "lookup_new"
        assert len(issuer.calls) == 1
        final_refresh = issuer.refresh
        revoke = client.post(path + "/oauth/revoke")
        assert revoke.json() == {"locally_revoked": True, "issuer_revoked": True}
        assert issuer.revoked == [final_refresh] and client.get(path).json()["status"] == "needs_auth"
        assert not service.require("connections", connection_id)["secret"] and not service.db.all("SELECT * FROM mcp_oauth_grants")
        events = service.db.all("SELECT event,user_id FROM mcp_oauth_events ORDER BY rowid")
        assert [item["event"] for item in events] == ["link_started", "linked", "refreshed", "unlinked", "issuer_revoked"]
        assert all(item["user_id"] == owner["id"] for item in events)
