import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import now
from app.file_tools import build_file_tools
from app.security import AppError
from app.service import Service


PASSWORD = "file-tool-test-password"


@pytest.fixture
def file_system(tmp_path):
    service = Service(Settings(data_dir=tmp_path, provider_settings="", model="fixture"))
    owner = service.create_user(
        {"username": "file-owner", "display_name": "File owner", "password": PASSWORD, "role": "admin"}
    )
    assistant = service.save_assistant({"name": "File assistant", "connections": []}, user=owner)
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "Files"}, owner)
    run, _ = service.prepare_run(
        conversation["id"],
        {"client_request_id": "file-tools-run", "text": "work with files", "file_ids": []},
        owner,
    )
    checks = []
    runtime = SimpleNamespace(
        service=service,
        conversation_id=conversation["id"],
        config={"assistant_snapshot": service.usable_assistant(assistant["id"], owner)},
        current_run={"id": run["run_id"]},
        check_tool=lambda name, arguments: checks.append((name, arguments)) or True,
    )
    tools = {tool.name: tool for tool in build_file_tools(runtime)}
    try:
        yield service, owner, conversation, runtime, tools, checks
    finally:
        service.db.close()


def invoke(tools, name, arguments):
    result = asyncio.run(tools[name].handler(arguments))
    text = result["content"][0]["text"]
    return result, (None if result.get("is_error") else json.loads(text))


def test_file_write_read_search_and_revision_conflict(file_system):
    _, _, _, runtime, tools, checks = file_system
    result, written = invoke(
        tools,
        "file_write",
        {"path": "notes/readme.txt", "content": "P-900 pump pressure is 7 bar.\n", "expected_revision": 0},
    )
    assert not result.get("is_error")
    assert written["revision"] == 1
    assert len(written["sha256"]) == 64
    assert checks[0][0] == "mcp__workspace__file_write"

    result, read = invoke(tools, "file_read", {"path": "notes/readme.txt"})
    assert not result.get("is_error")
    assert read["content"] == "P-900 pump pressure is 7 bar.\n"
    assert read["revision"] == 1

    result, searched = invoke(tools, "file_search", {"query": "7 BAR"})
    assert not result.get("is_error")
    assert searched["results"] == [
        {
            "path": "notes/readme.txt",
            "line": 1,
            "snippet": "P-900 pump pressure is 7 bar.",
            "revision": 1,
            "sha256": written["sha256"],
        }
    ]

    result, updated = invoke(
        tools,
        "file_write",
        {"path": "notes/readme.txt", "content": "P-900 pump pressure is 8 bar.\n", "expected_revision": 1},
    )
    assert not result.get("is_error") and updated["revision"] == 2
    result, _ = invoke(
        tools,
        "file_write",
        {"path": "notes/readme.txt", "content": "stale write", "expected_revision": 1},
    )
    assert result["is_error"]
    assert "版本" in result["content"][0]["text"]
    rows = runtime.service.db.all(
        "SELECT revision,sha256 FROM workspace_file_version_history WHERE conversation_id=? ORDER BY revision",
        (runtime.conversation_id,),
    )
    assert [row["revision"] for row in rows] == [1, 2]


def test_paths_are_confined_to_the_conversation_workbench(file_system, tmp_path):
    service, owner, conversation, runtime, tools, _ = file_system
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    workbench = service.settings.data_dir / "workspaces" / conversation["id"] / "workbench"
    workbench.mkdir(parents=True, exist_ok=True)
    (workbench / "outside-link.txt").symlink_to(outside)

    for path in ["../outside.txt", str(outside), ".hidden.txt", "dir/../../outside.txt"]:
        result, _ = invoke(tools, "file_read", {"path": path})
        assert result["is_error"]
    result, _ = invoke(
        tools,
        "file_write",
        {"path": "outside-link.txt", "content": "must stay in workspace", "expected_revision": 0},
    )
    assert result["is_error"]
    assert outside.read_text(encoding="utf-8") == "secret"


def test_untracked_and_denied_writes_do_not_create_files(file_system):
    service, _, conversation, runtime, tools, _ = file_system
    root = service.settings.data_dir / "workspaces" / conversation["id"] / "workbench"
    root.mkdir(parents=True, exist_ok=True)
    (root / "untracked.txt").write_text("do not overwrite", encoding="utf-8")
    result, _ = invoke(
        tools,
        "file_write",
        {"path": "untracked.txt", "content": "replace", "expected_revision": 0},
    )
    assert result["is_error"]
    assert (root / "untracked.txt").read_text(encoding="utf-8") == "do not overwrite"

    runtime.check_tool = lambda *_: (_ for _ in ()).throw(RuntimeError("permission denied"))
    result, _ = invoke(
        tools,
        "file_write",
        {"path": "denied.txt", "content": "must not exist", "expected_revision": 0},
    )
    assert result["is_error"]
    assert not (root / "denied.txt").exists()


def test_file_tools_reject_binary_nul_and_oversized_content(file_system):
    service, owner, conversation, runtime, tools, _ = file_system
    root = service.settings.data_dir / "workspaces" / conversation["id"] / "workbench"
    root.mkdir(parents=True, exist_ok=True)
    binary = b"\xff\xfe\x00binary"
    (root / "binary.dat").write_bytes(binary)
    stamp = now()
    service.db.execute(
        "INSERT INTO workspace_file_versions(conversation_id,path,revision,sha256,size,created_at,updated_at,updated_by) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            conversation["id"], "binary.dat", 1, hashlib.sha256(binary).hexdigest(), len(binary),
            stamp, stamp, owner["id"],
        ),
    )
    result, _ = invoke(tools, "file_read", {"path": "binary.dat"})
    assert result["is_error"] and "UTF-8" in result["content"][0]["text"]
    result, searched = invoke(tools, "file_search", {"query": "binary"})
    assert not result.get("is_error") and searched["results"] == []

    result, _ = invoke(tools, "file_write", {"path": "nul.txt", "content": "bad\x00value", "expected_revision": 0})
    assert result["is_error"]
    result, _ = invoke(tools, "file_write", {"path": "empty.txt", "content": "", "expected_revision": 0})
    assert not result.get("is_error")
    result, _ = invoke(tools, "file_write", {"path": "too-large.txt", "content": "x" * 524288, "expected_revision": 0})
    assert result["is_error"]


def test_authorization_validates_paths_and_requires_approval_for_writes(file_system):
    service, _, conversation, _, _, _ = file_system
    conversation_id = conversation["id"]
    assert service.authorize_tool(conversation_id, "mcp__workspace__file_read", {"path": "notes/a.txt"}) is True
    assert service.authorize_tool(conversation_id, "mcp__workspace__file_search", {"query": "pressure"}) is True
    # A write is allowed to reach the approval flow but is never auto-approved.
    assert service.authorize_tool(
        conversation_id, "mcp__workspace__file_write",
        {"path": "notes/a.txt", "content": "x", "expected_revision": 0},
    ) is False
    with pytest.raises(AppError) as escaped:
        service.authorize_tool(
            conversation_id, "mcp__workspace__file_write",
            {"path": "../outside.txt", "content": "x", "expected_revision": 0},
        )
    assert escaped.value.code == "PATH_DENIED"
    with pytest.raises(AppError):
        service.authorize_tool(conversation_id, "mcp__workspace__file_read", {"path": "/etc/passwd"})
