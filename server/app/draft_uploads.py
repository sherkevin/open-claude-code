"""Release an uploaded original when its owner removes an unsent attachment."""
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import now
from .security import AppError

DRAFT_RETENTION_DAYS = 7


def mark_draft_upload(db, file_id):
    expiry = datetime.now(timezone.utc) + timedelta(days=DRAFT_RETENTION_DAYS)
    db.execute("INSERT INTO chat_draft_expiry(file_id,expires_at) VALUES(?,?)", (file_id, expiry.isoformat()))


def sweep_expired_draft_uploads(service):
    root = Path(os.path.abspath(service.settings.data_dir)) / "files"
    if root.is_symlink():
        return
    with service.db.lock:
        for parked in root.glob(".discard-*"):
            if not re.fullmatch(r"\.discard-[0-9a-f]{32}", parked.name) or parked.is_symlink() or not parked.is_file():
                continue
            file_id = parked.name.removeprefix(".discard-")
            original = root / file_id
            if service.db.one("SELECT id FROM files WHERE id=? AND path=?", (file_id, str(original))) and not original.exists():
                parked.replace(original)
            else:
                parked.unlink(missing_ok=True)
    rows = service.db.all("""SELECT f.id,f.user_id FROM chat_draft_expiry e JOIN files f ON f.id=e.file_id
        WHERE e.expires_at<=? AND f.conversation_id IS NULL
        AND NOT EXISTS(SELECT 1 FROM messages m,json_each(m.file_ids) j WHERE j.value=f.id)
        AND NOT EXISTS(SELECT 1 FROM message_queue q,json_each(q.payload,'$.file_ids') j
                       WHERE q.status IN ('pending','accepted') AND j.value=f.id)
        ORDER BY e.expires_at LIMIT 100""", (now(),))
    for row in rows:
        try:
            release_draft_upload(service, row["id"], {"id": row["user_id"]})
        except AppError as error:
            if error.code != "UPLOAD_IN_USE":
                raise


def release_draft_upload(service, file_id, user):
    parked = None
    original = None
    try:
        with service.db.transaction():
            row = service.db.one("SELECT * FROM files WHERE id=? AND user_id=?", (file_id, user["id"]))
            if row is None:
                return
            root = Path(os.path.abspath(service.settings.data_dir)) / "files"
            original = Path(os.path.abspath(row["path"]))
            if row["conversation_id"] is not None or original.parent != root or original.name != file_id:
                raise AppError(409, "UPLOAD_IN_USE", "此附件已被会话使用，仍保留原文件")
            referenced = service.db.one(
                """SELECT 1 AS present FROM messages, json_each(messages.file_ids)
                   WHERE json_each.value=?
                   UNION ALL
                   SELECT 1 AS present FROM message_queue, json_each(message_queue.payload, '$.file_ids')
                   WHERE message_queue.status IN ('pending','accepted') AND json_each.value=? LIMIT 1""",
                (file_id, file_id),
            )
            if referenced:
                raise AppError(409, "UPLOAD_IN_USE", "此附件已被会话使用，仍保留原文件")
            try:
                mode = original.lstat().st_mode
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(mode) or root.is_symlink():
                    raise AppError(409, "UPLOAD_IN_USE", "此附件无法作为草稿撤回")
                parked = root / f".discard-{file_id}"
                original.replace(parked)
            service.db.execute("DELETE FROM files WHERE id=? AND user_id=?", (file_id, user["id"]))
    except BaseException:
        if parked is not None and parked.exists():
            parked.replace(original)
        raise
    if parked is not None:
        try:
            parked.unlink(missing_ok=True)
        except OSError:
            # The next sweep can unlink an already withdrawn file.
            pass
