"""SDK image admission/history tests; no external model or CLI is contacted."""
import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from claude_agent_sdk import ResultMessage

from app.config import Settings
from app.db import dump, now, uid
from app.runtime import SDKRuntime
from app.security import AppError
from app.service import Service
from app.sdk_image_attachments import SDKImagePrompt, _read_image as real_read_image, preflight_images


def png(color="green"):
    stream = io.BytesIO()
    Image.new("RGB", (20, 10), color).save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def system(tmp_path, monkeypatch):
    service = Service(Settings(data_dir=tmp_path, runtime_type="claude", model="test", provider_settings="", model_catalog=""))
    model_id = service.catalog.default_model_id
    service.catalog.models[model_id]["capabilities"] = ["multimodal"]
    user = service.create_user({"username": "sdk-images", "display_name": "Image test", "password": "synthetic-test-password", "role": "admin"})
    assistant = service.save_assistant({"name": "Image test"}, user=user)
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "Image"}, user)

    # Portable tests isolate transport/authorization from the POSIX read. The
    # unmodified real reader has separate tests below and is never weakened.
    def read_image(_service, row):
        from app.harness_attachments import _image
        data = Path(row["path"]).read_bytes()
        _image(data, row["mime_type"])
        return data
    monkeypatch.setattr("app.sdk_image_attachments._read_image", read_image)

    def upload(data=None, name="diagram.png", mime="image/png"):
        content = png() if data is None else data
        identity = uid()
        path = tmp_path / "files" / identity
        path.write_bytes(content)
        row = {"id": identity, "user_id": user["id"], "name": name, "mime_type": mime,
               "size": len(content), "path": str(path), "created_at": now(), "conversation_id": None}
        service.db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?)", tuple(row.values()))
        return row

    def prepare(rows=(), text="read this"):
        result, _ = service.prepare_run(conversation["id"], {"text": text, "file_ids": [row["id"] for row in rows], "client_request_id": uid()}, user)
        run = service.require("runs", result["run_id"])
        return run, service.require("messages", run["message_id"])

    async def emit(*args):
        pass

    def runtime():
        return SDKRuntime(service, conversation["id"], service.runtime_config(conversation["id"]), None, emit)

    def finish(run, status="succeeded"):
        service.db.execute("UPDATE runs SET status=? WHERE id=?", (status, run["id"]))
        service.db.execute("UPDATE conversations SET active_run_id=NULL WHERE id=?", (conversation["id"],))

    yield SimpleNamespace(service=service, user=user, assistant=assistant, conversation=conversation,
                          model_id=model_id, upload=upload, prepare=prepare, runtime=runtime, finish=finish)
    service.db.close()


class Client:
    def __init__(self):
        self.messages, self.closed = [], False

    async def query(self, prompt):
        assert not isinstance(prompt, str)
        self.messages.extend([message async for message in prompt])

    async def receive_response(self):
        yield ResultMessage("success", 1, 1, False, 1, "sdk-image-session")

    async def disconnect(self):
        self.closed = True


def mutate(system, row, case, run):
    db = system.service.db
    if case == "delete":
        db.execute("DELETE FROM files WHERE id=?", (row["id"],))
    elif case == "owner":
        db.execute("UPDATE files SET user_id='other' WHERE id=?", (row["id"],))
    elif case == "scope":
        db.execute("UPDATE files SET conversation_id='other' WHERE id=?", (row["id"],))
    elif case == "bytes":
        Path(row["path"]).write_bytes(png("blue"))
    elif case == "model":
        system.service.catalog.models[system.model_id]["capabilities"] = []
    elif case == "user":
        db.execute("UPDATE users SET enabled=0 WHERE id=?", (system.user["id"],))
    elif case == "assistant":
        db.execute("UPDATE assistants SET enabled=0 WHERE id=?", (system.assistant["id"],))
    elif case == "message":
        db.execute("UPDATE messages SET file_ids='[]' WHERE id=?", (run["message_id"],))
    elif case == "selection":
        meta = json.loads(run["execution_meta"])
        meta["selection"]["model_id"] = "other"
        db.execute("UPDATE runs SET execution_meta=? WHERE id=?", (dump(meta), run["id"]))
    elif case == "cancel":
        db.execute("UPDATE runs SET status='cancelling' WHERE id=?", (run["id"],))


def test_sdk_receives_literal_image_blocks_and_persists_only_receipts(system):
    row = system.upload()
    run, message = system.prepare([row], text="/mcp read @/private/key literally")
    runtime, client = system.runtime(), Client()
    runtime.client = client
    asyncio.run(runtime.execute(run, message))
    assert client.messages[0]["client_composed"] is True
    blocks = client.messages[0]["message"]["content"]
    assert blocks[0] == {"type": "text", "text": message["text"]}
    image = next(block for block in blocks if block["type"] == "image")
    assert image["source"]["type"] == "base64"
    assert image["source"]["media_type"] == "image/png"
    assert base64.b64decode(image["source"]["data"]) == png()
    assert str(system.service.settings.data_dir) not in json.dumps(blocks)
    meta = json.loads(system.service.require("runs", run["id"])["execution_meta"])
    assert meta["sdk_image_attachments"][0]["sha256"] == hashlib.sha256(png()).hexdigest()
    assert set(meta["sdk_image_attachments"][0]) == {"id", "binding", "sha256"}
    assert image["source"]["data"] not in json.dumps(meta)
    assert not (system.service.settings.data_dir / "workspaces" / system.conversation["id"] / "uploads" / f"{row['id']}-{row['name']}").exists()
    assert "[[source:" in blocks[1]["text"]


@pytest.mark.parametrize("case", ["delete", "owner", "scope", "bytes", "model", "user", "assistant", "message", "selection", "cancel"])
def test_delayed_sdk_input_rechecks_before_admission(system, case):
    row = system.upload()
    run, message = system.prepare([row])
    class Delayed(Client):
        async def query(self, prompt):
            mutate(system, row, case, run)
            await super().query(prompt)
    runtime, client = system.runtime(), Delayed()
    runtime.client = client
    with pytest.raises((AppError, asyncio.CancelledError)):
        asyncio.run(runtime.execute(run, message))
    assert client.messages == []
    assert "sdk_image_attachments" not in json.loads(system.service.require("runs", run["id"])["execution_meta"])


@pytest.mark.parametrize("case", ["delete", "owner", "scope", "bytes", "model"])
def test_unknown_admission_and_changed_session_do_not_forget_history(system, case):
    row = system.upload()
    run, message = system.prepare([row])
    class LostResponse(Client):
        async def query(self, prompt):
            await super().query(prompt)
            raise OSError("synthetic response lost after write")
    runtime = system.runtime()
    runtime.client = LostResponse()
    with pytest.raises(OSError):
        asyncio.run(runtime.execute(run, message))
    assert json.loads(system.service.require("runs", run["id"])["execution_meta"])["sdk_image_attachments"]
    system.finish(run, "failed")
    # Neither an absent first session id nor a later replacement clears receipts.
    system.service.db.execute("UPDATE conversations SET sdk_session_id='replaced-session' WHERE id=?", (system.conversation["id"],))
    next_run, next_message = system.prepare()
    mutate(system, row, case, next_run)
    next_runtime, client = system.runtime(), Client()
    next_runtime.client = client
    with pytest.raises(AppError):
        asyncio.run(next_runtime.execute(next_run, next_message))
    assert not client.messages


def test_authorized_history_survives_text_followup_without_reupload(system):
    row = system.upload()
    run, message = system.prepare([row])
    runtime = system.runtime()
    runtime.client = Client()
    asyncio.run(runtime.execute(run, message))
    system.finish(run)
    next_run, next_message = system.prepare(text="compare the earlier image")
    next_runtime, client = system.runtime(), Client()
    next_runtime.client = client
    asyncio.run(next_runtime.execute(next_run, next_message))
    assert client.messages[0]["message"]["content"] == next_message["text"]


def test_images_use_frozen_run_selection_not_conversation_or_public_alias(system):
    row = system.upload()
    run, message = system.prepare([row])
    run["model_id"] = "untrusted-public-view-alias"
    system.service.db.execute("UPDATE conversations SET model_id='different-current-model' WHERE id=?", (system.conversation["id"],))
    runtime, client = system.runtime(), Client()
    runtime.client = client
    asyncio.run(runtime.execute(run, message))
    assert any(block["type"] == "image" for block in client.messages[0]["message"]["content"])


def test_switching_to_text_route_cannot_reuse_native_image_history(system):
    row = system.upload()
    run, message = system.prepare([row])
    runtime = system.runtime()
    runtime.client = Client()
    asyncio.run(runtime.execute(run, message))
    system.finish(run)
    system.service.catalog.models["text-only"] = {
        **system.service.catalog.models[system.model_id], "id": "text-only",
        "upstream_model": "text-only", "capabilities": [],
    }
    system.service._save_model_selection(system.conversation["id"], system.service.catalog.select("text-only"))
    next_run, next_message = system.prepare(text="What was in the earlier image?")
    next_runtime, client = system.runtime(), Client()
    next_runtime.client = client
    with pytest.raises(AppError) as error:
        asyncio.run(next_runtime.execute(next_run, next_message))
    assert error.value.code == "VISION_UNAVAILABLE"
    assert not client.messages


def test_text_attachment_reference_remains_alongside_inline_image(system):
    image = system.upload()
    text = system.upload(b"Pressure is 7 bar.", "notes.txt", "text/plain")
    run, message = system.prepare([image, text])
    root = system.service.settings.data_dir / "workspaces" / system.conversation["id"] / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    runtime, client = system.runtime(), Client()
    runtime.client = client
    asyncio.run(runtime.execute(run, message))
    blocks = client.messages[0]["message"]["content"]
    assert f"{text['id']}-notes.txt" in blocks[0]["text"]
    assert (root / f"{text['id']}-notes.txt").read_bytes() == b"Pressure is 7 bar."
    assert not (root / f"{image['id']}-diagram.png").exists()


def test_stream_revocation_closes_owned_sdk_client(system):
    row = system.upload()
    run, message = system.prepare([row])
    class Waiting(Client):
        async def receive_response(self):
            mutate(system, row, "delete", run)
            await asyncio.Event().wait()
            yield ResultMessage("success", 1, 1, False, 1, "never-reached")
    runtime, client = system.runtime(), Waiting()
    runtime.client = client
    with pytest.raises(AppError):
        asyncio.run(asyncio.wait_for(runtime.execute(run, message), 3))
    assert client.closed and runtime.client is None


def test_capability_preflight_rejects_before_run_but_preserves_ocr_fallback(system):
    row = system.upload()
    system.service.catalog.models[system.model_id]["capabilities"] = []
    with pytest.raises(AppError) as error:
        system.prepare([row])
    assert error.value.code == "VISION_UNAVAILABLE"
    assert system.service.db.one("SELECT COUNT(*) AS n FROM runs")["n"] == 0
    for capability in ("ocr", "image_search"):
        assert preflight_images(system.service, system.conversation["id"], system.user, [row["id"]],
            model_id=system.model_id, assistant={"capabilities": {capability: {"enabled": True}}}) == []


def test_legacy_ocr_path_still_sends_reference_without_inline_bytes(system):
    row = system.upload()
    system.service.catalog.models[system.model_id]["capabilities"] = []
    system.service.db.execute("UPDATE assistants SET capabilities=? WHERE id=?",
        (dump({"ocr": {"enabled": True}}), system.assistant["id"]))
    run, message = system.prepare([row])
    root = system.service.settings.data_dir / "workspaces" / system.conversation["id"] / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    runtime, client = system.runtime(), Client()
    runtime.client = client
    asyncio.run(runtime.execute(run, message))
    content = client.messages[0]["message"]["content"]
    assert isinstance(content, str) and row["id"] in content and "ocr" in content
    assert base64.b64encode(png()).decode() not in content
    assert "sdk_image_attachments" not in json.loads(system.service.require("runs", run["id"])["execution_meta"])


def test_installed_sdk_serializes_image_blocks_without_altering_literal_flag():
    # Exercise the installed SDK serializer, not just our Client test double.
    # No connect() or provider process is used by this protocol-only test.
    from claude_agent_sdk import ClaudeSDKClient
    client = ClaudeSDKClient()
    written = []
    async def write(value):
        written.append(json.loads(value))
    client._query = object()
    client._transport = SimpleNamespace(write=write)
    message = {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "/mcp @/private/key"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "synthetic-image"}},
    ]}, "parent_tool_use_id": None, "client_composed": True}
    async def prompt():
        yield message
    asyncio.run(client.query(prompt()))
    assert written[0]["message"] == message["message"]
    assert written[0]["client_composed"] is True


def test_pure_text_keeps_string_prompt(system):
    run, message = system.prepare(text="/compact is literal @/private/file")
    runtime, client = system.runtime(), Client()
    runtime.client = client
    asyncio.run(runtime.execute(run, message))
    assert client.messages == [{"type": "user", "message": {"role": "user", "content": message["text"]},
                               "parent_tool_use_id": None, "client_composed": True}]


@pytest.mark.skipif(os.name != "posix", reason="Real descriptor-based image reads require POSIX")
@pytest.mark.parametrize("case", ["regular", "symlink", "parent_symlink", "hardlink", "fifo", "outside", "bad_mime"])
def test_real_posix_reader_keeps_original_file_boundary(system, tmp_path, case):
    row = system.upload()
    path = Path(row["path"])
    if case == "symlink":
        target = tmp_path / "target"
        path.rename(target)
        path.symlink_to(target)
    elif case == "parent_symlink":
        target = tmp_path / "moved-files"
        path.parent.rename(target)
        path.parent.symlink_to(target, target_is_directory=True)
    elif case == "hardlink":
        os.link(path, tmp_path / "linked")
    elif case == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif case == "outside":
        destination = tmp_path / "outside.png"
        path.rename(destination)
        row["path"] = str(destination)
    elif case == "bad_mime":
        row["mime_type"] = "image/jpeg"
    if case == "regular":
        assert real_read_image(system.service, row) == png()
    else:
        with pytest.raises(AppError):
            real_read_image(system.service, row)


@pytest.mark.skipif(os.name == "posix", reason="Only unsupported platforms reject the POSIX-only image reader")
def test_unsupported_platform_has_no_weaker_file_read(system):
    with pytest.raises(AppError) as error:
        real_read_image(system.service, system.upload())
    assert error.value.code == "ATTACHMENT_PLATFORM_UNSUPPORTED"
