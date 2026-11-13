#!/usr/bin/env python3
"""Build and verify a reproducible native trial-release staging tree.

The staging tree is a *candidate*, not a published release.  It re-uses the
reviewed selection in ``build_release.py``, adds only explicitly selected
public/synthetic fixture metadata, and records the full dirty-worktree truth,
per-file source digests, pinned toolchain identity, feature enablement and
acceptance binding.

This version intentionally has no publishable state.  It only declares an
``integrity-verified-staging`` candidate.  A JSON acceptance report or an
authorization string is recorded as evidence, but neither can set
``release_ready`` or ``publishable``: the complete product-runtime acceptance
aggregation contract has not been implemented, so no fixture report may be used
to fake a release pass.  Any manifest claiming otherwise is rejected by the
verifier.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Iterable

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_release  # noqa: E402
from setup_harness_runtime import NODE_SHA256, NODE_VERSION, PIN, PNPM_SHA512, PNPM_VERSION  # noqa: E402

SCHEMA = "trial-native-release/v1"
ACCEPTANCE_SCHEMA = "trial-release-acceptance/v1"
DEFAULT_VERSION = "0.4.2"
ACCEPTANCE_REPORT = "docs/evidence/TRIAL_PACKAGE_2026-11-12.json"
HARNESS_REPO = "https://github.com/deepseek-ai/deepseek-harness"
HARNESS_SOURCE = ".runtime/third_party/deepseek-harness"
TOOLCHAIN = ".runtime/harness-toolchain"
FIXTURE_SELECTION = (
    "fixtures/synthetic-industrial-v1/manifest.json",
    "fixtures/synthetic-industrial-v1/answers.json",
    "fixtures/industrial-public-v2/source-catalog.json",
    "fixtures/industrial-public-v2/README.md",
)
CAPABILITY_SCHEMA = "trial-capability-candidates/v1"
CAPABILITY_PACKAGE_PATH = "trial/capabilities.json"
CAPABILITY_POLICY = "docs/design/TRIAL_CAPABILITY_POLICY.md"
CAPABILITY_STATES = {"candidate", "disabled"}
CAPABILITY_DEFAULT = "disabled"
# A generated package-level staging cover.  Historical DELIVERY_* documents
# remain byte-for-byte original; this notice is what tells a reader that any
# "FINAL" wording inside them is history, not a conclusion about this package.
STAGING_NOTICE = "STAGING_README.md"
STAGING_NOTICE_MARKERS = (
    "integrity-verified-staging",
    "release_ready=false",
    "publishable=false",
    "product_runtime_verified=false",
    "不是最终发布",
)
HISTORICAL_FINAL_MARKER = b"Release readiness: FINAL"
# The real Harness handshake imports pydantic through the official Python SDK.
# These exact wheels are installed into an isolated target venv; the main
# project virtualenv is never reused.
HANDSHAKE_PACKAGES = (
    ("annotated-types", "0.8.0", "annotated_types-0.8.0-py3-none-any.whl",
     "f072f4d804ea359e4eaf198b1af7a8b0943881a87f31bb764f8bf219bb9419e0"),
    ("pydantic", "2.14.2", "pydantic-2.14.2-py3-none-any.whl",
     "7efbf7507b937931cd32a5755acb00ea1cf5ccb1d4e8067a5850d5069e382118"),
    ("pydantic-core", "2.50.2", "pydantic_core-2.50.2-cp310-cp310-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
     "5c12a5a080570a91bbcbf6d10db008b80e3f5dea015ef2f0137bbec70f24f3f3"),
    ("typing-extensions", "4.16.0", "typing_extensions-4.16.0-py3-none-any.whl",
     "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8"),
    ("typing-inspection", "0.4.5", "typing_inspection-0.4.5-py3-none-any.whl",
     "bc210535442b39e1bf2805e0c7a17dfd6a4ace760c84c7f7d16245d23a6b1c39"),
)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ReleaseSpec:
    """Pinned identities and explicit selections the manifest is allowed to trust."""

    harness_commit: str = PIN
    node_version: str = NODE_VERSION
    node_sha256: str = NODE_SHA256
    pnpm_version: str = PNPM_VERSION
    pnpm_sha512: str = PNPM_SHA512
    harness_license_sha256: str = build_release.LICENSE_INPUTS[0][2]
    hipporag_license_sha256: str = build_release.LICENSE_INPUTS[1][2]
    license_inputs: tuple[tuple[str, str, str], ...] = build_release.LICENSE_INPUTS
    fixture_selection: tuple[str, ...] = FIXTURE_SELECTION
    capability_input: str | None = None


DEFAULT_SPEC = ReleaseSpec()


def safety_violation(name: str, content: bytes) -> str | None:
    """Return a path-only reason, never the matched secret or file contents."""
    candidate = PurePosixPath(name)
    if candidate.is_absolute() or ".." in candidate.parts or "\\" in name:
        return f"unsafe archive path: {name}"
    if build_release.build_copy(candidate):
        return f"build copy or non-client dist cache is not packageable: {name}"
    lowered = [part.lower() for part in candidate.parts]
    if any(part in build_release.IGNORED for part in lowered):
        return f"private runtime path is not packageable: {name}"
    if candidate.name.lower() in build_release.PRIVATE_NAMES or candidate.name.lower().startswith(".env") and candidate.name.lower() != ".env.example":
        return f"private file name is not packageable: {name}"
    if candidate.suffix.lower() in build_release.PRIVATE_SUFFIXES:
        return f"database, log or key suffix is not packageable: {name}"
    if any(marker.search(content) for marker in build_release.SECRET_MARKERS):
        return f"potential secret marker found; inspect privately: {name}"
    return None


def tree_digest(entries: Iterable[tuple[str, bytes]]) -> str:
    """Digest source paths and contents. Acceptance evidence is excluded."""
    digest = hashlib.sha256()
    for name, content in sorted(entries):
        if name == "manifest.json" or name.startswith("evidence/"):
            continue
        digest.update(name.encode("utf-8") + b"\0" + sha256_bytes(content).encode("ascii") + b"\n")
    return digest.hexdigest()


def git_state(root: Path) -> dict:
    try:
        head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        branch = subprocess.check_output(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        status = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"], text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError) as error:
        raise SystemExit("Trial release requires the source tree to be a readable Git worktree") from error
    entries = sorted(line for line in status.splitlines() if line.strip())
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise SystemExit("Trial release source HEAD is not a full Git commit id")
    return {"head": head, "branch": branch, "dirty": bool(entries), "dirty_entries": entries, "dirty_entry_count": len(entries)}


def pinned_evidence(root: Path, spec: ReleaseSpec) -> dict:
    document = root / "docs/upstream-sources.json"
    if not document.is_file():
        raise SystemExit("Required release input missing: docs/upstream-sources.json")
    try:
        payload = json.loads(document.read_text(encoding="utf-8"))
        sources = {item["name"]: item for item in payload["sources"]}
    except (ValueError, KeyError, TypeError) as error:
        raise SystemExit("docs/upstream-sources.json is not a valid reviewed source record") from error
    harness = sources.get("DeepSeek Harness")
    hipporag = sources.get("HippoRAG")
    if harness is None or hipporag is None:
        raise SystemExit("docs/upstream-sources.json must record DeepSeek Harness and HippoRAG")
    if harness.get("commit") != spec.harness_commit:
        raise SystemExit("Reviewed Harness commit does not match the fixed release pin")
    if harness.get("license") != "MIT" or harness.get("license_file_sha256") != spec.harness_license_sha256:
        raise SystemExit("Reviewed Harness MIT license evidence does not match")
    if harness.get("node_version") != spec.node_version or harness.get("pnpm_version") != spec.pnpm_version:
        raise SystemExit("Reviewed Node/pnpm pins do not match the installer")
    if hipporag.get("license") != "MIT" or hipporag.get("license_file_sha256") != spec.hipporag_license_sha256:
        raise SystemExit("Reviewed HippoRAG MIT license evidence does not match")
    return {
        "harness": {
            "repo": harness.get("url", HARNESS_REPO),
            "commit": spec.harness_commit,
            "license": "MIT",
            "license_sha256": spec.harness_license_sha256,
            "node_version": spec.node_version,
            "node_sha256": spec.node_sha256,
            "pnpm_version": spec.pnpm_version,
            "pnpm_sha512": spec.pnpm_sha512,
            "runtime_installed": bool(harness.get("runtime_installed", False)),
        },
        "hipporag": {
            "repo": hipporag.get("url"),
            "commit": hipporag.get("commit"),
            "license": "MIT",
            "license_sha256": spec.hipporag_license_sha256,
            "application_runtime_enabled": bool(hipporag.get("application_runtime_enabled", False)),
            "distribution_review": hipporag.get("distribution_review"),
        },
    }


def _load_json(root: Path, relative: str) -> dict:
    path = root / relative
    if not path.is_file():
        raise SystemExit(f"Required fixture metadata missing: {relative}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise SystemExit(f"Fixture metadata is not valid JSON: {relative}") from error


def fixture_provenance(root: Path, relative: str) -> dict:
    """Only explicitly selected synthetic/public records may enter a package."""
    synthetic_manifest = _load_json(root, "fixtures/synthetic-industrial-v1/manifest.json")
    public_catalog = _load_json(root, "fixtures/industrial-public-v2/source-catalog.json")
    if synthetic_manifest.get("synthetic") is not True:
        raise SystemExit("synthetic-industrial-v1 must declare synthetic=true")
    if public_catalog.get("customer_data") is not False:
        raise SystemExit("industrial-public-v2 must declare customer_data=false")

    if relative.startswith("fixtures/synthetic-industrial-v1/"):
        record = {
            "kind": "synthetic",
            "fixture_id": synthetic_manifest.get("fixture_id"),
            "license": synthetic_manifest.get("license"),
            "purpose": synthetic_manifest.get("purpose"),
            "customer_data": False,
            "source": "project-authored synthetic fixture",
        }
        for item in synthetic_manifest.get("files", []):
            if f"fixtures/synthetic-industrial-v1/{item.get('path')}" == relative:
                actual = sha256_file(root / relative)
                if item.get("sha256") != actual:
                    raise SystemExit(f"Fixture hash does not match its manifest: {relative}")
                record["declared_sha256"] = item.get("sha256")
                record["media_type"] = item.get("media_type")
                break
        return record

    if relative.startswith("fixtures/industrial-public-v2/"):
        referenced = []
        for asset in public_catalog.get("local_assets", []):
            referenced.append({
                "asset_id": asset.get("asset_id"),
                "source_url": asset.get("source_url"),
                "license": asset.get("license"),
                "sha256": asset.get("sha256"),
            })
            if f"fixtures/industrial-public-v2/{asset.get('path')}" == relative and asset.get("sha256") != sha256_file(root / relative):
                raise SystemExit(f"Public fixture hash does not match its catalog: {relative}")
        if not referenced:
            raise SystemExit("industrial-public-v2 source catalog has no public source records")
        return {
            "kind": "public_metadata",
            "corpus_id": public_catalog.get("corpus_id"),
            "license": "see referenced public sources",
            "purpose": public_catalog.get("purpose"),
            "customer_data": False,
            "referenced_sources": referenced,
        }

    raise SystemExit(f"Fixture is not in the explicit synthetic/public families: {relative}")


def normalize_capabilities(content: bytes, origin: str) -> dict:
    """Validate an explicit candidate list; a candidate is never release-open."""
    try:
        payload = json.loads(content.decode("utf-8"))
    except ValueError as error:
        raise SystemExit("Capability candidate list is not valid JSON") from error
    if payload.get("schema") != CAPABILITY_SCHEMA:
        raise SystemExit(f"Capability candidate list schema must be {CAPABILITY_SCHEMA}")
    if payload.get("mode") != "trial":
        raise SystemExit("Capability candidate list must declare mode=trial")
    if payload.get("default_when_unlisted") != CAPABILITY_DEFAULT:
        raise SystemExit("Capability candidate list must declare default_when_unlisted=disabled")
    policy_sha = payload.get("policy_sha256")
    if not isinstance(policy_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", policy_sha):
        raise SystemExit("Capability candidate list must bind policy_sha256 to the packaged policy")
    items = payload.get("candidates")
    if not isinstance(items, list) or not items:
        raise SystemExit("Capability candidate list has no candidates")
    normalized = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise SystemExit("Capability candidate entry is not an object")
        ident = item.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_]{1,63}", ident):
            raise SystemExit(f"Capability candidate id is invalid: {ident!r}")
        if ident in seen:
            raise SystemExit(f"Duplicate capability candidate: {ident}")
        seen.add(ident)
        state = item.get("state")
        if state == "enabled":
            raise SystemExit("Capability candidate list may not claim enabled; candidate is not release-open")
        if state not in CAPABILITY_STATES:
            raise SystemExit(f"Capability candidate state is invalid: {ident}")
        scope = item.get("scope")
        if not isinstance(scope, str) or not scope.strip():
            raise SystemExit(f"Capability candidate scope is empty: {ident}")
        evidence = item.get("acceptance_evidence", [])
        if not isinstance(evidence, list):
            raise SystemExit(f"Capability candidate acceptance_evidence must be a list: {ident}")
        normalized.append({
            "id": ident,
            "state": state,
            "scope": scope.strip(),
            "acceptance_evidence": evidence,
            "release_open": False,
            "runtime_enforcement_verified": False,
            "runtime_state": "unverified",
            "runtime_reason": "this package records candidate scope only; it neither verifies nor enforces runtime interface blocking",
        })
    normalized.sort(key=lambda entry: entry["id"])
    return {
        "schema": CAPABILITY_SCHEMA,
        "mode": "trial",
        "policy": CAPABILITY_POLICY,
        "policy_sha256": policy_sha,
        "origin": origin,
        "input_sha256": sha256_bytes(content),
        "package_path": CAPABILITY_PACKAGE_PATH,
        "default_when_unlisted": "disabled",
        "release_open": [],
        "runtime_enforcement_verified": False,
        "runtime_state": "unverified",
        "candidate_count": sum(1 for entry in normalized if entry["state"] == "candidate"),
        "disabled_count": sum(1 for entry in normalized if entry["state"] == "disabled"),
        "candidates": normalized,
        "claim": "Candidate scope only; this manifest neither opens capabilities nor verifies their runtime enforcement.",
    }


def staging_notice_text(historical_final_documents: list[str]) -> str:
    """Deterministic staging cover; history is disclaimed, never rewritten."""
    lines = [
        "# 试用发行包 staging 封面（STAGING NOTICE）",
        "",
        "本目录是 `integrity-verified-staging` 候选包，不是最终发布，也不是可交付客户的试用包。",
        "`release_ready=false`，`publishable=false`，`product_runtime_verified=false`。",
        "判断本包状态请以 `manifest.json` 的 `release.stage`、`release.status`、"
        "`release.release_ready`、`release.publishable` 和本文件为准。",
        "",
        "## 历史 FINAL 文档说明",
        "",
        "本包为了保留项目历史，原样携带以下历史文档；它们记录的是过去版本的验收状态（例如 0.4.2 FINAL），",
        "不是本次 staging 候选的结论，也没有被修改或重写：",
        "",
        *[f"- `{name}`" for name in historical_final_documents],
        "",
        "这些历史 `FINAL` 字样不构成对本次包的发布通过；本次包只有本 staging 封面和 manifest 声明的完整性候选状态。",
        "",
    ]
    return "\n".join(lines)


def historical_final_documents(entries: dict[str, bytes]) -> list[str]:
    """List bundled historical FINAL documents without editing their bytes."""
    return sorted(
        name for name, content in entries.items()
        if name.startswith("docs/") and HISTORICAL_FINAL_MARKER in content
    )


def _stage_groups(root: Path, spec: ReleaseSpec) -> tuple[dict[str, bytes], dict[str, str], dict]:
    groups = build_release.collect(root=root, license_inputs=spec.license_inputs)
    entries: dict[str, bytes] = {}
    membership: dict[str, str] = {}
    for group, items in groups.items():
        for name, content in items:
            reason = safety_violation(name, content)
            if reason:
                raise SystemExit(reason)
            entries[name] = content
            membership[name] = group
    for relative in spec.fixture_selection:
        path = root / relative
        if not path.is_file():
            raise SystemExit(f"Explicit fixture selection is missing: {relative}")
        content = path.read_bytes()
        reason = safety_violation(relative, content)
        if reason:
            raise SystemExit(reason)
        entries[relative] = content
        membership[relative] = "fixtures"
    if not spec.capability_input:
        raise SystemExit("Explicit capability candidate input is required (--capabilities)")
    capability_source = (root / spec.capability_input).resolve()
    if not capability_source.is_file():
        raise SystemExit(f"Capability candidate input is missing: {spec.capability_input}")
    capability_content = capability_source.read_bytes()
    reason = safety_violation(CAPABILITY_PACKAGE_PATH, capability_content)
    if reason:
        raise SystemExit(reason)
    capabilities = normalize_capabilities(capability_content, spec.capability_input)
    policy_content = entries.get(CAPABILITY_POLICY)
    if policy_content is None:
        raise SystemExit(f"Required capability policy document is missing: {CAPABILITY_POLICY}")
    if capabilities["policy_sha256"] != sha256_bytes(policy_content):
        raise SystemExit("Capability candidate list policy_sha256 does not match the packaged policy document")
    entries[CAPABILITY_PACKAGE_PATH] = capability_content
    membership[CAPABILITY_PACKAGE_PATH] = "deployment"
    notice = staging_notice_text(historical_final_documents(entries))
    notice_reason = safety_violation(STAGING_NOTICE, notice.encode("utf-8"))
    if notice_reason:
        raise SystemExit(notice_reason)
    entries[STAGING_NOTICE] = notice.encode("utf-8")
    membership[STAGING_NOTICE] = "staging_notice"
    return entries, membership, capabilities


def _acceptance_reports(root: Path, entries: dict[str, bytes], membership: dict[str, str],
                        paths: tuple[str, ...], source_digest: str, *,
                        strict: bool) -> list[dict]:
    reports = []
    for relative in paths:
        path = root / relative
        if not path.is_file():
            raise SystemExit(f"Acceptance report is missing: {relative}")
        content = path.read_bytes()
        package_name = f"evidence/{path.name}"
        reason = safety_violation(package_name, content)
        if reason:
            raise SystemExit(reason)
        try:
            payload = json.loads(content.decode("utf-8"))
        except ValueError as error:
            raise SystemExit(f"Acceptance report is not valid JSON: {relative}") from error
        declared = payload.get("source_tree_sha256")
        status = str(payload.get("status", "")).upper()
        bound = declared == source_digest and status == "PASS"
        if not bound and strict:
            raise SystemExit(f"Acceptance report is not bound to this exact source tree: {relative}")
        if not bound:
            # A historical report for an earlier source tree must not be
            # packaged as evidence for this one.  Explicitly supplied reports
            # fail closed above; an auto-detected stale default is skipped.
            continue
        entries[package_name] = content
        membership[package_name] = "evidence"
        reports.append({
            "path": relative,
            "package_path": package_name,
            "sha256": sha256_bytes(content),
            "declared_source_tree_sha256": declared,
            "status": status or "UNKNOWN",
            "bound": bound,
        })
    return reports


def _manifest(root: Path, version: str, stage: str, label: str, entries: dict[str, bytes],
              membership: dict[str, str], spec: ReleaseSpec, git: dict, pinned: dict,
              reports: list[dict], capabilities: dict, authorized_by: str | None) -> dict:
    source_digest = tree_digest((name, content) for name, content in entries.items())
    source_files = [
        {"path": name, "bytes": len(content), "sha256": sha256_bytes(content), "group": membership.get(name, "source")}
        for name, content in entries.items()
        if membership.get(name) != "evidence"
    ]
    files = [
        {"path": name, "bytes": len(content), "sha256": sha256_bytes(content), "group": membership.get(name, "source")}
        for name, content in entries.items()
    ]
    licenses = []
    for archive_name, source_name, expected in spec.license_inputs:
        content = entries.get(archive_name)
        if content is None or sha256_bytes(content) != expected:
            raise SystemExit(f"License evidence is not staged correctly: {archive_name}")
        licenses.append({
            "path": archive_name,
            "source": source_name,
            "license": "MIT",
            "sha256": expected,
        })
    fixtures = [
        {
            "path": name,
            "bytes": len(content),
            "sha256": sha256_bytes(content),
            **fixture_provenance(root, name),
        }
        for name, content in entries.items()
        if membership.get(name) == "fixtures"
    ]
    all_bound = bool(reports) and all(report["bound"] for report in reports)
    authorization = bool(authorized_by and authorized_by.strip())
    historical_final = historical_final_documents(entries)
    notice_content = entries.get(STAGING_NOTICE)
    if notice_content is None:
        raise SystemExit(f"Required staging notice is missing: {STAGING_NOTICE}")
    # This packager has no authority to declare a release ready.  A bound JSON
    # report and an authorization string are advisory evidence only until the
    # complete product-runtime acceptance aggregation contract exists.
    ready = False
    gates = [
        {"name": "source_integrity", "status": "pass", "detail": "Git worktree readable; every staged source file has a SHA-256 digest"},
        {"name": "private_and_build_exclusion", "status": "pass", "detail": "Build copies, caches, databases, logs, keys and secret markers are excluded"},
        {"name": "license_evidence", "status": "pass", "detail": "Pinned Harness and HippoRAG MIT license files match docs/upstream-sources.json"},
        {"name": "pinned_toolchain", "status": "pass", "detail": "Harness commit, Node and pnpm pins match the reviewed installer"},
        {"name": "explicit_fixtures", "status": "pass", "detail": "Only synthetic/public fixture metadata is selected; customer_data is false"},
        {"name": "capability_candidates", "status": "pass", "detail": "Explicit candidate list bound by SHA-256; runtime enforcement is not claimed"},
        {"name": "staging_notice", "status": "pass", "detail": "A generated staging cover disclaims historical FINAL documents without modifying their bytes"},
        {"name": "acceptance_binding", "status": "pass" if all_bound else "missing",
         "detail": "Optional acceptance evidence matches this exact source tree; it cannot make the package publishable" if all_bound else "No PASS acceptance report is bound to this source tree"},
        {"name": "publishable_claim", "status": "blocked",
         "detail": "The complete product-runtime acceptance aggregation contract is not implemented; this packager only declares integrity-verified staging"},
        {"name": "authorization", "status": "recorded_not_sufficient" if authorization else "not_requested",
         "detail": "Authorization is recorded for audit only and cannot make this package publishable" if authorization else "No authorization supplied; staging candidates are not publishable"},
    ]
    return {
        "schema": SCHEMA,
        "release": {
            "version": version,
            "label": label,
            "stage": stage,
            "status": "integrity-verified-staging",
            "claim_scope": "integrity_verified_staging",
            "release_ready": ready,
            "publishable": ready,
            "release_blocked_reason": "complete product-runtime acceptance aggregation is not implemented; no JSON report or authorization string can mark this package publishable",
            "acceptance_evidence_recorded": all_bound,
            "authorized_by": authorized_by if authorization else None,
            "authorization_effect": "none",
            "deterministic": True,
        },
        "source_control": {
            "root": ".",
            "head": git["head"],
            "branch": git["branch"],
            "dirty": git["dirty"],
            "dirty_entry_count": git["dirty_entry_count"],
            "dirty_entries": git["dirty_entries"],
            "worktree": True,
        },
        "source_tree_sha256": source_digest,
        "staging_notice": {
            "path": STAGING_NOTICE,
            "sha256": sha256_bytes(notice_content),
            "stage_claim": "integrity-verified-staging",
            "release_ready": False,
            "publishable": False,
            "product_runtime_verified": False,
            "historical_final_documents": historical_final,
            "historical_documents_modified": False,
            "note": "The generated staging notice is the package-level cover. Historical DELIVERY_* documents are preserved byte-for-byte; any FINAL wording inside them describes a past version and is not a conclusion about this staging candidate.",
        },
        "source_files": sorted(source_files, key=lambda item: item["path"]),
        "files": sorted(files, key=lambda item: item["path"]),
        "build_environment": {
            "python_requires": ">=3.10",
            "server_dependency_pins": "server/pyproject.toml",
            "harness_commit": spec.harness_commit,
            "node_version": spec.node_version,
            "pnpm_version": spec.pnpm_version,
            "deterministic": "sorted paths, fixed JSON ordering and no wall-clock timestamps",
            "private_runtime_access_scope": "license_text_only",
            "private_runtime_license_inputs": sorted(source_name for _archive, source_name, _expected in spec.license_inputs),
            "private_runtime_credentials_used": False,
            "private_runtime_code_or_data_read": False,
            "private_runtime_code_or_data_copied": False,
            "private_runtime_note": "The packager reads only the two pinned MIT LICENSE text files from .runtime/third_party for license evidence; it does not read or copy private code, account databases, administrator passwords, provider settings, tokens, customer data or model weights.",
            "product_runtime_installed": False,
            "python_runtime_scope": "setup_harness_runtime.py uses the system Python standard library; verify_harness_runtime.py runs in an isolated target venv containing only pinned pydantic handshake wheels; the FastAPI product runtime is not installed or verified",
        },
        "pinned": pinned,
        "licenses": sorted(licenses, key=lambda item: item["path"]),
        "fixtures": sorted(fixtures, key=lambda item: item["path"]),
        "capabilities": capabilities,
        "acceptance": {
            "schema": ACCEPTANCE_SCHEMA,
            "reports": reports,
            "bound": all_bound,
            "default_report": ACCEPTANCE_REPORT,
            "effect": "advisory_only; a JSON report cannot make this package release-ready or publishable",
        },
        "gates": gates,
        "install_contract": {
            "scope": "harness_handshake_only",
            "new_empty_private_directory": True,
            "environment_switch_performed": False,
            "requires_git_worktree": True,
            "harness_source": HARNESS_SOURCE,
            "harness_commit": spec.harness_commit,
            "toolchain": TOOLCHAIN,
            "cache_roots": [f"{TOOLCHAIN}/cache", f"{TOOLCHAIN}/tmp", f"{TOOLCHAIN}/pnpm-store"],
            "no_private_runtime_copy": True,
            "trusted_expected_source_tree_sha256_required": True,
            "target_verified_after_copy_before_execution": True,
            "target_verification": "exact file set, per-file SHA-256 and byte size, no symlinks; measured target tree digest must equal the trusted expected digest",
            "target_verification_after_execution": "the source tree digest is re-measured after every target execution; any drift outside .runtime (including .pyc files) fails closed",
            "bytecode_policy": "every target Python execution sets PYTHONDONTWRITEBYTECODE=1 so handshake, plugin verification and product probes cannot write .pyc into the staged source tree",
            "pnpm_store_policy": "requires --pnpm-store-expected-manifest-sha256; only the versioned files/ and index.db* payload may be reused; each content-addressed name is checked against the content digest (SHA-256 for 64-hex names, SHA-512 for 128-hex names), the projects/ registry is never copied, and symlinks or unsupported names fail closed",
            "product_runtime_verified": False,
            "handshake_venv": ".runtime/venv",
            "handshake_dependencies": [f"{name}=={version}" for name, version, _wheel, _digest in HANDSHAKE_PACKAGES],
            "handshake_wheel_sha256": {wheel: digest for _name, _version, wheel, digest in HANDSHAKE_PACKAGES},
            "python_dependencies": "the isolated target venv installs only pinned pydantic and its transitive wheels for the Harness handshake; the full server/pyproject.toml product runtime is not installed",
            "commands": [
                f"git clone {HARNESS_REPO} {HARNESS_SOURCE} && git -C {HARNESS_SOURCE} checkout --detach {spec.harness_commit}",
                "python3 scripts/setup_harness_runtime.py --install --prepare-source",
                "python3 scripts/verify_harness_runtime.py",
                "python3 scripts/verify_harness_plugins.py",
                "python3 scripts/verify_harness_sdk_protocol.py",
                "python3 scripts/trial_smoke.py",
                "python3 scripts/trial_capacity.py",
            ],
        },
        "rollback": {
            "independent_directory": True,
            "independent_port": True,
            "replaces_existing_service": False,
            "environment_switch_performed": False,
            "steps": [
                "在新独立目录安装候选包，不改动现有服务、现有数据目录和现有端口。",
                "使用独立回环端口启动候选服务并执行同一套合成烟囱。",
                "任何失败停止候选进程并删除候选目录；旧服务继续运行。",
                "只有学校云验收通过并经用户书面批准后才讨论入口切换；本包不执行切换。",
            ],
        },
        "limitations": [
            "这是学校云可复现 staging 候选，不是已验收的最终试用版。",
            "包内 STAGING_README.md 是生成的 staging 封面。历史 DELIVERY_* FINAL 文档保持字节原样，只代表过去版本的验收状态，不代表本次候选已通过发布或验收。",
            "构建只从 .runtime/third_party 读取两个固定 MIT 许可证文本作为许可证据；不读取或复制私有代码、账号库、管理员密码、供应商配置、凭据、客户数据或模型权重。",
            "源工作树可能包含未提交变更；manifest 记录真实 HEAD 与 dirty 清单，不伪称是冻结提交。",
            "知识库数据未随包分发；只含显式选择的合成/公开元数据，客户资料必须在正常接口中另行导入。",
            "能力清单只记录候选范围，本包既不打开能力，也不验证运行时封禁；未列出的能力按部署策略默认不开放，候选不等于已验收或已开放。",
            "本包和安装器只验证 Node/Harness 工具链与零供应商握手：握手所需的 pydantic 安装在一个隔离的 .runtime/venv 中，但不安装 FastAPI 等完整产品依赖（server/pyproject.toml）；完整产品运行仍是缺口，不能用主项目虚拟环境冒称。",
            "Harness 安装需要 Git 工作树；仅复制散文件不能通过 setup 校验。",
            "本包不包含私有 .runtime、账号库、管理员密码、客户凭据、数据库或模型权重。",
            "真实模型调用、容量压测和浏览器验收不由本包自动执行。",
        ],
    }


def build_trial_release(root: Path | None = None, version: str = DEFAULT_VERSION, stage: str = "staging",
                        output: Path | None = None, spec: ReleaseSpec | None = None,
                        acceptance_paths: tuple[str, ...] | None = None, authorized_by: str | None = None) -> dict:
    root = (root or ROOT).resolve()
    spec = spec or DEFAULT_SPEC
    if stage != "staging":
        raise SystemExit(
            "Final/publishable builds are disabled: the complete product-runtime acceptance "
            "aggregation contract is not implemented. Build integrity-verified staging only."
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
        raise SystemExit("Version must start with a letter or digit and contain only letters, digits, dots, dashes or underscores")
    git = git_state(root)
    pinned = pinned_evidence(root, spec)
    entries, membership, capabilities = _stage_groups(root, spec)
    source_digest = tree_digest((name, content) for name, content in entries.items())
    explicit_acceptance = acceptance_paths is not None
    if acceptance_paths is None:
        acceptance_paths = (ACCEPTANCE_REPORT,) if (root / ACCEPTANCE_REPORT).is_file() else ()
    reports = _acceptance_reports(
        root, entries, membership, tuple(acceptance_paths), source_digest, strict=explicit_acceptance,
    )
    label = f"{version}-staging"
    destination = (output or (root / "dist/trial-releases" / label)).resolve()
    if destination.exists():
        raise SystemExit(f"Trial release destination already exists; choose a new directory: {destination}")
    metadata = _manifest(root, version, stage, label, entries, membership, spec, git, pinned, reports, capabilities, authorized_by)
    payload = json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp_root = root / ".runtime/tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="trial-release-", dir=temp_root))
    try:
        for name, content in sorted(entries.items()):
            target = temporary / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (temporary / "manifest.json").write_text(payload, encoding="utf-8")
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {"status": "built", "stage": stage, "claim_scope": metadata["release"]["claim_scope"],
            "release_ready": metadata["release"]["release_ready"],
            "publishable": metadata["release"]["publishable"],
            "destination": str(destination), "source_tree_sha256": metadata["source_tree_sha256"],
            "manifest_sha256": sha256_file(destination / "manifest.json")}


def _actual_files(package: Path) -> dict[str, bytes]:
    content: dict[str, bytes] = {}
    for path in sorted(package.rglob("*")):
        relative = path.relative_to(package).as_posix()
        if path.is_symlink():
            raise SystemExit(f"Package contains a symlink: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise SystemExit(f"Package contains an unsupported entry: {relative}")
        if relative == "manifest.json":
            continue
        content[relative] = path.read_bytes()
    return content


def verify_trial_release(package: Path, spec: ReleaseSpec | None = None, *,
                         expected_source_tree_sha256: str | None = None) -> dict:
    spec = spec or DEFAULT_SPEC
    package = package.resolve()
    manifest_path = package / "manifest.json"
    reasons: list[str] = []
    if not manifest_path.is_file():
        return {"status": "FAIL", "reasons": ["manifest.json is missing"]}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError:
        return {"status": "FAIL", "reasons": ["manifest.json is not valid JSON"]}
    if manifest.get("schema") != SCHEMA:
        reasons.append("manifest schema is not trial-native-release/v1")
    try:
        actual = _actual_files(package)
    except SystemExit as error:
        reasons.append(str(error))
        actual = {}
    declared = {item["path"]: item for item in manifest.get("files", [])}
    if set(actual) != set(declared):
        missing = sorted(set(declared) - set(actual))
        extra = sorted(set(actual) - set(declared))
        if missing:
            reasons.append(f"declared files are missing: {', '.join(missing[:8])}")
        if extra:
            reasons.append(f"unexpected files are present: {', '.join(extra[:8])}")
    for name, content in actual.items():
        record = declared.get(name)
        if record is None:
            continue
        if len(content) != record.get("bytes") or sha256_bytes(content) != record.get("sha256"):
            reasons.append(f"file digest mismatch: {name}")
        reason = safety_violation(name, content)
        if reason:
            reasons.append(reason)
    computed_digest = tree_digest(actual.items())
    if manifest.get("source_tree_sha256") != computed_digest:
        reasons.append("source_tree_sha256 does not match the staged source tree")
    if expected_source_tree_sha256 is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_source_tree_sha256):
            reasons.append("trusted expected_source_tree_sha256 is not a lowercase SHA-256 digest")
        elif computed_digest != expected_source_tree_sha256:
            reasons.append("source_tree_sha256 does not match the trusted expected digest")
    derived_sources = sorted(
        ({"path": name, "bytes": len(content), "sha256": sha256_bytes(content),
          "group": declared.get(name, {}).get("group", "source")}
         for name, content in actual.items() if not name.startswith("evidence/")),
        key=lambda item: item["path"],
    )
    declared_sources = sorted(manifest.get("source_files", []), key=lambda item: item.get("path", ""))
    if derived_sources != declared_sources:
        reasons.append("source_files does not match the staged non-evidence files")

    notice_record = manifest.get("staging_notice")
    if not isinstance(notice_record, dict):
        reasons.append("staging_notice block is missing")
    else:
        notice_content = actual.get(STAGING_NOTICE)
        if notice_content is None:
            reasons.append(f"packaged staging notice is missing: {STAGING_NOTICE}")
        else:
            if sha256_bytes(notice_content) != notice_record.get("sha256"):
                reasons.append("packaged staging notice digest does not match")
            notice_text = notice_content.decode("utf-8", errors="replace")
            missing_markers = [marker for marker in STAGING_NOTICE_MARKERS if marker not in notice_text]
            if missing_markers:
                reasons.append("staging notice is missing required disclaimers: " + ", ".join(missing_markers))
            if notice_record.get("historical_final_documents") != historical_final_documents(actual):
                reasons.append("staging notice historical FINAL document list does not match the package")
            if notice_record.get("historical_documents_modified") is not False:
                reasons.append("staging notice must declare historical_documents_modified=false")
            if notice_record.get("stage_claim") != "integrity-verified-staging":
                reasons.append("staging notice stage claim must be integrity-verified-staging")
            if (notice_record.get("release_ready") is not False
                    or notice_record.get("publishable") is not False
                    or notice_record.get("product_runtime_verified") is not False):
                reasons.append("staging notice must keep release_ready/publishable/product_runtime_verified false")

    build_environment = manifest.get("build_environment")
    if not isinstance(build_environment, dict):
        reasons.append("build_environment block is missing")
    else:
        if build_environment.get("private_runtime_access_scope") != "license_text_only":
            reasons.append("build_environment must declare private_runtime_access_scope=license_text_only")
        if build_environment.get("private_runtime_credentials_used") is not False:
            reasons.append("build_environment must declare private_runtime_credentials_used=false")
        if build_environment.get("private_runtime_code_or_data_read") is not False:
            reasons.append("build_environment must declare private_runtime_code_or_data_read=false")
        if build_environment.get("private_runtime_code_or_data_copied") is not False:
            reasons.append("build_environment must declare private_runtime_code_or_data_copied=false")
        if build_environment.get("private_runtime_used") is True:
            reasons.append("build_environment may not claim private_runtime_used=true")
        expected_license_inputs = sorted(source_name for _archive, source_name, _expected in spec.license_inputs)
        if sorted(build_environment.get("private_runtime_license_inputs", [])) != expected_license_inputs:
            reasons.append("build_environment private_runtime_license_inputs do not match the pinned license inputs")
        if build_environment.get("product_runtime_installed") is not False:
            reasons.append("build_environment must keep product_runtime_installed=false")

    install_contract = manifest.get("install_contract")
    if not isinstance(install_contract, dict):
        reasons.append("install_contract block is missing")
    else:
        if install_contract.get("scope") != "harness_handshake_only":
            reasons.append("install_contract.scope must remain harness_handshake_only")
        if install_contract.get("new_empty_private_directory") is not True:
            reasons.append("install_contract must require a new empty private directory")
        if install_contract.get("environment_switch_performed") is not False:
            reasons.append("install_contract must keep environment_switch_performed=false")
        if install_contract.get("no_private_runtime_copy") is not True:
            reasons.append("install_contract must prohibit private runtime copies")
        if install_contract.get("trusted_expected_source_tree_sha256_required") is not True:
            reasons.append("install_contract must require a trusted expected source-tree digest")
        if install_contract.get("target_verified_after_copy_before_execution") is not True:
            reasons.append("install_contract must verify the target before execution")
        if install_contract.get("product_runtime_verified") is not False:
            reasons.append("install_contract must keep product_runtime_verified=false")

    # These fields are not part of the staging schema, but older or hand-edited
    # manifests could add them.  A truthy value must never survive verification.
    if manifest.get("product_runtime_verified") not in (None, False):
        reasons.append("top-level product_runtime_verified must be absent or false")
    release_truth = manifest.get("release", {})
    if isinstance(release_truth, dict):
        if release_truth.get("product_runtime_verified") not in (None, False):
            reasons.append("release.product_runtime_verified must be absent or false")
        if release_truth.get("product_runtime_installed") not in (None, False):
            reasons.append("release.product_runtime_installed must be absent or false")

    pinned = manifest.get("pinned", {})
    harness = pinned.get("harness", {})
    if harness.get("commit") != spec.harness_commit:
        reasons.append("pinned Harness commit does not match")
    if harness.get("node_version") != spec.node_version or harness.get("node_sha256") != spec.node_sha256:
        reasons.append("pinned Node identity does not match")
    if harness.get("pnpm_version") != spec.pnpm_version or harness.get("pnpm_sha512") != spec.pnpm_sha512:
        reasons.append("pinned pnpm identity does not match")
    if harness.get("license_sha256") != spec.harness_license_sha256:
        reasons.append("pinned Harness license does not match")
    if pinned.get("hipporag", {}).get("license_sha256") != spec.hipporag_license_sha256:
        reasons.append("pinned HippoRAG license does not match")

    try:
        embedded_pins = json.loads(actual.get("docs/upstream-sources.json", b"{}").decode("utf-8"))
        sources = {item["name"]: item for item in embedded_pins.get("sources", [])}
        if sources.get("DeepSeek Harness", {}).get("commit") != spec.harness_commit:
            reasons.append("packaged upstream-sources.json Harness commit does not match")
        if sources.get("DeepSeek Harness", {}).get("license_file_sha256") != spec.harness_license_sha256:
            reasons.append("packaged upstream-sources.json Harness license does not match")
        if sources.get("HippoRAG", {}).get("license_file_sha256") != spec.hipporag_license_sha256:
            reasons.append("packaged upstream-sources.json HippoRAG license does not match")
    except (ValueError, KeyError, TypeError):
        reasons.append("packaged docs/upstream-sources.json is not a valid reviewed record")

    licenses = {item.get("path"): item for item in manifest.get("licenses", [])}
    for archive_name, _source_name, expected in spec.license_inputs:
        record = licenses.get(archive_name)
        content = actual.get(archive_name)
        if record is None or content is None:
            reasons.append(f"license evidence is missing: {archive_name}")
        elif sha256_bytes(content) != expected or record.get("sha256") != expected:
            reasons.append(f"license evidence digest mismatch: {archive_name}")

    fixture_paths = {name for name in actual if name.startswith("fixtures/")}
    if fixture_paths - set(spec.fixture_selection):
        reasons.append("package contains a fixture outside the explicit selection")
    for item in manifest.get("fixtures", []):
        path = item.get("path")
        content = actual.get(path)
        if content is None or sha256_bytes(content) != item.get("sha256"):
            reasons.append(f"fixture digest mismatch: {path}")
        if item.get("customer_data") is not False:
            reasons.append(f"fixture does not assert customer_data=false: {path}")

    capability_contract = manifest.get("capabilities")
    if not isinstance(capability_contract, dict):
        reasons.append("capabilities block is missing")
    else:
        capability_content = actual.get(CAPABILITY_PACKAGE_PATH)
        policy_content = actual.get(CAPABILITY_POLICY)
        if capability_content is None:
            reasons.append("packaged capability candidate list is missing")
        elif sha256_bytes(capability_content) != capability_contract.get("input_sha256"):
            reasons.append("packaged capability candidate list digest does not match")
        else:
            try:
                recomputed = normalize_capabilities(capability_content, str(capability_contract.get("origin", "")))
            except SystemExit as error:
                reasons.append(str(error))
            else:
                if recomputed != capability_contract:
                    reasons.append("capabilities block does not match the packaged candidate list")
                if recomputed["release_open"]:
                    reasons.append("capabilities block claims release-open capabilities")
                if capability_contract.get("runtime_enforcement_verified") is not False:
                    reasons.append("capabilities block claims verified runtime enforcement")
                if capability_contract.get("runtime_state") != "unverified":
                    reasons.append("capabilities block does not declare runtime_state=unverified")
                if any(item.get("runtime_state") != "unverified" or item.get("runtime_enforcement_verified") is not False
                       for item in capability_contract.get("candidates", [])):
                    reasons.append("a candidate claims verified runtime enforcement or a non-unverified runtime state")
        if policy_content is None:
            reasons.append("packaged capability policy document is missing")
        elif sha256_bytes(policy_content) != capability_contract.get("policy_sha256"):
            reasons.append("capability policy digest does not match")

    release = manifest.get("release", {})
    if release.get("stage") != "staging":
        reasons.append("manifest release.stage must be staging; final and publishable stages are disabled")
    if release.get("status") != "integrity-verified-staging":
        reasons.append("manifest release.status must be integrity-verified-staging")
    if release.get("claim_scope") != "integrity_verified_staging":
        reasons.append("manifest release.claim_scope must be integrity_verified_staging")
    if release.get("release_ready") is not False or release.get("publishable") is not False:
        reasons.append("release_ready/publishable must be false; this packager cannot declare a publishable release")
    if release.get("authorization_effect") != "none":
        reasons.append("authorization must not have an effect on release readiness")
    reports = manifest.get("acceptance", {}).get("reports", [])
    for report in reports:
        package_path = report.get("package_path")
        content = actual.get(package_path)
        if content is None:
            reasons.append(f"acceptance evidence is missing: {package_path}")
            continue
        if sha256_bytes(content) != report.get("sha256"):
            reasons.append(f"acceptance evidence digest mismatch: {package_path}")
            continue
        try:
            payload = json.loads(content.decode("utf-8"))
        except ValueError:
            reasons.append(f"acceptance evidence is not valid JSON: {package_path}")
            continue
        if payload.get("source_tree_sha256") != computed_digest:
            reasons.append(f"acceptance evidence is not bound to this source tree: {package_path}")
        if str(payload.get("status", "")).upper() != "PASS":
            reasons.append(f"acceptance evidence is not PASS: {package_path}")
    bound = bool(reports) and all(report.get("bound") for report in reports)
    gate_items = manifest.get("gates", [])
    if not isinstance(gate_items, list) or not all(isinstance(item, dict) for item in gate_items):
        reasons.append("manifest gates block is missing or malformed")
        gate_items = []
    gate_names = [item.get("name") for item in gate_items]
    if len(gate_names) != len(set(gate_names)):
        reasons.append("manifest contains duplicate gate names")
    expected_gate_names = {
        "source_integrity",
        "private_and_build_exclusion",
        "license_evidence",
        "pinned_toolchain",
        "explicit_fixtures",
        "capability_candidates",
        "staging_notice",
        "acceptance_binding",
        "publishable_claim",
        "authorization",
    }
    if set(gate_names) != expected_gate_names:
        reasons.append("manifest gate names do not match the staging contract")
    gates = {item.get("name"): item.get("status") for item in gate_items}
    for name in ("source_integrity", "private_and_build_exclusion", "license_evidence",
                 "pinned_toolchain", "explicit_fixtures", "capability_candidates",
                 "staging_notice"):
        if gates.get(name) != "pass":
            reasons.append(f"manifest hard gate is not pass: {name}")
    expected_acceptance_gate = "pass" if bound else "missing"
    if gates.get("acceptance_binding") != expected_acceptance_gate:
        reasons.append(
            f"manifest acceptance_binding gate must be {expected_acceptance_gate}"
        )
    if gates.get("publishable_claim") != "blocked":
        reasons.append("manifest publishable_claim gate must remain blocked")
    authorization_present = bool(
        isinstance(release_truth, dict) and release_truth.get("authorized_by")
    )
    expected_authorization_gate = (
        "recorded_not_sufficient" if authorization_present else "not_requested"
    )
    if gates.get("authorization") != expected_authorization_gate:
        reasons.append(
            "manifest authorization gate must remain recorded_not_sufficient "
            "or not_requested and never imply sufficiency"
        )
    if bool(manifest.get("acceptance", {}).get("bound")) != bound:
        reasons.append("manifest acceptance.bound does not match the recomputed evidence")
    if isinstance(release_truth, dict):
        if bool(release_truth.get("acceptance_evidence_recorded")) != bound:
            reasons.append("release.acceptance_evidence_recorded does not match the recomputed evidence")
    acceptance_effect = manifest.get("acceptance", {}).get("effect")
    if not isinstance(acceptance_effect, str) or not acceptance_effect.startswith("advisory_only"):
        reasons.append("manifest acceptance.effect must remain advisory_only")
    report = {
        "schema": "trial-release-verification/v1",
        "status": "PASS" if not reasons else "FAIL",
        "package": str(package),
        "source_tree_sha256": computed_digest,
        "expected_source_tree_sha256": expected_source_tree_sha256,
        "release_stage": release.get("stage"),
        "claim_scope": release.get("claim_scope"),
        "release_ready": bool(release.get("release_ready")),
        "publishable": bool(release.get("publishable")),
        "acceptance_bound": bound,
        "file_count": len(actual),
        "reasons": reasons,
    }
    return report


def _write_json(document: dict) -> None:
    sys.stdout.write(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--version", default=DEFAULT_VERSION)
    build.add_argument("--stage", choices=("staging", "final"), default="staging",
                       help="Only staging is supported in this version; final is rejected because publishable aggregation is not implemented")
    build.add_argument("--output", type=Path)
    build.add_argument("--acceptance", action="append", default=None,
                       help="Acceptance JSON report; repeatable. Explicit reports must be bound to this exact source tree. Defaults to docs/evidence/TRIAL_PACKAGE_2026-11-12.json when present and bound.")
    build.add_argument("--capabilities", required=True,
                       help="Explicit trial-capability-candidates/v1 JSON input; its SHA-256 is bound into the package.")
    build.add_argument("--authorized-by", default=None,
                       help="Recorded for audit only; this packager never marks a package publishable")
    verify = subparsers.add_parser("verify")
    verify.add_argument("--package", type=Path, required=True)
    verify.add_argument("--expected-source-tree-sha256", default=None,
                        help="Trusted out-of-band source-tree digest; the package must match it exactly")
    args = parser.parse_args(argv)
    if args.command == "build":
        acceptance = tuple(args.acceptance) if args.acceptance else None
        spec = replace(DEFAULT_SPEC, capability_input=args.capabilities)
        try:
            result = build_trial_release(version=args.version, stage=args.stage, output=args.output,
                                         spec=spec, acceptance_paths=acceptance,
                                         authorized_by=args.authorized_by)
        except SystemExit as error:
            _write_json({"status": "FAIL", "reason": str(error)})
            return 1
        _write_json(result)
        return 0
    report = verify_trial_release(args.package, expected_source_tree_sha256=args.expected_source_tree_sha256)
    _write_json(report)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
