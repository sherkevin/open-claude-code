"""Conversation-isolated native memory, with read-only legacy shared exports."""
from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from .db import now, uid
from .security import AppError, workspace_path

MAX_FILE = 128 * 1024
MAX_TOTAL = 1024 * 1024
MAX_FILES = 100


def relative_name(raw):
    path = Path(raw)
    if path.is_absolute() or path.drive or "\\" in raw or any(part in {".", ".."} or part.startswith(".") for part in raw.split("/")):
        raise AppError(403, "MEMORY_PATH_DENIED", "请使用记忆目录内的相对 Markdown 路径")
    return raw


class MemoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=240)
    content: str = Field(max_length=MAX_FILE)
    revision: int = Field(ge=0)


class NativeMemory:
    def __init__(self, service):
        self.service = service

    def _root(self, category, user_id, item_id):
        root = self.service.settings.data_dir / category / user_id / item_id
        base = self.service.settings.data_dir.resolve()
        for ancestor in (root.parent.parent, root.parent, root):
            if ancestor.is_symlink() or not ancestor.resolve().is_relative_to(base):
                raise AppError(403, "MEMORY_PATH_DENIED", "记忆目录路径无效")
        return root

    def root(self, user_id, conversation_id):
        return self._root("conversation-memory", user_id, conversation_id)

    def legacy_root(self, user_id, assistant_id):
        return self._root("memory", user_id, assistant_id)

    def authorize(self, user, conversation_id):
        self.service.require("conversations", conversation_id, user)
        return self.root(user["id"], conversation_id)

    def initialize_empty(self, conversation_id):
        self.service.db.execute("INSERT INTO conversation_memory_scopes(conversation_id,revision,digest,updated_at,origin) VALUES(?,0,?,?,'empty')", (conversation_id, hashlib.sha256(b"").hexdigest(), now()))

    def initialize(self, user_id, conversation_id):
        db = self.service.db
        # Compatible with callers that already hold the run/edit transaction;
        # BEGIN IMMEDIATE also serializes first migration across server workers.
        with db.lock:
            if db.conn.in_transaction:
                return self._initialize(user_id, conversation_id)
            with db.transaction():
                return self._initialize(user_id, conversation_id)

    def _initialize(self, user_id, conversation_id):
        conversation = self.service.require("conversations", conversation_id)
        if conversation["user_id"] != user_id:
            raise AppError(404, "NOT_FOUND", "会话不存在或无访问权限")
        db = self.service.db
        row = db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (conversation_id,))
        if not row:
            # Official new-session creation always inserts this atomically.
            # An imported conversation without a marker starts empty, never
            # silently inherits another session's old shared files.
            self.initialize_empty(conversation_id)
            row = db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (conversation_id,))
        if row["origin"] != "legacy_pending":
            return row
        root = self.root(user_id, conversation_id)
        if not root.exists():
            source = self.legacy_root(user_id, conversation["assistant_id"])
            files = self.files(source)
            root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = root.parent / (".migration-" + uid())
            temporary.mkdir(mode=0o700)
            try:
                total = 0
                for path, _ in files:
                    data = path.read_bytes()
                    total += len(data)
                    if len(data) > MAX_FILE or total > MAX_TOTAL:
                        raise AppError(413, "MEMORY_TOO_LARGE", "历史记忆超过容量上限，原件已保留")
                    target = temporary / path.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    target.write_bytes(data)
                    target.chmod(0o600)
                # Atomic directory publication. A crash after this rename but
                # before the DB update is resumed from this existing snapshot.
                temporary.rename(root)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        # Validate recovered snapshots too; never replace an existing snapshot.
        digest = self.digest(root)
        db.execute("UPDATE conversation_memory_scopes SET origin='legacy_snapshot',digest=?,updated_at=? WHERE conversation_id=?", (digest, now(), conversation_id))
        return db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (conversation_id,))

    def path(self, root, raw, allow_directory=False):
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise AppError(403, "MEMORY_PATH_DENIED", "记忆路径无效")
        resolved = workspace_path(root, raw)
        relative = resolved.relative_to(root.resolve())
        parts = relative.parts
        if any(":" in part or any(ord(char) < 32 for char in part) for part in parts):
            raise AppError(403, "MEMORY_PATH_DENIED", "记忆路径无效")
        if len(parts) > 5 or len(str(relative)) > 240:
            raise AppError(403, "MEMORY_PATH_DENIED", "记忆路径过长")
        original = Path(raw)
        candidate = original if original.is_absolute() else root / original
        for part in (candidate, *candidate.parents):
            if part == root.parent:
                break
            if part.is_symlink():
                raise AppError(403, "MEMORY_PATH_DENIED", "记忆文件不允许使用符号链接")
        if not allow_directory and (not parts or resolved.suffix.lower() != ".md"):
            raise AppError(403, "MEMORY_PATH_DENIED", "记忆仅支持 Markdown 文件")
        return resolved

    def files(self, root):
        result = []
        if not root.exists():
            return result
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if not name.startswith(".") and not (Path(directory) / name).is_symlink()]
            for name in names:
                candidate = Path(directory) / name
                if candidate.suffix.lower() != ".md":
                    continue
                path = self.path(root, str(candidate))
                size = path.stat().st_size
                if size > MAX_FILE or len(result) >= MAX_FILES:
                    raise AppError(413, "MEMORY_TOO_LARGE", "原生记忆超过管理上限，请联系管理员检查")
                result.append((path, size))
        if sum(size for _, size in result) > MAX_TOTAL:
            raise AppError(413, "MEMORY_TOO_LARGE", "原生记忆超过管理上限，请联系管理员检查")
        return sorted(result)

    def refresh(self, user_id, conversation_id):
        # Sync HTTP handlers run concurrently. Keep digest and revision changes
        # atomic with manual edits and first-time scope initialization.
        with self.service.db.lock:
            return self._refresh(user_id, conversation_id)

    def digest(self, root):
        digest = hashlib.sha256()
        for path, _ in self.files(root):
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\x00")
            digest.update(path.read_bytes())
            digest.update(b"\x00")
        return digest.hexdigest()

    def _refresh(self, user_id, conversation_id):
        row = self.initialize(user_id, conversation_id)
        root = self.root(user_id, conversation_id)
        value = self.digest(root)
        db = self.service.db
        if value != row["digest"]:
            db.execute("UPDATE conversation_memory_scopes SET digest=?,revision=revision+1,updated_at=? WHERE conversation_id=?", (value, now(), conversation_id))
        return db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (conversation_id,))

    def busy(self, user_id, conversation_id):
        if self.service.db.one("SELECT id FROM conversations WHERE user_id=? AND id=? AND active_run_id IS NOT NULL", (user_id, conversation_id)):
            return True
        manager = self.service.manager
        return bool(manager and conversation_id in manager.context_requests)

    def view(self, user, conversation_id):
        with self.service.db.lock:
            return self._view(user, conversation_id)

    def _view(self, user, conversation_id):
        root = self.authorize(user, conversation_id)
        conversation = self.service.require("conversations", conversation_id, user)
        row = self.revision(user["id"], conversation_id)
        return {"conversation_id": conversation_id, "assistant_id": conversation["assistant_id"], "scope": "conversation", "origin": row["origin"], "read_only": False, "revision": row["revision"], "busy": self.busy(user["id"], conversation_id), "files": self.file_index(root), "max_file_bytes": MAX_FILE, "max_total_bytes": MAX_TOTAL}

    def file_index(self, root):
        return [{"path": path.relative_to(root).as_posix(), "size": size, "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()} for path, size in self.files(root)]

    def read(self, user, conversation_id, raw):
        with self.service.db.lock:
            root = self.authorize(user, conversation_id)
            row = self.revision(user["id"], conversation_id)
            return self._read(root, row, raw)

    def _read(self, root, row, raw):
        relative_name(raw)
        path = self.path(root, raw)
        if not path.is_file():
            raise AppError(404, "MEMORY_FILE_NOT_FOUND", "记忆文件不存在")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise AppError(422, "MEMORY_ENCODING", "记忆文件不是 UTF-8 文本") from exc
        return {"path": path.relative_to(root).as_posix(), "content": content, "revision": row["revision"], "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()}

    def revision(self, user_id, conversation_id):
        # Native writes during an active turn must not invalidate that same turn's
        # tool policy. Refresh after it ends, or before starting the next turn.
        if not self.busy(user_id, conversation_id):
            return self.refresh(user_id, conversation_id)
        return self.initialize(user_id, conversation_id)

    def edit(self, user, conversation_id, revision, raw=None, content=None):
        if raw is not None:
            relative_name(raw)
        root = self.authorize(user, conversation_id)
        with self.service.db.transaction():
            if self.busy(user["id"], conversation_id):
                raise AppError(409, "MEMORY_BUSY", "此会话仍有任务执行中，请完成或停止任务后修改记忆")
            row = self.refresh(user["id"], conversation_id)
            if revision != row["revision"]:
                raise AppError(409, "MEMORY_REVISION_CONFLICT", "记忆已更新，请刷新后再修改")
            if raw is None:
                for path, _ in self.files(root):
                    path.unlink()
            else:
                path = self.path(root, raw)
                if content is None:
                    if not path.is_file():
                        raise AppError(404, "MEMORY_FILE_NOT_FOUND", "记忆文件不存在")
                    path.unlink()
                else:
                    data = content.encode("utf-8")
                    files = self.files(root)
                    total = sum(size for item, size in files if item != path) + len(data)
                    if len(data) > MAX_FILE or total > MAX_TOTAL or (not path.exists() and len(files) >= MAX_FILES):
                        raise AppError(413, "MEMORY_TOO_LARGE", "记忆内容超过文件或总容量上限")
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    temporary = path.parent / (".memory-" + uid())
                    try:
                        with temporary.open("xb") as output:
                            output.write(data)
                        temporary.chmod(0o600)
                        temporary.replace(path)
                    finally:
                        temporary.unlink(missing_ok=True)
            updated = self.refresh(user["id"], conversation_id)
            if updated["revision"] != row["revision"]:
                self.service.db.execute("UPDATE conversations SET context_usage=NULL WHERE id=?", (conversation_id,))
        return self.read(user, conversation_id, raw) if content is not None else self.view(user, conversation_id)

    def legacy_authorize(self, user, assistant_id):
        # These are the caller's own historical files, including for an archived
        # conversation whose assistant is no longer usable.
        if not self.service.db.one("SELECT id FROM conversations WHERE user_id=? AND assistant_id=?", (user["id"], assistant_id)) and not self.service.db.one("SELECT assistant_id FROM memory_scopes WHERE user_id=? AND assistant_id=?", (user["id"], assistant_id)):
            self.service.require_resource("assistants", assistant_id, user)
        return self.legacy_root(user["id"], assistant_id)

    def legacy_view(self, user, assistant_id):
        root = self.legacy_authorize(user, assistant_id)
        row = self.service.db.one("SELECT * FROM memory_scopes WHERE user_id=? AND assistant_id=?", (user["id"], assistant_id)) or {"revision": 0}
        return {"assistant_id": assistant_id, "scope": "legacy_user_assistant", "read_only": True, "revision": row["revision"], "busy": False, "files": self.file_index(root), "max_file_bytes": MAX_FILE, "max_total_bytes": MAX_TOTAL}

    def legacy_read(self, user, assistant_id, raw):
        root = self.legacy_authorize(user, assistant_id)
        row = self.service.db.one("SELECT * FROM memory_scopes WHERE user_id=? AND assistant_id=?", (user["id"], assistant_id)) or {"revision": 0}
        self.files(root)
        return self._read(root, row, raw)

    def validate_write(self, root, path, tool, arguments):
        if tool not in {"Write", "Edit"}:
            return
        if tool == "Write":
            content = arguments.get("content")
        else:
            if not path.is_file() or path.stat().st_size > MAX_FILE:
                raise AppError(403, "MEMORY_WRITE_DENIED", "记忆文件不存在或超过容量上限")
            old, new = arguments.get("old_string"), arguments.get("new_string")
            if not isinstance(old, str) or not isinstance(new, str):
                raise AppError(403, "MEMORY_WRITE_DENIED", "记忆编辑参数无效")
            try:
                content = path.read_text(encoding="utf-8").replace(old, new, -1 if arguments.get("replace_all") else 1)
            except UnicodeError as exc:
                raise AppError(403, "MEMORY_WRITE_DENIED", "记忆内容不是 UTF-8 文本") from exc
        if not isinstance(content, str):
            raise AppError(403, "MEMORY_WRITE_DENIED", "记忆写入内容无效")
        files = self.files(root)
        size = len(content.encode("utf-8"))
        if size > MAX_FILE or sum(length for item, length in files if item != path) + size > MAX_TOTAL or (not path.exists() and len(files) >= MAX_FILES):
            raise AppError(403, "MEMORY_WRITE_DENIED", "记忆写入超过容量上限")


def create_memory_router(service_dependency, authenticated_dependency):
    router = APIRouter(prefix="/api/v1", tags=["Native memory"])

    @router.get("/conversations/{conversation_id}/memory")
    def listing(conversation_id: str, service=Depends(service_dependency), user=Depends(authenticated_dependency)):
        return service.memory.view(user, conversation_id)

    @router.get("/conversations/{conversation_id}/memory/file")
    def read(conversation_id: str, path: str = Query(min_length=1, max_length=240), service=Depends(service_dependency), user=Depends(authenticated_dependency)):
        return service.memory.read(user, conversation_id, path)

    @router.put("/conversations/{conversation_id}/memory/file")
    def write(conversation_id: str, body: MemoryWrite, service=Depends(service_dependency), user=Depends(authenticated_dependency)):
        return service.memory.edit(user, conversation_id, body.revision, body.path, body.content)

    @router.delete("/conversations/{conversation_id}/memory/file")
    def delete(conversation_id: str, path: str = Query(min_length=1, max_length=240), revision: int = Query(ge=0), service=Depends(service_dependency), user=Depends(authenticated_dependency)):
        return service.memory.edit(user, conversation_id, revision, path)

    @router.delete("/conversations/{conversation_id}/memory")
    def clear(conversation_id: str, revision: int = Query(ge=0), service=Depends(service_dependency), user=Depends(authenticated_dependency)):
        return service.memory.edit(user, conversation_id, revision)

    @router.get("/assistants/{assistant_id}/memory")
    def legacy_listing(assistant_id: str, service=Depends(service_dependency), user=Depends(authenticated_dependency)):
        return service.memory.legacy_view(user, assistant_id)

    @router.get("/assistants/{assistant_id}/memory/file")
    def legacy_read(assistant_id: str, path: str = Query(min_length=1, max_length=240), service=Depends(service_dependency), user=Depends(authenticated_dependency)):
        return service.memory.legacy_read(user, assistant_id, path)

    @router.put("/assistants/{assistant_id}/memory/file")
    @router.delete("/assistants/{assistant_id}/memory/file")
    @router.delete("/assistants/{assistant_id}/memory")
    def legacy_write(assistant_id: str, service=Depends(service_dependency), user=Depends(authenticated_dependency)):
        service.memory.legacy_authorize(user, assistant_id)
        raise AppError(403, "MEMORY_LEGACY_READ_ONLY", "历史共享记忆仅供读取，请在具体会话中管理记忆")

    return router
