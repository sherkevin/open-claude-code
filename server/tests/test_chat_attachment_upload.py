import json
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from test_api import FakeRuntime, PASSWORD, client, configured, login


MIB = 1024 * 1024
PATH = "/api/v1/files/binary"
HEADERS = {
    "Content-Type": "application/octet-stream",
    "X-Upload-Filename": quote("../../现场报告 100%.pdf", safe=""),
}


def test_chat_draft_upload_retry_reuses_original_across_transport(client, configured):
    query = "?draft=true&upload_id=draft-upload-test-id"
    first = client.post(PATH + query, content=b"document", headers=HEADERS)
    assert first.status_code == 201, first.text
    assert client.post(PATH + query, content=b"document", headers=HEADERS).json() == first.json()
    assert client.post("/api/v1/files" + query, files={"file": (first.json()["name"], b"document")}).json() == first.json()
    changed = client.post(PATH + query, content=b"changed!", headers=HEADERS)
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "UPLOAD_REQUEST_CHANGED"
    assert len(list((configured.data_dir / "files").iterdir())) == 1
    assert client.get(f"/api/v1/files/{first.json()['id']}/content").content == b"document"


def post_packets(client, packets, headers=None, *, signed_in=True):
    """Feed one ASGI packet at a time. The TestClient body adapter collects its iterator first."""
    inputs = iter(packets)
    messages = []
    request_headers = {**HEADERS, **(headers or {})}
    if signed_in:
        cookie = client.app.state.service.settings.session_cookie_name
        request_headers["Cookie"] = f"{cookie}={client.cookies[cookie]}"
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": PATH,
        "raw_path": PATH.encode(),
        "query_string": b"",
        "headers": [(key.lower().encode(), value.encode()) for key, value in request_headers.items()],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    async def receive():
        return next(inputs, {"type": "http.disconnect"})

    async def send(message):
        messages.append(message)

    client.portal.call(client.app, scope, receive, send)
    status = messages[0]["status"]
    response = json.loads(b"".join(message.get("body", b"") for message in messages))
    return status, response


def assert_no_upload(client, configured):
    assert not list((configured.data_dir / "files").iterdir())
    assert client.app.state.service.db.one("SELECT COUNT(*) n FROM files")["n"] == 0


@pytest.mark.parametrize("size", [20 * MIB + 1, 50 * MIB])
def test_chat_attachment_streams_to_disk_before_reading_entire_body(client, configured, size):
    config = client.get("/api/v1/config").json()
    assert config["chat_attachment_upload_max_bytes"] == 50 * MIB
    assert config["chat_attachment_limits"]["max_file_bytes"] == 50 * MIB
    assert config["chat_attachment_limits"]["max_total_bytes"] == 200 * MIB

    def packets():
        chunk = b"x" * MIB
        for offset in range(0, size, MIB):
            if offset:
                pending = list((configured.data_dir / "files").iterdir())
                assert len(pending) == 1 and pending[0].stat().st_size == offset
            yield {"type": "http.request", "body": chunk[:size - offset], "more_body": offset + MIB < size}

    status, body = post_packets(client, packets(), {"Content-Length": str(size)})
    assert status == 201
    assert body["size"] == size and body["name"] == "现场报告 100%.pdf"
    assert body["mime_type"] == "application/pdf"
    assert (configured.data_dir / "files" / body["id"]).stat().st_size == size
    assert client.get(f"/api/v1/files/{body['id']}").json() == body


def test_chat_attachment_rejects_declared_oversize_before_reading_body(client, configured):
    def unreadable():
        pytest.fail("oversized request body was read")
        yield

    status, body = post_packets(client, unreadable(), {"Content-Length": str(50 * MIB + 1)})
    assert status == 413 and body["error"]["code"] == "UPLOAD_TOO_LARGE"
    assert_no_upload(client, configured)


def test_chat_attachment_rejects_chunked_oversize_and_removes_partial_file(client, configured):
    configured.chat_attachment_upload_max_bytes = MIB
    config = client.get("/api/v1/config").json()
    assert config["chat_attachment_upload_max_bytes"] == MIB
    assert config["chat_attachment_limits"]["max_image_bytes"] == MIB
    packets = [
        {"type": "http.request", "body": b"x" * MIB, "more_body": True},
        {"type": "http.request", "body": b"x", "more_body": False},
    ]
    status, body = post_packets(client, packets, {"Transfer-Encoding": "chunked"})
    assert status == 413 and body["error"]["code"] == "UPLOAD_TOO_LARGE"
    assert_no_upload(client, configured)


def test_chat_attachment_removes_partial_file_on_disconnect(client, configured):
    packets = [
        {"type": "http.request", "body": b"x" * MIB, "more_body": True},
        {"type": "http.disconnect"},
    ]
    status, body = post_packets(client, packets)
    assert status == 400 and body["error"]["code"] == "UPLOAD_DISCONNECTED"
    assert_no_upload(client, configured)


def test_chat_attachment_checks_origin_and_auth_before_reading(client, configured):
    def unreadable():
        pytest.fail("untrusted request body was read")
        yield

    status, body = post_packets(client, unreadable(), signed_in=False)
    assert status == 401 and body["error"]["code"] == "UNAUTHENTICATED"
    status, body = post_packets(client, unreadable(), {"Origin": "https://denied.example"})
    assert status == 403 and body["error"]["code"] == "ORIGIN_DENIED"
    assert_no_upload(client, configured)


def test_chat_attachment_rejects_empty_body_and_invalid_headers(client, configured):
    empty = client.post(PATH, content=b"", headers=HEADERS)
    assert empty.status_code == 400 and empty.json()["error"]["code"] == "UPLOAD_EMPTY"
    invalid_name = client.post(PATH, content=b"x", headers={**HEADERS, "X-Upload-Filename": "%FF"})
    assert invalid_name.status_code == 400 and invalid_name.json()["error"]["code"] == "INVALID_UPLOAD_FILENAME"
    wrong_media = client.post(PATH, content=b"x", headers={**HEADERS, "Content-Type": "text/plain"})
    assert wrong_media.status_code == 415 and wrong_media.json()["error"]["code"] == "UNSUPPORTED_UPLOAD_TYPE"
    assert_no_upload(client, configured)


def test_chat_attachment_uses_existing_file_owner_and_cors_rules(client):
    preflight = client.options(PATH, headers={
        "Origin": "http://testserver",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type, x-upload-filename",
    })
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://testserver"
    uploaded = client.post(PATH, content=b"file contents", headers=HEADERS)
    assert uploaded.status_code == 201
    content_url = f"/api/v1/files/{uploaded.json()['id']}/content"
    assert client.get(content_url).content == b"file contents"
    created = client.post("/api/v1/users", json={"username": "member", "password": PASSWORD, "display_name": "员工"})
    assert created.status_code == 201
    login(client, "member")
    assert client.get(content_url).status_code == 404


def test_trial_does_not_advertise_or_allow_chat_attachment_upload(configured):
    configured.deployment_mode = "trial"
    app = create_app(configured, FakeRuntime)
    with TestClient(app) as client:
        config = client.get("/api/v1/config").json()
        assert "chat_attachment_upload_max_bytes" not in config
        assert "chat_attachment_limits" not in config
        status, body = post_packets(client, [], signed_in=False)
        assert status == 403 and body["error"]["code"] == "FEATURE_DISABLED"
        assert_no_upload(client, configured)


def test_document_preview_paginates_text_and_preserves_original(client):
    content = ("开" * 40_000 + "第二页").encode()
    uploaded = client.post("/api/v1/files", files={"file": ("长文档.txt", content, "text/plain")})
    assert uploaded.status_code == 201, uploaded.text
    route = f"/api/v1/files/{uploaded.json()['id']}"
    first = client.get(f"{route}/preview").json()
    second = client.get(f"{route}/preview", params={"page": 2}).json()
    assert first["total_pages"] == second["total_pages"] == 2
    assert first["text"] + second["text"] == content.decode()
    assert client.get(f"{route}/preview", params={"page": 3}).status_code == 404
    assert client.get(f"{route}/content").content == content


@pytest.mark.parametrize("suffix", ["docx", "xlsx"])
def test_document_preview_extracts_office_sections(client, suffix):
    if suffix == "docx":
        import io
        import zipfile

        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("word/document.xml", """
                <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                  <w:body><w:p><w:r><w:t>北区订单</w:t></w:r></w:p></w:body>
                </w:document>
            """.strip())
        content = stream.getvalue()
    else:
        from app.artifacts import xlsx_bytes

        content = xlsx_bytes([{"name": "北区", "rows": [["北区订单"]]}, {"name": "南区", "rows": [["南区订单"]]}])

    uploaded = client.post("/api/v1/files", files={"file": (f"订单.{suffix}", content)})
    assert uploaded.status_code == 201, uploaded.text
    route = f"/api/v1/files/{uploaded.json()['id']}/preview"
    first = client.get(route)
    assert first.status_code == 200, first.text
    assert "北区订单" in first.json()["text"]
    if suffix == "xlsx":
        second = client.get(route, params={"page": 2})
        assert second.status_code == 200, second.text
        assert "南区订单" in second.json()["text"]


@pytest.mark.parametrize("binary", [False, True])
def test_expired_composer_upload_is_removed_on_both_transports(client, binary):
    from app.draft_uploads import sweep_expired_draft_uploads

    if binary:
        upload = client.post(f"{PATH}?draft=true", content=b"draft", headers={**HEADERS, "X-Upload-Filename": "draft.txt"})
    else:
        upload = client.post("/api/v1/files?draft=true", files={"file": ("draft.txt", b"draft", "text/plain")})
    assert upload.status_code == 201, upload.text
    file_id = upload.json()["id"]
    service = client.app.state.service
    registration = service.db.execute("UPDATE chat_draft_expiry SET expires_at='2000-01-01' WHERE file_id=?", (file_id,))
    assert registration.rowcount == 1
    sweep_expired_draft_uploads(service)
    assert client.get(f"/api/v1/files/{file_id}").status_code == 404
    assert not (service.settings.data_dir / "files" / file_id).exists()
