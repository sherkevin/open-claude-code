"""Side-by-side deployments keep login cookies separate without changing defaults."""
from http.cookies import SimpleCookie

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.mark.parametrize("kwargs", [
    {"session_cookie_name": "bad;name"}, {"session_cookie_name": ""}, {"session_cookie_name": "bad\r\nHeader"},
    {"session_cookie_name": "Path"}, {"session_cookie_name": "samesite"},
    {"cookie_path": "relative"}, {"cookie_path": "/;Secure"}, {"cookie_path": "/\r\nInjected"},
    {"cookie_path": "/汉字"}, {"session_cookie_name": "__Secure-test", "cookie_secure": False},
    {"session_cookie_name": "__Host-test", "cookie_secure": True, "cookie_path": "/preview/"},
])
def test_cookie_configuration_rejects_header_injection_and_invalid_prefixes(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)


def test_cookie_defaults_are_backward_compatible():
    settings = Settings()
    assert settings.session_cookie_name == "zhixing_session" and settings.cookie_path == "/"


def test_login_read_and_logout_use_configured_name_and_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    settings = Settings(data_dir=tmp_path, session_cookie_name="zhixing_preview_20261115", cookie_path="/zhixing-next/")
    with TestClient(create_app(settings)) as client:
        service = client.app.state.service
        service.create_user({"username": "preview", "password": "synthetic-cookie-password", "display_name": "Preview", "role": "admin"})
        login = client.post("/api/v1/auth/login", json={"username": "preview", "password": "synthetic-cookie-password"})
        assert login.status_code == 200
        cookie = SimpleCookie(login.headers["set-cookie"])
        assert set(cookie) == {settings.session_cookie_name}
        entry = cookie[settings.session_cookie_name]
        assert entry["path"] == settings.cookie_path and entry["httponly"]
        # The reverse proxy strips the prefix; simulate the browser's Cookie header.
        token = entry.value
        assert client.get("/api/v1/auth/me", headers={"cookie": "zhixing_session=" + token}).status_code == 401
        assert client.get("/api/v1/auth/me", headers={"cookie": settings.session_cookie_name + "=" + token}).status_code == 200
        logout = client.post("/api/v1/auth/logout", headers={"cookie": settings.session_cookie_name + "=" + token})
        deleted = SimpleCookie(logout.headers["set-cookie"])[settings.session_cookie_name]
        assert deleted["path"] == settings.cookie_path and deleted["max-age"] == "0"
        assert client.get("/api/v1/auth/me", headers={"cookie": settings.session_cookie_name + "=" + token}).status_code == 401
