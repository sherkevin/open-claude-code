"""Upload isolation and stable-snapshot tests for OCR/image tools (synthetic)."""
import io
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.authorized_uploads import AuthorizedUploads
from app.config import Settings
from app.db import now, uid
from app.external_capabilities import ToolContext
from app.security import AppError
from app.service import Service


def png():
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    service = Service(Settings(data_dir=tmp_path, runtime_type="harness", model="test",
                               provider_settings="", model_catalog="", knowledge_vision_enabled=False))
    # prepare_run gates image attachments on the model's vision capability, so a
    # text-only default model would reject the upload before the isolation code
    # under test runs. Match test_harness_attachments.py and declare multimodal.
    service.catalog.models[service.catalog.default_model_id]["capabilities"] = ["multimodal"]
    user = service.create_user({"username": "uploads", "display_name": "Test",
                                "password": "safe-test-password-123", "role": "admin"})
    assistant = service.save_assistant({"name": "Upload test", "connections": []}, user=user)
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "test"}, user)

    def upload(data=None, name="scan.png", mime="image/png", **fields):
        data = png() if data is None else data
        file_id = uid()
        path = tmp_path / "files" / file_id
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_bytes(data)
        row = {"id": file_id, "user_id": user["id"], "name": name, "mime_type": mime,
               "size": len(data), "path": str(path), "created_at": now(), "conversation_id": None, **fields}
        service.db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?)", tuple(row.values()))
        return row

    def context(files=(), conversation_id=None, recheck=None):
        conversation_id = conversation_id or conversation["id"]
        response, _ = service.prepare_run(conversation_id, {
            "text": "read", "file_ids": [row["id"] for row in files], "client_request_id": uid()}, user)
        run = service.require("runs", response["run_id"])
        service.db.execute("UPDATE runs SET status='running' WHERE id=?", (run["id"],))
        return ToolContext(run_id=run["id"], conversation_id=conversation_id, user_id=user["id"],
                           assistant_id=assistant["id"], tool_call_id="call-1",
                           recheck=recheck or (lambda: None))

    yield SimpleNamespace(service=service, user=user, assistant=assistant,
                          conversation=conversation, upload=upload, context=context,
                          tmp_path=tmp_path)
    service.db.close()


def snapshot(system, context, row, mode="ocr"):
    return AuthorizedUploads(system.service).snapshot(context, row["id"], mode=mode)


def test_current_conversation_attachment_returns_stable_snapshot(system):
    row = system.upload()
    context = system.context([row])
    result = snapshot(system, context, row)
    assert result.data == Path(row["path"]).read_bytes() and result.file_id == row["id"]
    assert result.mime_type == "image/png" and result.size == len(result.data)
    assert len(result.sha256) == 64 and len(result.binding) == 64


def test_other_users_file_is_not_readable(system):
    row = system.upload(user_id="someone-else")
    with pytest.raises(AppError) as error:
        snapshot(system, system.context(), row)
    assert error.value.status == 404


def test_unattached_upload_is_not_readable(system):
    row = system.upload()
    with pytest.raises(AppError) as error:
        snapshot(system, system.context([]), row)
    assert error.value.code == "UPLOAD_UNAVAILABLE"


def test_attachment_from_another_conversation_is_not_readable(system):
    row = system.upload()
    other = system.service.create_conversation(
        {"assistant_id": system.assistant["id"], "title": "other"}, system.user)
    other_context = system.context([row], conversation_id=other["id"])
    main_context = system.context([])
    # Bound to the other conversation, not the one asking for it.
    with pytest.raises(AppError) as error:
        snapshot(system, main_context, row)
    assert error.value.code == "UPLOAD_UNAVAILABLE"
    assert snapshot(system, other_context, row).file_id == row["id"]


def test_unsupported_media_and_mime_mismatch_are_rejected(system):
    text = system.upload(b"plain text", "notes.txt", "text/plain")
    with pytest.raises(AppError) as error:
        snapshot(system, system.context([text]), text, mode="ocr")
    assert error.value.code == "UPLOAD_UNSUPPORTED"
    fake = system.upload(png(), "fake.png", "image/jpeg")
    with pytest.raises(AppError):
        snapshot(system, system.context([fake]), fake)


def test_reverse_image_accepts_only_static_png_jpeg_webp(system):
    gif = io.BytesIO()
    Image.new("RGB", (8, 8)).save(gif, format="GIF")
    row = system.upload(gif.getvalue(), "anim.gif", "image/gif")
    with pytest.raises(AppError) as error:
        snapshot(system, system.context([row]), row, mode="reverse_image")
    assert error.value.code == "UPLOAD_UNSUPPORTED"


def test_pdf_signature_mismatch_is_rejected(system):
    row = system.upload(b"not a pdf", "scan.pdf", "application/pdf")
    with pytest.raises(AppError) as error:
        snapshot(system, system.context([row]), row)
    assert error.value.code == "UPLOAD_INVALID"


def test_hardlinked_or_symlinked_file_is_rejected(system):
    row = system.upload()
    context = system.context([row])
    os.link(row["path"], str(system.tmp_path / "files" / (row["id"] + "-link")))
    with pytest.raises(AppError):
        snapshot(system, context, row)


def test_disabled_user_and_inactive_run_fail_closed(system):
    row = system.upload()
    context = system.context([row])
    system.service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (system.user["id"],))
    with pytest.raises(AppError):
        snapshot(system, context, row)
    system.service.db.execute("UPDATE users SET enabled=1 WHERE id=?", (system.user["id"],))
    system.service.db.execute("UPDATE runs SET status='cancelled' WHERE id=?", (context.run_id,))
    with pytest.raises(AppError):
        snapshot(system, context, row)


def test_recheck_runs_before_and_after_reading(system):
    row = system.upload()
    calls = []
    context = system.context([row], recheck=lambda: calls.append(1))
    snapshot(system, context, row)
    assert len(calls) >= 2


def test_snapshot_change_between_calls_requires_refresh(system):
    row = system.upload()
    context = system.context([row])
    first = snapshot(system, context, row)
    # A different valid PNG with a new size changes the recorded binding.
    replacement = io.BytesIO()
    Image.new("RGB", (20, 20), "green").save(replacement, format="PNG")
    Path(row["path"]).write_bytes(replacement.getvalue())
    system.service.db.execute("UPDATE files SET size=? WHERE id=?", (len(replacement.getvalue()), row["id"]))
    second = snapshot(system, context, row)
    assert first.binding != second.binding and first.sha256 != second.sha256


from pathlib import Path  # noqa: E402  (used by the snapshot-change test)
