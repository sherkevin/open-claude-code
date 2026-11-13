import asyncio
import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.service import Service
from app.workspace_tools import build_workspace_tools


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
    tools = {tool.name: tool for tool in build_workspace_tools(runtime)}
    try:
        yield service, owner, conversation, runtime, tools, checks
    finally:
        service.db.close()


def invoke(tools, name, arguments):
    result = asyncio.run(tools[name].handler(arguments))
    text = result["content"][0]["text"]
    # Successful tool results carry JSON; failures keep the SDK handler's
    # plain-text envelope, so only parse JSON for non-error responses.
    return result, text if result.get("is_error") else json.loads(text)


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
    result, text = invoke(
        tools,
        "file_write",
        {"path": "notes/readme.txt", "content": "stale write", "expected_revision": 1},
    )
    assert result["is_error"]
    assert "版本" in text
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

    for path, message in [
        ("../outside.txt", "工作区内的相对路径"),
        (str(outside), "工作区内的相对路径"),
        (".hidden.txt", "运行配置目录"),
        ("dir/../../outside.txt", "工作区内的相对路径"),
    ]:
        result, text = invoke(tools, "file_read", {"path": path})
        assert result["is_error"]
        assert message in text
    result, text = invoke(
        tools,
        "file_write",
        {"path": "outside-link.txt", "content": "must stay in workspace", "expected_revision": 0},
    )
    assert result["is_error"]
    assert "工作区内" in text
    assert outside.read_text(encoding="utf-8") == "secret"


def test_untracked_and_denied_writes_do_not_create_files(file_system):
    service, _, conversation, runtime, tools, _ = file_system
    root = service.settings.data_dir / "workspaces" / conversation["id"] / "workbench"
    root.mkdir(parents=True, exist_ok=True)
    (root / "untracked.txt").write_text("do not overwrite", encoding="utf-8")
    result, text = invoke(
        tools,
        "file_write",
        {"path": "untracked.txt", "content": "replace", "expected_revision": 0},
    )
    assert result["is_error"]
    assert "未登记" in text
    assert (root / "untracked.txt").read_text(encoding="utf-8") == "do not overwrite"

    runtime.check_tool = lambda *_: (_ for _ in ()).throw(RuntimeError("permission denied"))
    result, text = invoke(
        tools,
        "file_write",
        {"path": "denied.txt", "content": "must not exist", "expected_revision": 0},
    )
    assert result["is_error"]
    assert "失败" in text
    assert not (root / "denied.txt").exists()


def test_file_tools_reject_binary_and_oversized_content(file_system):
    _, _, _, _, tools, _ = file_system
    result, _ = invoke(tools, "file_write", {"path": "empty.txt", "content": "", "expected_revision": 0})
    assert not result.get("is_error")
    result, text = invoke(tools, "file_write", {"path": "too-large.txt", "content": "x" * 524288, "expected_revision": 0})
    assert result["is_error"]
    assert "限制" in text
