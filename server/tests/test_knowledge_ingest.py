"""OC-T4: unified, versioned and retryable knowledge ingestion.

These tests use only synthetic fixtures and public-source-shaped text. They
exercise the real parser, chunker, local lexical index, version tables and
job ledger; no external model or network call is made. OCR is replaced with a
deterministic local double because the shared development machine may not have
the optional RapidOCR weights installed.
"""
from __future__ import annotations

import hashlib
import io
import json
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from app import knowledge as knowledge_module
from app.config import Settings
from app.security import AppError
from app.service import Service


def _pdf(text: str) -> bytes:
    """Build a one-page PDF with real extractable Helvetica text."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _docx(text: str) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>",
        )
    return data.getvalue()


def _xlsx() -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>设备</t></si><si><t>压力</t></si></sst>",
        )
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="质量数据" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>0</v></c><c r="B2"><v>7.2</v></c></row>'
            "</sheetData></worksheet>",
        )
    return data.getvalue()


def _png() -> bytes:
    data = io.BytesIO()
    Image.new("RGB", (320, 240), "#6b8e23").save(data, format="PNG")
    return data.getvalue()


@pytest.fixture(scope="module")
def system(tmp_path_factory):
    # One isolated database is enough for the in-process cases. Individual
    # tests create their own knowledge bases and never share document IDs.
    root = tmp_path_factory.mktemp("knowledge-ingest")
    service = Service(Settings(data_dir=root, provider_settings="", model="test"))
    user = service.create_user({"username": "owner", "display_name": "Owner", "password": "test-password-123", "role": "member"})
    yield service, user
    service.knowledge.stop_worker()


def _upload(service, user, kb, name, data, document_id=None, wait=True):
    return service.knowledge.upload(kb["id"], user, name, data, document_id, wait=wait)


def _wait_for(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for ingestion condition")


FORMAT_CASES = [
    ("markdown", "guide.md", "# 泵站维护\n\n压力上限 7 bar。".encode("utf-8"), "base", "压力", ["text"]),
    ("text", "notes.txt", b"Pump P-101 pressure limit 7 bar.", "base", "P-101", ["text"]),
    ("csv", "batches.csv", "设备,压力\nP-101,7 bar\n".encode("utf-8"), "base", "P-101", ["text"]),
    ("json", "record.json", json.dumps({"equipment": "P-101", "pressure": "7 bar"}, ensure_ascii=False).encode(), "base", "P-101", ["text"]),
    ("docx", "manual.docx", _docx("Valve P-101 tolerance 0.02 mm."), "base", "P-101", ["docx-paragraphs"]),
    ("xlsx", "quality.xlsx", _xlsx(), "base", "7.2", ["xlsx-cells"]),
    ("pdf", "spec.pdf", _pdf("Pump P-101 pressure limit 7 bar."), "base", "P-101", ["pdf-text"]),
    ("png", "nameplate.png", _png(), "ocr", "P-101", ["local-ocr"]),
]


@pytest.mark.parametrize("kind,name,data,mode,query,extraction", FORMAT_CASES, ids=[item[0] for item in FORMAT_CASES])
def test_multi_format_ingestion_records_version_stages_and_evidence(system, monkeypatch, kind, name, data, mode, query, extraction):
    service, user = system
    kb = service.knowledge.save({"name": f"Multi-format {kind}"}, user)
    if mode == "ocr":
        monkeypatch.setattr(service.knowledge.parser, "ocr_image", lambda _path: "设备铭牌 P-101 压力 7 bar")
    document = _upload(service, user, kb, name, data)

    assert document["status"] == "ready", document
    assert document["version"] == 1
    assert document["sha256"] == hashlib.sha256(data).hexdigest()
    assert document["page_count"] >= 1
    assert document["chunk_count"] >= 1
    assert document["extraction"] == extraction
    assert document["last_ready_version"] == 1
    assert document["error_code"] is None
    job = document["ingestion"]
    assert job["status"] == "succeeded" and job["stage"] == "done"
    assert job["attempts"] == 1 and job["max_attempts"] == service.knowledge.retry_limit
    stages = [event["kind"] for event in job["events"]]
    assert {"stage:parse", "stage:chunk", "stage:embed", "stage:graph", "stage:done", "succeeded"} <= set(stages)
    chunks = service.knowledge.chunks(document["id"], user)
    assert chunks and any(query in chunk["text"] for chunk in chunks)
    result = service.knowledge.search([kb["id"]], user, query)
    assert result["results"] and result["results"][0]["version"] == 1
    if kind == "png":
        assert document["image"]["ocr_status"] == "ready"


def test_long_text_is_split_into_bounded_chunks_with_original_offsets(system):
    service, user = system
    kb = service.knowledge.save({"name": "Long text"}, user)
    text = "UNIQUE_MIDDLE_TOKEN 中间标记 " + ("A" * 6000) + " END_OF_SOURCE"
    document = _upload(service, user, kb, "long.txt", text.encode())
    assert document["status"] == "ready"
    chunks = service.knowledge.chunks(document["id"], user)
    assert len(chunks) >= 5
    assert all(0 < len(chunk["text"]) <= 1100 for chunk in chunks)
    assert all(text[chunk["start"]:chunk["end"]] == chunk["text"] for chunk in chunks)
    assert any("UNIQUE_MIDDLE_TOKEN" in chunk["text"] for chunk in chunks)
    assert any("END_OF_SOURCE" in chunk["text"] for chunk in chunks)
    assert service.knowledge.search([kb["id"]], user, "UNIQUE_MIDDLE_TOKEN")["results"]


def test_version_upgrade_keeps_previous_version_and_both_jobs(system):
    service, user = system
    kb = service.knowledge.save({"name": "Versioned"}, user)
    first = _upload(service, user, kb, "limits.md", "# 压力\n\n上限 7 bar。".encode())
    first_chunks = service.knowledge.chunks(first["id"], user)
    second = _upload(service, user, kb, "limits.md", "# 压力\n\n上限 9 bar。".encode(), first["id"])

    assert second["version"] == 2 and second["last_ready_version"] == 2
    assert second["ingestion"]["status"] == "succeeded"
    assert service.knowledge.chunks(first["id"], user, version=1) == first_chunks
    assert any("7 bar" in chunk["text"] for chunk in first_chunks)
    assert any("9 bar" in chunk["text"] for chunk in service.knowledge.chunks(first["id"], user))
    jobs = service.db.all("SELECT version,status,attempts FROM knowledge_ingest_jobs WHERE document_id=? ORDER BY version", (first["id"],))
    assert [(job["version"], job["status"], job["attempts"]) for job in jobs] == [(1, "succeeded", 1), (2, "succeeded", 1)]
    graph = service.knowledge.graph(kb["id"], user)
    assert graph["nodes"] and all(node["source"]["version"] == 2 for node in graph["nodes"])


def test_transient_parse_failure_is_retried_then_succeeds(system, monkeypatch):
    service, user = system
    kernel = service.knowledge
    kernel.retry_delay = 0
    original = kernel.parser.parse
    calls = {"count": 0}

    def flaky(path, name, check_cancel=None):
        calls["count"] += 1
        if calls["count"] < 3:
            raise AppError(503, "PARSER_UNAVAILABLE", "temporary parser outage")
        return original(path, name, check_cancel=check_cancel)

    monkeypatch.setattr(kernel.parser, "parse", flaky)
    kb = service.knowledge.save({"name": "Retry"}, user)
    document = _upload(service, user, kb, "retry.txt", b"retry eventually succeeds with 7 bar")

    assert document["status"] == "ready"
    job = document["ingestion"]
    assert job["status"] == "succeeded" and job["attempts"] == 3
    retries = [event for event in job["events"] if event["kind"] == "retry_scheduled"]
    assert len(retries) == 2
    assert service.knowledge.search([kb["id"]], user, "eventually")["results"]


def test_retry_is_bounded_and_failure_never_becomes_a_ready_index(system, monkeypatch):
    service, user = system
    kernel = service.knowledge
    kernel.retry_delay = 0
    calls = {"count": 0}

    def unavailable(*args, **kwargs):
        calls["count"] += 1
        raise AppError(503, "PARSER_UNAVAILABLE", "parser unavailable")

    monkeypatch.setattr(kernel.parser, "parse", unavailable)
    kb = service.knowledge.save({"name": "Bounded retry"}, user)
    document = _upload(service, user, kb, "never.txt", b"this content must never become ready")
    job = document["ingestion"]

    assert document["status"] == "failed"
    assert document["error_code"] == "PARSER_UNAVAILABLE"
    assert document["chunk_count"] == 0 and calls["count"] == 3
    assert job["status"] == "failed" and job["attempts"] == 3 and job["can_retry"]
    assert service.knowledge.search([kb["id"]], user, "never")["results"] == []
    assert service.db.one("SELECT COUNT(*) n FROM knowledge_chunks WHERE document_id=?", (document["id"],))["n"] == 0


def test_deterministic_invalid_document_fails_once(system, monkeypatch):
    service, user = system
    calls = {"count": 0}
    original = service.knowledge.parser.parse

    def counted(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(service.knowledge.parser, "parse", counted)
    kb = service.knowledge.save({"name": "Invalid"}, user)
    document = _upload(service, user, kb, "broken.pdf", b"%PDF not a document")

    assert document["status"] == "failed"
    assert document["error_code"] == "INVALID_DOCUMENT"
    assert document["ingestion"]["attempts"] == 1 and calls["count"] == 1
    assert document["ingestion"]["can_retry"]


def test_missing_ocr_is_visible_and_reindex_opens_the_index(system, monkeypatch):
    service, user = system
    kb = service.knowledge.save({"name": "OCR repair"}, user)

    def missing_ocr(_path):
        raise AppError(422, "OCR_REQUIRED", "local OCR is not installed")

    monkeypatch.setattr(service.knowledge.parser, "ocr_image", missing_ocr)
    first = _upload(service, user, kb, "nameplate.png", _png())

    assert first["status"] == "needs_ocr"
    assert first["error_code"] == "OCR_REQUIRED"
    assert first["chunk_count"] == 0
    assert first["ingestion"]["attempts"] == 1 and first["ingestion"]["can_retry"]
    assert first["image"]["ocr_status"] == "needs_ocr"
    assert service.knowledge.search([kb["id"]], user, "P-101")["results"] == []

    monkeypatch.setattr(service.knowledge.parser, "ocr_image", lambda _path: "设备铭牌 P-101 压力 7 bar")
    reopened = service.knowledge.reindex(first["id"], user)
    assert reopened["status"] == "ready" and reopened["chunk_count"] >= 1
    assert reopened["last_ready_version"] == 1
    assert reopened["image"]["ocr_status"] == "ready"
    assert reopened["ingestion"]["status"] == "succeeded"
    jobs = service.db.all("SELECT status FROM knowledge_ingest_jobs WHERE document_id=? ORDER BY rowid", (first["id"],))
    assert [job["status"] for job in jobs] == ["failed", "succeeded"]
    assert service.knowledge.search([kb["id"]], user, "P-101")["results"]


def test_queued_job_can_be_cancelled_then_reindexed(system):
    service, user = system
    kb = service.knowledge.save({"name": "Cancel queued"}, user)
    document = _upload(service, user, kb, "queued.txt", b"queued content should not be indexed", wait=False)

    assert document["status"] == "processing"
    assert document["ingestion"]["status"] == "queued" and document["ingestion"]["can_cancel"]
    cancelled = service.knowledge.cancel(document["id"], user)
    assert cancelled["status"] == "cancelled"
    assert cancelled["ingestion"]["status"] == "cancelled" and not cancelled["ingestion"]["can_cancel"]
    assert service.knowledge.chunks(document["id"], user) == []
    assert service.knowledge.search([kb["id"]], user, "queued")["results"] == []

    service.knowledge.process_pending()
    still_cancelled = service.knowledge.ingestion_view(document["id"], None, user)["job"]
    assert still_cancelled["status"] == "cancelled"
    reopened = service.knowledge.reindex(document["id"], user)
    assert reopened["status"] == "ready" and reopened["ingestion"]["status"] == "succeeded"
    assert service.knowledge.search([kb["id"]], user, "queued")["results"]


def test_running_job_cancels_between_pages(system, monkeypatch):
    service, user = system
    kernel = service.knowledge
    kb = service.knowledge.save({"name": "Cancel running"}, user)
    started = threading.Event()

    def blocking_parse(path, name, check_cancel=None):
        started.set()
        for _ in range(2000):
            if check_cancel:
                check_cancel()
            time.sleep(0.005)
        raise AssertionError("cancellation was not observed")

    monkeypatch.setattr(kernel.parser, "parse", blocking_parse)
    kernel.start_worker()
    try:
        document = _upload(service, user, kb, "slow.txt", b"slow content", wait=False)
        assert started.wait(8)
        service.knowledge.cancel(document["id"], user)
        _wait_for(lambda: service.db.one(
            "SELECT status FROM knowledge_versions WHERE document_id=? AND version=1", (document["id"],)
        )["status"] == "cancelled")
        row = service.db.one("SELECT status FROM knowledge_versions WHERE document_id=? AND version=1", (document["id"],))
        assert row["status"] == "cancelled"
    finally:
        kernel.stop_worker()
    assert service.knowledge.chunks(document["id"], user) == []


def test_interrupted_job_is_recovered_without_losing_attempt_budget(system):
    service, user = system
    kb = service.knowledge.save({"name": "Resume"}, user)
    document = _upload(service, user, kb, "resume.txt", b"resume after a process restart", wait=False)
    job = document["ingestion"]
    assert job["status"] == "queued"

    expired = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    service.db.execute(
        "UPDATE knowledge_ingest_jobs SET status='running',attempts=1,lease_owner='dead-process',lease_expires_at=? WHERE job_id=?",
        (expired, job["job_id"]),
    )
    service.knowledge.recover()
    recovered = service.knowledge.ingestion_view(document["id"], None, user)["job"]
    assert recovered["status"] == "queued"
    assert any(event["kind"] == "requeued" for event in recovered["events"])
    assert service.knowledge.process_pending() >= 1
    resumed = service.knowledge.version(document["id"], None, user)
    assert resumed["status"] == "ready"
    final = service.knowledge.ingestion_view(document["id"], None, user)["job"]
    assert final["status"] == "succeeded" and final["attempts"] == 2


def test_interrupted_job_with_exhausted_budget_fails_explicitly(system):
    service, user = system
    kb = service.knowledge.save({"name": "Exhausted lease"}, user)
    document = _upload(service, user, kb, "stuck.txt", b"stuck content", wait=False)
    job = document["ingestion"]
    expired = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    service.db.execute(
        "UPDATE knowledge_ingest_jobs SET status='running',attempts=max_attempts,lease_owner='dead-process',lease_expires_at=? WHERE job_id=?",
        (expired, job["job_id"]),
    )
    service.knowledge.recover()
    failed = service.knowledge.ingestion_view(document["id"], None, user)["job"]
    assert failed["status"] == "failed" and failed["error_code"] == "LEASE_EXPIRED"
    assert service.knowledge.version(document["id"], None, user)["status"] == "failed"


def test_over_limit_text_is_rejected_without_a_partial_index(system):
    service, user = system
    kb = service.knowledge.save({"name": "Limits"}, user)
    document = _upload(service, user, kb, "oversize.txt", b"x" * 2_000_001)

    assert document["status"] == "failed"
    assert document["error_code"] == "DOCUMENT_TOO_LARGE"
    assert document["chunk_count"] == 0
    assert document["ingestion"]["attempts"] == 1
    assert service.db.one("SELECT COUNT(*) n FROM knowledge_chunks WHERE document_id=?", (document["id"],))["n"] == 0
    assert service.knowledge.search([kb["id"]], user, "x")["results"] == []
    limits = service.knowledge.ingestion_limits()
    assert limits["max_text_characters"] == 2_000_000
    assert "明确拒绝" in limits["behavior"]


def test_chunk_limit_is_rejected_without_a_partial_index(system, monkeypatch):
    service, user = system
    kb = service.knowledge.save({"name": "Chunk limit"}, user)
    monkeypatch.setattr(knowledge_module, "MAX_CHUNKS", 2)
    document = _upload(service, user, kb, "many-chunks.txt", b"A" * 3000)

    assert document["status"] == "failed"
    assert document["error_code"] == "DOCUMENT_TOO_LARGE"
    assert document["ingestion"]["attempts"] == 1 and document["ingestion"]["can_retry"]
    assert document["chunk_count"] == 0
    assert service.knowledge.chunks(document["id"], user) == []


def test_upload_byte_limit_is_rejected_before_a_version_is_created(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    service = Service(Settings(data_dir=tmp_path, provider_settings="", model="test", upload_max_bytes=1024))
    try:
        user = service.create_user({"username": "owner", "display_name": "Owner", "password": "test-password-123", "role": "member"})
        kb = service.knowledge.save({"name": "Small limit"}, user)
        with pytest.raises(AppError) as error:
            service.knowledge.upload(kb["id"], user, "large.txt", b"y" * 2048)
        assert error.value.code == "DOCUMENT_TOO_LARGE" and error.value.status == 413
        assert service.db.one("SELECT COUNT(*) n FROM knowledge_documents")["n"] == 0
    finally:
        service.knowledge.stop_worker()


def test_api_exposes_ingestion_stage_cancel_and_reindex(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    monkeypatch.setenv("AGENT_KNOWLEDGE_INGEST_RETRY_SECONDS", "0")
    from fastapi.testclient import TestClient
    from app.main import create_app

    app = create_app(Settings(data_dir=tmp_path / "api-data", provider_settings="", model="test"))
    with TestClient(app) as client:
        service = app.state.service
        service.create_user({"username": "owner", "display_name": "Owner", "password": "test-password-123", "role": "admin"})
        assert client.post("/api/v1/auth/login", json={"username": "owner", "password": "test-password-123"}).status_code == 200
        kb = client.post("/api/v1/knowledge-bases", json={"name": "API ingestion"}).json()

        ready = client.post(
            f"/api/v1/knowledge-bases/{kb['id']}/documents?background=true",
            files={"file": ("ready.md", "# 泵站\n\n压力上限 7 bar。".encode(), "text/markdown")},
        )
        assert ready.status_code == 201, ready.text
        document = ready.json()
        assert document["ingestion"] is not None
        _wait_for(lambda: client.get(f"/api/v1/knowledge-documents/{document['id']}/ingestion").json()["job"]["status"] == "succeeded")
        detail = client.get(f"/api/v1/knowledge-documents/{document['id']}/ingestion").json()
        assert detail["limits"]["max_attempts"] == service.knowledge.retry_limit
        assert detail["job"]["stage_label"] == "索引完成"
        assert detail["job"]["events"]

        started = threading.Event()
        original_parse = service.knowledge.parser.parse

        def blocking_parse(path, name, check_cancel=None):
            started.set()
            for _ in range(2000):
                if check_cancel:
                    check_cancel()
                time.sleep(0.005)
            return []

        monkeypatch.setattr(service.knowledge.parser, "parse", blocking_parse)
        slow = client.post(
            f"/api/v1/knowledge-bases/{kb['id']}/documents?background=true",
            files={"file": ("slow.txt", b"slow cancellation fixture", "text/plain")},
        )
        assert slow.status_code == 201, slow.text
        slow_document = slow.json()
        assert started.wait(8)
        cancelled = client.post(f"/api/v1/knowledge-documents/{slow_document['id']}/cancel").json()
        # Cancellation is cooperative: the endpoint records the request and
        # the worker acknowledges it between pages.
        assert cancelled["ingestion"]["cancel_requested"] is True
        _wait_for(lambda: client.get(
            f"/api/v1/knowledge-documents/{slow_document['id']}/ingestion"
        ).json()["job"]["status"] == "cancelled")

        service.knowledge.parser.parse = original_parse
        reopened = client.post(f"/api/v1/knowledge-documents/{slow_document['id']}/reindex").json()
        assert reopened["status"] == "ready"
        assert reopened["last_ready_version"] == 1
        listed = client.get(f"/api/v1/knowledge-bases/{kb['id']}/documents").json()
        assert all("ingestion" in item and "error_code" in item for item in listed)


