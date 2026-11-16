"""Real upload/SQLite authorization with synthetic bytes; no model calls."""
import asyncio
import base64
import hashlib
import io
import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.config import Settings
from app.db import now, uid
from app.harness_attachments import AttachmentPrompt, MAX_IMAGE_BYTES, MAX_TEXT_BYTES, preflight
from app.security import AppError
from app.service import Service


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    service = Service(Settings(data_dir=tmp_path, runtime_type="harness", model="test", provider_settings="", model_catalog="", knowledge_vision_enabled=False))
    service.catalog.models[service.catalog.default_model_id]["capabilities"] = ["multimodal"]
    user = service.create_user({"username": "attachment", "display_name": "Test", "password": "safe-test-password-123", "role": "admin"})
    assistant = service.save_assistant({"name": "Attachment test", "connections": []}, user=user)
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "test"}, user)
    runtime = SimpleNamespace(service=service, conversation_id=conversation["id"], interrupted=False)

    def upload(data=b"Measured count: 42\nVerified", name="facts.txt", mime="text/plain", **fields):
        file_id = uid()
        path = tmp_path / "files" / file_id
        path.write_bytes(data)
        row = {"id": file_id, "user_id": user["id"], "name": name, "mime_type": mime, "size": len(data), "path": str(path), "created_at": now(), "conversation_id": None, **fields}
        service.db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?)", tuple(row.values()))
        return row

    def prepare(files, text="read"):
        response, _ = service.prepare_run(conversation["id"], {"text": text, "file_ids": [row["id"] for row in files], "client_request_id": uid()}, user)
        run = service.require("runs", response["run_id"])
        message = service.require("messages", run["message_id"])
        return run, message

    yield SimpleNamespace(service=service, user=user, assistant=assistant, runtime=runtime, upload=upload, prepare=prepare)
    service.db.close()


def client():
    calls = []
    return SimpleNamespace(session_prompt=lambda *args: calls.append(args) or "receipt"), calls


def png():
    stream = io.BytesIO()
    Image.new("RGB", (20, 10), "green").save(stream, format="PNG")
    return stream.getvalue()


def test_text_and_image_are_inline_snapshots_with_private_path_absent(system):
    rows = [system.upload(), system.upload(png(), "diagram.png", "image/png")]
    run, message = system.prepare(rows)
    prompt = AttachmentPrompt(system.runtime, run, message)
    peer, calls = client()
    assert prompt.dispatch(peer, "owned", "compare") == "receipt"
    payload = calls[0][1]
    assert "Measured count: 42" in json.dumps(payload)
    image = next(block for block in payload if block["type"] == "image")
    assert base64.b64decode(image["data"]) == png()
    assert image["mimeType"] == "image/png"
    assert str(system.service.settings.data_dir) not in json.dumps(payload)
    recorded = json.loads(system.service.require("runs", run["id"])["execution_meta"])["harness_attachments"]
    assert recorded[0]["sha256"] == hashlib.sha256(Path(rows[0]["path"]).read_bytes()).hexdigest()
    assert set(recorded[0]) == {"id", "binding", "sha256"}


def test_mobile_webp_with_jpg_filename_is_normalized_without_changing_evidence(system):
    stream = io.BytesIO()
    Image.new("RGB", (652, 5149), "green").save(stream, format="WEBP")
    original = stream.getvalue()
    row = system.upload(original, "mobile-export.jpg", "image/jpeg")
    run, message = system.prepare([row])
    prompt = AttachmentPrompt(system.runtime, run, message)
    peer, calls = client()
    prompt.dispatch(peer, "owned", "read")
    block = next(item for item in calls[0][1] if item["type"] == "image")
    with Image.open(io.BytesIO(base64.b64decode(block["data"]))) as image:
        assert image.format == "JPEG" and image.size == (652, 5149)
        image.load()
    assert block["mimeType"] == "image/jpeg"
    assert Path(row["path"]).read_bytes() == original
    assert prompt.snapshots[0]["sha256"] == hashlib.sha256(original).hexdigest()


def test_mobile_normalization_still_rejects_animated_images():
    from app.harness_attachments import inline_image
    stream = io.BytesIO()
    Image.new("RGB", (20, 10), "green").save(stream, format="GIF", save_all=True,
        append_images=[Image.new("RGB", (20, 10), "red")], duration=100)
    with pytest.raises(AppError) as error:
        inline_image(stream.getvalue(), "image/jpeg")
    assert error.value.code == "ATTACHMENT_INVALID"


@pytest.mark.parametrize("case,code", [("owner", "NOT_FOUND"), ("conversation", "ATTACHMENT_SCOPE"), ("unsupported", "ATTACHMENT_UNSUPPORTED"), ("duplicate", "ATTACHMENT_LIMIT"), ("too_many", "ATTACHMENT_LIMIT"), ("image_mime", "ATTACHMENT_INVALID"), ("image_large", "ATTACHMENT_LIMIT")])
def test_metadata_rejection_precedes_run_creation(system, case, code):
    row = system.upload()
    rows = [row]
    updates = {
        "owner": ("user_id", "someone-else"), "conversation": ("conversation_id", "other-conversation"),
        "unsupported": ("name", "archive.zip"), "image_mime": ("name", "fake.png"),
        "image_large": ("size", MAX_IMAGE_BYTES + 1),
    }
    if case in updates:
        if case == "image_large":
            system.service.db.execute("UPDATE files SET name='large.png',mime_type='image/png' WHERE id=?", (row["id"],))
        key, value = updates[case]
        system.service.db.execute(f"UPDATE files SET {key}=? WHERE id=?", (value, row["id"]))
    elif case == "duplicate":
        rows *= 2
    else:
        rows = [system.upload() for _ in range(11)]
    with pytest.raises(AppError) as error:
        system.prepare(rows)
    assert error.value.code == code
    assert system.service.db.one("SELECT COUNT(*) n FROM runs")["n"] == 0


@pytest.mark.parametrize("case", ["outside", "symlink", "parent_symlink", "hardlink", "fifo", "size", "binary", "false_image", "animated", "empty", "too_much_text"])
def test_bytes_fail_closed_before_any_model_dispatch(system, tmp_path, case):
    row = system.upload()
    path = Path(row["path"])
    if case == "outside":
        path = tmp_path / "private.txt"
        path.write_bytes(b"private")
        system.service.db.execute("UPDATE files SET path=?,size=7 WHERE id=?", (str(path), row["id"]))
    elif case in {"symlink", "hardlink", "fifo"}:
        path.unlink()
        target = tmp_path / "secret"
        target.write_text("private")
        if case == "symlink":
            path.symlink_to(target)
        elif case == "hardlink":
            os.link(target, path)
        else:
            os.mkfifo(path)
    elif case == "parent_symlink":
        parent = path.parent
        moved = parent.with_name("moved-files")
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
    elif case == "size":
        path.write_text("changed-size")
    elif case == "binary":
        path.write_bytes(b"binary\x00")
    elif case == "false_image":
        system.service.db.execute("UPDATE files SET name='fake.png',mime_type='image/png' WHERE id=?", (row["id"],))
    elif case == "animated":
        stream = io.BytesIO()
        Image.new("RGB", (10, 10), "red").save(stream, format="GIF", save_all=True, append_images=[Image.new("RGB", (10, 10), "blue")])
        path.write_bytes(stream.getvalue())
        system.service.db.execute("UPDATE files SET name='animated.gif',mime_type='image/gif' WHERE id=?", (row["id"],))
    elif case == "empty":
        path.write_bytes(b" \n ")
    else:
        path.write_bytes(b"a" * (MAX_TEXT_BYTES + 1))
    if case in {"binary", "animated", "empty", "too_much_text"}:
        system.service.db.execute("UPDATE files SET size=? WHERE id=?", (path.stat().st_size, row["id"]))
    run, message = system.prepare([row])
    with pytest.raises(AppError):
        AttachmentPrompt(system.runtime, run, message)


@pytest.mark.parametrize("mutation", ["owner", "delete", "scope", "bytes", "user_disabled", "assistant_disabled", "archived", "active_run"])
def test_revoke_between_parse_and_dispatch_never_reaches_peer(system, mutation):
    row = system.upload()
    run, message = system.prepare([row])
    prompt = AttachmentPrompt(system.runtime, run, message)
    mutate(system, row, mutation)
    peer, calls = client()
    with pytest.raises(AppError):
        prompt.dispatch(peer, "session", "read")
    assert not calls


def mutate(system, row, mutation):
    db = system.service.db
    if mutation == "owner":
        db.execute("UPDATE files SET user_id='other' WHERE id=?", (row["id"],))
    elif mutation == "delete":
        db.execute("DELETE FROM files WHERE id=?", (row["id"],))
    elif mutation == "scope":
        db.execute("UPDATE files SET conversation_id='other' WHERE id=?", (row["id"],))
    elif mutation == "bytes":
        path = Path(row["path"])
        path.write_bytes(b"x" * path.stat().st_size)
    elif mutation == "user_disabled":
        db.execute("UPDATE users SET enabled=0 WHERE id=?", (system.user["id"],))
    elif mutation == "assistant_disabled":
        db.execute("UPDATE assistants SET enabled=0 WHERE id=?", (system.assistant["id"],))
    elif mutation == "archived":
        db.execute("UPDATE conversations SET archived=1 WHERE id=?", (system.runtime.conversation_id,))
    else:
        db.execute("UPDATE conversations SET active_run_id='other' WHERE id=?", (system.runtime.conversation_id,))


@pytest.mark.parametrize("mutation", ["owner", "delete", "scope", "bytes"])
def test_revoked_native_history_cannot_be_reused_without_new_attachments(system, mutation):
    row = system.upload()
    run, message = system.prepare([row])
    prompt = AttachmentPrompt(system.runtime, run, message)
    peer, _ = client()
    prompt.dispatch(peer, "native", "read")
    system.service.db.execute("UPDATE runs SET status='succeeded' WHERE id=?", (run["id"],))
    system.service.db.execute("UPDATE conversations SET active_run_id=NULL WHERE id=?", (system.runtime.conversation_id,))
    next_run, next_message = system.prepare([])
    mutate(system, row, mutation)
    with pytest.raises(AppError):
        AttachmentPrompt(system.runtime, next_run, next_message)


@pytest.mark.parametrize("cancel", ["runtime", "database"])
def test_cancel_after_parse_prevents_dispatch(system, cancel):
    row = system.upload()
    run, message = system.prepare([row])
    prompt = AttachmentPrompt(system.runtime, run, message)
    if cancel == "runtime":
        system.runtime.interrupted = True
    else:
        system.service.db.execute("UPDATE runs SET status='cancelling' WHERE id=?", (run["id"],))
    peer, calls = client()
    with pytest.raises(asyncio.CancelledError):
        prompt.dispatch(peer, "native", "read")
    assert not calls


@pytest.mark.parametrize("suffix", [".pdf", ".docx", ".xlsx"])
def test_local_parser_reads_private_snapshot_not_original(system, monkeypatch, suffix):
    row = system.upload(b"synthetic parser input", "doc" + suffix)
    run, message = system.prepare([row])
    paths = []

    def parse(path, name):
        paths.append(path)
        assert path != Path(row["path"]) and path.read_bytes() == b"synthetic parser input"
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        return [{"page": 2, "text": "local extraction", "extraction": "fixture-parser"}]

    monkeypatch.setattr(system.service.knowledge.parser, "parse", parse)
    prompt = AttachmentPrompt(system.runtime, run, message)
    assert "local extraction" in prompt.blocks[0]["text"]
    assert all(not path.exists() for path in paths)


def test_path_replacement_during_read_is_detected(system, monkeypatch):
    row = system.upload()
    run, message = system.prepare([row])
    original_read, switched = os.read, False

    def replace_after_read(fd, amount):
        nonlocal switched
        result = original_read(fd, amount)
        if result and not switched:
            switched = True
            path = Path(row["path"])
            path.unlink()
            path.write_bytes(b"x" * len(result))
        return result

    monkeypatch.setattr(os, "read", replace_after_read)
    with pytest.raises(AppError, match="附件"):
        AttachmentPrompt(system.runtime, run, message)


def test_repeated_history_is_deduplicated_and_disk_reads_do_not_hold_db_lock(system, monkeypatch):
    import app.harness_attachments as module
    row = system.upload()
    run, message = system.prepare([row])
    prompt = AttachmentPrompt(system.runtime, run, message)
    peer, _ = client()
    prompt.dispatch(peer, "native", "read")
    metadata = system.service.require("runs", run["id"])["execution_meta"]
    for _ in range(205):
        system.service.db.execute("INSERT INTO runs(id,conversation_id,user_id,client_request_id,message_id,status,created_at,execution_meta) VALUES(?,?,?,?,?,'succeeded',?,?)", (uid(), system.runtime.conversation_id, system.user["id"], uid(), uid(), now(), metadata))
    real_read = module._read_upload
    reads = []

    def read(service, record):
        assert not service.db.lock._is_owned()
        reads.append(record["id"])
        return real_read(service, record)

    monkeypatch.setattr(module, "_read_upload", read)
    prompt = AttachmentPrompt(system.runtime, run, message)
    assert len(prompt.history) == 1
    prompt.dispatch(peer, "native", "read")
    assert reads == [row["id"]] * 3


@pytest.mark.parametrize("mutation", ["cancel", "owner", "bytes"])
def test_change_during_slow_local_parser_prevents_dispatch(system, monkeypatch, mutation):
    row = system.upload(b"private snapshot", "doc.pdf")
    run, message = system.prepare([row])

    def parse(*_args):
        if mutation == "cancel":
            system.runtime.interrupted = True
        else:
            mutate(system, row, mutation)
        return [{"page": 1, "text": "parsed content", "extraction": "fixture-parser"}]

    monkeypatch.setattr(system.service.knowledge.parser, "parse", parse)
    with pytest.raises(asyncio.CancelledError if mutation == "cancel" else AppError):
        AttachmentPrompt(system.runtime, run, message)
    assert not list((system.service.settings.data_dir / "harness-attachment-tmp").iterdir())


def test_active_stream_revocation_closes_owned_runtime(system):
    from app.harness_runtime import HarnessRuntime
    row = system.upload()
    run, message = system.prepare([row])
    runtime = object.__new__(HarnessRuntime)
    runtime.service, runtime.conversation_id = system.service, system.runtime.conversation_id
    closed = []

    async def operation(_run, _message, _attachments):
        mutate(system, row, "owner")
        await asyncio.sleep(10)

    async def close():
        closed.append(True)

    runtime._execute_chat, runtime.close = operation, close
    with pytest.raises(AppError):
        # Isolate attachment revocation; HTTP receipt scope is tested by the
        # actual pinned runtime and its synthetic provider fixtures.
        asyncio.run(runtime._execute_owned(run, message))
    assert closed == [True] and runtime.current_run is None


def test_awaiting_human_approval_remains_authorized(system):
    row = system.upload()
    run, message = system.prepare([row])
    prompt = AttachmentPrompt(system.runtime, run, message)
    system.service.db.execute("UPDATE runs SET status='awaiting_approval' WHERE id=?", (run["id"],))
    prompt.recheck(content=True)


@pytest.mark.parametrize("field,value", [("id", "foreign-message"), ("file_ids", "[]"), ("text", "changed")])
def test_message_must_match_current_run_snapshot(system, field, value):
    row = system.upload()
    run, message = system.prepare([row])
    with pytest.raises(AppError):
        AttachmentPrompt(system.runtime, run, {**message, field: value})


def test_history_plus_current_unique_count_is_bounded_before_dispatch(system, monkeypatch):
    row = system.upload()
    run, message = system.prepare([row])
    prompt = AttachmentPrompt(system.runtime, run, message)
    extra = system.upload()
    import app.harness_attachments as module
    extra_snapshot = {"id": extra["id"], "binding": module._binding(extra), "sha256": hashlib.sha256(Path(extra["path"]).read_bytes()).hexdigest()}
    prompt.history = [extra_snapshot]
    monkeypatch.setattr(module, "MAX_HISTORY_FILES", 1)
    peer, calls = client()
    with pytest.raises(AppError) as error:
        prompt.dispatch(peer, "native", "read")
    assert error.value.code == "ATTACHMENT_LIMIT" and not calls


@pytest.mark.parametrize("suffix", [".docx", ".xlsx", ".pdf"])
def test_real_local_document_parser_support_is_not_placeholder(system, suffix):
    if suffix == ".pdf":
        content = b"BT /F1 12 Tf 40 100 Td (Harness attached pressure is 7 bar.) Tj ET"
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                   b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
                   b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"]
        data, offsets = b"%PDF-1.4\n", []
        for index, body in enumerate(objects, 1):
            offsets.append(len(data))
            data += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(data)
        data += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
        data += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    else:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            if suffix == ".docx":
                archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Harness attached pressure is 7 bar.</w:t></w:r></w:p></w:body></w:document>')
            else:
                archive.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Harness attached pressure is 7 bar.</t></is></c></row></sheetData></worksheet>')
        data = stream.getvalue()
    row = system.upload(data, "pressure" + suffix)
    run, message = system.prepare([row])
    prompt = AttachmentPrompt(system.runtime, run, message)
    assert "Harness attached pressure is 7 bar." in prompt.blocks[0]["text"]
