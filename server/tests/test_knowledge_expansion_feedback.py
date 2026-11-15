"""Public package/import checks; all writes are temporary, all HTTP is in-process."""
import importlib.util
import json
from pathlib import Path
import shutil
import zipfile

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("knowledge_expansion", ROOT / "scripts/expand_knowledge_feedback.py")
expansion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(expansion)
PACKAGE = ROOT / "fixtures/knowledge-expansion-20261115"


def copied_package(tmp_path):
    target = tmp_path / "package"
    shutil.copytree(PACKAGE, target)
    return target


def rewrite_manifest(package, change):
    manifest = json.loads((package / "manifest.json").read_bytes())
    change(manifest)
    (package / "manifest.json").write_bytes(expansion.encoded(manifest))


def test_offline_cli_never_constructs_network_client_or_reads_password(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline planning started an HTTP client")
    monkeypatch.setattr(httpx, "Client", forbidden)
    report, bundle = tmp_path / "plan.json", tmp_path / "package.zip"
    assert expansion.main(["--report", str(report), "--bundle", str(bundle),
                           "--password-file", str(tmp_path / "does-not-exist")]) == 0
    plan = json.loads(report.read_bytes())
    assert plan["network_requests"] == 0 and plan["production_changed"] is False
    assert len(plan["documents"]) == 8 and len(plan["knowledge_bases"]) == 3
    with zipfile.ZipFile(bundle) as archive:
        expected = {"manifest.json", "README.md", *(d["path"] for d in plan["documents"])}
        assert set(archive.namelist()) == expected
        assert archive.testzip() is None
    second = tmp_path / "package-second.zip"
    expansion.bundle_package(PACKAGE, second)
    assert second.read_bytes() == bundle.read_bytes()


@pytest.mark.parametrize("mutation", ["tamper", "traversal", "unreviewed-source", "duplicate"])
def test_package_rejects_changes_before_network(tmp_path, mutation):
    package = copied_package(tmp_path)
    if mutation == "tamper":
        path = package / "documents/maintenance-strategies.md"
        path.write_bytes(path.read_bytes() + b"changed")
    elif mutation == "traversal":
        rewrite_manifest(package, lambda m: m["documents"][0].update(path="documents/../../private.md"))
    elif mutation == "unreviewed-source":
        rewrite_manifest(package, lambda m: m["documents"][0].update(source_url="https://unreviewed.invalid/source"))
    else:
        rewrite_manifest(package, lambda m: m["documents"].append(dict(m["documents"][0])))
    with pytest.raises(expansion.ImportProblem):
        expansion.build_plan(package)


def test_wrong_reviewed_digest_makes_no_request_or_journal(tmp_path):
    class NoClient:
        def request(self, *args, **kwargs):
            pytest.fail("unreviewed package made a request")
    journal = tmp_path / "journal.jsonl"
    with pytest.raises(expansion.ImportProblem, match="reviewed plan"):
        expansion.apply_package(NoClient(), PACKAGE, "0" * 64, journal)
    assert not journal.exists()


@pytest.fixture
def isolated_product(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app

    def deny_network(*args, **kwargs):
        pytest.fail("knowledge package attempted external HTTP")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_network)
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    monkeypatch.setenv("AGENT_KNOWLEDGE_ALLOW_MODEL_DOWNLOAD", "false")
    settings = Settings(data_dir=tmp_path / "product", runtime_type="claude", provider_settings="",
                        model="test", model_catalog="", knowledge_vision_enabled=False,
                        deepseek_api_enabled=False, deepseek_api_ocr_enabled=False)
    app = create_app(settings)
    with TestClient(app, base_url="http://testserver/api/v1/") as client:
        service = app.state.service
        user = service.create_user({"username": "package-admin", "display_name": "Package test",
                                    "password": "test-only-password-123", "role": "admin"})
        assert client.post("auth/login", json={"username": user["username"],
                           "password": "test-only-password-123"}).status_code == 200
        yield client, service, user


def test_real_local_api_import_search_sources_and_idempotent_rerun(isolated_product, tmp_path):
    client, service, user = isolated_product
    manifest, _, digest = expansion.load_package(PACKAGE)
    receipt = expansion.apply_package(client, PACKAGE, digest, tmp_path / "first.jsonl")
    assert receipt["status"] == "completed"
    assert len(receipt["knowledge_bases"]) == 3
    assert all(d["status"] == "imported" and d["chunks"] > 0 for d in receipt["documents"])
    target_ids = {kb["id"]: kb["target_id"] for kb in receipt["knowledge_bases"]}
    imported = {d["id"]: d["document_id"] for d in receipt["documents"]}
    for item in manifest["documents"]:
        document = service.knowledge.document(imported[item["id"]], user)
        assert document["name"] == item["title"] + ".md"
        focused = service.knowledge.search([target_ids[item["knowledge_base"]]], user,
                    " ".join(item["retrieval_terms"]), mode="bm25", limit=3)["results"]
        assert focused[0]["document_id"] == imported[item["id"]]
        for query in item["suggested_questions"]:
            results = service.knowledge.search([target_ids[item["knowledge_base"]]], user,
                                               query, mode="bm25", limit=10)["results"]
            assert any(r["document_id"] == imported[item["id"]] for r in results), query
        chunks = service.knowledge.chunks(imported[item["id"]], user)
        assert any(item["source_url"] in c["text"] for c in chunks)
    assert all(not row["default_for_new_assistants"] for row in service.knowledge.list(user))
    assistant = service.save_assistant({"name": "Unbound check"}, user=user)
    assert assistant["knowledge_base_ids"] == []
    before = service.db.one("SELECT COUNT(*) n FROM knowledge_documents")["n"]
    receipt2 = expansion.apply_package(client, PACKAGE, digest, tmp_path / "second.jsonl")
    assert all(d["status"] == "already_present" for d in receipt2["documents"])
    assert service.db.one("SELECT COUNT(*) n FROM knowledge_documents")["n"] == before == 8
    assert service.db.one("SELECT COUNT(*) n FROM knowledge_bases")["n"] == 3
    assert '"upload_intent"' not in (tmp_path / "second.jsonl").read_text(encoding="utf-8")


def test_uncertain_upload_stops_without_retry_and_next_attempt_reconciles(isolated_product, tmp_path):
    client, service, _ = isolated_product
    _, _, digest = expansion.load_package(PACKAGE)
    class LostResponse:
        posts = 0
        def request(self, method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            if method == "POST" and path.endswith("/documents"):
                self.posts += 1
                assert response.status_code == 201
                raise TimeoutError("private response must never enter audit")
            return response
    uncertain = LostResponse()
    journal = tmp_path / "uncertain.jsonl"
    with pytest.raises(expansion.ImportProblem, match="outcome unknown"):
        expansion.apply_package(uncertain, PACKAGE, digest, journal)
    assert uncertain.posts == 1
    text = journal.read_text(encoding="utf-8")
    assert '"upload_intent"' in text and '"stopped"' in text
    assert "private response" not in text
    result = expansion.apply_package(client, PACKAGE, digest, tmp_path / "reconciled.jsonl")
    assert result["documents"][0]["status"] == "already_present"
    assert service.db.one("SELECT COUNT(*) n FROM knowledge_documents")["n"] == 8


def test_failed_existing_content_is_not_duplicated(isolated_product, tmp_path):
    client, service, user = isolated_product
    manifest, contents, digest = expansion.load_package(PACKAGE)
    kb = service.knowledge.save({**manifest["knowledge_bases"][0], "default_for_new_assistants": False}, user)
    first = manifest["documents"][0]
    doc = service.knowledge.upload(kb["id"], user, "existing.md", contents[first["id"]])
    service.db.execute("UPDATE knowledge_versions SET status='failed' WHERE document_id=?", (doc["id"],))
    with pytest.raises(expansion.ImportProblem, match="not ready"):
        expansion.apply_package(client, PACKAGE, digest, tmp_path / "failed-content.jsonl")
    assert service.db.one("SELECT COUNT(*) n FROM knowledge_documents")["n"] == 1


def test_non_admin_cannot_start_import(isolated_product, tmp_path):
    client, service, _ = isolated_product
    service.create_user({"username": "member", "display_name": "Member", "password": "test-member-password", "role": "member"})
    assert client.post("auth/login", json={"username": "member", "password": "test-member-password"}).status_code == 200
    _, _, digest = expansion.load_package(PACKAGE)
    with pytest.raises(expansion.ImportProblem, match="administrator"):
        expansion.apply_package(client, PACKAGE, digest, tmp_path / "member.jsonl")
    assert service.db.one("SELECT COUNT(*) n FROM knowledge_bases")["n"] == 0
