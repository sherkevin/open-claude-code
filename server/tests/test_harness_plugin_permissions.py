from contextlib import contextmanager
from dataclasses import replace

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.plugin_catalog import (
    KNOWN_HOST_PERMISSIONS,
    DiscoveryPolicy,
    authorize_activation,
    catalog_for,
    discover_plugins,
    persist_discovery_audit,
    with_updates,
)


PASSWORD = "plugin-boundary-password"
DIGEST = "c" * 64


def login(client, username):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response


def manifest(**overrides):
    value = {
        "name": "billion-context",
        "version": "0.1.213",
        "source_url": "https://github.com/ranxianglei/billion-context",
        "license": "MIT",
        "package_sha256": DIGEST,
        "declared_permissions": ["workspace.files.read", "web.read"],
        "review_note": "离线来源与许可证核验",
    }
    value.update(overrides)
    return value


@contextmanager
def new_client(tmp_path):
    app = create_app(
        Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",))
    )
    with TestClient(app) as client:
        app.state.service.create_user(
            {"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"}
        )
        app.state.service.create_user(
            {"username": "member", "display_name": "成员", "password": PASSWORD, "role": "member"}
        )
        yield app, client


def test_registration_rejects_missing_source_version_license_and_digest(tmp_path):
    with new_client(tmp_path) as (_, client):
        login(client, "admin")
        for payload in (
            {key: value for key, value in manifest().items() if key != "package_sha256"},
            {key: value for key, value in manifest().items() if key != "license"},
            {key: value for key, value in manifest().items() if key != "version"},
            manifest(source_url="http://github.com/example/plugin"),
            manifest(source_url="https://user:secret@github.com/example/plugin"),
            manifest(package_sha256="not-a-digest"),
            manifest(declared_permissions=["shell.execute"]),
        ):
            response = client.post("/api/v1/admin/plugins", json=payload)
            assert response.status_code == 422, (payload, response.text)


def test_approval_enforces_license_and_revocation_is_audited(tmp_path):
    with new_client(tmp_path) as (app, client):
        login(client, "admin")
        created = client.post("/api/v1/admin/plugins", json=manifest(license="GPL-3.0"))
        assert created.status_code == 201, created.text
        plugin = created.json()
        denied = client.post(f"/api/v1/admin/plugins/{plugin['id']}/approve", json={})
        assert denied.status_code == 422
        assert denied.json()["error"]["code"] == "PLUGIN_LICENSE_DENIED"

        good = client.post(
            "/api/v1/admin/plugins",
            json=manifest(version="0.1.214", package_sha256="f" * 64),
        ).json()
        approved = client.post(f"/api/v1/admin/plugins/{good['id']}/approve", json={})
        assert approved.status_code == 200, approved.text
        revoked = client.post(
            f"/api/v1/admin/plugins/{good['id']}/revoke", json={"review_note": "撤权测试"}
        )
        assert revoked.status_code == 200, revoked.text
        events = client.get(f"/api/v1/admin/plugins/{good['id']}/events").json()
        assert [event["action"] for event in events] == ["revoked", "approved", "registered"]
        assert app.state.service.db.one(
            "SELECT COUNT(*) AS count FROM plugin_registry_events WHERE plugin_id=?",
            (good["id"],),
        )["count"] == 3


def test_discovery_endpoint_is_admin_only_and_writes_audit(tmp_path):
    with new_client(tmp_path) as (app, client):
        login(client, "member")
        assert client.get("/api/v1/admin/plugins/discovery").status_code == 403

        login(client, "admin")
        response = client.get("/api/v1/admin/plugins/discovery?harness_version=0.2.1")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["status"] == "ok"
        assert {entry["name"] for entry in payload["entries"]} == {"dshmarket", "billion-context"}
        assert all(entry["metadata_only"] for entry in payload["entries"])
        rows = app.state.service.db.all("SELECT * FROM plugin_catalog_events ORDER BY id")
        assert len(rows) == 2
        assert {row["action"] for row in rows} == {"discovered"}
        assert all(row["actor_id"] for row in rows)


def test_discovery_rejections_cancellation_and_network_failure_are_audited(tmp_path):
    with new_client(tmp_path) as (app, client):
        login(client, "admin")
        policy = DiscoveryPolicy(harness_version="0.2.1")
        revoked = with_updates(catalog_for(["dshmarket"])[0], revoked=True)
        rejected = discover_plugins(policy, catalog=(revoked,))
        cancelled = discover_plugins(policy, cancel=lambda: True)

        def broken_fetcher():
            raise TimeoutError("registry unavailable")

        blocked = discover_plugins(policy, fetcher=broken_fetcher)
        for result in (rejected, cancelled, blocked):
            persist_discovery_audit(app.state.service, result, "admin-test")

        rows = app.state.service.db.all("SELECT * FROM plugin_catalog_events ORDER BY id")
        assert [row["action"] for row in rows] == [
            "rejected",
            "discovery_cancelled",
            "discovery_network_error",
        ]
        assert rows[0]["reason"] == "plugin_revoked"
        assert all(row["actor_id"] == "admin-test" for row in rows)


def test_activation_gate_requires_exact_approval_and_all_declared_permissions(tmp_path):
    with new_client(tmp_path) as (_, client):
        login(client, "admin")
        candidate = catalog_for(["billion-context"])[0]
        row = client.post(
            "/api/v1/admin/plugins",
            json=manifest(package_sha256=candidate.package_sha256),
        ).json()
        approved = client.post(f"/api/v1/admin/plugins/{row['id']}/approve", json={}).json()

        policy = DiscoveryPolicy(harness_version="0.2.1")
        denied = authorize_activation(candidate, policy, approved, ("workspace.files.read",))
        assert denied.allowed is False
        codes = {reason["code"] for reason in denied.reasons}
        assert "native_permission_not_granted" in codes
        assert denied.plugin is None

        broad = DiscoveryPolicy(
            harness_version="0.2.1",
            allowed_permissions=tuple(sorted(KNOWN_HOST_PERMISSIONS)),
        )
        allowed = authorize_activation(candidate, broad, approved, ("workspace.files.read",))
        assert allowed.allowed is True, allowed.reasons
        assert allowed.plugin["name"] == "billion-context"
        assert allowed.plugin["execution_state"] == "not_executed"

        pending = client.post(
            "/api/v1/admin/plugins",
            json=manifest(version="0.1.999", package_sha256="d" * 64),
        ).json()
        not_approved = authorize_activation(candidate, broad, pending, ())
        assert "registry_not_approved" in {reason["code"] for reason in not_approved.reasons}

        mismatch = authorize_activation(
            candidate, broad, {**approved, "package_sha256": "e" * 64}, ()
        )
        assert "registry_digest_mismatch" in {reason["code"] for reason in mismatch.reasons}

        revoked_candidate = replace(candidate, revoked=True)
        revoked = authorize_activation(revoked_candidate, broad, approved, ())
        assert "plugin_revoked" in {reason["code"] for reason in revoked.reasons}

        stopped = authorize_activation(candidate, broad, {**approved, "status": "revoked"}, ())
        assert "registry_not_approved" in {reason["code"] for reason in stopped.reasons}


def test_activation_gate_rejects_unsupported_and_ungranted_permissions(tmp_path):
    with new_client(tmp_path) as (_, client):
        login(client, "admin")
        dshmarket = catalog_for(["dshmarket"])[0]
        row = client.post(
            "/api/v1/admin/plugins",
            json=manifest(
                name="dshmarket",
                version="1.73.18",
                source_url="https://github.com/dsh-market/dsh-market",
                package_sha256=dshmarket.package_sha256,
                declared_permissions=["web.read"],
            ),
        ).json()
        approved = client.post(f"/api/v1/admin/plugins/{row['id']}/approve", json={}).json()
        broad = DiscoveryPolicy(
            harness_version="0.2.1",
            allowed_permissions=tuple(sorted(KNOWN_HOST_PERMISSIONS)),
        )
        decision = authorize_activation(dshmarket, broad, approved, ("web.read",))
        assert decision.allowed is False
        assert "permission_unsupported" in {reason["code"] for reason in decision.reasons}

        billion = catalog_for(["billion-context"])[0]
        requested = authorize_activation(billion, broad, approved, ("process.execute",))
        codes = {reason["code"] for reason in requested.reasons}
        assert "permission_unsupported" in codes
        undeclared = authorize_activation(billion, broad, approved, ("artifact.write",))
        assert "permission_undeclared" in {reason["code"] for reason in undeclared.reasons}
