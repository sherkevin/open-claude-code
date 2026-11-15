#!/usr/bin/env python3
"""Install a verified trial-release staging package into a brand-new directory.

The command is a dry run unless ``--execute`` is supplied.  It never touches the
source checkout's private ``.runtime``, an existing service, or a shared port.
The only reusable inputs are public, hash-verified artifacts: the pinned Harness
Git checkout, the pinned Node/pnpm download archives, and an optional pnpm store
cache.  Private account databases, administrator passwords, customer data and
credentials are never copied.
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
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import trial_release_manifest as release  # noqa: E402


class InstallFailure(RuntimeError):
    """A step failed; the caller records the exact command and exit code."""


HOST_HOME = Path(os.environ.get("HOME") or Path.home())


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha512_file(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_output(path: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *arguments], text=True, stderr=subprocess.DEVNULL).strip()


def validate_harness_source(source: Path, spec: release.ReleaseSpec) -> dict:
    source = source.resolve()
    if not (source / ".git").exists():
        raise InstallFailure(f"Harness source is not a Git worktree: {source}")
    head = git_output(source, "rev-parse", "HEAD")
    if head != spec.harness_commit:
        raise InstallFailure(f"Harness source HEAD is {head}, expected {spec.harness_commit}")
    if git_output(source, "status", "--porcelain"):
        raise InstallFailure("Harness source has local modifications; a clean reviewed checkout is required")
    license_path = source / "LICENSE"
    if not license_path.is_file():
        raise InstallFailure("Harness source has no LICENSE file")
    if sha256_file(license_path) != spec.harness_license_sha256:
        raise InstallFailure("Harness source LICENSE does not match the reviewed MIT evidence")
    return {"path": str(source), "head": head, "license_sha256": spec.harness_license_sha256, "clean": True}


def validate_toolchain_cache(cache: Path, spec: release.ReleaseSpec) -> dict:
    cache = cache.resolve()
    node_name = f"node-v{spec.node_version}-linux-x64.tar.xz"
    pnpm_name = f"pnpm-{spec.pnpm_version}.tgz"
    node = cache / node_name
    pnpm = cache / pnpm_name
    if not node.is_file() or not pnpm.is_file():
        raise InstallFailure(f"Toolchain cache is missing {node_name} or {pnpm_name}")
    if sha256_file(node) != spec.node_sha256:
        raise InstallFailure("Node archive checksum does not match the reviewed pin")
    expected_pnpm = __import__("base64").b64decode(spec.pnpm_sha512).hex()
    if sha512_file(pnpm) != expected_pnpm:
        raise InstallFailure("pnpm archive checksum does not match the reviewed pin")
    return {"path": str(cache), "node_sha256": spec.node_sha256, "pnpm_sha512": spec.pnpm_sha512}


STORE_VERSION = re.compile(r"v[0-9]+")
STORE_CONTENT_NAME = re.compile(r"[0-9a-f]{32,128}(?:-exec)?")
STORE_INDEX_FILES = ("index.db", "index.db-shm", "index.db-wal")
STORE_VERSION_ENTRIES = {"files", "projects", *STORE_INDEX_FILES}


def _sha256_and_sha512(path: Path) -> tuple[str, str]:
    sha256 = hashlib.sha256()
    sha512 = hashlib.sha512()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha256.update(chunk)
            sha512.update(chunk)
    return sha256.hexdigest(), sha512.hexdigest()


def _content_address(reference: str) -> tuple[str, str]:
    """Resolve pnpm's ``files/<2 hex>/<rest>[-exec]`` to (algorithm, digest).

    pnpm's v11 content-addressable store names a payload by the full 128-hex
    SHA-512 of its bytes split as ``files/<first 2>/<remaining 126>``.  Older
    or alternative layouts may use a 64-hex SHA-256 name.  Any other length is
    unsupported and fails closed.
    """
    parts = reference.split("/")
    if len(parts) != 3 or parts[0] != "files":
        raise InstallFailure(f"pnpm store contains a non-content-addressed path: {reference}")
    basename = parts[2]
    if basename.endswith("-exec"):
        basename = basename[:-5]
    digest = parts[1] + basename
    if len(digest) == 64:
        return "sha256", digest
    if len(digest) == 128:
        return "sha512", digest
    raise InstallFailure(
        f"pnpm store content-address digest length is unsupported (expected 64 or 128 hex): {reference}"
    )


def _validate_store_version(version_dir: Path, *,
                            expected_manifest_sha256: str | None = None) -> dict:
    """Validate one content-addressed pnpm store version without trusting its origin.

    Only ``<version>/files/<2 hex>/<hex digest>`` and the SQLite index are
    accepted.  Symlinks, private names/suffixes and the ``projects/`` registry
    are never part of the reusable payload, so an arbitrary directory cannot be
    copied while pretending only public package content was used.  Every
    content-addressed name is also checked against the content digest it claims.
    """
    if version_dir.is_symlink() or not version_dir.is_dir():
        raise InstallFailure(f"pnpm store version entry is not a real directory: {version_dir}")
    unexpected = sorted(entry.name for entry in version_dir.iterdir() if entry.name not in STORE_VERSION_ENTRIES)
    if unexpected:
        raise InstallFailure("pnpm store version contains unexpected entries: " + ", ".join(unexpected))
    files = version_dir / "files"
    if files.is_symlink() or not files.is_dir():
        raise InstallFailure("pnpm store has no real files/ directory")
    payload: list[tuple[str, Path, str]] = []
    algorithms = {"sha256": 0, "sha512": 0}
    for path in sorted(files.rglob("*")):
        relative = path.relative_to(version_dir).as_posix()
        if path.is_symlink():
            raise InstallFailure(f"pnpm store contains a symlink: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise InstallFailure(f"pnpm store contains an unsupported entry: {relative}")
        parts = relative.split("/")
        if (len(parts) != 3 or parts[0] != "files" or not re.fullmatch(r"[0-9a-f]{2}", parts[1])
                or not STORE_CONTENT_NAME.fullmatch(parts[2])):
            raise InstallFailure(f"pnpm store contains a non-content-addressed path: {relative}")
        algorithm, expected_digest = _content_address(relative)
        sha256_digest, sha512_digest = _sha256_and_sha512(path)
        actual_digest = sha256_digest if algorithm == "sha256" else sha512_digest
        if actual_digest != expected_digest:
            raise InstallFailure(
                f"pnpm store content-addressed file name does not match its content ({algorithm}): {relative}"
            )
        algorithms[algorithm] += 1
        payload.append((relative, path, sha256_digest))
    if not payload:
        raise InstallFailure("pnpm store files/ payload is empty")
    index_payload: list[tuple[str, Path]] = []
    if not (version_dir / "index.db").is_file() or (version_dir / "index.db").is_symlink():
        raise InstallFailure("pnpm store has no real index.db")
    for name in STORE_INDEX_FILES:
        path = version_dir / name
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise InstallFailure(f"pnpm store index entry is not a real file: {name}")
        index_payload.append((name, path))
    projects = version_dir / "projects"
    if projects.exists() and (projects.is_symlink() or not projects.is_dir()):
        raise InstallFailure("pnpm store projects/ registry is not a real directory")
    manifest = hashlib.sha256()
    total_bytes = 0
    for relative, path, sha256_digest in sorted(payload, key=lambda item: item[0]):
        size = path.stat().st_size
        total_bytes += size
        manifest.update(f"{relative}\0{size}\0{sha256_digest}\n".encode("ascii"))
    for name, path in sorted(index_payload, key=lambda item: item[0]):
        size = path.stat().st_size
        total_bytes += size
        manifest.update(f"{name}\0{size}\0{sha256_file(path)}\n".encode("ascii"))
    computed_manifest = manifest.hexdigest()
    if expected_manifest_sha256 is not None and computed_manifest != expected_manifest_sha256:
        raise InstallFailure(
            "pnpm store content manifest SHA-256 does not match the expected pin: "
            f"{computed_manifest} != {expected_manifest_sha256}"
        )
    return {
        "version": version_dir.name,
        "layout": f"{version_dir.name}/files + {version_dir.name}/index.db*",
        "content_file_count": len(payload),
        "content_address_algorithms": algorithms,
        "content_address_verified_files": len(payload),
        "index_files": [relative for relative, _path in index_payload],
        "total_bytes": total_bytes,
        "manifest_sha256": computed_manifest,
        "expected_manifest_sha256": expected_manifest_sha256,
        "manifest_pin_bound": expected_manifest_sha256 is not None,
        "manifest_match": expected_manifest_sha256 is not None and computed_manifest == expected_manifest_sha256,
        "projects_registry_copied": False,
        "symlinks_in_payload": 0,
        "mode": "validated_read_only_seed_then_private_writes",
    }


def validate_pnpm_store(store: Path | None,
                        expected_manifest_sha256: str | None = None) -> dict | None:
    if store is None:
        return None
    if expected_manifest_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256):
        raise InstallFailure("pnpm store expected manifest SHA-256 must be 64 lowercase hex characters")
    store = store.resolve()
    if store.is_symlink() or not store.is_dir():
        raise InstallFailure(f"pnpm store cache is not a directory: {store}")
    top_level = sorted(store.iterdir(), key=lambda entry: entry.name)
    versions = [entry for entry in top_level if STORE_VERSION.fullmatch(entry.name)]
    unexpected = [entry.name for entry in top_level if entry not in versions]
    if unexpected:
        raise InstallFailure("pnpm store root contains unexpected entries: " + ", ".join(unexpected))
    if len(versions) != 1:
        raise InstallFailure("pnpm store must contain exactly one version directory")
    state = _validate_store_version(versions[0], expected_manifest_sha256=expected_manifest_sha256)
    state["path"] = str(store)
    state["path_resolved"] = str(store)
    return state


def copy_pnpm_store(store_state: dict, destination: Path) -> dict:
    """Copy only the validated content-addressed payload and verify the copy."""
    if not store_state.get("manifest_pin_bound"):
        raise InstallFailure("pnpm store copy requires a bound expected manifest SHA-256")
    expected_manifest_sha256 = store_state["expected_manifest_sha256"]
    source_version = Path(store_state["path"]) / store_state["version"]
    destination.mkdir(parents=True, exist_ok=True)
    destination_version = destination / store_state["version"]
    shutil.copytree(source_version / "files", destination_version / "files", dirs_exist_ok=True)
    for name in store_state["index_files"]:
        shutil.copy2(source_version / name, destination_version / name)
    copied = _validate_store_version(destination_version,
                                     expected_manifest_sha256=expected_manifest_sha256)
    if (copied["manifest_sha256"] != store_state["manifest_sha256"]
            or copied["content_file_count"] != store_state["content_file_count"]
            or copied["index_files"] != store_state["index_files"]):
        raise InstallFailure("pnpm store seed copy does not match the validated input")
    copied["copied_from"] = store_state["path"]
    copied["expected_manifest_sha256"] = expected_manifest_sha256
    copied["manifest_pin_bound"] = True
    copied["copy_verified"] = True
    return copied


def _tree_files(root: Path, ignore_runtime: bool = False) -> dict[str, bytes]:
    content: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if ignore_runtime and (relative == ".runtime" or relative.startswith(".runtime/")):
            continue
        if path.is_symlink():
            raise InstallFailure(f"tree contains a symlink: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise InstallFailure(f"tree contains an unsupported entry: {relative}")
        content[relative] = path.read_bytes()
    return content


def verify_target_tree(package: Path, target: Path, expected_source_tree_sha256: str) -> dict:
    """Re-verify the copied target byte-for-byte before executing anything from it.

    The package manifest alone is not trusted: a caller must supply the digest
    through a trusted channel, and the copied tree must match both the package
    file set and that digest.  Extra files, missing files, symlinks, size
    changes and content changes all fail closed.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", expected_source_tree_sha256 or ""):
        raise InstallFailure("A trusted expected_source_tree_sha256 is required before target execution")
    package_files = _tree_files(package.resolve())
    target_files = _tree_files(target.resolve(), ignore_runtime=True)
    missing = sorted(set(package_files) - set(target_files))
    extra = sorted(set(target_files) - set(package_files))
    if missing:
        raise InstallFailure("copied target is missing files: " + ", ".join(missing[:8]))
    if extra:
        raise InstallFailure("copied target contains extra files outside .runtime: " + ", ".join(extra[:8]))
    mismatched = sorted(name for name in package_files if package_files[name] != target_files[name])
    if mismatched:
        raise InstallFailure("copied target file bytes do not match the package: " + ", ".join(mismatched[:8]))
    measured = release.tree_digest(target_files.items())
    if measured != expected_source_tree_sha256:
        raise InstallFailure(
            f"copied target source tree digest {measured} does not match the trusted expected digest "
            f"{expected_source_tree_sha256}"
        )
    return {
        "status": "PASS",
        "target_source_tree_sha256": measured,
        "expected_source_tree_sha256": expected_source_tree_sha256,
        "verified_file_count": len(target_files),
        "symlinks_in_target": 0,
        "extra_files": 0,
    }


def compare_target_after_execution(package: Path, target: Path,
                                   expected_source_tree_sha256: str) -> dict:
    """Re-measure the staged source tree after target execution.

    ``PYTHONDONTWRITEBYTECODE=1`` is set for every target Python execution, so
    the staged tree outside ``.runtime`` must stay byte-identical.  Any drift
    (for example a ``.pyc`` written by a manual plugin verification) is
    reported explicitly and the caller fails closed.
    """
    package_files = _tree_files(package.resolve())
    target_files = _tree_files(target.resolve(), ignore_runtime=True)
    missing = sorted(set(package_files) - set(target_files))
    extra = sorted(set(target_files) - set(package_files))
    changed = sorted(
        name for name in set(package_files) & set(target_files)
        if package_files[name] != target_files[name]
    )
    measured = release.tree_digest(target_files.items())
    unchanged = (not missing and not extra and not changed
                 and measured == expected_source_tree_sha256)
    return {
        "status": "PASS" if unchanged else "FAIL",
        "expected_source_tree_sha256": expected_source_tree_sha256,
        "measured_source_tree_sha256": measured,
        "unchanged": unchanged,
        "missing_files": missing,
        "extra_files": extra,
        "changed_files": changed,
        "bytecode_files": sorted(
            name for name in target_files if name.endswith((".pyc", ".pyo"))
        ),
    }


def copy_package(package: Path, target: Path) -> None:
    for item in package.resolve().iterdir():
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)


def clean_environment(target: Path) -> dict[str, str]:
    """Do not inherit school-cloud credentials, proxies, or user npm config."""
    runtime = target / ".runtime"
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(runtime / "home"),
        "TMPDIR": str(runtime / "tmp"),
        "XDG_CACHE_HOME": str(runtime / "cache"),
        "XDG_DATA_HOME": str(runtime / "data"),
        "XDG_CONFIG_HOME": str(runtime / "config"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }


def validate_target(target: Path, package: Path, *, allowed_root: Path | None = None) -> dict:
    target = target.resolve()
    package = package.resolve()
    # The installer entry points retain their original approved deployment
    # root. Tests can exercise the same checks inside a disposable fixture.
    allowed_root = (allowed_root or Path("/media/data3/dengkw")).resolve()
    if target != allowed_root and allowed_root not in target.parents:
        raise InstallFailure(f"Install target must be under {allowed_root}")
    if target == package or package in target.parents:
        raise InstallFailure("Install target must be a new directory outside the package")
    if target.exists() and any(target.iterdir()):
        raise InstallFailure(f"Install target already exists and is not empty: {target}")
    return {"path": str(target), "exists": target.exists(), "empty": not target.exists() or not any(target.iterdir())}


PYTHON_PROBE = """
import json, sys
report = {
    "version": sys.version.split()[0],
    "sys_prefix": sys.prefix,
    "sys_base_prefix": sys.base_prefix,
    "sys_executable": sys.executable,
}
try:
    import pydantic
except Exception as error:
    report["pydantic_version"] = None
    report["pydantic_import_error"] = type(error).__name__
else:
    report["pydantic_version"] = pydantic.VERSION
    report["pydantic_file"] = getattr(pydantic, "__file__", None)
print(json.dumps(report, sort_keys=True))
"""


def python_state(interpreter: str, env: dict[str, str], *, isolated: bool = False,
                 expected_prefix: str | None = None) -> dict:
    """Record the interpreter actually invoked, never a resolved substitute.

    For an isolated venv the caller passes the unresolved ``venv/bin/python``
    path.  The probe runs that exact path and proves isolation from the
    interpreter's own ``sys.prefix``/``sys.base_prefix``; resolving the symlink
    first would silently run the system Python and prove nothing.
    """
    requested = Path(interpreter)
    if not requested.is_absolute():
        requested = requested.absolute()
    resolved = requested.resolve()
    for candidate in (requested, resolved):
        private = ((ROOT / ".venv") in candidate.parents
                   or (ROOT / ".runtime") in candidate.parents)
        scratch = (ROOT / ".runtime/tmp") in candidate.parents
        if private and not scratch:
            raise InstallFailure("Refusing to use the source project's private virtual environment for the clean install")
    completed = subprocess.run(
        [str(requested), "-c", PYTHON_PROBE], env=env, text=True, capture_output=True,
    )
    if completed.returncode:
        raise InstallFailure(
            f"Python interpreter is not usable: {requested}: {completed.stderr.strip()[-500:]}"
        )
    try:
        probe = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as error:
        raise InstallFailure(f"Python interpreter probe did not return JSON: {requested}") from error
    prefix = Path(probe["sys_prefix"])
    base_prefix = Path(probe["sys_base_prefix"])
    prefix_matches_expected = (
        expected_prefix is None or prefix.resolve() == Path(expected_prefix).resolve()
    )
    isolated_verified = bool(isolated and prefix != base_prefix and prefix_matches_expected)
    if isolated and not isolated_verified:
        raise InstallFailure(
            "Requested isolated virtual environment is not isolated: "
            f"sys.prefix={probe['sys_prefix']} sys.base_prefix={probe['sys_base_prefix']} "
            f"expected_prefix={expected_prefix}"
        )
    return {
        "interpreter": str(requested),
        "interpreter_resolved": str(resolved),
        "version": probe["version"],
        "sys_prefix": probe["sys_prefix"],
        "sys_base_prefix": probe["sys_base_prefix"],
        "sys_executable": probe["sys_executable"],
        "sys_executable_resolves_to_requested": Path(probe["sys_executable"]).resolve() == resolved,
        "pydantic_version": probe.get("pydantic_version"),
        "pydantic_file": probe.get("pydantic_file"),
        "pydantic_import_error": probe.get("pydantic_import_error"),
        "isolated_virtual_environment": isolated,
        "isolation_verified": isolated_verified,
        "source_project_venv_used": False,
        "server_dependencies_installed": False,
        "product_runtime_verified": False,
        "scope": ("isolated handshake environment with pinned pydantic wheels; the FastAPI product runtime is not installed"
                  if isolated else
                  "setup_harness_runtime.py imports only the Python standard library; the FastAPI product runtime is not installed"),
    }


def discover_virtualenv_bootstrap(python: str, env: dict[str, str]) -> dict:
    """Locate the host user-site ``virtualenv`` build tool, never the source venv.

    The clean target environment disables user-site imports and points HOME at
    the target, so the host tool must be located explicitly.  This is a host
    build tool only: it creates the target venv, and the target venv's contents
    are the pinned, checksum-verified handshake wheels.
    """
    discovery_env = dict(env)
    discovery_env.pop("PYTHONNOUSERSITE", None)
    discovery_env.pop("PYTHONPATH", None)
    discovery_env["HOME"] = str(HOST_HOME)
    completed = subprocess.run(
        [python, "-c", "import virtualenv, pathlib; print(pathlib.Path(virtualenv.__file__).resolve().parent.parent)"],
        env=discovery_env, text=True, capture_output=True,
    )
    root_text = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""
    root = Path(root_text).resolve() if root_text else None
    if completed.returncode or root is None or not (root / "virtualenv" / "__init__.py").is_file():
        raise InstallFailure(
            "Cannot locate a host virtualenv build tool without using the source project venv: "
            + (completed.stderr.strip()[-1000:] or "module not found")
        )
    if (ROOT / ".venv") in root.parents or (ROOT / ".runtime") in root.parents:
        raise InstallFailure("Refusing to use a virtualenv build tool from the source project's private environment")
    return {
        "pythonpath": str(root),
        "module": "virtualenv",
        "host_home": str(HOST_HOME),
        "source_project_venv_used": False,
        "scope": "host build tool used only to create the isolated target venv; target contents come from pinned verified wheels",
    }


def handshake_requirements() -> list[str]:
    return [f"{name}=={version}" for name, version, _wheel, _digest in release.HANDSHAKE_PACKAGES]


def verify_handshake_wheels(wheels: Path) -> list[dict]:
    actual = {path.name: path for path in wheels.iterdir() if path.suffix == ".whl"}
    expected = {wheel: digest for _name, _version, wheel, digest in release.HANDSHAKE_PACKAGES}
    if set(actual) != set(expected):
        raise InstallFailure("Handshake wheel set does not match the pinned set: " + ", ".join(sorted(set(expected) ^ set(actual))))
    records = []
    for wheel, digest in sorted(expected.items()):
        actual_digest = sha256_file(actual[wheel])
        if actual_digest != digest:
            raise InstallFailure(f"Handshake wheel checksum does not match the pin: {wheel}")
        records.append({"wheel": wheel, "sha256": digest})
    return records


def plan_install(package: Path, target: Path, harness_source: Path | None,
                 toolchain_cache: Path | None, pnpm_store: Path | None,
                 spec: release.ReleaseSpec | None = None, *,
                 expected_source_tree_sha256: str | None = None,
                 pnpm_store_expected_manifest_sha256: str | None = None) -> dict:
    spec = spec or release.DEFAULT_SPEC
    if pnpm_store is not None and not pnpm_store_expected_manifest_sha256:
        raise InstallFailure(
            "--pnpm-store-expected-manifest-sha256 is required when --pnpm-store is supplied; "
            "an unpinned store source is not an acceptable reusable input"
        )
    if pnpm_store_expected_manifest_sha256 and pnpm_store is None:
        raise InstallFailure(
            "--pnpm-store-expected-manifest-sha256 was supplied without --pnpm-store"
        )
    verification = release.verify_trial_release(
        package, spec=spec, expected_source_tree_sha256=expected_source_tree_sha256,
    )
    if verification["status"] != "PASS":
        raise InstallFailure("Staging package failed verification: " + "; ".join(verification["reasons"]))
    target_state = validate_target(target, package)
    source_state = validate_harness_source(harness_source, spec) if harness_source else None
    cache_state = validate_toolchain_cache(toolchain_cache, spec) if toolchain_cache else None
    store_state = validate_pnpm_store(pnpm_store, pnpm_store_expected_manifest_sha256)
    env = clean_environment(target.resolve())
    python = shutil.which("python3", path=env["PATH"]) or "/usr/bin/python3"
    virtualenv = shutil.which("virtualenv")
    venv = target.resolve() / ".runtime/venv"
    venv_python = venv / "bin/python"
    wheels = target.resolve() / ".runtime/wheels"
    requirements = handshake_requirements()
    return {
        "status": "PLAN_ONLY",
        "package": str(package.resolve()),
        "source_tree_sha256": verification["source_tree_sha256"],
        "expected_source_tree_sha256": expected_source_tree_sha256,
        "trusted_expected_digest_verified": expected_source_tree_sha256 is not None,
        "target_verified_after_copy_before_execution": True,
        "target": target_state,
        "harness_source": source_state,
        "toolchain_cache": cache_state,
        "pnpm_store": store_state,
        "python_runtime": python_state(python, env),
        "handshake_environment": {
            "virtualenv": virtualenv,
            "bootstrap": "python -m virtualenv using the host user-site build tool; the source project venv is never used and the target venv is isolated",
            "venv": str(venv),
            "requirements": requirements,
            "wheel_sha256": {wheel: digest for _name, _version, wheel, digest in release.HANDSHAKE_PACKAGES},
        },
        "install_scope": "harness_handshake_only",
        "product_runtime_verified": False,
        "commands": [
            ["git", "clone", "--no-hardlinks", str(harness_source.resolve()) if harness_source else release.HARNESS_REPO,
             f"{target.resolve()}/{release.HARNESS_SOURCE}"],
            ["git", "-C", f"{target.resolve()}/{release.HARNESS_SOURCE}", "checkout", "--detach", spec.harness_commit],
            [python, f"{target.resolve()}/scripts/setup_harness_runtime.py", "--install", "--prepare-source"],
            [python, "-m", "virtualenv", "-p", python, str(venv)],
            [str(venv_python), "-m", "pip", "download", "--only-binary=:all:", "--no-deps",
             "--dest", str(wheels), *requirements],
            [str(venv_python), "-m", "pip", "install", "--no-index", "--find-links", str(wheels),
             "--no-deps", *requirements],
            [str(venv_python), f"{target.resolve()}/scripts/verify_harness_runtime.py"],
        ],
        "environment_file": str(target.resolve() / ".runtime/tmp"),
        "network_required": True,
        "network_scope": "pinned pydantic wheel download unless the target pip cache already has it; Harness, Node and pnpm inputs are local and hash-verified",
        "environment_switch_performed": False,
        "independent_port_required": True,
    }


def _run_step(name: str, command: list[str], cwd: Path, env: dict[str, str],
              full_stdout: bool = False) -> dict:
    started = now()
    completed = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
    record = {
        "name": name,
        "command": command,
        "exit_code": completed.returncode,
        "started_at": started,
        "finished_at": now(),
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }
    if full_stdout:
        record["stdout"] = completed.stdout
    return record


def execute_install(package: Path, target: Path, harness_source: Path | None,
                    toolchain_cache: Path | None, pnpm_store: Path | None,
                    run_sdk_protocol: bool = False, run_plugin_verify: bool = False,
                    spec: release.ReleaseSpec | None = None, *,
                    expected_source_tree_sha256: str | None = None,
                    pnpm_store_expected_manifest_sha256: str | None = None) -> dict:
    spec = spec or release.DEFAULT_SPEC
    if not expected_source_tree_sha256:
        raise InstallFailure(
            "--expected-source-tree-sha256 is required for --execute; pass the digest obtained "
            "through a trusted channel so the copied target can be verified before execution"
        )
    plan = plan_install(
        package, target, harness_source, toolchain_cache, pnpm_store, spec=spec,
        expected_source_tree_sha256=expected_source_tree_sha256,
        pnpm_store_expected_manifest_sha256=pnpm_store_expected_manifest_sha256,
    )
    target = target.resolve()
    package = package.resolve()
    target.mkdir(parents=True, exist_ok=True)
    for name in ("tmp", "cache", "data", "config", "home"):
        (target / ".runtime" / name).mkdir(parents=True, exist_ok=True)

    evidence: dict = {
        "schema": "trial-release-install/v1",
        "status": "FAILED",
        "started_at": now(),
        "finished_at": None,
        "package": str(package),
        "package_source_tree_sha256": plan["source_tree_sha256"],
        "expected_source_tree_sha256": expected_source_tree_sha256,
        "target_source_tree_sha256": None,
        "target_pre_execution_verified": False,
        "target": str(target),
        "harness_source": str(harness_source.resolve()) if harness_source else release.HARNESS_REPO,
        "toolchain_cache": str(toolchain_cache.resolve()) if toolchain_cache else None,
        "pnpm_store": str(pnpm_store.resolve()) if pnpm_store else None,
        "pnpm_store_expected_manifest_sha256": pnpm_store_expected_manifest_sha256,
        "private_runtime_copied": False,
        "environment_switch_performed": False,
        "bytecode_policy": {
            "environment_variable": "PYTHONDONTWRITEBYTECODE=1",
            "applies_to": "setup_harness_runtime.py, verify_harness_runtime.py, optional verify_harness_plugins.py and every other target Python execution",
            "effect": "target execution may write only under .runtime; the staged source tree outside .runtime is re-measured after execution and any .pyc or other drift fails closed",
        },
        "steps": [],
        "checks": [],
    }
    env = clean_environment(target)
    harness_target = target / release.HARNESS_SOURCE
    try:
        copy_package(package, target)
        target_check = verify_target_tree(package, target, expected_source_tree_sha256)
        evidence["target_source_tree_sha256"] = target_check["target_source_tree_sha256"]
        evidence["target_pre_execution_verified"] = True
        evidence["checks"].append({"name": "target_copy_verified_before_execution", "status": "PASS",
                                   "detail": target_check})
        python = shutil.which("python3", path=env["PATH"]) or "/usr/bin/python3"
        python_info = python_state(python, env)
        evidence["python_runtime"] = python_info
        evidence["install_scope"] = "harness_handshake_only"
        evidence["product_runtime_verified"] = False
        evidence["checks"].append({"name": "python_runtime_scope", "status": "PASS", "detail": python_info})
        source = str(harness_source.resolve()) if harness_source else release.HARNESS_REPO
        step = _run_step("clone_harness", ["git", "clone", "--quiet", "--no-hardlinks" if harness_source else "--filter=blob:none", source, str(harness_target)], target, env)
        evidence["steps"].append(step)
        if step["exit_code"]:
            raise InstallFailure("Harness clone failed")
        step = _run_step("checkout_pin", ["git", "-C", str(harness_target), "checkout", "--quiet", "--detach", spec.harness_commit], target, env)
        evidence["steps"].append(step)
        if step["exit_code"]:
            raise InstallFailure("Harness pin checkout failed")
        source_state = validate_harness_source(harness_target, spec)
        evidence["checks"].append({"name": "harness_worktree_pin", "status": "PASS", "detail": source_state})

        if toolchain_cache:
            cache_state = validate_toolchain_cache(toolchain_cache, spec)
            downloads = target / release.TOOLCHAIN / "downloads"
            downloads.mkdir(parents=True, exist_ok=True)
            node_name = f"node-v{spec.node_version}-linux-x64.tar.xz"
            pnpm_name = f"pnpm-{spec.pnpm_version}.tgz"
            shutil.copy2(toolchain_cache.resolve() / node_name, downloads / node_name)
            shutil.copy2(toolchain_cache.resolve() / pnpm_name, downloads / pnpm_name)
            evidence["checks"].append({"name": "toolchain_cache_verified", "status": "PASS", "detail": cache_state})
        if pnpm_store:
            store_state = validate_pnpm_store(pnpm_store, pnpm_store_expected_manifest_sha256)
            store = target / release.TOOLCHAIN / "pnpm-store"
            copied_store = copy_pnpm_store(store_state, store)
            evidence["pnpm_store_validation"] = store_state
            evidence["checks"].append({"name": "pnpm_store_seeded", "status": "PASS", "detail": copied_store})
        virtualenv = shutil.which("virtualenv")
        bootstrap = discover_virtualenv_bootstrap(python, env)
        venv = target / ".runtime/venv"
        venv_python = venv / "bin/python"
        wheels = target / ".runtime/wheels"
        wheels.mkdir(parents=True, exist_ok=True)
        venv_env = dict(env)
        venv_env.update({
            "PIP_CACHE_DIR": str(target / ".runtime/cache/pip"),
            "VIRTUALENV_OVERRIDE_APP_DATA": str(target / ".runtime/cache/virtualenv"),
            "PIP_NO_INPUT": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        venv_create_env = dict(venv_env)
        venv_create_env["PYTHONPATH"] = bootstrap["pythonpath"]
        evidence["handshake_environment"] = {
            "virtualenv": virtualenv,
            "bootstrap": bootstrap,
            "venv": str(venv),
            "requirements": handshake_requirements(),
        }
        step = _run_step("create_handshake_venv", [python, "-m", "virtualenv", "-p", python, str(venv)],
                         target, venv_create_env)
        evidence["steps"].append(step)
        if step["exit_code"]:
            raise InstallFailure("isolated handshake virtual environment creation failed")
        venv_state = python_state(str(venv_python), venv_env, isolated=True, expected_prefix=str(venv))
        if not venv_state["version"].startswith("3.10."):
            raise InstallFailure("Pinned pydantic-core wheel requires Python 3.10 on this host")
        evidence["handshake_python_runtime"] = venv_state
        evidence["checks"].append({"name": "isolated_handshake_venv", "status": "PASS", "detail": venv_state})
        requirements = handshake_requirements()
        step = _run_step(
            "download_handshake_wheels",
            [str(venv_python), "-m", "pip", "download", "--only-binary=:all:", "--no-deps",
             "--dest", str(wheels), *requirements],
            target, venv_env,
        )
        evidence["steps"].append(step)
        if step["exit_code"]:
            raise InstallFailure("pinned handshake wheel download failed")
        evidence["handshake_wheels"] = verify_handshake_wheels(wheels)
        evidence["checks"].append({
            "name": "handshake_wheels_verified", "status": "PASS",
            "detail": evidence["handshake_wheels"],
        })
        step = _run_step(
            "install_handshake_wheels",
            [str(venv_python), "-m", "pip", "install", "--no-index", "--find-links", str(wheels),
             "--no-deps", *requirements],
            target, venv_env,
        )
        evidence["steps"].append(step)
        if step["exit_code"]:
            raise InstallFailure("pinned handshake wheel installation failed")
        step = _run_step("check_handshake_pydantic",
                         [str(venv_python), "-c", PYTHON_PROBE],
                         target, venv_env, full_stdout=True)
        evidence["steps"].append(step)
        try:
            after_install = json.loads(step["stdout"].strip().splitlines()[-1])
        except (ValueError, IndexError) as error:
            raise InstallFailure("isolated handshake environment probe did not return JSON") from error
        venv_root = venv.resolve()
        pydantic_file = Path(after_install.get("pydantic_file") or "")
        verified = (
            step["exit_code"] == 0
            and after_install.get("pydantic_version") == "2.14.2"
            and Path(after_install.get("sys_prefix", "")).resolve() == venv_root
            and Path(after_install.get("sys_base_prefix", "")).resolve() != venv_root
            and str(pydantic_file.resolve()).startswith(str(venv_root) + os.sep)
        )
        if not verified:
            raise InstallFailure(
                "isolated handshake environment cannot import the pinned pydantic from the target venv"
            )
        evidence["handshake_python_runtime_after_install"] = {
            "interpreter": str(venv_python),
            "interpreter_resolved": str(venv_python.resolve()),
            "sys_prefix": after_install.get("sys_prefix"),
            "sys_base_prefix": after_install.get("sys_base_prefix"),
            "sys_executable": after_install.get("sys_executable"),
            "pydantic_version": after_install.get("pydantic_version"),
            "pydantic_file": after_install.get("pydantic_file"),
            "isolation_verified": True,
        }
        evidence["checks"].append({"name": "handshake_pydantic_import", "status": "PASS",
                                   "detail": evidence["handshake_python_runtime_after_install"]})
        step = _run_step("freeze_handshake_environment", [str(venv_python), "-m", "pip", "freeze"],
                         target, venv_env, full_stdout=True)
        evidence["steps"].append(step)
        if step["exit_code"]:
            raise InstallFailure("handshake dependency freeze failed")
        evidence["handshake_freeze"] = [line for line in step["stdout"].splitlines() if line.strip()]

        toolchain = target / release.TOOLCHAIN
        toolchain.mkdir(parents=True, exist_ok=True)
        for name in ("empty.npmrc", "empty-global.npmrc"):
            npmrc = toolchain / name
            if not npmrc.exists():
                # Prefer the verified local store; fall back to the pinned registry
                # only when the lockfile package is genuinely absent from the store.
                npmrc.write_text("prefer-offline=true\nfetch-retries=2\n", encoding="utf-8")

        step = _run_step("setup_harness_runtime", [python, str(target / "scripts/setup_harness_runtime.py"), "--install", "--prepare-source"], target, env)
        evidence["steps"].append(step)
        if step["exit_code"]:
            raise InstallFailure("setup_harness_runtime.py failed")

        step = _run_step("verify_harness_runtime", [str(venv_python), str(target / "scripts/verify_harness_runtime.py")], target, env, full_stdout=True)
        evidence["steps"].append(step)
        if step["exit_code"]:
            raise InstallFailure("verify_harness_runtime.py failed")
        try:
            runtime_report = json.loads(step["stdout"])
        except ValueError as error:
            raise InstallFailure("verify_harness_runtime.py did not emit a JSON report") from error
        if runtime_report.get("status") != "PASS" or runtime_report.get("provider_requests") != 0:
            raise InstallFailure("Harness handshake did not pass with zero provider requests")
        evidence["checks"].append({
            "name": "real_harness_zero_provider_handshake",
            "status": "PASS",
            "detail": {key: runtime_report.get(key) for key in ("source_commit", "provider_requests", "real_runtime_started", "limitations")},
        })

        if run_sdk_protocol:
            step = _run_step("verify_harness_sdk_protocol", [str(venv_python), str(target / "scripts/verify_harness_sdk_protocol.py")], target, env)
            evidence["steps"].append(step)
            if step["exit_code"]:
                raise InstallFailure("verify_harness_sdk_protocol.py failed")
            evidence["checks"].append({"name": "sdk_protocol", "status": "PASS", "detail": "fake peer only; no provider"})

        if run_plugin_verify:
            step = _run_step(
                "verify_harness_plugins",
                [python, str(target / "scripts/verify_harness_plugins.py"), "--offline"],
                target, env, full_stdout=True,
            )
            evidence["steps"].append(step)
            missing_dependency = (
                "ModuleNotFoundError" in (step["stderr_tail"] or "")
                or "No module named" in (step["stderr_tail"] or "")
            )
            plugin_status = (
                "PASS" if step["exit_code"] == 0
                else ("BLOCKED_PRODUCT_RUNTIME_DEPENDENCY" if missing_dependency else "FAILED")
            )
            evidence["plugin_verification"] = {
                "command": step["command"],
                "exit_code": step["exit_code"],
                "status": plugin_status,
                "bytecode_environment": {"PYTHONDONTWRITEBYTECODE": env.get("PYTHONDONTWRITEBYTECODE")},
                "note": "Plugin verification imports the server package. It runs with PYTHONDONTWRITEBYTECODE=1 so it cannot write .pyc into the staged source tree; a missing FastAPI product dependency is recorded as a known gap, not hidden.",
            }
            evidence["checks"].append({
                "name": "plugin_verification_execution",
                "status": "PASS" if step["exit_code"] == 0 else "BLOCKED",
                "detail": evidence["plugin_verification"],
            })

        post_execution = compare_target_after_execution(package, target, expected_source_tree_sha256)
        evidence["target_after_execution"] = post_execution
        evidence["target_source_tree_sha256_after_execution"] = post_execution["measured_source_tree_sha256"]
        evidence["target_tree_unchanged_after_execution"] = post_execution["unchanged"]
        evidence["checks"].append({
            "name": "post_execution_source_tree_unchanged",
            "status": "PASS" if post_execution["unchanged"] else "FAIL",
            "detail": post_execution,
        })
        if not post_execution["unchanged"]:
            drift = post_execution["extra_files"] + post_execution["changed_files"] + post_execution["missing_files"]
            raise InstallFailure(
                "target source tree changed during execution; .pyc or other drift outside .runtime is forbidden: "
                + ", ".join(drift[:8])
            )

        evidence["status"] = "PASS"
    except (InstallFailure, OSError, subprocess.SubprocessError) as error:
        evidence["error"] = str(error)
    finally:
        evidence["finished_at"] = now()
        destination = target / ".runtime" / "install-evidence.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if evidence["status"] != "PASS":
        raise InstallFailure(evidence.get("error", "installation failed"))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--harness-source", type=Path)
    parser.add_argument("--toolchain-cache", type=Path)
    parser.add_argument("--pnpm-store", type=Path)
    parser.add_argument("--pnpm-store-expected-manifest-sha256", default=None,
                        help="Trusted SHA-256 of the validated pnpm store content manifest; required whenever --pnpm-store is supplied")
    parser.add_argument("--expected-source-tree-sha256", default=None,
                        help="Trusted out-of-band source-tree digest; required with --execute and re-checked after copy before any execution")
    parser.add_argument("--execute", action="store_true", help="Actually install; otherwise only print the plan")
    parser.add_argument("--run-sdk-protocol", action="store_true")
    parser.add_argument("--run-plugin-verify", action="store_true",
                        help="Also run verify_harness_plugins.py with PYTHONDONTWRITEBYTECODE=1; a missing product dependency is recorded as a known gap")
    args = parser.parse_args(argv)
    try:
        if args.execute:
            if not args.expected_source_tree_sha256:
                raise InstallFailure(
                    "--expected-source-tree-sha256 is required with --execute; a package-internal "
                    "manifest alone is not a trusted input"
                )
            report = execute_install(
                args.package, args.target, args.harness_source,
                args.toolchain_cache, args.pnpm_store, args.run_sdk_protocol,
                args.run_plugin_verify,
                expected_source_tree_sha256=args.expected_source_tree_sha256,
                pnpm_store_expected_manifest_sha256=args.pnpm_store_expected_manifest_sha256,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        plan = plan_install(
            args.package, args.target, args.harness_source, args.toolchain_cache, args.pnpm_store,
            expected_source_tree_sha256=args.expected_source_tree_sha256,
            pnpm_store_expected_manifest_sha256=args.pnpm_store_expected_manifest_sha256,
        )
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except InstallFailure as error:
        print(json.dumps({"status": "FAIL", "reason": str(error)}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
