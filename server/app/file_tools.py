"""Conversation-scoped text file tools with revisions and path containment."""
from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import dump, now
from .security import AppError, workspace_path


MAX_FILE_BYTES = 512 * 1024
MAX_READ_CHARS = 200_000
MAX_SEARCH_RESULTS = 50
MAX_SEARCH_QUERY = 200
TEXT_SUFFIXES = frozenset({
    ".csv", ".htm", ".html", ".ini", ".json", ".log", ".md", ".py",
    ".rst", ".sql", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
})


def _bounded_text(value, *, name: str, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise AppError(422, "FILE_ARGUMENT_INVALID", f"{name}必须是文本")
    if not allow_empty and not value.strip():
        raise AppError(422, "FILE_ARGUMENT_INVALID", f"{name}不能为空")
    if len(value) > maximum:
        raise AppError(422, "FILE_ARGUMENT_INVALID", f"{name}超过长度限制")
    if "\x00" in value:
        raise AppError(422, "FILE_ARGUMENT_INVALID", f"{name}包含无效字符")
    return value


class WorkspaceFiles:
    """Store bounded UTF-8 text under one conversation's private workspace."""

    def __init__(self, service, conversation_id: str, user_id: str):
        self.service = service
        self.conversation_id = conversation_id
        self.user_id = user_id
        self.root = service.settings.data_dir / "workspaces" / conversation_id / "workbench"

    def _relative(self, raw: str) -> tuple[str, Path]:
        value = _bounded_text(raw, name="文件路径", maximum=512).replace("\\", "/").strip()
        if value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/")):
            raise AppError(403, "PATH_DENIED", "文件路径必须是当前工作区内的相对路径")
        relative = Path(value)
        if relative.is_absolute():
            raise AppError(403, "PATH_DENIED", "文件路径必须是当前工作区内的相对路径")
        path = workspace_path(self.root, value)
        return path.relative_to(self.root.resolve()).as_posix(), path

    def validate_path(self, raw: str) -> str:
        """Return the normalized path without reading or writing a file.

        The execution adapter uses this before an approval prompt so a denied
        or escaping path is rejected before any confirmation is shown.
        """
        return self._relative(raw)[0]

    def _metadata(self, relative: str) -> dict | None:
        return self.service.db.one(
            "SELECT * FROM workspace_file_versions WHERE conversation_id=? AND path=? AND deleted=0",
            (self.conversation_id, relative),
        )

    def _read_tracked(self, relative: str, path: Path) -> tuple[bytes, dict]:
        metadata = self._metadata(relative)
        if not metadata:
            raise AppError(404, "FILE_NOT_FOUND", "文件不存在或不在可读写工作区内")
        try:
            if path.is_symlink() or not path.is_file():
                raise AppError(403, "PATH_DENIED", "文件路径已不再指向受控工作区")
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise AppError(413, "FILE_TOO_LARGE", "文件超过在线读取大小限制")
            data = path.read_bytes()
        except AppError:
            raise
        except OSError as exc:
            raise AppError(503, "FILE_UNAVAILABLE", "文件暂时无法读取") from exc
        if len(data) != metadata["size"] or hashlib.sha256(data).hexdigest() != metadata["sha256"]:
            raise AppError(409, "FILE_VERSION_CONFLICT", "文件内容与已登记版本不一致，请重新写入")
        return data, metadata

    def read(self, arguments: dict) -> dict:
        relative, path = self._relative(arguments.get("path"))
        data, metadata = self._read_tracked(relative, path)
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AppError(415, "FILE_NOT_TEXT", "当前文件工具只读取 UTF-8 文本") from exc
        return {
            "path": relative,
            "content": content[:MAX_READ_CHARS],
            "revision": metadata["revision"],
            "sha256": metadata["sha256"],
            "size": metadata["size"],
            "truncated": len(content) > MAX_READ_CHARS,
        }

    def write(self, arguments: dict) -> dict:
        relative, path = self._relative(arguments.get("path"))
        content = _bounded_text(arguments.get("content"), name="文件内容", maximum=MAX_FILE_BYTES - 1, allow_empty=True)
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise AppError(413, "FILE_TOO_LARGE", "文件超过可写入大小限制")
        expected = arguments.get("expected_revision")
        if type(expected) is not int or expected < 0:
            raise AppError(422, "FILE_ARGUMENT_INVALID", "expected_revision 必须是非负整数")
        current = self._metadata(relative)
        current_revision = int(current["revision"]) if current else 0
        if expected != current_revision:
            raise AppError(409, "FILE_REVISION_CONFLICT", "文件版本已变化，请重新读取后再写入")
        if path.exists() and current is None:
            raise AppError(409, "FILE_UNTRACKED", "工作区存在未登记文件，不能覆盖")
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise AppError(403, "PATH_DENIED", "文件路径已不再指向受控工作区")

        old = None
        if path.exists():
            try:
                old = path.read_bytes()
            except OSError as exc:
                raise AppError(503, "FILE_UNAVAILABLE", "文件暂时无法读取") from exc
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".workbench-", dir=path.parent)
        temporary = Path(temporary_name)
        digest = hashlib.sha256(encoded).hexdigest()
        revision = current_revision + 1
        stamp = now()
        replaced = False
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            with self.service.db.transaction():
                if current:
                    changed = self.service.db.execute(
                        "UPDATE workspace_file_versions SET revision=?,sha256=?,size=?,updated_at=?,updated_by=? "
                        "WHERE conversation_id=? AND path=? AND revision=? AND deleted=0",
                        (revision, digest, len(encoded), stamp, self.user_id, self.conversation_id, relative, current_revision),
                    )
                    if changed.rowcount != 1:
                        raise AppError(409, "FILE_REVISION_CONFLICT", "文件版本已变化，请重新读取后再写入")
                else:
                    self.service.db.execute(
                        "INSERT INTO workspace_file_versions(conversation_id,path,revision,sha256,size,created_at,updated_at,updated_by) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (self.conversation_id, relative, revision, digest, len(encoded), stamp, stamp, self.user_id),
                    )
                self.service.db.execute(
                    "INSERT INTO workspace_file_version_history(conversation_id,path,revision,sha256,size,created_at,created_by) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (self.conversation_id, relative, revision, digest, len(encoded), stamp, self.user_id),
                )
                os.replace(temporary, path)
                replaced = True
        except BaseException:
            temporary.unlink(missing_ok=True)
            if replaced:
                try:
                    if old is None:
                        path.unlink(missing_ok=True)
                    else:
                        restore = tempfile.NamedTemporaryFile(prefix=".workbench-restore-", dir=path.parent, delete=False)
                        try:
                            restore.write(old)
                            restore.flush()
                            os.fsync(restore.fileno())
                        finally:
                            restore.close()
                        os.replace(restore.name, path)
                except OSError:
                    raise AppError(503, "FILE_ROLLBACK_FAILED", "文件写入失败，且无法恢复旧版本") from None
            raise
        return {
            "path": relative,
            "revision": revision,
            "sha256": digest,
            "size": len(encoded),
            "status": "written",
        }

    def search(self, arguments: dict) -> dict:
        query = _bounded_text(arguments.get("query"), name="检索词", maximum=MAX_SEARCH_QUERY).strip()
        rows = self.service.db.all(
            "SELECT path,revision,sha256,size FROM workspace_file_versions "
            "WHERE conversation_id=? AND deleted=0 ORDER BY path",
            (self.conversation_id,),
        )
        matches = []
        needle = query.casefold()
        for row in rows:
            relative, path = self._relative(row["path"])
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                data, metadata = self._read_tracked(relative, path)
                content = data.decode("utf-8")
            except (AppError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(content.splitlines(), start=1):
                position = line.casefold().find(needle)
                if position < 0:
                    continue
                start = max(0, position - 60)
                end = min(len(line), position + len(query) + 60)
                matches.append({
                    "path": relative,
                    "line": line_number,
                    "snippet": line[start:end],
                    "revision": metadata["revision"],
                    "sha256": metadata["sha256"],
                })
                if len(matches) >= MAX_SEARCH_RESULTS:
                    return {"query": query, "results": matches, "truncated": True}
                break
        return {"query": query, "results": matches, "truncated": False}


@dataclass(frozen=True)
class FileToolDefinition:
    """Framework-neutral file tool kept separate from any model SDK."""

    name: str
    description: str
    input_schema: dict[str, type]
    read_only: bool
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def build_file_tools(runtime) -> list[FileToolDefinition]:
    """Build the three controlled file tools for one conversation run.

    The conversation workspace is resolved lazily on every call so a stale
    registration cannot outlive a revoked account or a switched assistant.
    """

    service = runtime.service
    conversation = service.require("conversations", runtime.conversation_id)

    async def invoke(name, arguments, callback):
        try:
            runtime.check_tool(f"mcp__workspace__{name}", arguments)
            if not runtime.current_run:
                raise AppError(409, "RUN_REQUIRED", "工具需要活动任务")
            current_user = service.require("users", conversation["user_id"])
            service.usable_assistant(conversation["assistant_id"], current_user)
            files = WorkspaceFiles(service, runtime.conversation_id, current_user["id"])
            return {"content": [{"type": "text", "text": dump(callback(files, arguments))}]}
        except AppError as exc:
            return {"is_error": True, "content": [{"type": "text", "text": exc.message}]}
        except Exception:
            return {"is_error": True, "content": [{"type": "text", "text": "文件操作暂时失败，请稍后重试"}]}

    async def file_read(arguments):
        return await invoke("file_read", arguments, lambda files, args: files.read(args))

    async def file_write(arguments):
        return await invoke("file_write", arguments, lambda files, args: files.write(args))

    async def file_search(arguments):
        return await invoke("file_search", arguments, lambda files, args: files.search(args))

    return [
        FileToolDefinition(
            "file_read",
            "读取当前会话工作区内已登记的 UTF-8 文本文件，返回版本号、哈希和内容。",
            {"path": str},
            True,
            file_read,
        ),
        FileToolDefinition(
            "file_write",
            "在当前会话工作区内创建或更新 UTF-8 文本文件。写入必须携带读取得到的 expected_revision，并经过用户确认。",
            {"path": str, "content": str, "expected_revision": int},
            False,
            file_write,
        ),
        FileToolDefinition(
            "file_search",
            "仅搜索当前会话工作区内已登记的文本文件，返回匹配行、路径、版本号和哈希。",
            {"query": str},
            True,
            file_search,
        ),
    ]
