import asyncio
import json

from fastapi.testclient import TestClient

from app import feishu_h5
from app.config import Settings
from app.main import create_app
from tests.test_api import FakeRuntime, PASSWORD


def _settings(tmp_path, **overrides):
    values = {
        "data_dir": tmp_path,
        "provider_settings": "",
        "origins": ("http://testserver",),
        "feishu_h5_enabled": True,
        "feishu_app_id": "cli-bot",
        "feishu_app_secret": "bot-only-secret",
        "feishu_h5_app_id": "cli-test",
        "feishu_h5_app_secret": "server-only-secret",
        "feishu_h5_allowed_open_ids": ("ou-owner",),
        "feishu_h5_user_map": json.dumps({"ou-owner": "admin"}),
    }
    values.update(overrides)
    return Settings(**values)


def test_h5_login_exchanges_identity_and_sets_existing_session(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    app = create_app(settings, runtime_factory=FakeRuntime)

    async def fake_exchange(_settings, code):
        assert code == "one-time-code"
        return {"open_id": "ou-owner", "display_name": "飞书用户"}

    monkeypatch.setattr(feishu_h5, "exchange_feishu_identity", fake_exchange)
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
        config = client.get("/api/v1/config")
        assert config.status_code == 200
        assert config.json()["feishu_h5"] == {"enabled": True, "app_id": "cli-test", "path": "/feishu"}
        assert "server-only-secret" not in config.text

        response = client.post("/api/v1/feishu/h5/login", json={"code": "one-time-code"})
        assert response.status_code == 200, response.text
        assert response.json()["username"] == "admin"
        assert client.get("/api/v1/auth/me").json()["id"] == response.json()["id"]


def test_feishu_identity_exchange_uses_server_side_token_sequence(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self.status_code = 200
            self.payload = payload

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            calls.append(("POST", url, kwargs))
            if url.endswith("app_access_token/internal"):
                return FakeResponse({"code": 0, "app_access_token": "app-token"})
            return FakeResponse({"code": 0, "data": {"access_token": "user-token"}})

        async def get(self, url, **kwargs):
            calls.append(("GET", url, kwargs))
            return FakeResponse({"code": 0, "data": {"open_id": "ou-owner", "name": "飞书用户"}})

    monkeypatch.setattr(feishu_h5.httpx, "AsyncClient", FakeClient)
    identity = asyncio.run(feishu_h5.exchange_feishu_identity(settings, "one-time-code"))
    assert identity == {"open_id": "ou-owner", "display_name": "飞书用户"}
    assert calls[0][2]["json"]["app_secret"] == "server-only-secret"
    assert calls[1][2]["headers"] == {"Authorization": "Bearer app-token"}
    assert calls[2][2]["headers"] == {"Authorization": "Bearer user-token"}


def test_h5_login_rejects_open_id_outside_allowlist(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    app = create_app(settings, runtime_factory=FakeRuntime)

    async def fake_exchange(_settings, code):
        return {"open_id": "ou-other", "display_name": "陌生账号"}

    monkeypatch.setattr(feishu_h5, "exchange_feishu_identity", fake_exchange)
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
        response = client.post("/api/v1/feishu/h5/login", json={"code": "one-time-code"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FEISHU_H5_USER_DENIED"


def test_h5_config_is_disabled_and_does_not_expose_app_id_without_explicit_enable(tmp_path):
    settings = _settings(tmp_path, feishu_h5_enabled=False)
    app = create_app(settings, runtime_factory=FakeRuntime)
    with TestClient(app) as client:
        config = client.get("/api/v1/feishu/h5/config")
        assert config.status_code == 200
        assert config.json() == {"enabled": False, "app_id": "", "path": "/feishu"}
