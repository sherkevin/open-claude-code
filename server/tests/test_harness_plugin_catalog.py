from dataclasses import replace

from app.plugin_catalog import (
    CATALOG_SNAPSHOT,
    DiscoveryPolicy,
    catalog_for,
    compare_versions,
    discover_plugins,
    harness_compatibility,
    validate_candidate,
    with_updates,
)


def policy(**overrides):
    overrides.setdefault("harness_version", "0.2.1")
    return DiscoveryPolicy(**overrides)


def test_catalog_snapshot_has_pinned_reviewable_metadata():
    assert {item.name for item in CATALOG_SNAPSHOT} == {"dshmarket", "billion-context"}
    for candidate in CATALOG_SNAPSHOT:
        assert validate_candidate(candidate) == []
        assert candidate.commit
        assert len(candidate.package_sha256) == 64
        assert candidate.install_state == "not_installed"
        assert candidate.execution_state == "not_executed"
        assert candidate.review_status == "offline_verified"
        assert candidate.discovery_permissions
        assert set(candidate.discovery_permissions) <= set(candidate.permissions)


def test_discovery_returns_metadata_only_and_audits_each_decision():
    result = discover_plugins(policy())
    assert result.status == "ok"
    assert {entry["name"] for entry in result.entries} == {"dshmarket", "billion-context"}
    for entry in result.entries:
        assert entry["metadata_only"] is True
        assert entry["install_state"] == "not_installed"
        assert entry["execution_state"] == "not_executed"
        assert "install_command" not in entry
        assert "code" not in entry
        assert entry["permissions"]["discovery"]
    assert [event["action"] for event in result.audit] == ["discovered", "discovered"]
    assert {event["reason"] for event in result.audit} == {"metadata_only"}


def test_discovery_rejects_missing_integrity_and_never_claims_verified():
    broken = with_updates(catalog_for(["dshmarket"])[0], package_sha256="")
    assert any(
        item["code"] == "missing_or_invalid_sha256" for item in validate_candidate(broken)
    )
    result = discover_plugins(policy(), catalog=(broken,))
    assert result.status == "ok"
    assert result.entries == ()
    assert result.audit[0]["action"] == "rejected"
    assert result.audit[0]["reason"] == "missing_or_invalid_sha256"


def test_discovery_rejects_missing_license_and_source():
    no_license = with_updates(catalog_for(["billion-context"])[0], license="")
    assert any(item["code"] == "license_not_permitted" for item in validate_candidate(no_license))
    bad_source = with_updates(
        catalog_for(["dshmarket"])[0], distribution_url="http://evil.example/dshmarket.tgz"
    )
    result = discover_plugins(policy(), catalog=(bad_source,))
    assert result.entries == ()
    assert result.audit[0]["reason"] == "distribution_url_not_https"


def test_discovery_filters_unallowlisted_sources():
    result = discover_plugins(policy(allowed_source_hosts=("github.com",)))
    assert [entry["name"] for entry in result.entries] == ["dshmarket"]
    rejected = [event for event in result.audit if event["action"] == "rejected"]
    assert rejected and rejected[0]["plugin"] == "billion-context"
    assert rejected[0]["reason"] == "source_not_allowlisted"


def test_discovery_filters_undeclared_unknown_and_forbidden_permissions():
    without_permissions = with_updates(
        catalog_for(["dshmarket"])[0], discovery_permissions=()
    )
    assert any(
        item["code"] == "discovery_permissions_undeclared"
        for item in validate_candidate(without_permissions)
    )
    unknown = with_updates(
        catalog_for(["dshmarket"])[0],
        permissions=("web.read", "shell.execute"),
    )
    assert any(item["code"] == "permissions_unknown" for item in validate_candidate(unknown))
    forbidden = with_updates(
        catalog_for(["billion-context"])[0],
        permissions=("web.read", "credentials.read"),
        discovery_permissions=("web.read",),
    )
    assert any(item["code"] == "permissions_forbidden" for item in validate_candidate(forbidden))

    denied = discover_plugins(policy(allowed_permissions=("workspace.files.read",)))
    assert denied.entries == ()
    assert {event["reason"] for event in denied.audit} == {"permission_not_granted"}


def test_discovery_filters_incompatible_harness_version():
    old = discover_plugins(policy(harness_version="0.1.0-rc.5"))
    assert "dshmarket" not in {entry["name"] for entry in old.entries}
    reasons = {(event["plugin"], event["reason"]) for event in old.audit}
    assert ("dshmarket", "incompatible_harness") in reasons
    assert compare_versions("0.1.0-rc.10", "0.1.0-rc.6") > 0
    assert compare_versions("0.1.0", "0.1.0-rc.6") > 0
    assert harness_compatibility(catalog_for(["dshmarket"])[0], "0.2.1")["compatible"] is True
    assert harness_compatibility(catalog_for(["dshmarket"])[0], "0.1.0-rc.5")["compatible"] is False


def test_discovery_network_failure_fails_closed_with_audit():
    def broken_fetcher():
        raise TimeoutError("registry timed out")

    result = discover_plugins(policy(), fetcher=broken_fetcher)
    assert result.status == "blocked_network"
    assert result.entries == ()
    assert result.audit[0]["action"] == "discovery_network_error"
    assert result.audit[0]["reason"] == "network_error"


def test_discovery_cancellation_fails_closed_with_audit():
    result = discover_plugins(policy(), cancel=lambda: True)
    assert result.status == "cancelled"
    assert result.entries == ()
    assert result.audit[0]["action"] == "discovery_cancelled"


def test_discovery_skips_revoked_candidate():
    revoked = with_updates(catalog_for(["dshmarket"])[0], revoked=True)
    result = discover_plugins(policy(), catalog=(revoked,))
    assert result.entries == ()
    assert result.audit[0]["reason"] == "plugin_revoked"


def test_unknown_catalog_name_is_rejected():
    try:
        catalog_for(["not-a-real-plugin"])
    except Exception as error:  # noqa: BLE001 - explicit catalog error contract
        assert getattr(error, "code", "") == "CATALOG_PLUGIN_UNKNOWN"
    else:
        raise AssertionError("unknown plugin must not be silently accepted")


def test_catalog_snapshot_declares_native_install_capability_unsupported():
    dshmarket = catalog_for(["dshmarket"])[0]
    assert "process.execute" in dshmarket.permissions
    assert "process.execute" not in dshmarket.discovery_permissions
    assert "plugin.install.manual_review_only" in dshmarket.capabilities
    assert replace(dshmarket, revoked=False) == dshmarket
