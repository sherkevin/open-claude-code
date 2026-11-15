"""Host-authorized, conversation-bound upload snapshots for OCR/image tools.

The model may only supply an opaque ``file_id``.  Ownership, conversation
binding, media type and bytes all come from host state.  The read reuses the
already-reviewed no-symlink/one-inode reader in ``harness_attachments`` rather
than introducing a second path sandbox, and the file binding is checked before
and after the read so a swapped or resized file never reaches a provider.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import dump
from .harness_attachments import _binding, _image, _read_upload
from .security import AppError

MAX_UPLOAD_ID = 128
MAX_NAME = 240
MAX_OCR_BYTES = 100 * 1024 * 1024
# Independent OCR is an image/PDF text extraction capability.  Office text
# extraction belongs to the knowledge parser and is not silently relabelled.
OCR_MEDIA = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
REVERSE_IMAGE_MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _error(status: int, code: str, message: str) -> AppError:
    return AppError(status, code, message)


def _unavailable() -> AppError:
    # Deliberately indistinguishable between "another user's file", "another
    # conversation's file" and "never attached" to prevent ID probing.
    return _error(404, "UPLOAD_UNAVAILABLE", "该附件不在当前会话授权范围内")


@dataclass(frozen=True)
class UploadSnapshot:
    file_id: str
    name: str
    mime_type: str
    size: int
    sha256: str
    binding: str
    data: bytes


def _field(context: Any, name: str) -> Any:
    if isinstance(context, Mapping):
        return context.get(name)
    return getattr(context, name, None)


def _recheck(context: Any) -> None:
    callback = _field(context, "recheck")
    if callable(callback):
        callback()


class AuthorizedUploads:
    def __init__(self, service):
        self.service = service

    def _context(self, context: Any):
        fields = {name: _field(context, name) for name in
                  ("run_id", "conversation_id", "user_id", "assistant_id", "tool_call_id")}
        if any(not isinstance(fields[name], str) or not fields[name]
               for name in ("run_id", "conversation_id", "user_id", "assistant_id")):
            raise _error(403, "TOOL_CONTEXT_INVALID", "工具调用上下文无效")
        return fields

    def _binding_in_conversation(self, file_id: str, conversation_id: str) -> bool:
        rows = self.service.db.all(
            "SELECT file_ids FROM messages WHERE conversation_id=? AND role='user'", (conversation_id,))
        for row in rows:
            try:
                ids = json.loads(row.get("file_ids") or "[]")
            except (TypeError, ValueError):
                continue
            if isinstance(ids, list) and file_id in ids:
                return True
        return False

    def snapshot(self, context: Any, file_id: str, *, mode: str) -> UploadSnapshot:
        """Return a stable byte snapshot or fail before any provider dispatch.

        ``mode`` is ``"ocr"`` or ``"reverse_image"`` and selects the supported
        media set.  The returned bytes/hash never enter the model arguments.
        """
        fields = self._context(context)
        if mode not in {"ocr", "reverse_image"}:
            raise _error(422, "UPLOAD_MODE_INVALID", "附件用途无效")
        if (not isinstance(file_id, str) or not file_id or len(file_id) > MAX_UPLOAD_ID
                or any(ord(char) < 33 or ord(char) > 126 for char in file_id)):
            raise _unavailable()

        _recheck(context)
        service = self.service
        user = service.require("users", fields["user_id"])
        if not user["enabled"]:
            raise _error(403, "USER_DISABLED", "账号已停用")
        run = service.require("runs", fields["run_id"], user)
        if run["conversation_id"] != fields["conversation_id"] or run["user_id"] != user["id"]:
            raise _unavailable()
        if run["status"] not in {"running", "awaiting_approval"}:
            raise _error(409, "RUN_INACTIVE", "此任务不再允许读取附件")
        conversation = service.require("conversations", fields["conversation_id"], user)
        if conversation["assistant_id"] != fields["assistant_id"]:
            raise _unavailable()
        service.usable_assistant(conversation["assistant_id"], user)

        row = service.require("files", file_id, user)
        if row["user_id"] != user["id"]:
            raise _unavailable()
        # ``conversation_id`` is NULL for fresh uploads, so membership in a
        # message of *this* conversation is the authoritative binding.
        if row.get("conversation_id") == conversation["id"]:
            bound = True
        else:
            bound = self._binding_in_conversation(file_id, conversation["id"])
        if not bound:
            raise _unavailable()

        name = row.get("name")
        if (not isinstance(name, str) or not name or len(name) > MAX_NAME or Path(name).name != name
                or "\\" in name or any(ord(char) < 32 for char in name)):
            raise _error(422, "UPLOAD_INVALID", "附件名称无效")
        suffix = Path(name).suffix.lower()
        media = OCR_MEDIA if mode == "ocr" else REVERSE_IMAGE_MEDIA
        expected_mime = media.get(suffix)
        if expected_mime is None or row.get("mime_type") != expected_mime:
            if mode == "ocr":
                raise _error(422, "UPLOAD_UNSUPPORTED", "独立识别仅支持 PDF 与静态 PNG/JPEG/WebP/GIF")
            raise _error(422, "UPLOAD_UNSUPPORTED", "以图搜图仅支持静态 PNG/JPEG/WebP 图片")

        size = row.get("size")
        limit = MAX_OCR_BYTES
        configured = getattr(getattr(service, "settings", None), "upload_max_bytes", None)
        if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
            limit = min(limit, configured)
        if type(size) is not int or size <= 0 or size > limit:
            raise _error(413, "UPLOAD_TOO_LARGE", "附件超过识别大小上限")

        before = _binding(row)
        _recheck(context)
        data = _read_upload(service, row)
        digest = hashlib.sha256(data).hexdigest()
        current = service.require("files", file_id, user)
        if _binding(current) != before or len(data) != size:
            raise _error(409, "UPLOAD_CHANGED", "附件内容或授权已变化，请重新附加后重试")
        if mode == "reverse_image":
            _image(data, expected_mime)
        elif suffix == ".pdf" and not data.startswith(b"%PDF-"):
            raise _error(422, "UPLOAD_INVALID", "文件内容与 PDF 声明不符")
        _recheck(context)
        return UploadSnapshot(file_id=file_id, name=name, mime_type=expected_mime,
                              size=len(data), sha256=digest, binding=before, data=data)


    def verify(self, snapshot: UploadSnapshot) -> None:
        """Re-read current bytes; metadata/size equality is not sufficient.

        A same-path, same-length replacement keeps the database binding stable
        while changing the original document behind an OCR/image source.
        """
        row = self.service.require("files", snapshot.file_id)
        if _binding(row) != snapshot.binding or row.get("size") != snapshot.size:
            raise _error(409, "UPLOAD_CHANGED", "附件内容或授权已变化，请重新附加后重试")
        data = _read_upload(self.service, row)
        if len(data) != snapshot.size or hashlib.sha256(data).hexdigest() != snapshot.sha256:
            raise _error(409, "UPLOAD_CHANGED", "附件内容或授权已变化，请重新附加后重试")


def attachment_binding(row: dict) -> str:
    """Expose the reviewed binding digest for host-side audit/tests."""
    return hashlib.sha256(dump({key: row[key] for key in
                                ("id", "user_id", "conversation_id", "name", "mime_type", "size", "path", "created_at")}).encode()).hexdigest()


def owned_by_current_process(path: str) -> bool:
    try:
        return os.stat(path).st_uid == os.getuid()
    except OSError:
        return False
