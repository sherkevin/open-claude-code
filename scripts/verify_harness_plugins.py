#!/usr/bin/env python3
"""Offline verifier for the reviewed DeepSeek Harness plugin candidates.

It checks pinned source, version, commit, license, SHA-256 and permissions, and
optionally hashes the local release artifacts.  It never downloads, installs,
imports or executes third-party plugin code.

Exit codes:
    0  every requested plugin is verified at the requested scope
    1  at least one plugin is rejected, blocked or missing an artifact
    2  invalid command line input
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from app.plugin_catalog import (  # noqa: E402
    CATALOG_SNAPSHOT,
    DiscoveryPolicy,
    catalog_for,
    harness_compatibility,
    validate_candidate,
)
from app.plugin_catalog import _host_allowed, _https_url  # noqa: E402


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(candidate, policy: DiscoveryPolicy, artifact_dir: Path | None) -> dict:
    reasons = list(validate_candidate(candidate))
    compatibility = harness_compatibility(candidate, policy.harness_version)
    if not compatibility["compatible"]:
        reasons.append({"code": "incompatible_harness", "detail": compatibility["warning"]})
    for field, value in (
        ("source_url", candidate.source_url),
        ("distribution_url", candidate.distribution_url),
    ):
        ok, host = _https_url(value)
        if not ok or not _host_allowed(host, policy.allowed_source_hosts):
            reasons.append({"code": "source_not_allowlisted", "detail": f"{field}:{value}"})
    denied = [item for item in candidate.discovery_permissions if item not in policy.allowed_permissions]
    if denied:
        reasons.append({"code": "permission_not_granted", "detail": ",".join(sorted(denied))})

    integrity = {
        "pinned_sha256": candidate.package_sha256,
        "scope": "pinned_review_record",
        "artifact_path": None,
        "artifact_sha256": None,
        "artifact_present": False,
        "match": None,
    }
    artifact = None
    if artifact_dir is not None:
        artifact = artifact_dir / f"{candidate.name}-{candidate.version}.tgz"
        integrity["artifact_path"] = str(artifact)
        integrity["artifact_present"] = artifact.is_file()
        integrity["scope"] = "local_artifact"
        if artifact.is_file():
            actual = sha256_file(artifact)
            integrity["artifact_sha256"] = actual
            integrity["match"] = actual == candidate.package_sha256
            if not integrity["match"]:
                reasons.append({"code": "sha256_mismatch", "detail": actual})
        else:
            reasons.append({"code": "artifact_missing", "detail": str(artifact)})

    status = "verified" if not reasons else "rejected"
    codes = {reason["code"] for reason in reasons}
    if "sha256_mismatch" in codes:
        status = "blocked_integrity"
    elif "artifact_missing" in codes:
        status = "blocked_external"
    return {
        "name": candidate.name,
        "version": candidate.version,
        "commit": candidate.commit,
        "license": candidate.license,
        "license_extra_terms": list(candidate.license_extra_terms),
        "source_url": candidate.source_url,
        "distribution_url": candidate.distribution_url,
        "package_sha256": candidate.package_sha256,
        "integrity": integrity,
        "permissions": {
            "declared": list(candidate.permissions),
            "discovery": list(candidate.discovery_permissions),
            "granted": sorted(
                item for item in candidate.discovery_permissions if item in policy.allowed_permissions
            ),
            "denied": sorted(denied),
        },
        "capabilities": list(candidate.capabilities),
        "harness": {
            "requested": policy.harness_version,
            "min": candidate.dsh_min_version,
            "max_tested": candidate.dsh_max_tested_version,
            "enforcement": candidate.dsh_compat_enforcement,
            **compatibility,
        },
        "node_min_version": candidate.node_min_version,
        "review_status": candidate.review_status,
        "install_state": candidate.install_state,
        "execution_state": candidate.execution_state,
        "status": status,
        "reasons": reasons,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plugins",
        default="dshmarket,billion-context",
        help="comma separated plugin names present in the pinned catalog",
    )
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="assert that no network access is used (the verifier never uses the network)",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=None,
        help="directory containing <name>-<version>.tgz release artifacts to hash",
    )
    parser.add_argument("--harness-version", default="0.2.1")
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="also run the synthetic compression boundary benchmark (no plugin execution)",
    )
    args = parser.parse_args(argv)

    names = [item.strip() for item in args.plugins.split(",") if item.strip()]
    if not names:
        print("no plugin names requested", file=sys.stderr)
        return 2
    try:
        selected = catalog_for(names)
    except Exception as error:  # noqa: BLE001 - CLI boundary
        print(str(error), file=sys.stderr)
        return 2

    policy = DiscoveryPolicy(harness_version=args.harness_version)
    report = {
        "mode": "offline",
        "network_used": False,
        "third_party_code_executed": False,
        "harness_version": args.harness_version,
        "catalog_size": len(CATALOG_SNAPSHOT),
        "plugins": [verify(candidate, policy, args.artifact_dir) for candidate in selected],
    }
    if args.benchmark:
        from app.billion_context import run_synthetic_benchmark

        report["synthetic_benchmark"] = run_synthetic_benchmark()
    report["ok"] = all(item["status"] == "verified" for item in report["plugins"])

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False))
    else:
        for item in report["plugins"]:
            print(f"{item['name']} {item['version']}: {item['status']}")
            for reason in item["reasons"]:
                print(f"  - {reason['code']}: {reason['detail']}")
        print("ok" if report["ok"] else "not ok")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
