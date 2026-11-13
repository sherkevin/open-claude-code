from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


PASSWORD = "plugin-governance-password"
DIGEST = "a" * 64


def login(client, username):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response


def manifest(**overrides):
    value = {
        "name": "industrial-connectors",
        "version": "1.2.0",
        "source_url": "https://github.com/example/industrial-connectors",
        "license": "MIT",
        "package_sha256": DIGEST,
        "declared_permissions": ["knowledge.read", "workspace.files.read"],
        "review_note": "公开组件，仅读取已授权资料",
    }
    value.update(overrides)
    return value


def test_plugin_manifest_registration_approval_revocation_and_audit(tmp_path):
    app = create_app(
        Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",))
    )
    with TestClient(app) as client:
        service = app.state.service
        service.create_user(
            {"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"}
        )
        login(client, "admin")
        response = client.post("/api/v1/admin/plugins", json=manifest())
        assert response.status_code == 201, response.text
        plugin = response.json()
        assert plugin["status"] == "pending"
        assert plugin["declared_permissions"] == ["knowledge.read", "workspace.files.read"]

        response = client.post(f"/api/v1/admin/plugins/{plugin['id']}/approve", json={"review_note": "许可证和权限已核验"})
        assert response.status_code == 200, response.text
        approved = response.json()
        assert approved["status"] == "approved"
        assert approved["revision"] == 2
        assert approved["approved_at"]

        response = client.post(f"/api/v1/admin/plugins/{plugin['id']}/revoke", json={"review_note": "上游安全公告"})
        assert response.status_code == 200, response.text
        revoked = response.json()
        assert revoked["status"] == "revoked"
        assert revoked["revision"] == 3
        assert revoked["revoked_at"]

        events = client.get(f"/api/v1/admin/plugins/{plugin['id']}/events").json()
        assert [event["action"] for event in events] == ["revoked", "approved", "registered"]
        assert events[0]["details"]["review_note"] == "上游安全公告"
        assert events[1]["details"]["review_note"] == "许可证和权限已核验"
        assert events[2]["details"]["review_note"] == "公开组件，仅读取已授权资料"
        assert all(event["actor_id"] == service.db.one("SELECT id FROM users WHERE username='admin'")["id"] for event in events)


def test_plugin_registration_is_admin_only_and_version_is_immutable(tmp_path):
    app = create_app(
        Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",))
    )
    with TestClient(app) as client:
        service = app.state.service
        service.create_user(
            {"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"}
        )
        service.create_user(
            {"username": "member", "display_name": "成员", "password": PASSWORD, "role": "member"}
        )
        login(client, "member")
        assert client.get("/api/v1/admin/plugins").status_code == 403
        assert client.post("/api/v1/admin/plugins", json=manifest()).status_code == 403

        login(client, "admin")
        assert client.post("/api/v1/admin/plugins", json=manifest()).status_code == 201
        changed_digest = client.post("/api/v1/admin/plugins", json=manifest(package_sha256="b" * 64))
        assert changed_digest.status_code == 409
        same_digest = client.post("/api/v1/admin/plugins", json=manifest())
        assert same_digest.status_code == 409


def test_plugin_license_and_source_are_reviewed_before_approval(tmp_path):
    app = create_app(
        Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",))
    )
    with TestClient(app) as client:
        app.state.service.create_user(
            {"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"}
        )
        login(client, "admin")
        assert client.post("/api/v1/admin/plugins", json=manifest(source_url="http://example.com/plugin")).status_code == 422
        assert client.post("/api/v1/admin/plugins", json=manifest(name="   ")).status_code == 422
        response = client.post("/api/v1/admin/plugins", json=manifest(license="GPL-3.0"))
        assert response.status_code == 201
        plugin = response.json()
        denied = client.post(f"/api/v1/admin/plugins/{plugin['id']}/approve", json={})
        assert denied.status_code == 422
        assert denied.json()["error"]["code"] == "PLUGIN_LICENSE_DENIED"
