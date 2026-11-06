"""Build deterministic client/server/handoff archives from reviewed source files.

Default output is staging. Use --stage final only after the verification document
records final release readiness. No credentials or live execution data are bundled.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
IGNORED = {"node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".git", "data", ".runtime", ".local", "internal", ".credentials", "credentials", "secrets", ".secrets", ".cache", ".mypy_cache", ".ruff_cache", "coverage"}
PRIVATE_NAMES = {"provider-settings.json", "settings.local.json", "admin-password", "password", "credentials.json", "credentials", "token", ".coverage", ".ds_store", "todo.md", "acceptance_todo.md"}
PRIVATE_SUFFIXES = {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".log", ".jsonl", ".pem", ".key", ".p12", ".pfx", ".tsbuildinfo"}
HANDOFF_SCRIPTS = ("scripts/build_release.py", "scripts/preview.py", "scripts/managed_preview.py", "scripts/smoke_api.py", "scripts/browser_smoke.mjs", "scripts/complete_client_browser.mjs", "scripts/benchmark.py", "scripts/lifecycle_smoke.py", "scripts/restart_safety_smoke.py", "scripts/native_context_smoke.py", "scripts/native_boundary_smoke.py", "scripts/local_browser_check.py")
HANDOFF_DOCUMENTS = ("README.md", "compose.yaml", ".env.example", "docs/API_CONTRACT.md", "docs/DELIVERY_VERIFICATION.md", "docs/OPERATIONS.md", "docs/WORKSPACE_GUIDE.md", "docs/COMPLETE_CLIENT_SCOPE.md", "docs/acceptance-tasks.example.json", "docs/verification/SESSION_LIFECYCLE.md", "docs/verification/RESTART_SAFETY.md", "docs/verification/NATIVE_CONTEXT_MEMORY.md", "docs/verification/DELIVERY_0_1_0.md")
REVIEWED_SCREENSHOTS = ("docs/screenshots/desktop-connections-20261105.png", "docs/screenshots/mobile-workspace-20261105.png", "docs/screenshots/mobile-chat-20261105.png", "docs/screenshots/desktop-real-model-20261105.png", "docs/screenshots/desktop-memory-0.2.0.png", "docs/screenshots/mobile-memory-0.2.0.png")
SECRET_MARKERS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bsk-(?:ant-[A-Za-z0-9_-]{24,}|[A-Za-z0-9]{32,})\b"),
    re.compile(rb"\b(?:ghp|github_pat)_[A-Za-z0-9_]{30,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
)
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
HANDOFF_SCRIPTS += ("scripts/local_client_host.cjs", "scripts/local_client_launcher.py", "scripts/sdk_effort_probe.py", "scripts/tokenhub_probe.py", "scripts/multimodel_smoke.py", "scripts/identity_cases.py", "scripts/identity_smoke.py")
HANDOFF_DOCUMENTS += ("docs/verification/DELIVERY_0_2_0.md", "docs/IDENTITY_VERIFICATION.md", "docs/IDENTITY_REVIEW_0_3.md")
HANDOFF_SCRIPTS += ("scripts/runtime_routing_probe.py",)
HANDOFF_SCRIPTS += ("scripts/prepare_knowledge_models.py", "scripts/bootstrap_workspace_04.py", "scripts/workspace_04_smoke.py")
HANDOFF_DOCUMENTS += ("docs/CONFIGURABLE_WORKSPACE.md", "docs/MCP_SOURCE_CONTRACT.md", "docs/KNOWLEDGE_AND_SOURCES.md", "docs/verification/DELIVERY_0_3_0.md")
REVIEWED_SCREENSHOTS += ("docs/screenshots/desktop-workspace-0.4.0.png",)
HANDOFF_DOCUMENTS += ("docs/verification/DELIVERY_0_4_0.md", "docs/verification/DELIVERY_0_4_1.md")
HANDOFF_SCRIPTS += ("scripts/session_isolation_smoke.py", "scripts/identity_selection_smoke.py")
REVIEWED_SCREENSHOTS += ("docs/screenshots/assistant-detail-0.4.1.png",)
REVIEWED_SCREENSHOTS += ("docs/screenshots/session-memory-0.4.2.png", "docs/screenshots/model-identity-0.4.2.png")

def permitted(path: Path) -> bool:
    """Exclude runtime state and private configuration even inside source roots."""
    relative = path.relative_to(ROOT)
    if path.is_symlink() or any(part.lower() in IGNORED or part.startswith('.test-') for part in relative.parts):
        return False
    name = path.name.lower()
    if name in PRIVATE_NAMES or name.startswith(".env") and name != ".env.example":
        return False
    if path.suffix.lower() in PRIVATE_SUFFIXES:
        return False
    if name.endswith(("-wal", "-shm", "-journal")) or name.startswith(("id_rsa", "id_ed25519")):
        return False
    return True

def files_under(folder: Path):
    for directory, subdirs, names in os.walk(folder, followlinks=False):
        subdirs[:] = sorted(name for name in subdirs if name.lower() not in IGNORED and not name.startswith('.test-') and not name.endswith(".egg-info") and not (Path(directory) / name).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            if path.is_file() and permitted(path):
                yield path

def reviewed_content(path: Path) -> bytes:
    if not permitted(path):
        raise SystemExit(f"Refusing a private or unsupported input: {path.relative_to(ROOT)}")
    content = path.read_bytes()
    if any(marker.search(content) for marker in SECRET_MARKERS):
        # Never print the matched credential or surrounding file contents.
        raise SystemExit(f"Potential secret marker found; inspect privately: {path.relative_to(ROOT)}")
    return content

def collect() -> dict[str, list[tuple[str, bytes]]]:
    for required in ("client/dist/index.html", "client/package-lock.json", "server/app/main.py", *HANDOFF_DOCUMENTS, *HANDOFF_SCRIPTS):
        if not (ROOT / required).is_file():
            raise SystemExit(f"Required release input missing: {required}")
    groups: dict[str, list[tuple[str, bytes]]] = {}
    groups["client"] = [(path.relative_to(ROOT).as_posix(), reviewed_content(path)) for path in files_under(ROOT / "client")]
    groups["server"] = [(path.relative_to(ROOT).as_posix(), reviewed_content(path)) for folder in (ROOT / "server", ROOT / "examples") for path in files_under(folder)]
    groups["deployment"] = [(name, reviewed_content(ROOT / name)) for name in (*HANDOFF_DOCUMENTS, *HANDOFF_SCRIPTS)]
    groups["deployment"].extend((name, reviewed_content(ROOT / name)) for name in REVIEWED_SCREENSHOTS if (ROOT / name).is_file())
    for entries in groups.values():
        entries.sort(key=lambda entry: entry[0])
        names = [name for name, _ in entries]
        if len(names) != len(set(names)):
            raise SystemExit("Duplicate archive member detected")
        for name in names:
            candidate = PurePosixPath(name)
            if candidate.is_absolute() or ".." in candidate.parts or "\\" in name:
                raise SystemExit("Unsafe archive member detected")
    return groups

def write_zip(path: Path, entries: list[tuple[str, bytes]]):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in entries:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content, compresslevel=9)
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None or archive.namelist() != [name for name, _ in entries]:
            raise SystemExit(f"ZIP verification failed: {path.name}")
        for name, expected in entries:
            if archive.read(name) != expected:
                raise SystemExit(f"ZIP member content mismatch: {name}")

def write_tar(path: Path, entries: list[tuple[str, bytes]]):
    with path.open("wb") as output, gzip.GzipFile(fileobj=output, filename="", mode="wb", mtime=0, compresslevel=9) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name, content in entries:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o644
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                archive.addfile(info, io.BytesIO(content))
    with tarfile.open(path, "r:gz") as archive:
        if archive.getnames() != [name for name, _ in entries]:
            raise SystemExit(f"TAR verification failed: {path.name}")
        for name, expected in entries:
            member = archive.getmember(name)
            stream = archive.extractfile(member)
            if not member.isfile() or stream is None or stream.read() != expected:
                raise SystemExit(f"TAR member content mismatch: {name}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="0.4.2")
    parser.add_argument("--stage", choices=("staging", "final"), default="staging")
    parser.add_argument("--check-only", action="store_true", help="Review selected inputs without writing archives")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.version):
        parser.error("Version must start with a letter or digit and contain only letters, digits, dots, dashes or underscores")
    groups = collect()
    verification = (ROOT / "docs/DELIVERY_VERIFICATION.md").read_text(encoding="utf-8")
    if args.stage == "final" and ("Release readiness: FINAL" not in verification or f"Release version: {args.version}\n" not in verification):
        raise SystemExit("Final release awaits a completed verification record for this exact version; build --stage staging meanwhile.")
    if args.check_only:
        print(json.dumps({"status": "inputs_checked", "stage": args.stage, "member_counts": {name: len(entries) for name, entries in groups.items()}, "selected_handoff_scripts": HANDOFF_SCRIPTS}, ensure_ascii=False, indent=2))
        return
    label = args.version if args.stage == "final" else args.version + "-staging"
    dest = ROOT / "dist/releases" / label
    if args.stage == "final" and dest.exists():
        raise SystemExit("Published final releases are immutable; select a new version or use staging.")
    dest.mkdir(parents=True, exist_ok=True)
    manifest = {"version": args.version, "release_status": args.stage, "verification_document": "docs/DELIVERY_VERIFICATION.md", "deterministic_archive_metadata": True, "checks": ["reviewed path selection", "private path exclusions", "high-confidence secret markers", "archive readback", "archive SHA-256"], "files": []}
    for kind, entries in groups.items():
        suffix = ".tar.gz" if kind == "server" else ".zip"
        path = dest / f"zhixing-{kind}-{label}{suffix}"
        (write_tar if kind == "server" else write_zip)(path, entries)
        manifest["files"].append({"name": path.name, "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "members": [name for name, _ in entries]})
    manifest_path = dest / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"release_status": args.stage, "directory": str(dest), "manifest": str(manifest_path), "files": [{key: value for key, value in item.items() if key != "members"} for item in manifest["files"]]}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
