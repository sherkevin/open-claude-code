"""Contract tests for the reproducible native trial-release staging package.

These tests deliberately use a synthetic mini-project under the project
``.runtime/tmp`` directory (never ``/tmp``) so they do not depend on the real
dirty worktree, private ``.runtime`` data, Harness installation or the network.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_release  # noqa: E402
import trial_release_install  # noqa: E402
import trial_release_manifest  # noqa: E402
from setup_harness_runtime import NODE_SHA256, NODE_VERSION, PIN, PNPM_SHA512, PNPM_VERSION  # noqa: E402

HARNESS_LICENSE = b"MIT License\n\nSynthetic Harness fixture license.\n"
HIPPORAG_LICENSE = b"MIT License\n\nSynthetic HippoRAG fixture license.\n"
CHOSEN_FIXTURES = trial_release_manifest.FIXTURE_SELECTION
SPEC = trial_release_manifest.ReleaseSpec(
    harness_commit=PIN,
    node_version=NODE_VERSION,
    node_sha256=NODE_SHA256,
    pnpm_version=PNPM_VERSION,
    pnpm_sha512=PNPM_SHA512,
    harness_license_sha256=hashlib.sha256(HARNESS_LICENSE).hexdigest(),
    hipporag_license_sha256=hashlib.sha256(HIPPORAG_LICENSE).hexdigest(),
    license_inputs=(
        ("licenses/deepseek-harness-MIT.txt", ".runtime/third_party/deepseek-harness/LICENSE",
         hashlib.sha256(HARNESS_LICENSE).hexdigest()),
        ("licenses/HippoRAG-MIT.txt", ".runtime/third_party/HippoRAG/LICENSE",
         hashlib.sha256(HIPPORAG_LICENSE).hexdigest()),
    ),
    fixture_selection=CHOSEN_FIXTURES,
    capability_input="capabilities.json",
)


def capability_document(policy_sha256: str) -> dict:
    return {
        "schema": trial_release_manifest.CAPABILITY_SCHEMA,
        "mode": "trial",
        "default_when_unlisted": "disabled",
        "policy_sha256": policy_sha256,
        "candidates": [
            {"id": "login_and_workspace", "state": "candidate", "scope": "fixture candidate"},
            {"id": "mcp_business_connections", "state": "disabled", "scope": "fixture default-closed capability"},
        ],
    }


def write(path: Path, content: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)


def temp_root() -> Path:
    base = ROOT / ".runtime/tmp"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=base, prefix="trial-release-test-"))


def upstream_sources() -> dict:
    return {
        "schema": "reviewed-upstream-sources/v1",
        "reviewed_on": "2026-11-12",
        "sources": [
            {
                "name": "DeepSeek Harness",
                "url": trial_release_manifest.HARNESS_REPO,
                "commit": PIN,
                "license": "MIT",
                "license_file_sha256": SPEC.harness_license_sha256,
                "node_version": NODE_VERSION,
                "pnpm_version": PNPM_VERSION,
                "runtime_installed": True,
            },
            {
                "name": "HippoRAG",
                "url": "https://github.com/OSU-NLP-Group/HippoRAG",
                "commit": "d5c8329422e0a0b834a15874545cb6a74b4f9b26",
                "license": "MIT",
                "license_file_sha256": SPEC.hipporag_license_sha256,
                "application_runtime_enabled": False,
            },
        ],
    }


def make_project(root: Path) -> Path:
    write(root / ".gitignore", ".runtime/\ndist/\n__pycache__/\n")

    # Required release inputs.
    write(root / "client/dist/index.html", "<!doctype html><script src=\"/assets/app.js\"></script>\n")
    write(root / "client/dist/assets/app.js", "console.log('fixture');\n")
    write(root / "client/package-lock.json", json.dumps({"lockfileVersion": 3}))
    write(root / "client/package.json", json.dumps({"name": "fixture-client"}))
    write(root / "client/index.html", "<!doctype html>\n")
    write(root / "client/src/app.ts", "export const fixture = true;\n")
    write(root / "server/app/main.py", "APP = 'fixture'\n")
    write(root / "examples/demo.txt", "fixture example\n")

    # Build copies and private state must never enter the package.
    write(root / "server/build/lib/app/main.py", "STALE = True\n")
    write(root / "server/build/lib/harness/profile.py", "STALE = True\n")
    write(root / "client/node_modules/pkg/index.js", "module.exports = 1;\n")
    write(root / "server/.pytest_cache/state", "cache\n")
    write(root / "server/app/leak.sqlite3", b"SQLite format 3\0private")
    write(root / "server/app/leak.log", "private log\n")
    write(root / "server/app/leak.pem", "-----BEGIN " + "PRIVATE KEY-----" + "\nfixture\n" + "-----END " + "PRIVATE KEY-----\n")

    for name in build_release.HANDOFF_DOCUMENTS:
        if name == "docs/upstream-sources.json":
            write(root / name, json.dumps(upstream_sources(), indent=2) + "\n")
        else:
            write(root / name, f"# fixture document {name}\n")
    for name in build_release.HANDOFF_SCRIPTS:
        write(root / name, "# fixture script\n")

    policy_content = f"# fixture document {trial_release_manifest.CAPABILITY_POLICY}\n"
    write(root / trial_release_manifest.CAPABILITY_POLICY, policy_content)
    write(root / "capabilities.json", json.dumps(
        capability_document(hashlib.sha256(policy_content.encode("utf-8")).hexdigest()), indent=2,
    ) + "\n")

    # Pinned license evidence lives in a private checkout but is read explicitly.
    write(root / ".runtime/third_party/deepseek-harness/LICENSE", HARNESS_LICENSE)
    write(root / ".runtime/third_party/HippoRAG/LICENSE", HIPPORAG_LICENSE)

    answers = b'{"fixture": "answers"}\n'
    write(root / "fixtures/synthetic-industrial-v1/answers.json", answers)
    write(root / "fixtures/synthetic-industrial-v1/manifest.json", json.dumps({
        "fixture_id": "synthetic-industrial-v1",
        "fixture_version": "1.0.0",
        "synthetic": True,
        "purpose": "development_and_acceptance_only",
        "license": "internal-test-fixture",
        "files": [{
            "path": "answers.json",
            "media_type": "application/json",
            "sha256": hashlib.sha256(answers).hexdigest(),
        }],
    }, indent=2) + "\n")
    write(root / "fixtures/industrial-public-v2/README.md", "# Public fixture metadata\n")
    write(root / "fixtures/industrial-public-v2/source-catalog.json", json.dumps({
        "corpus_id": "industrial-public-v2",
        "synthetic": False,
        "customer_data": False,
        "purpose": "local_development_visual_graph_and_acceptance_only",
        "source_policy": "Public sources with explicit license.",
        "local_assets": [{
            "asset_id": "public-example",
            "path": "images/example.png",
            "source_url": "https://example.org/public/example.png",
            "license": "CC0 1.0",
            "sha256": hashlib.sha256(b"public example").hexdigest(),
        }],
    }, indent=2) + "\n")
    write(root / "fixtures/industrial-public-v2/secret-customer.pdf", b"%PDF-customer-data")

    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Fixture"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "fixture"], check=True)
    return root


@pytest.fixture()
def project():
    root = make_project(temp_root())
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def manifest_of(package: Path) -> dict:
    return json.loads((package / "manifest.json").read_text(encoding="utf-8"))


def build(project: Path, name: str = "package", **kwargs) -> tuple[Path, dict]:
    destination = project / "dist" / name
    result = trial_release_manifest.build_trial_release(
        root=project, version="0.1.0", output=destination, spec=SPEC, **kwargs,
    )
    assert result["status"] == "built"
    return destination, manifest_of(destination)


def test_build_release_excludes_build_copies_and_caches_but_keeps_client_dist(project):
    groups = build_release.collect(root=project, license_inputs=SPEC.license_inputs)
    names = {name for entries in groups.values() for name, _ in entries}
    assert "client/dist/index.html" in names
    assert "client/dist/assets/app.js" in names
    assert "server/app/main.py" in names
    assert "server/build/lib/app/main.py" not in names
    assert not any("node_modules" in name for name in names)
    assert not any(".pytest_cache" in name for name in names)
    assert not any(name.endswith((".sqlite3", ".log", ".pem")) for name in names)
    assert "licenses/deepseek-harness-MIT.txt" in names
    assert "licenses/HippoRAG-MIT.txt" in names


def test_manifest_records_git_truth_per_file_digests_and_is_not_release_ready(project):
    head = subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()
    package, manifest = build(project)
    assert manifest["schema"] == trial_release_manifest.SCHEMA
    assert manifest["source_control"]["head"] == head
    assert manifest["source_control"]["dirty"] is False
    assert manifest["release"]["stage"] == "staging"
    assert manifest["release"]["status"] == "integrity-verified-staging"
    assert manifest["release"]["claim_scope"] == "integrity_verified_staging"
    assert manifest["release"]["release_ready"] is False
    assert manifest["release"]["publishable"] is False
    assert manifest["release"]["authorization_effect"] == "none"
    files = {item["path"]: item for item in manifest["files"]}
    assert files["server/app/main.py"]["sha256"] == hashlib.sha256(b"APP = 'fixture'\n").hexdigest()
    assert files["server/app/main.py"]["bytes"] == len(b"APP = 'fixture'\n")
    assert "server/build/lib/app/main.py" not in files
    assert manifest["source_tree_sha256"]
    assert trial_release_manifest.verify_trial_release(package, spec=SPEC)["status"] == "PASS"


def test_manifest_is_deterministic_and_source_changes_invalidate_the_tree_digest(project):
    first, first_manifest = build(project, "first")
    second, second_manifest = build(project, "second")
    assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()
    original_digest = first_manifest["source_tree_sha256"]
    write(project / "server/app/main.py", "APP = 'changed'\n")
    third, third_manifest = build(project, "third")
    assert third_manifest["source_tree_sha256"] != original_digest
    assert third_manifest["source_control"]["dirty"] is True
    assert third_manifest["source_control"]["dirty_entry_count"] >= 1
    assert any("server/app/main.py" in entry for entry in third_manifest["source_control"]["dirty_entries"])


def test_package_tampering_is_rejected(project):
    package, _ = build(project)
    write(package / "server/app/main.py", "APP = 'tampered'\n")
    report = trial_release_manifest.verify_trial_release(package, spec=SPEC)
    assert report["status"] == "FAIL"
    assert any("server/app/main.py" in reason for reason in report["reasons"])


def test_secret_marker_fails_closed(project):
    write(project / "server/app/leak.py", "KEY = 'sk-ant-" + "a" * 32 + "'\n")
    with pytest.raises(SystemExit, match="Potential secret marker"):
        build(project)


def test_missing_handoff_dependency_fails_closed(project):
    (project / "scripts/verify_harness_runtime.py").unlink()
    with pytest.raises(SystemExit, match="Required release input missing"):
        build(project)


def test_wrong_license_hash_fails_closed(project):
    bad = replace(
        SPEC,
        harness_license_sha256="0" * 64,
        license_inputs=(("licenses/deepseek-harness-MIT.txt", ".runtime/third_party/deepseek-harness/LICENSE", "0" * 64),
                        SPEC.license_inputs[1]),
    )
    with pytest.raises(SystemExit) as error:
        trial_release_manifest.build_trial_release(root=project, version="0.1.0", output=project / "dist" / "bad", spec=bad)
    assert "license" in str(error.value).lower()


def test_explicit_fixture_selection_and_provenance(project):
    _, manifest = build(project)
    paths = {item["path"] for item in manifest["fixtures"]}
    assert paths == set(CHOSEN_FIXTURES)
    assert all(item["customer_data"] is False for item in manifest["fixtures"])
    synthetic = next(item for item in manifest["fixtures"] if item["path"].endswith("answers.json"))
    assert synthetic["kind"] == "synthetic"
    assert synthetic["license"] == "internal-test-fixture"
    public = next(item for item in manifest["fixtures"] if item["path"].endswith("source-catalog.json"))
    assert public["kind"] == "public_metadata"
    assert public["referenced_sources"][0]["source_url"].startswith("https://")


def test_publishable_claim_is_always_rejected(project):
    package, _ = build(project)
    manifest = manifest_of(package)
    manifest["release"].update({
        "release_ready": True,
        "publishable": True,
        "stage": "final",
        "status": "final-candidate",
        "claim_scope": "publishable",
        "authorization_effect": "sufficient",
    })
    (package / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = trial_release_manifest.verify_trial_release(package, spec=SPEC)
    assert report["status"] == "FAIL"
    assert any("stage must be staging" in reason for reason in report["reasons"])
    assert any("release_ready/publishable must be false" in reason for reason in report["reasons"])
    assert any("claim_scope" in reason for reason in report["reasons"])
    assert any("authorization" in reason for reason in report["reasons"])


@pytest.mark.parametrize(
    ("claim_path", "claim_value", "reason_fragment"),
    (
        (("build_environment", "product_runtime_installed"), True, "product_runtime_installed"),
        (("install_contract", "product_runtime_verified"), True, "install_contract"),
        (("install_contract", "scope"), "full_product_runtime", "harness_handshake_only"),
        (("product_runtime_verified",), True, "top-level product_runtime_verified"),
        (("release", "product_runtime_verified"), True, "release.product_runtime_verified"),
        (("release", "product_runtime_installed"), True, "release.product_runtime_installed"),
        (("gate", "publishable_claim"), "pass", "publishable_claim"),
        (("gate", "acceptance_binding"), "pass", "acceptance_binding"),
    ),
)
def test_security_relevant_manifest_claims_cannot_be_upgraded(
    project, claim_path, claim_value, reason_fragment,
):
    package, _ = build(project)
    manifest = manifest_of(package)
    if claim_path[0] == "gate":
        gate = next(item for item in manifest["gates"] if item["name"] == claim_path[1])
        gate["status"] = claim_value
    else:
        node = manifest
        for part in claim_path[:-1]:
            node = node[part]
        node[claim_path[-1]] = claim_value
    (package / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    report = trial_release_manifest.verify_trial_release(package, spec=SPEC)
    assert report["status"] == "FAIL"
    assert any(reason_fragment in reason for reason in report["reasons"])


def test_gate_inventory_and_acceptance_boundary_are_verified(project):
    package, _ = build(project)
    manifest = manifest_of(package)
    duplicated = dict(manifest["gates"][0])
    manifest["gates"].append(duplicated)
    (package / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report = trial_release_manifest.verify_trial_release(package, spec=SPEC)
    assert report["status"] == "FAIL"
    assert any("duplicate gate names" in reason for reason in report["reasons"])


def test_fixture_acceptance_report_cannot_make_release_publishable(project):
    _, manifest = build(project)
    digest = manifest["source_tree_sha256"]
    (project / "acceptance.json").write_text(json.dumps({
        "schema": trial_release_manifest.ACCEPTANCE_SCHEMA,
        "source_tree_sha256": digest,
        "status": "PASS",
        "tests": [{"name": "fixture", "status": "PASS", "exit_code": 0}],
    }) + "\n", encoding="utf-8")

    staged, staged_manifest = build(
        project, "with-acceptance", acceptance_paths=("acceptance.json",), authorized_by="fixture L2",
    )
    assert staged_manifest["acceptance"]["bound"] is True
    assert staged_manifest["acceptance"]["effect"].startswith("advisory_only")
    assert staged_manifest["release"]["acceptance_evidence_recorded"] is True
    assert staged_manifest["release"]["release_ready"] is False
    assert staged_manifest["release"]["publishable"] is False
    assert staged_manifest["release"]["authorized_by"] == "fixture L2"
    assert staged_manifest["release"]["authorization_effect"] == "none"
    assert trial_release_manifest.verify_trial_release(staged, spec=SPEC)["status"] == "PASS"

    with pytest.raises(SystemExit, match="Final/publishable builds are disabled"):
        trial_release_manifest.build_trial_release(
            root=project, version="0.1.0", stage="final", output=project / "dist" / "final",
            spec=SPEC, acceptance_paths=("acceptance.json",), authorized_by="fixture L2",
        )

    write(project / "server/app/main.py", "APP = 'changed after acceptance'\n")
    with pytest.raises(SystemExit, match="not bound"):
        trial_release_manifest.build_trial_release(
            root=project, version="0.1.0", output=project / "dist" / "stale",
            spec=SPEC, acceptance_paths=("acceptance.json",),
        )


def test_capability_input_is_required_and_defaults_closed(project):
    with pytest.raises(SystemExit, match="Explicit capability candidate input is required"):
        trial_release_manifest.build_trial_release(
            root=project, version="0.1.0", output=project / "dist" / "no-capabilities",
            spec=replace(SPEC, capability_input=None),
        )
    _, manifest = build(project)
    capabilities = manifest["capabilities"]
    assert capabilities["schema"] == trial_release_manifest.CAPABILITY_SCHEMA
    assert capabilities["default_when_unlisted"] == "disabled"
    assert capabilities["release_open"] == []
    assert capabilities["runtime_enforcement_verified"] is False
    assert capabilities["runtime_state"] == "unverified"
    assert capabilities["candidate_count"] == 1
    assert capabilities["disabled_count"] == 1
    assert all(item["release_open"] is False for item in capabilities["candidates"])
    assert all(item["runtime_enforcement_verified"] is False for item in capabilities["candidates"])
    assert all(item["runtime_state"] == "unverified" for item in capabilities["candidates"])
    assert all("effective_runtime_state" not in item for item in capabilities["candidates"])
    assert capabilities["input_sha256"] == hashlib.sha256(
        (project / "capabilities.json").read_bytes()
    ).hexdigest()
    assert "trial/capabilities.json" in {item["path"] for item in manifest["files"]}


def test_capability_list_may_not_claim_enabled(project):
    document = capability_document(hashlib.sha256(
        (project / trial_release_manifest.CAPABILITY_POLICY).read_bytes()
    ).hexdigest())
    document["candidates"][0]["state"] = "enabled"
    write(project / "capabilities.json", json.dumps(document, indent=2) + "\n")
    with pytest.raises(SystemExit, match="may not claim enabled"):
        build(project, "enabled-claim")


def test_capability_policy_hash_mismatch_fails_closed(project):
    document = capability_document("0" * 64)
    write(project / "capabilities.json", json.dumps(document, indent=2) + "\n")
    with pytest.raises(SystemExit, match="policy_sha256 does not match"):
        build(project, "policy-mismatch")


def test_capability_tampering_is_rejected(project):
    package, _ = build(project)
    write(package / "trial/capabilities.json", json.dumps({
        "schema": trial_release_manifest.CAPABILITY_SCHEMA,
        "mode": "trial",
        "default_when_unlisted": "disabled",
        "policy_sha256": "0" * 64,
        "candidates": [{"id": "everything", "state": "candidate", "scope": "tampered"}],
    }, indent=2) + "\n")
    report = trial_release_manifest.verify_trial_release(package, spec=SPEC)
    assert report["status"] == "FAIL"
    assert any("capabilit" in reason or "policy" in reason for reason in report["reasons"])


def test_handshake_wheel_pins_fail_closed():
    root = temp_root()
    try:
        wheels = root / "wheels"
        wheels.mkdir()
        for _name, _version, wheel, _digest in trial_release_manifest.HANDSHAKE_PACKAGES:
            write(wheels / wheel, b"not the pinned wheel")
        with pytest.raises(trial_release_install.InstallFailure, match="checksum"):
            trial_release_install.verify_handshake_wheels(wheels)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize("target", [
    Path("/tmp/trial-install-outside-approved-root"),
    Path("/media/data3/dengkw-sibling/install"),
    Path("/media/data3/dengkw/../outside/install"),
])
def test_install_target_default_rejects_escape_from_approved_root(target, tmp_path):
    with pytest.raises(trial_release_install.InstallFailure, match="Install target must be under"):
        trial_release_install.validate_target(target, tmp_path / "package")


def test_install_target_rejects_symlink_escape_from_allowed_root(tmp_path):
    allowed = tmp_path / "approved"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (allowed / "alias").symlink_to(outside, target_is_directory=True)
    target = allowed / "alias" / "install"
    with pytest.raises(trial_release_install.InstallFailure, match="Install target must be under"):
        trial_release_install.validate_target(target, tmp_path / "package", allowed_root=allowed)
    assert not (outside / "install").exists()


def test_install_plan_is_dry_run_and_requires_empty_target(project, monkeypatch):
    original_validate = trial_release_install.validate_target
    monkeypatch.setattr(trial_release_install, "validate_target", lambda target, package:
                        original_validate(target, package, allowed_root=project.parent))
    package, manifest = build(project)
    expected = manifest["source_tree_sha256"]
    target = project.parent / f"install-target-{os.getpid()}"
    if target.exists():
        shutil.rmtree(target)
    plan = trial_release_install.plan_install(
        package, target, None, None, None, spec=SPEC, expected_source_tree_sha256=expected,
    )
    assert plan["status"] == "PLAN_ONLY"
    assert plan["target"]["empty"] is True
    assert plan["network_required"] is True
    assert plan["environment_switch_performed"] is False
    assert plan["install_scope"] == "harness_handshake_only"
    assert plan["product_runtime_verified"] is False
    assert plan["trusted_expected_digest_verified"] is True
    assert plan["target_verified_after_copy_before_execution"] is True
    assert all("==" in requirement for requirement in plan["handshake_environment"]["requirements"])
    assert len(plan["handshake_environment"]["wheel_sha256"]) == len(trial_release_manifest.HANDSHAKE_PACKAGES)
    with pytest.raises(trial_release_install.InstallFailure, match="trusted expected digest"):
        trial_release_install.plan_install(
            package, target, None, None, None, spec=SPEC,
            expected_source_tree_sha256="0" * 64,
        )
    with pytest.raises(trial_release_install.InstallFailure, match="expected-source-tree-sha256"):
        trial_release_install.execute_install(package, target, None, None, None, spec=SPEC)
    with pytest.raises(trial_release_install.InstallFailure, match="pnpm-store-expected-manifest-sha256"):
        trial_release_install.plan_install(
            package, target, None, None, Path("/nonexistent"), spec=SPEC,
            expected_source_tree_sha256=expected,
        )
    with pytest.raises(trial_release_install.InstallFailure, match="without --pnpm-store"):
        trial_release_install.plan_install(
            package, target, None, None, None, spec=SPEC,
            expected_source_tree_sha256=expected,
            pnpm_store_expected_manifest_sha256="0" * 64,
        )
    write(target / "occupied.txt", "not empty\n")
    with pytest.raises(trial_release_install.InstallFailure, match="not empty"):
        trial_release_install.plan_install(
            package, target, None, None, None, spec=SPEC, expected_source_tree_sha256=expected,
        )
    shutil.rmtree(target, ignore_errors=True)


def test_verify_rejects_extra_files_and_private_suffixes(project):
    package, _ = build(project)
    write(package / "server/app/unexpected.sqlite3", b"SQLite format 3\0")
    report = trial_release_manifest.verify_trial_release(package, spec=SPEC)
    assert report["status"] == "FAIL"
    assert any("unexpected" in reason or "suffix" in reason for reason in report["reasons"])


def test_capability_runtime_enforcement_claim_is_rejected(project):
    package, _ = build(project)
    manifest = manifest_of(package)
    manifest["capabilities"]["runtime_enforcement_verified"] = True
    manifest["capabilities"]["runtime_state"] = "disabled"
    manifest["capabilities"]["candidates"][0]["runtime_enforcement_verified"] = True
    manifest["capabilities"]["candidates"][0]["runtime_state"] = "disabled"
    (package / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = trial_release_manifest.verify_trial_release(package, spec=SPEC)
    assert report["status"] == "FAIL"
    assert any("runtime enforcement" in reason or "runtime_state" in reason for reason in report["reasons"])


def test_target_copy_is_verified_byte_for_byte_before_execution(project):
    package, manifest = build(project)
    target = temp_root() / "copied-target"
    try:
        target.mkdir()
        (target / ".runtime/tmp").mkdir(parents=True)
        trial_release_install.copy_package(package, target)
        result = trial_release_install.verify_target_tree(package, target, manifest["source_tree_sha256"])
        assert result["status"] == "PASS"
        assert result["target_source_tree_sha256"] == manifest["source_tree_sha256"]
        assert result["expected_source_tree_sha256"] == manifest["source_tree_sha256"]

        write(target / "server/app/extra.txt", "extra\n")
        with pytest.raises(trial_release_install.InstallFailure, match="extra files"):
            trial_release_install.verify_target_tree(package, target, manifest["source_tree_sha256"])
        (target / "server/app/extra.txt").unlink()

        write(target / "server/app/main.py", "APP = 'tampered copy'\n")
        with pytest.raises(trial_release_install.InstallFailure, match="file bytes"):
            trial_release_install.verify_target_tree(package, target, manifest["source_tree_sha256"])
        shutil.copy2(package / "server/app/main.py", target / "server/app/main.py")

        os.symlink("/etc/passwd", target / "server/app/link")
        with pytest.raises(trial_release_install.InstallFailure, match="symlink"):
            trial_release_install.verify_target_tree(package, target, manifest["source_tree_sha256"])
        (target / "server/app/link").unlink()

        with pytest.raises(trial_release_install.InstallFailure, match="trusted expected digest"):
            trial_release_install.verify_target_tree(package, target, "0" * 64)
    finally:
        shutil.rmtree(target, ignore_errors=True)


def _store_payload_path(version: Path, content: bytes, *, executable: bool = False,
                        algorithm: str = "sha512") -> Path:
    hasher = hashlib.sha512 if algorithm == "sha512" else hashlib.sha256
    digest = hasher(content).hexdigest()
    suffix = "-exec" if executable else ""
    return version / "files" / digest[:2] / (digest[2:] + suffix)


def make_pnpm_store(root: Path) -> Path:
    version = root / "v11"
    payload = b"public package content\n"
    write(_store_payload_path(version, payload), payload)
    executable_payload = b"public executable package content\n"
    write(_store_payload_path(version, executable_payload, executable=True), executable_payload)
    sha256_payload = b"public sha256 addressed content\n"
    write(_store_payload_path(version, sha256_payload, algorithm="sha256"), sha256_payload)
    write(version / "index.db", b"sqlite index fixture\n")
    registry = version / "projects"
    registry.mkdir(parents=True)
    os.symlink("../../third_party/deepseek-harness", registry / "project-link")
    return root


def test_pnpm_store_reuses_only_validated_content_payload():
    root = temp_root()
    try:
        store = make_pnpm_store(root / "store")
        state = trial_release_install.validate_pnpm_store(store)
        assert state is not None
        assert state["version"] == "v11"
        assert state["content_file_count"] == 3
        assert state["content_address_algorithms"] == {"sha256": 1, "sha512": 2}
        assert state["content_address_verified_files"] == 3
        assert state["manifest_pin_bound"] is False
        assert state["projects_registry_copied"] is False
        assert state["symlinks_in_payload"] == 0
        pinned = trial_release_install.validate_pnpm_store(
            store, expected_manifest_sha256=state["manifest_sha256"],
        )
        assert pinned["manifest_pin_bound"] is True
        assert pinned["manifest_match"] is True
        destination = root / "seed"
        copied = trial_release_install.copy_pnpm_store(pinned, destination)
        assert copied["copy_verified"] is True
        assert copied["manifest_sha256"] == state["manifest_sha256"]
        assert copied["expected_manifest_sha256"] == state["manifest_sha256"]
        assert _store_payload_path(destination / "v11", b"public package content\n").is_file()
        assert _store_payload_path(destination / "v11", b"public executable package content\n",
                                   executable=True).is_file()
        assert not (destination / "v11" / "projects").exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_pnpm_store_requires_a_pin_and_verifies_content_address():
    root = temp_root()
    try:
        store = make_pnpm_store(root / "store")
        unpinned = trial_release_install.validate_pnpm_store(store)
        with pytest.raises(trial_release_install.InstallFailure, match="bound expected manifest"):
            trial_release_install.copy_pnpm_store(unpinned, root / "seed")
        with pytest.raises(trial_release_install.InstallFailure, match="does not match the expected pin"):
            trial_release_install.validate_pnpm_store(store, expected_manifest_sha256="0" * 64)
        with pytest.raises(trial_release_install.InstallFailure, match="lowercase hex"):
            trial_release_install.validate_pnpm_store(store, expected_manifest_sha256="NOT-A-DIGEST")

        bad = root / "bad-store"
        version = bad / "v11"
        write(_store_payload_path(version, b"different bytes\n"), b"actual bytes\n")
        write(version / "index.db", b"index\n")
        with pytest.raises(trial_release_install.InstallFailure, match="does not match its content"):
            trial_release_install.validate_pnpm_store(bad)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_pnpm_store_rejects_symlinks_private_names_and_unexpected_root_entries():
    root = temp_root()
    try:
        store = make_pnpm_store(root / "store")
        version = store / "v11"
        link = _store_payload_path(version, b"symlink target content\n")
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink("../../outside", link)
        with pytest.raises(trial_release_install.InstallFailure, match="symlink"):
            trial_release_install.validate_pnpm_store(store)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    root = temp_root()
    try:
        store = make_pnpm_store(root / "store")
        write(store / "v11" / "files" / "ab" / "private.env", b"SECRET=1\n")
        with pytest.raises(trial_release_install.InstallFailure, match="non-content-addressed"):
            trial_release_install.validate_pnpm_store(store)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    root = temp_root()
    try:
        store = make_pnpm_store(root / "store")
        write(store / "customer.db", b"private\n")
        with pytest.raises(trial_release_install.InstallFailure, match="unexpected entries"):
            trial_release_install.validate_pnpm_store(store)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_staging_notice_disclaims_historical_final_documents_without_rewriting_them(project):
    historical = b"Release version: 0.4.2\nRelease readiness: FINAL\n"
    write(project / "docs/DELIVERY_VERIFICATION.md", historical)
    package, manifest = build(project)
    notice = (package / trial_release_manifest.STAGING_NOTICE).read_text(encoding="utf-8")
    for marker in trial_release_manifest.STAGING_NOTICE_MARKERS:
        assert marker in notice
    assert manifest["staging_notice"]["historical_final_documents"] == ["docs/DELIVERY_VERIFICATION.md"]
    assert manifest["staging_notice"]["historical_documents_modified"] is False
    assert (package / "docs/DELIVERY_VERIFICATION.md").read_bytes() == historical
    assert trial_release_manifest.verify_trial_release(package, spec=SPEC)["status"] == "PASS"


def test_staging_notice_tampering_is_rejected(project):
    package, _ = build(project)
    write(package / trial_release_manifest.STAGING_NOTICE, "# tampered notice\n")
    report = trial_release_manifest.verify_trial_release(package, spec=SPEC)
    assert report["status"] == "FAIL"
    assert any("staging notice" in reason for reason in report["reasons"])


def test_build_environment_records_license_only_private_runtime_access(project):
    _, manifest = build(project)
    environment = manifest["build_environment"]
    assert environment["private_runtime_access_scope"] == "license_text_only"
    assert environment["private_runtime_credentials_used"] is False
    assert environment["private_runtime_code_or_data_read"] is False
    assert environment["private_runtime_code_or_data_copied"] is False
    assert "private_runtime_used" not in environment
    assert environment["private_runtime_license_inputs"] == sorted(
        source for _archive, source, _expected in SPEC.license_inputs
    )


def test_contradictory_private_runtime_claim_is_rejected(project):
    package, _ = build(project)
    manifest = manifest_of(package)
    manifest["build_environment"]["private_runtime_credentials_used"] = True
    manifest["build_environment"]["private_runtime_used"] = True
    (package / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    report = trial_release_manifest.verify_trial_release(package, spec=SPEC)
    assert report["status"] == "FAIL"
    assert any("private_runtime" in reason for reason in report["reasons"])


def test_python_state_records_unresolved_interpreter_and_proves_isolation():
    root = temp_root()
    try:
        venv = root / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
        env = trial_release_install.clean_environment(root)
        for name in ("tmp", "home", "cache", "data", "config"):
            (root / ".runtime" / name).mkdir(parents=True, exist_ok=True)
        requested = str(venv / "bin/python")
        state = trial_release_install.python_state(
            requested, env, isolated=True, expected_prefix=str(venv),
        )
        assert state["interpreter"] == requested
        assert state["interpreter_resolved"] == str((venv / "bin/python").resolve())
        assert state["sys_prefix"] == str(venv)
        assert state["sys_base_prefix"] != state["sys_prefix"]
        assert state["isolation_verified"] is True
        assert state["pydantic_version"] is None
        with pytest.raises(trial_release_install.InstallFailure, match="not isolated"):
            trial_release_install.python_state(
                shutil.which("python3", path="/usr/local/bin:/usr/bin:/bin") or "/usr/bin/python3",
                env, isolated=True, expected_prefix=str(venv),
            )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_bytecode_is_disabled_and_post_execution_drift_is_detected(project):
    assert trial_release_install.clean_environment(project)["PYTHONDONTWRITEBYTECODE"] == "1"
    package, manifest = build(project)
    target = temp_root() / "bytecode-target"
    try:
        target.mkdir()
        (target / ".runtime/tmp").mkdir(parents=True)
        trial_release_install.copy_package(package, target)
        trial_release_install.verify_target_tree(package, target, manifest["source_tree_sha256"])
        clean = trial_release_install.compare_target_after_execution(
            package, target, manifest["source_tree_sha256"],
        )
        assert clean["unchanged"] is True
        assert clean["bytecode_files"] == []
        write(target / "server/app/__pycache__/main.cpython-310.pyc", b"bytecode drift")
        dirty = trial_release_install.compare_target_after_execution(
            package, target, manifest["source_tree_sha256"],
        )
        assert dirty["status"] == "FAIL"
        assert dirty["unchanged"] is False
        assert "server/app/__pycache__/main.cpython-310.pyc" in dirty["bytecode_files"]
    finally:
        shutil.rmtree(target, ignore_errors=True)
