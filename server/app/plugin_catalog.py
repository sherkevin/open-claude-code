"""Offline, fail-closed catalog for reviewed DeepSeek Harness plugins.

This module only deals with pinned metadata: source, version, commit, license,
SHA-256 digest and declared permissions.  It never downloads, imports or runs
third-party plugin code.  Discovery results intentionally contain no install
command and no code path, only reviewable metadata plus an audit trail.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Iterable, Literal, Sequence
from urllib.parse import urlsplit

from .plugins import PERMITTED_LICENSES, Permission


KNOWN_HOST_PERMISSIONS = frozenset(Permission.__args__)
READ_ONLY_PERMISSIONS = tuple(
    sorted(
        {
            "knowledge.read",
            "workspace.files.read",
            "web.read",
            "mcp.connection.read",
            "memory.read",
        }
    )
)
# Known native capabilities that this product deliberately cannot grant.
UNSUPPORTED_NATIVE_PERMISSIONS = frozenset({"process.execute"})
# A candidate asking for these must never be shown, let alone activated.
FORBIDDEN_PERMISSIONS = frozenset({"credentials.read", "filesystem.any"})
KNOWN_NATIVE_PERMISSIONS = KNOWN_HOST_PERMISSIONS | UNSUPPORTED_NATIVE_PERMISSIONS | FORBIDDEN_PERMISSIONS
REQUIRED_REVIEW_STATUS = "offline_verified"


class CatalogError(Exception):
    """Raised for invalid catalog inputs that must not be silently ignored."""

    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


@dataclass(frozen=True)
class PluginCandidate:
    name: str
    version: str
    source_url: str
    distribution_url: str
    package_sha256: str
    commit: str
    license: str
    permissions: tuple[str, ...]
    discovery_permissions: tuple[str, ...]
    capabilities: tuple[str, ...]
    dsh_min_version: str | None = None
    dsh_max_tested_version: str | None = None
    dsh_compat_enforcement: Literal["block", "advisory"] = "block"
    node_min_version: str | None = None
    license_extra_terms: tuple[str, ...] = ()
    install_state: str = "not_installed"
    execution_state: str = "not_executed"
    review_status: str = REQUIRED_REVIEW_STATUS
    revoked: bool = False


@dataclass(frozen=True)
class DiscoveryPolicy:
    harness_version: str
    allowed_source_hosts: tuple[str, ...] = ("github.com", "registry.npmjs.org")
    allowed_permissions: tuple[str, ...] = READ_ONLY_PERMISSIONS
    required_review_status: str = REQUIRED_REVIEW_STATUS


@dataclass(frozen=True)
class DiscoveryResult:
    status: Literal["ok", "cancelled", "blocked_network", "blocked_input"]
    entries: tuple[dict, ...]
    audit: tuple[dict, ...]
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "entries": list(self.entries),
            "audit": list(self.audit),
            "error": self.error,
        }


# Pinned snapshot reviewed on 2026-11-12.  The catalog is intentionally small
# and static: dshmarket's own remote catalog is untrusted input and is never
# fetched or expanded here.
CATALOG_SNAPSHOT: tuple[PluginCandidate, ...] = (
    PluginCandidate(
        name="dshmarket",
        version="1.73.18",
        source_url="https://github.com/dsh-market/dsh-market",
        distribution_url=(
            "https://github.com/dsh-market/dsh-market/releases/download/"
            "v1.73.18/dshmarket-1.73.18.tgz"
        ),
        package_sha256="a41997fcb10e1e72360f18abecf6a92346dc43c272054e3bd0b7996c61616c85",
        commit="7f32bb6c8566053916c5ba646263c132ac8d703c",
        license="MIT",
        permissions=("web.read", "workspace.files.write", "process.execute"),
        discovery_permissions=("web.read",),
        capabilities=(
            "plugin.catalog.read",
            "plugin.install.manual_review_only",
            "plugin.update.manual_review_only",
        ),
        dsh_min_version="0.1.0-rc.6",
        dsh_max_tested_version=None,
        dsh_compat_enforcement="block",
        node_min_version=None,
    ),
    PluginCandidate(
        name="billion-context",
        version="0.1.213",
        source_url="https://github.com/ranxianglei/billion-context",
        distribution_url="https://registry.npmjs.org/billion-context/-/billion-context-0.1.213.tgz",
        package_sha256="e3dab47c9e2cbbe67b541190188a3217571c10cb3ea771ab7c04b1301251df98",
        commit="133f3525c628c4b959bb79ece82af6df82e131a3",
        license="MIT",
        permissions=("workspace.files.read", "workspace.files.write", "web.read", "memory.read"),
        discovery_permissions=("workspace.files.read", "web.read"),
        capabilities=(
            "context.compression.candidate",
            "context.compression.usage_report",
        ),
        dsh_min_version=None,
        dsh_max_tested_version="0.2.0-rc.2",
        dsh_compat_enforcement="advisory",
        node_min_version="20",
        license_extra_terms=(
            "Product surfaces must visibly credit billion-context and link to "
            "https://github.com/ranxianglei/billion-context",
        ),
    ),
)


_VERSION_RE = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


def parse_version(value: str) -> tuple[int, int, int, tuple[tuple[int, object], ...]]:
    match = _VERSION_RE.fullmatch(value.strip())
    if not match:
        raise CatalogError("CATALOG_VERSION_INVALID", f"无法解析版本号：{value!r}")
    prerelease: tuple[tuple[int, object], ...] = ()
    raw_pre = match.group("pre")
    if raw_pre:
        parts = []
        for item in raw_pre.split("."):
            parts.append((0, int(item)) if item.isdigit() else (1, item))
        prerelease = tuple(parts)
    return int(match.group("major")), int(match.group("minor")), int(match.group("patch")), prerelease


def compare_versions(left: str, right: str) -> int:
    a, b = parse_version(left), parse_version(right)
    if a[:3] != b[:3]:
        return -1 if a[:3] < b[:3] else 1
    # A release outranks any prerelease of the same core version.
    if not a[3] and not b[3]:
        return 0
    if not a[3]:
        return 1
    if not b[3]:
        return -1
    for index, (kind_a, value_a) in enumerate(a[3]):
        if index >= len(b[3]):
            return 1
        kind_b, value_b = b[3][index]
        if (kind_a, value_a) == (kind_b, value_b):
            continue
        if kind_a != kind_b:
            return -1 if kind_a < kind_b else 1
        return -1 if value_a < value_b else 1
    if len(a[3]) != len(b[3]):
        return -1
    return 0


def _host_allowed(host: str, allowed: Sequence[str]) -> bool:
    return any(host == item or host.endswith("." + item) for item in allowed)


def _https_url(value: str) -> tuple[bool, str]:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False, "invalid"
    if parsed.scheme != "https" or not parsed.hostname:
        return False, "not_https"
    if parsed.username or parsed.password or parsed.fragment:
        return False, "contains_credentials"
    return True, parsed.hostname.lower()


def _license_key(value: str) -> str:
    return re.sub(r"\s+", "-", value.strip().lower())


def validate_candidate(candidate: PluginCandidate) -> list[dict]:
    """Return stable rejection reasons.  An empty list means the metadata is sound."""
    reasons: list[dict] = []

    def reject(code: str, detail: str = "") -> None:
        reasons.append({"code": code, "detail": detail})

    if not candidate.name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", candidate.name):
        reject("missing_or_invalid_name")
    if not candidate.version or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}", candidate.version):
        reject("missing_version")
    if not candidate.commit or not re.fullmatch(r"[0-9a-f]{7,64}", candidate.commit):
        reject("missing_source_commit")
    for field, value in (("source_url", candidate.source_url), ("distribution_url", candidate.distribution_url)):
        ok, detail = _https_url(value)
        if not ok:
            reject(f"{field}_{detail}")
    if not re.fullmatch(r"[0-9a-f]{64}", candidate.package_sha256 or ""):
        reject("missing_or_invalid_sha256")
    if not candidate.license or _license_key(candidate.license) not in PERMITTED_LICENSES:
        reject("license_not_permitted")
    if not candidate.permissions:
        reject("permissions_undeclared")
    unknown = [item for item in candidate.permissions if item not in KNOWN_NATIVE_PERMISSIONS]
    if unknown:
        reject("permissions_unknown", ",".join(sorted(unknown)))
    forbidden = [item for item in candidate.permissions if item in FORBIDDEN_PERMISSIONS]
    if forbidden:
        reject("permissions_forbidden", ",".join(sorted(forbidden)))
    if not candidate.discovery_permissions:
        reject("discovery_permissions_undeclared")
    if any(item not in candidate.permissions for item in candidate.discovery_permissions):
        reject("discovery_permissions_not_declared")
    if any(item in FORBIDDEN_PERMISSIONS for item in candidate.discovery_permissions):
        reject("discovery_permissions_forbidden")
    if not candidate.capabilities:
        reject("capabilities_undeclared")
    if candidate.review_status != REQUIRED_REVIEW_STATUS:
        reject("review_status_unverified", candidate.review_status)
    if candidate.install_state != "not_installed":
        reject("install_state_not_trusted", candidate.install_state)
    if candidate.execution_state != "not_executed":
        reject("execution_state_not_trusted", candidate.execution_state)
    for field, value in (
        ("dsh_min_version", candidate.dsh_min_version),
        ("dsh_max_tested_version", candidate.dsh_max_tested_version),
    ):
        if value is not None:
            try:
                parse_version(value)
            except CatalogError:
                reject(f"{field}_invalid", value)
    if candidate.dsh_compat_enforcement not in ("block", "advisory"):
        reject("dsh_compat_enforcement_invalid", candidate.dsh_compat_enforcement)
    return reasons


def harness_compatibility(candidate: PluginCandidate, harness_version: str) -> dict:
    """Evaluate the declared DSH range without guessing unknown versions."""
    try:
        parse_version(harness_version)
    except CatalogError as error:
        return {"compatible": False, "compatibility": "unknown", "warning": error.message}
    if candidate.dsh_min_version:
        if compare_versions(harness_version, candidate.dsh_min_version) < 0:
            return {
                "compatible": False,
                "compatibility": "incompatible",
                "warning": f"需要 DSH >= {candidate.dsh_min_version}",
            }
    if candidate.dsh_max_tested_version:
        newer = compare_versions(harness_version, candidate.dsh_max_tested_version) > 0
        if newer:
            if candidate.dsh_compat_enforcement == "block":
                return {
                    "compatible": False,
                    "compatibility": "incompatible",
                    "warning": f"仅验证到 DSH {candidate.dsh_max_tested_version}",
                }
            return {
                "compatible": True,
                "compatibility": "advisory",
                "warning": f"高于已验证版本 DSH {candidate.dsh_max_tested_version}，需要回归",
            }
    return {"compatible": True, "compatibility": "compatible", "warning": None}


def _audit_entry(candidate: PluginCandidate | None, action: str, reason: str | None = None, detail: str = "") -> dict:
    return {
        "plugin": candidate.name if candidate else None,
        "version": candidate.version if candidate else None,
        "package_sha256": candidate.package_sha256 if candidate else None,
        "action": action,
        "reason": reason,
        "detail": detail,
        "at": datetime.now(timezone.utc).isoformat(),
    }


def _metadata_view(candidate: PluginCandidate, policy: DiscoveryPolicy) -> dict:
    compatibility = harness_compatibility(candidate, policy.harness_version)
    return {
        "name": candidate.name,
        "version": candidate.version,
        "commit": candidate.commit,
        "license": candidate.license,
        "license_extra_terms": list(candidate.license_extra_terms),
        "source_url": candidate.source_url,
        "distribution_url": candidate.distribution_url,
        "package_sha256": candidate.package_sha256,
        "capabilities": list(candidate.capabilities),
        "permissions": {
            "declared": list(candidate.permissions),
            "discovery": list(candidate.discovery_permissions),
            "unsupported_native": sorted(
                item for item in candidate.permissions if item in UNSUPPORTED_NATIVE_PERMISSIONS
            ),
            "granted": sorted(
                item for item in candidate.discovery_permissions if item in policy.allowed_permissions
            ),
        },
        "harness": {
            "min": candidate.dsh_min_version,
            "max_tested": candidate.dsh_max_tested_version,
            "enforcement": candidate.dsh_compat_enforcement,
            **compatibility,
        },
        "node_min_version": candidate.node_min_version,
        "review_status": candidate.review_status,
        "install_state": candidate.install_state,
        "execution_state": candidate.execution_state,
        "metadata_only": True,
    }


def discover_plugins(
    policy: DiscoveryPolicy,
    catalog: Iterable[PluginCandidate] = CATALOG_SNAPSHOT,
    *,
    fetcher: Callable[[], Iterable[PluginCandidate]] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> DiscoveryResult:
    """Discover reviewable metadata only.

    ``fetcher`` exists so callers can plug in an online catalog refresh.  Any
    failure returns an empty, blocked result: partial discovery is never
    mistaken for a usable plugin list.  ``cancel`` is polled before and after
    the optional fetch.
    """
    audit: list[dict] = []
    if cancel and cancel():
        audit.append(_audit_entry(None, "discovery_cancelled", "cancelled"))
        return DiscoveryResult("cancelled", (), tuple(audit), "调用方已取消")

    candidates: Iterable[PluginCandidate] = catalog
    if fetcher is not None:
        try:
            candidates = fetcher()
            if cancel and cancel():
                audit.append(_audit_entry(None, "discovery_cancelled", "cancelled"))
                return DiscoveryResult("cancelled", (), tuple(audit), "拉取目录后调用方取消")
        except Exception as error:  # noqa: BLE001 - network boundary must fail closed
            audit.append(_audit_entry(None, "discovery_network_error", "network_error", type(error).__name__))
            return DiscoveryResult("blocked_network", (), tuple(audit), "插件目录网络请求失败，已失败关闭")
        if not isinstance(candidates, (list, tuple)):
            audit.append(_audit_entry(None, "discovery_input_rejected", "catalog_not_sequence"))
            return DiscoveryResult("blocked_input", (), tuple(audit), "目录返回值不是可审计列表")

    try:
        parse_version(policy.harness_version)
    except CatalogError as error:
        audit.append(_audit_entry(None, "discovery_input_rejected", "harness_version_invalid", error.message))
        return DiscoveryResult("blocked_input", (), tuple(audit), error.message)

    entries: list[dict] = []
    for candidate in candidates:
        if not isinstance(candidate, PluginCandidate):
            audit.append(_audit_entry(None, "discovery_input_rejected", "catalog_entry_not_candidate"))
            continue
        reasons = validate_candidate(candidate)
        if reasons:
            first = reasons[0]
            audit.append(_audit_entry(candidate, "rejected", first["code"], first["detail"]))
            continue
        if candidate.revoked:
            audit.append(_audit_entry(candidate, "rejected", "plugin_revoked"))
            continue
        for url in (candidate.source_url, candidate.distribution_url):
            ok, host = _https_url(url)
            if not ok or not _host_allowed(host, policy.allowed_source_hosts):
                audit.append(_audit_entry(candidate, "rejected", "source_not_allowlisted", url))
                break
        else:
            compatibility = harness_compatibility(candidate, policy.harness_version)
            if not compatibility["compatible"]:
                audit.append(
                    _audit_entry(
                        candidate,
                        "rejected",
                        "incompatible_harness",
                        compatibility["warning"] or "",
                    )
                )
                continue
            denied = [
                item for item in candidate.discovery_permissions if item not in policy.allowed_permissions
            ]
            if denied:
                audit.append(_audit_entry(candidate, "rejected", "permission_not_granted", ",".join(sorted(denied))))
                continue
            entries.append(_metadata_view(candidate, policy))
            audit.append(
                _audit_entry(
                    candidate,
                    "discovered",
                    "metadata_only",
                    compatibility["compatibility"],
                )
            )

    return DiscoveryResult("ok", tuple(entries), tuple(audit))


@dataclass(frozen=True)
class ActivationDecision:
    allowed: bool
    reasons: tuple[dict, ...]
    plugin: dict | None

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "plugin": self.plugin,
            "execution": "not_started",
        }


def authorize_activation(
    candidate: PluginCandidate,
    policy: DiscoveryPolicy,
    registry_row: dict | None,
    requested_permissions: Sequence[str] = (),
) -> ActivationDecision:
    """Fail-closed gate in front of any runtime integration.

    A registry approval is necessary but not sufficient: version, digest,
    license, revocation, Harness compatibility and every requested permission
    are re-checked at activation time.
    """
    reasons = list(validate_candidate(candidate))
    if candidate.revoked:
        reasons.append({"code": "plugin_revoked", "detail": ""})
    compatibility = harness_compatibility(candidate, policy.harness_version)
    if not compatibility["compatible"]:
        reasons.append({"code": "incompatible_harness", "detail": compatibility["warning"] or ""})
    if registry_row is None:
        reasons.append({"code": "registry_entry_missing", "detail": ""})
    else:
        if registry_row.get("status") != "approved":
            reasons.append({"code": "registry_not_approved", "detail": str(registry_row.get("status"))})
        if registry_row.get("name") != candidate.name:
            reasons.append({"code": "registry_name_mismatch", "detail": ""})
        if registry_row.get("version") != candidate.version:
            reasons.append({"code": "registry_version_mismatch", "detail": ""})
        if registry_row.get("package_sha256") != candidate.package_sha256:
            reasons.append({"code": "registry_digest_mismatch", "detail": ""})
        if _license_key(registry_row.get("license", "")) != _license_key(candidate.license):
            reasons.append({"code": "registry_license_mismatch", "detail": ""})
    requested = tuple(dict.fromkeys(requested_permissions))
    # Loading a host plugin grants its whole declared capability set, not a
    # subset of it, so every native permission must be explicitly grantable.
    unsupported_native = [
        item for item in candidate.permissions if item in UNSUPPORTED_NATIVE_PERMISSIONS
    ]
    if unsupported_native:
        reasons.append({"code": "permission_unsupported", "detail": ",".join(sorted(unsupported_native))})
    ungranted_native = [
        item for item in candidate.permissions if item not in policy.allowed_permissions
    ]
    if ungranted_native:
        reasons.append({"code": "native_permission_not_granted", "detail": ",".join(sorted(ungranted_native))})
    undeclared = [item for item in requested if item not in candidate.permissions]
    if undeclared:
        reasons.append({"code": "permission_undeclared", "detail": ",".join(sorted(undeclared))})
    forbidden = [item for item in requested if item in FORBIDDEN_PERMISSIONS]
    if forbidden:
        reasons.append({"code": "permission_forbidden", "detail": ",".join(sorted(forbidden))})
    unsupported = [item for item in requested if item in UNSUPPORTED_NATIVE_PERMISSIONS]
    if unsupported:
        reasons.append({"code": "permission_unsupported", "detail": ",".join(sorted(unsupported))})
    ungranted = [item for item in requested if item not in policy.allowed_permissions]
    if ungranted:
        reasons.append({"code": "permission_not_granted", "detail": ",".join(sorted(ungranted))})
    plugin = _metadata_view(candidate, policy)
    return ActivationDecision(not reasons, tuple(reasons), plugin if not reasons else None)


def persist_discovery_audit(service, result: DiscoveryResult, actor_id: str | None = None) -> None:
    """Write the discovery decision trail to SQLite without secrets."""
    from .db import now

    stamp = now()
    for event in result.audit:
        service.db.execute(
            "INSERT INTO plugin_catalog_events(plugin_name,action,reason,detail,actor_id,created_at) VALUES(?,?,?,?,?,?)",
            (
                event.get("plugin"),
                event.get("action", "unknown"),
                event.get("reason"),
                event.get("detail", ""),
                actor_id,
                stamp,
            ),
        )


def catalog_for(names: Sequence[str]) -> tuple[PluginCandidate, ...]:
    by_name = {candidate.name: candidate for candidate in CATALOG_SNAPSHOT}
    unknown = [name for name in names if name not in by_name]
    if unknown:
        raise CatalogError("CATALOG_PLUGIN_UNKNOWN", "目录中没有该插件：" + ",".join(sorted(unknown)))
    return tuple(by_name[name] for name in names)


def with_updates(candidate: PluginCandidate, **changes) -> PluginCandidate:
    """Test and review helper; no runtime caller should mutate pinned metadata."""
    return replace(candidate, **changes)
