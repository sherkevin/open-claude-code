"""Review or append a small, source-bound public knowledge package.

Default mode is entirely offline: no Service, database migration, authentication,
provider, or network client is started. --apply is an operator-only API import;
it never deletes documents, changes assistant bindings, or imports private files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from urllib.parse import urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "fixtures/knowledge-expansion-20261115"
OFFICIAL_HOSTS = {"www.pnnl.gov", "www.energy.gov", "www.itl.nist.gov", "csrc.nist.gov", "www.cyber.gov.au"}
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


class ImportProblem(Exception):
    """Safe public error; never include HTTP bodies or authentication values."""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def load_package(fixture: Path):
    fixture = fixture.resolve()
    manifest_path = fixture / "manifest.json"
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 262144:
        raise ImportProblem("Invalid manifest file")
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if (manifest.get("schema") != "knowledge-expansion/v1"
            or manifest.get("scope") != "public-reference-not-company-policy"
            or manifest.get("paid_model_calls") != 0
            or manifest.get("auto_bind_assistants") is not False):
        raise ImportProblem("Unsupported package scope")
    bases = manifest.get("knowledge_bases", [])
    documents = manifest.get("documents", [])
    if not 1 <= len(bases) <= 5 or not 1 <= len(documents) <= 50:
        raise ImportProblem("Package exceeds review bounds")
    ids, names = set(), set()
    for item in bases:
        if (not IDENTIFIER.fullmatch(item.get("id", "")) or item["id"] in ids
                or item.get("scope") != "team" or not isinstance(item.get("name"), str)
                or not 1 <= len(item["name"]) <= 100 or item["name"] in names
                or not isinstance(item.get("description"), str)):
            raise ImportProblem("Invalid or duplicate knowledge base")
        ids.add(item["id"])
        names.add(item["name"])
    paths, doc_ids, hashes, contents = set(), set(), set(), {}
    for item in documents:
        relative = PurePosixPath(item.get("path", ""))
        if (not IDENTIFIER.fullmatch(item.get("id", "")) or item["id"] in doc_ids
                or item.get("knowledge_base") not in ids or item.get("source_kind") != "official_public"
                or item.get("content_kind") != "independent_chinese_summary"
                or not isinstance(item.get("title"), str) or not 1 <= len(item["title"]) <= 100
                or any(c in item["title"] for c in '/\\\r\n\x00')
                or relative.is_absolute() or ".." in relative.parts or "\\" in str(relative)
                or relative.suffix != ".md" or relative.parts[:1] != ("documents",)
                or str(relative) in paths):
            raise ImportProblem("Invalid document identity, scope or path")
        path = fixture / relative
        if not path.resolve().is_relative_to(fixture) or any(p.is_symlink() for p in [path, *path.parents] if p != fixture.parent):
            raise ImportProblem("Document path must be an ordinary file inside the package")
        if not path.is_file() or not 100 <= path.stat().st_size <= 65536:
            raise ImportProblem("Document is missing or outside the size bounds")
        data = path.read_bytes()
        text = data.decode("utf-8")
        digest = sha(data)
        if digest != item.get("sha256") or len(data) != item.get("bytes") or digest in hashes:
            raise ImportProblem("Document hash/size mismatch or duplicate content")
        source = urlsplit(item.get("source_url", ""))
        if (source.scheme != "https" or source.hostname not in OFFICIAL_HOSTS or source.username
                or source.password or source.port not in (None, 443) or source.query or source.fragment
                or item["source_url"] not in text or "## 来源与版本" not in text):
            raise ImportProblem("Document requires a credential-free, reviewed official source")
        if not item.get("retrieval_terms") or not item.get("suggested_questions"):
            raise ImportProblem("Document lacks a retrieval acceptance case")
        paths.add(str(relative)); doc_ids.add(item["id"]); hashes.add(digest)
        contents[item["id"]] = data
    return manifest, contents, sha(manifest_bytes)


def build_plan(fixture: Path, inventory: dict | None = None):
    manifest, contents, digest = load_package(fixture)
    existing = inventory.get("knowledge_bases", []) if inventory else []
    known = {value for kb in existing for value in kb.get("content_sha256", [])}
    known_names = {kb["name"] for kb in existing}
    return {
        "schema": "knowledge-expansion-plan/v1", "corpus_id": manifest["corpus_id"],
        "manifest_sha256": digest, "mode": "offline_plan", "network_requests": 0,
        "production_changed": False, "paid_model_calls": 0, "auto_bind_assistants": False,
        "inventory_supplied": inventory is not None,
        "knowledge_bases": [{"id": kb["id"], "name": kb["name"],
                             "action": "reuse_if_compatible" if kb["name"] in known_names else "create"} for kb in manifest["knowledge_bases"]],
        "documents": [{"id": item["id"], "knowledge_base": item["knowledge_base"], "path": item["path"],
                       "sha256": item["sha256"], "source_url": item["source_url"],
                       "action": "already_present_review_target" if item["sha256"] in known else "append"} for item in manifest["documents"]],
        "total_document_bytes": sum(map(len, contents.values())),
        "note": "Plan is not an import receipt. Apply rechecks target hashes; no replacement, deletion, role changes or assistant binding.",
    }


def write_new(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        output.write(data)


def bundle_package(fixture: Path, destination: Path):
    manifest, contents, _ = load_package(fixture)
    entries = {"manifest.json": (fixture / "manifest.json").read_bytes(),
               **{d["path"]: contents[d["id"]] for d in manifest["documents"]}}
    readme = fixture / "README.md"
    if readme.is_symlink():
        raise ImportProblem("Bundle README must not be a symbolic link")
    if readme.is_file():
        if readme.stat().st_size > 65536:
            raise ImportProblem("Bundle README exceeds review bounds")
        entries["README.md"] = readme.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None or any(archive.read(n) != data for n, data in entries.items()):
            raise ImportProblem("Bundle readback failed")
    return {"file": destination.name, "bytes": destination.stat().st_size, "sha256": sha(destination.read_bytes())}


def apply_package(client, fixture: Path, expected_digest: str, journal: Path):
    """Append via the existing product API. Caller owns a logged-in client.

    No retry is performed after an uncertain POST. A durable intent is written
    before each mutation; rerun with a NEW journal re-discovers hashes first.
    """
    manifest, contents, digest = load_package(fixture)
    if digest != expected_digest:
        raise ImportProblem("Manifest differs from the reviewed plan")
    journal.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents overwriting an interrupted attempt's evidence.
    handle = journal.open("x", encoding="utf-8")
    os.chmod(journal, 0o600)

    def record(value):
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")
        handle.flush(); os.fsync(handle.fileno())

    def request(method, path, **kwargs):
        try:
            response = client.request(method, path, **kwargs)
        except Exception:
            raise ImportProblem("API transport outcome unknown; inspect journal and target before retrying") from None
        if not 200 <= response.status_code < 300:
            raise ImportProblem(f"API request failed (HTTP {response.status_code}); no automatic retry")
        try:
            return response.json()
        except Exception:
            raise ImportProblem("API response was not valid JSON; no automatic retry") from None

    try:
        user = request("GET", "auth/me")
        if user.get("role") != "admin" or user.get("enabled") is False:
            raise ImportProblem("Import requires an enabled administrator")
        existing = request("GET", "knowledge-bases")
        result = {"schema": "knowledge-expansion-receipt/v1", "corpus_id": manifest["corpus_id"],
                  "manifest_sha256": digest, "status": "running", "knowledge_bases": [], "documents": [],
                  "auto_bind_assistants": False, "paid_model_calls_requested_by_script": 0}
        record({"event": "begin", "manifest_sha256": digest})
        for kb in manifest["knowledge_bases"]:
            matches = [row for row in existing if row.get("name") == kb["name"]]
            if len(matches) > 1:
                raise ImportProblem("Ambiguous target knowledge-base name")
            target = matches[0] if matches else None
            if target and (target.get("scope") != "team" or not target.get("enabled")):
                raise ImportProblem("Existing knowledge base has incompatible visibility/status")
            if target is None:
                record({"event": "create_intent", "knowledge_base": kb["id"]})
                target = request("POST", "knowledge-bases", json={"name": kb["name"], "description": kb["description"],
                    "scope": "team", "enabled": True, "default_for_new_assistants": False})
                record({"event": "created", "knowledge_base": kb["id"], "target_id": target["id"]})
            documents = request("GET", f"knowledge-bases/{target['id']}/documents")
            result["knowledge_bases"].append({"id": kb["id"], "target_id": target["id"]})
            for item in (d for d in manifest["documents"] if d["knowledge_base"] == kb["id"]):
                matching = [d for d in documents if d.get("sha256") == item["sha256"]]
                if matching:
                    if not any(d.get("status") == "ready" for d in matching):
                        raise ImportProblem("Matching content exists but is not ready; inspect/reindex instead of duplicating")
                    outcome = {"id": item["id"], "status": "already_present", "sha256": item["sha256"]}
                else:
                    record({"event": "upload_intent", "id": item["id"], "target_id": target["id"], "sha256": item["sha256"]})
                    uploaded = request("POST", f"knowledge-bases/{target['id']}/documents", files={
                        "file": (item["title"] + ".md", contents[item["id"]], "text/markdown")})
                    if uploaded.get("sha256") != item["sha256"] or uploaded.get("status") != "ready":
                        raise ImportProblem("Uploaded document is not hash-matched and ready; inspect target, do not replay")
                    outcome = {"id": item["id"], "document_id": uploaded["id"], "status": "imported",
                               "sha256": item["sha256"], "chunks": uploaded.get("chunk_count")}
                    documents.append(uploaded)
                result["documents"].append(outcome)
                record({"event": "document_result", **outcome})
        result["status"] = "completed"
        record({"event": "completed", "documents": len(result["documents"])})
        return result
    except BaseException as exc:
        record({"event": "stopped", "error_type": type(exc).__name__, "automatic_retry": False})
        raise
    finally:
        handle.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--inventory", type=Path, help="Optional sanitized read-only inventory JSON")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--base-url", help="HTTPS product API base ending /api/v1/")
    parser.add_argument("--admin-user")
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--journal", type=Path)
    args = parser.parse_args(argv)
    if args.report.exists():
        parser.error("Report exists; use a new path to preserve previous evidence")
    if not args.apply:
        inventory = json.loads(args.inventory.read_text(encoding="utf-8-sig")) if args.inventory else None
        result = build_plan(args.fixture, inventory)
        if args.bundle:
            result["bundle"] = bundle_package(args.fixture, args.bundle)
    else:
        if not all([args.expected_manifest_sha256, args.base_url, args.admin_user, args.password_file, args.journal]):
            parser.error("Apply requires reviewed manifest SHA, HTTPS API, admin user, password file and a new journal")
        if args.bundle or args.inventory:
            parser.error("Apply does not generate bundles or use a stale offline inventory")
        # Verify local content and endpoint before reading authentication material.
        _, _, digest = load_package(args.fixture)
        if digest != args.expected_manifest_sha256:
            parser.error("Manifest differs from the reviewed plan")
        endpoint = urlsplit(args.base_url)
        if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment or not endpoint.path.rstrip("/").endswith("/api/v1")):
            parser.error("Use a credential-free HTTPS /api/v1/ endpoint")
        if args.journal.exists():
            parser.error("Journal exists; inspect its outcome before choosing a new attempt")
        if args.password_file.is_symlink() or not args.password_file.is_file():
            parser.error("Password file must be an ordinary local file")
        if os.name != "nt" and stat.S_IMODE(args.password_file.stat().st_mode) != 0o600:
            parser.error("Password file must have mode 0600")
        import httpx
        origin = f"{endpoint.scheme}://{endpoint.netloc}"
        with httpx.Client(base_url=args.base_url.rstrip("/") + "/", timeout=120,
                          trust_env=False, follow_redirects=False, headers={"Origin": origin}) as client:
            password = args.password_file.read_text(encoding="utf-8").strip()
            try:
                response = client.post("auth/login", json={"username": args.admin_user, "password": password})
            except Exception:
                raise ImportProblem("Authentication transport failed") from None
            finally:
                password = None
            if response.status_code != 200:
                raise ImportProblem("Administrator authentication failed")
            result = apply_package(client, args.fixture, args.expected_manifest_sha256, args.journal)
    write_new(args.report, encoded(result))
    print(json.dumps({"status": result.get("status", "planned"), "report": str(args.report),
                      "documents": len(result["documents"]), "production_import_requested": args.apply}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ImportProblem, OSError, ValueError, KeyError, TypeError):
        # Never print a credential, server response body, or private document.
        print("Knowledge expansion stopped; inspect the local package/plan and journal. No automatic retry.")
        raise SystemExit(1)
