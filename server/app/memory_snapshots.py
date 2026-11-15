"""Private immutable memory trees. SQLite, not a directory, authorizes publication."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .security import AppError
from .db import dump, now, uid

MAX_FILE = 128 * 1024
MAX_TOTAL = 1024 * 1024
MAX_FILES = 100
MAX_MANIFEST = 64 * 1024
MAX_SNAPSHOTS = 32
MAX_PROJECT = 256 * 1024 * 1024
MIN_FREE = 1024 * 1024 * 1024
# Importing the application must not require POSIX-only APIs. The snapshot
# implementation itself does: never replace missing no-follow/dir-fd checks
# with permissive flags or path-based operations on another platform.
SNAPSHOT_FILES_SUPPORTED = (
    os.name == "posix"
    and all(hasattr(os, attr) for attr in (
        "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC", "getuid", "fstatvfs",
    ))
    and {os.open, os.mkdir, os.stat} <= os.supports_dir_fd
    and os.listdir in os.supports_fd
    and os.stat in os.supports_follow_symlinks
)
DIR_FLAGS = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC) if SNAPSHOT_FILES_SUPPORTED else None
FILE_FLAGS = (os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC) if SNAPSHOT_FILES_SUPPORTED else None


def denied(code="MEMORY_INTEGRITY", message="记忆快照无法安全核验，请联系管理员检查"):
    return AppError(409, code, message)


def require_snapshot_support():
    if not SNAPSHOT_FILES_SUPPORTED:
        raise AppError(501, "MEMORY_PLATFORM_UNSUPPORTED", "当前平台不支持安全记忆快照，请使用支持 POSIX 安全文件操作的环境")


def name(raw):
    if (type(raw) is not str or not raw or len(raw) > 240 or "\\" in raw or ":" in raw
            or any(ord(c) < 32 or ord(c) == 127 for c in raw)):
        raise denied("MEMORY_PATH_DENIED", "请使用记忆目录内的相对 Markdown 路径")
    parts = raw.split("/")
    if len(parts) > 5 or any(not p or p.startswith(".") for p in parts) or not parts[-1].lower().endswith(".md"):
        raise denied("MEMORY_PATH_DENIED", "请使用记忆目录内的相对 Markdown 路径")
    try:
        raw.encode("utf-8")
    except UnicodeError:
        raise denied("MEMORY_PATH_DENIED") from None
    return raw


def content_bytes(value):
    if type(value) is not str:
        raise denied("MEMORY_ENCODING")
    try:
        data = value.encode("utf-8")
    except UnicodeError:
        raise denied("MEMORY_ENCODING") from None
    if len(data) > MAX_FILE:
        raise AppError(413, "MEMORY_TOO_LARGE", "记忆内容超过文件容量上限")
    return data


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def digest(files):
    result = hashlib.sha256()
    for path, data in sorted(files.items()):
        result.update(path.encode("utf-8") + b"\0" + data + b"\0")
    return result.hexdigest()


def check_files(files):
    if len(files) > MAX_FILES or sum(len(data) for data in files.values()) > MAX_TOTAL:
        raise AppError(413, "MEMORY_TOO_LARGE", "记忆内容超过文件数或总容量上限")
    for path, data in files.items():
        name(path)
        if type(data) is not bytes or len(data) > MAX_FILE:
            raise AppError(413, "MEMORY_TOO_LARGE", "记忆内容超过文件容量上限")
        try:
            data.decode("utf-8")
        except UnicodeError:
            raise denied("MEMORY_ENCODING") from None


def signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def check_node(fd, directory=False, private=True):
    require_snapshot_support()
    info = os.fstat(fd)
    if directory:
        good = stat.S_ISDIR(info.st_mode)
    else:
        good = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    if not good or (private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600))):
        raise denied()
    return info


@contextmanager
def absolute_dir(path):
    """Do not resolve symlinks, including in ancestors of the private root."""
    require_snapshot_support()
    path = Path(os.path.abspath(path))
    fd = os.open("/", DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def child_dir(parent, part, create=False):
    require_snapshot_support()
    if create:
        try:
            os.mkdir(part, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
    fd = os.open(part, DIR_FLAGS, dir_fd=parent)
    try:
        check_node(fd, True)
        yield fd
    finally:
        os.close(fd)


class VerifiedTree:
    def __init__(self, path, expected=None, capacity_errors=False):
        self.path, self.expected = Path(path), expected
        self.capacity_errors = capacity_errors
        self.files, self.infos, self.handles = {}, {}, []
        self.root_fd = None

    def __enter__(self):
        try:
            self._root = absolute_dir(self.path)
            self.root_fd = self._root.__enter__()
            check_node(self.root_fd, True)
            self._scan(self.root_fd, "", 0)
            check_files(self.files)
            if self.expected is not None:
                raw = self.manifest
                if hashlib.sha256(raw).hexdigest() != self.expected["manifest_digest"]:
                    raise denied()
                entries = [{"path": p, "size": len(v), "sha256": hashlib.sha256(v).hexdigest()} for p, v in sorted(self.files.items())]
                wanted = {"schema": 1, "user_id": self.expected["user_id"], "conversation_id": self.expected["conversation_id"],
                          "snapshot_id": self.expected["snapshot_id"], "digest": digest(self.files), "files": entries}
                if raw != canonical(wanted) or wanted["digest"] != self.expected["digest"]:
                    raise denied()
            elif getattr(self, "manifest", None) is not None:
                raise denied()
            self.assert_unchanged()
            return self
        except (OSError, UnicodeError, ValueError):
            self.__exit__(None, None, None)
            raise denied() from None
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _scan(self, directory, prefix, depth):
        if depth > 4:
            raise denied()
        before = signature(check_node(directory, True))
        names = os.listdir(directory)
        if len(names) > MAX_FILES + 1 or len(self.handles) > MAX_FILES * 6:
            raise denied()
        for part in sorted(names):
            rel = prefix + part
            info = os.stat(part, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if part.startswith(".") or ":" in part or "\\" in part or not part or any(ord(c) < 32 for c in part):
                    raise denied()
                child = os.open(part, DIR_FLAGS, dir_fd=directory)
                self.handles.append((child, signature(check_node(child, True))))
                self._scan(child, rel + "/", depth + 1)
                continue
            is_manifest = rel == ".manifest.json" and self.expected is not None
            if not is_manifest:
                name(rel)
            fd = os.open(part, FILE_FLAGS, dir_fd=directory)
            try:
                start = check_node(fd)
                limit = MAX_MANIFEST if is_manifest else MAX_FILE
                if start.st_size > limit:
                    if not is_manifest and self.capacity_errors:
                        raise AppError(413, "MEMORY_TOO_LARGE", "历史记忆超过容量上限，原件已保留")
                    raise denied()
                data = bytearray()
                while True:
                    chunk = os.read(fd, min(65536, limit + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > limit:
                        raise denied()
                if len(data) != start.st_size or signature(start) != signature(os.fstat(fd)):
                    raise denied()
                self.handles.append((fd, signature(start)))
                fd = None
                if is_manifest:
                    self.manifest = bytes(data)
                else:
                    self.files[rel] = bytes(data)
                    self.infos[rel] = start
                    if len(self.files) > MAX_FILES or sum(map(len, self.files.values())) > MAX_TOTAL:
                        if self.capacity_errors:
                            raise AppError(413, "MEMORY_TOO_LARGE", "历史记忆超过容量上限，原件已保留")
                        raise denied()
            finally:
                if fd is not None:
                    os.close(fd)
        if before != signature(os.fstat(directory)):
            raise denied()
        if depth == 0:
            self.root_signature = before
            if self.expected is not None and not hasattr(self, "manifest"):
                raise denied()

    def assert_unchanged(self):
        require_snapshot_support()
        for fd in getattr(self, "parent_handles", ()):
            check_node(fd, True)
        if signature(os.fstat(self.root_fd)) != self.root_signature:
            raise denied()
        for fd, previous in self.handles:
            if signature(os.fstat(fd)) != previous:
                raise denied()
        with absolute_dir(self.path) as current:
            if signature(os.fstat(current)) != self.root_signature:
                raise denied()

    def __exit__(self, *args):
        for fd, _ in self.handles:
            os.close(fd)
        self.handles.clear()
        if self.root_fd is not None:
            self._root.__exit__(*args)
            self.root_fd = None


class SnapshotFiles:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)

    def path(self, user_id, conversation_id, snapshot_id):
        if any(type(v) is not str or not re.fullmatch(r"[a-f0-9]{32}", v) for v in (user_id, conversation_id, snapshot_id)):
            raise denied()
        return self.data_dir / "conversation-memory-snapshots" / user_id / conversation_id / snapshot_id

    @contextmanager
    def verify(self, binding):
        require_snapshot_support()
        path = self.path(binding["user_id"], binding["conversation_id"], binding["snapshot_id"])
        try:
            with absolute_dir(self.data_dir) as data:
                with child_dir(data, "conversation-memory-snapshots") as module:
                    with child_dir(module, binding["user_id"]) as user:
                        with child_dir(user, binding["conversation_id"]) as conv:
                            with VerifiedTree(path, binding) as tree:
                                tree.parent_handles = (module, user, conv)
                                tree.assert_unchanged()
                                yield tree
        except OSError:
            raise denied() from None

    def build(self, binding, files):
        require_snapshot_support()
        check_files(files)
        manifest = canonical({"schema": 1, "user_id": binding["user_id"], "conversation_id": binding["conversation_id"],
                              "snapshot_id": binding["snapshot_id"], "digest": digest(files),
                              "files": [{"path": p, "size": len(v), "sha256": hashlib.sha256(v).hexdigest()} for p, v in sorted(files.items())]})
        if len(manifest) > MAX_MANIFEST:
            raise denied()
        self.path(binding["user_id"], binding["conversation_id"], binding["snapshot_id"])
        try:
            with absolute_dir(self.data_dir) as data:
                available = os.fstatvfs(data)
                if available.f_bavail * available.f_frsize < MIN_FREE:
                    raise denied("MEMORY_STORAGE_FULL", "记忆存储空间不足，现有记忆保持不变")
                with child_dir(data, "conversation-memory-snapshots", True) as module:
                    with child_dir(module, binding["user_id"], True) as user:
                        with child_dir(user, binding["conversation_id"], True) as conv:
                            os.mkdir(binding["snapshot_id"], 0o700, dir_fd=conv)
                            with child_dir(conv, binding["snapshot_id"]) as root:
                                for path, content in [*sorted(files.items()), (".manifest.json", manifest)]:
                                    self._write(root, path.split("/"), content)
                                os.fsync(root)
                            os.fsync(conv)
            return {**binding, "digest": digest(files), "manifest_digest": hashlib.sha256(manifest).hexdigest()}
        except OSError:
            raise denied("MEMORY_STORAGE_FAILED", "记忆快照未发布，原有效记忆保持不变") from None

    def _write(self, parent, parts, content):
        require_snapshot_support()
        if len(parts) > 1:
            with child_dir(parent, parts[0], True) as child:
                self._write(child, parts[1:], content)
                os.fsync(child)
            os.fsync(parent)
            return
        fd = os.open(parts[0], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        try:
            offset = 0
            while offset < len(content):
                offset += os.write(fd, content[offset:])
            os.fsync(fd)
            check_node(fd)
        finally:
            os.close(fd)


def process_start(pid):
    if not SNAPSHOT_FILES_SUPPORTED:
        return "unverifiable"
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None
    except (OSError, IndexError):
        return "unverifiable"


class HarnessMemoryStore:
    """The Harness strategy of NativeMemory; no separate user-visible memory."""
    def __init__(self, native):
        self.native, self.service = native, native.service
        self.db = self.service.db
        self.fs = SnapshotFiles(self.service.settings.data_dir)
        self.local = threading.local()

    @contextmanager
    def transaction(self):
        with self.db.lock:
            if self.db.conn.in_transaction:
                yield
            else:
                with self.db.transaction():
                    yield

    def authorized(self, user_id, cid, *, write=False, run_id=None):
        user = self.service.require("users", user_id)
        if not user["enabled"]:
            raise AppError(403, "USER_DISABLED", "账号已停用")
        conversation = self.service.require("conversations", cid, user)
        if conversation.get("runtime_type") != "harness":
            raise denied("MEMORY_RUNTIME_MISMATCH")
        if write or run_id:
            self.service.usable_assistant(conversation["assistant_id"], user)
        if run_id:
            run = self.service.require("runs", run_id, user)
            if (conversation["active_run_id"] != run_id or run["conversation_id"] != cid
                    or run["status"] not in {"queued", "running", "awaiting_approval"} or conversation["archived"]):
                raise denied("MEMORY_RUN_CHANGED", "任务已结束或不再允许访问记忆")
        return conversation

    def state(self, user_id, cid, run_id=None):
        conversation = self.authorized(user_id, cid, run_id=run_id)
        require_snapshot_support()
        row = self.db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (cid,))
        if not row or row["storage_version"] != 1 or not row["active_snapshot_id"]:
            self.initialize(user_id, cid)
            conversation = self.authorized(user_id, cid, run_id=run_id)
            row = self.db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (cid,))
        if row["mutation_id"]:
            op = self.db.one("SELECT run_id FROM memory_operations WHERE id=?", (row["mutation_id"],))
            if not run_id or not op or op["run_id"] != run_id:
                raise denied("MEMORY_BUSY", "记忆变更尚未完成，请稍后重试")
        return {"user_id": user_id, "conversation_id": cid, "revision": row["revision"], "digest": row["digest"],
                "snapshot_id": row["active_snapshot_id"], "manifest_digest": row["manifest_digest"],
                "native_epoch": conversation["native_epoch"]}

    def snapshot(self, user_id, cid, run_id=None):
        binding = self.state(user_id, cid, run_id)
        held = getattr(self.local, "verified", None)
        if held and held[0] == binding:
            held[1].assert_unchanged()
            return binding
        with self.fs.verify(binding) as tree:
            with self.transaction():
                if binding != self.state(user_id, cid, run_id):
                    raise denied("MEMORY_REVISION_CONFLICT")
                tree.assert_unchanged()
        return binding

    @contextmanager
    def verified(self, user_id, cid, run_id=None):
        binding = self.state(user_id, cid, run_id)
        previous = getattr(self.local, "verified", None)
        if previous and previous[0] == binding:
            previous[1].assert_unchanged()
            yield binding
            return
        with self.fs.verify(binding) as tree:
            self.local.verified = (binding, tree)
            try:
                # Callers do their final pointer/authorization CAS in their
                # own short transaction; no fallible checks after its commit.
                yield binding
            finally:
                self.local.verified = previous

    def _budget(self, amount, cid=None, snapshot=False):
        total = self.db.one("SELECT COALESCE(SUM(reserved_bytes),0) n FROM memory_snapshot_records")["n"]
        total += self.db.one("SELECT COALESCE(SUM(reserved_bytes),0) n FROM memory_operations")["n"]
        if total + amount > MAX_PROJECT or self.db.one("SELECT COUNT(*) n FROM memory_operations")["n"] >= 4096:
            raise denied("MEMORY_STORAGE_FULL", "记忆保留配额已满，请联系管理员；历史不会自动删除")
        if snapshot and self.db.one("SELECT COUNT(*) n FROM memory_snapshot_records WHERE conversation_id=?", (cid,))["n"] >= MAX_SNAPSHOTS:
            raise denied("MEMORY_STORAGE_FULL", "会话记忆快照保留配额已满，历史不会自动删除")
        if snapshot and self.db.one("SELECT COALESCE(SUM(reserved_bytes),0) n FROM memory_snapshot_records WHERE conversation_id=?", (cid,))["n"] + amount > 32 * MAX_TOTAL:
            raise denied("MEMORY_STORAGE_FULL", "会话记忆保留容量已满，历史不会自动删除")

    def _reserve_tree(self, binding, files):
        require_snapshot_support()
        check_files(files)
        amount = sum(map(len, files.values())) + MAX_MANIFEST
        with self.transaction():
            self._budget(amount, binding["conversation_id"], True)
            self.db.execute("INSERT INTO memory_snapshot_records(id,conversation_id,user_id,state,reserved_bytes,created_at) VALUES(?,?,?,'building',?,?)",
                            (binding["snapshot_id"], binding["conversation_id"], binding["user_id"], amount, now()))
        result = self.fs.build(binding, files)
        with self.transaction():
            self.db.execute("UPDATE memory_snapshot_records SET state='prepared',digest=?,manifest_digest=? WHERE id=?", (result["digest"], result["manifest_digest"], result["snapshot_id"]))
        return result

    def _operation(self, row):
        if row is None:
            return None
        return {**row, "operation_id": row["id"], "binding": json.loads(row["binding"]),
                "expected_revision": json.loads(row["binding"])["revision"],
                "result": json.loads(row["result"]) if row["result"] else None}

    def pending(self, run_id):
        return self._operation(self.db.one("SELECT * FROM memory_operations WHERE run_id=? ORDER BY rowid DESC LIMIT 1", (run_id,)))

    def _new_operation(self, binding, key, kind, path=None, content=None, run_id=None):
        require_snapshot_support()
        if type(key) is not str or not 1 <= len(key) <= 128 or any(ord(c) < 32 for c in key):
            raise AppError(422, "MEMORY_OPERATION_REQUIRED", "请提供稳定的记忆操作标识")
        if path is not None:
            name(path)
        data = content_bytes(content) if content is not None else b""
        request = hashlib.sha256(canonical({"binding": binding, "kind": kind, "path": path, "content": content, "run_id": run_id})).hexdigest()
        existing = self.db.one("SELECT * FROM memory_operations WHERE conversation_id=? AND operation_key=?", (binding["conversation_id"], key))
        if existing:
            if existing["request_digest"] != request:
                raise denied("MEMORY_OPERATION_CONFLICT", "该操作标识已用于不同的记忆请求")
            return self._operation(existing)
        if run_id and self.pending(run_id):
            raise denied("MEMORY_WRITE_LIMIT", "每轮仅允许一个记忆写入申请")
        # Retain raw proposal plus the escaped JSON copy in either the manual
        # result or model approval. Reserve bounded metadata separately. This
        # is a logical payload quota, not a claim about SQLite/WAL page size.
        amount = len(data) + len(canonical({"content": content})) + MAX_MANIFEST
        self._budget(amount)
        with absolute_dir(self.service.settings.data_dir) as directory:
            available = os.fstatvfs(directory)
            if available.f_bavail * available.f_frsize < MIN_FREE:
                raise denied("MEMORY_STORAGE_FULL")
        operation_id = hashlib.sha256((binding["conversation_id"] + "\0" + key).encode()).hexdigest()
        self.db.execute("INSERT INTO memory_operations(id,conversation_id,user_id,run_id,operation_key,request_digest,binding,path,content,kind,status,reserved_bytes,owner_pid,owner_start,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'proposed',?,?,?,?,?)",
                        (operation_id, binding["conversation_id"], binding["user_id"], run_id, key, request, dump(binding), path, content, kind, amount, os.getpid(), process_start(os.getpid()) or "unknown", now(), now()))
        return self._operation(self.db.one("SELECT * FROM memory_operations WHERE id=?", (operation_id,)))

    def _gate(self, op):
        row = self.db.one("SELECT mutation_id FROM conversation_memory_scopes WHERE conversation_id=?", (op["conversation_id"],))
        if row["mutation_id"] not in (None, op["id"]):
            raise denied("MEMORY_BUSY")
        self.db.execute("UPDATE conversation_memory_scopes SET mutation_id=? WHERE conversation_id=?", (op["id"], op["conversation_id"]))

    def initialize(self, user_id, cid):
        conversation = self.authorized(user_id, cid)
        require_snapshot_support()
        with self.transaction():
            row = self.db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (cid,))
            if row and row["storage_version"] == 1:
                return
            if conversation["active_run_id"] or (self.service.manager and cid in self.service.manager.context_requests):
                raise denied("MEMORY_MIGRATION_REQUIRED", "请在会话空闲时初始化记忆快照")
            if not row:
                self.native.initialize_empty(cid)
                row = self.db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (cid,))
            if row["mutation_id"]:
                raise denied("MEMORY_BUSY")
            origin = "legacy_snapshot" if row["origin"] == "legacy_pending" else row["origin"]
            base = {"user_id": user_id, "conversation_id": cid, "revision": row["revision"], "digest": row["digest"],
                    "snapshot_id": None, "manifest_digest": None, "native_epoch": conversation["native_epoch"]}
            op = self._new_operation(base, "migration-" + uid(), "migration")
            self._gate(op)
        try:
            source = self.native._root("conversation-memory", user_id, cid)
            if not source.exists() and row["origin"] == "legacy_pending":
                source = self.native.legacy_root(user_id, conversation["assistant_id"])
            files = {}
            if source.exists() or source.is_symlink():
                with VerifiedTree(source, capacity_errors=True) as original:
                    files = dict(original.files)
                    built = self._reserve_tree({**base, "snapshot_id": uid()}, files)
                    original.assert_unchanged()
            else:
                built = self._reserve_tree({**base, "snapshot_id": uid()}, files)
            with self.fs.verify(built) as tree:
                with self.transaction():
                    current = self.authorized(user_id, cid)
                    active = self.db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (cid,))
                    if active["mutation_id"] != op["id"] or active["storage_version"] != 0 or current["active_run_id"]:
                        raise denied("MEMORY_REVISION_CONFLICT")
                    tree.assert_unchanged()
                    old_history = bool(current["harness_session_id"] or self.db.one("SELECT id FROM runs WHERE conversation_id=? LIMIT 1", (cid,)))
                    epoch = current["native_epoch"] + int(old_history)
                    self.db.execute("UPDATE conversation_memory_scopes SET storage_version=1,active_snapshot_id=?,manifest_digest=?,digest=?,origin=?,mutation_id=NULL,updated_at=? WHERE conversation_id=?", (built["snapshot_id"], built["manifest_digest"], built["digest"], origin, now(), cid))
                    self.db.execute("UPDATE conversations SET native_epoch=?,harness_session_id=NULL,context_usage=NULL WHERE id=?", (epoch, cid))
                    self.db.execute("UPDATE memory_snapshot_records SET state='published' WHERE id=?", (built["snapshot_id"],))
                    self.db.execute("UPDATE memory_operations SET status='committed',snapshot_id=?,result=?,updated_at=? WHERE id=?", (built["snapshot_id"], dump({"revision": row["revision"], "native_epoch": epoch, "context_reset": old_history}), now(), op["id"]))
        except BaseException:
            self.abort_operation(op["id"], "initialization_failed")
            raise

    def assert_binding(self, binding, run_id=None):
        if type(binding) is not dict or binding != self.state(binding.get("user_id"), binding.get("conversation_id"), run_id):
            raise denied("MEMORY_REVISION_CONFLICT", "记忆版本已改变，请重新开始任务")

    def read(self, binding, path=None, expected_revision=None, run_id=None):
        self.assert_binding(binding, run_id)
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision != binding["revision"]):
            raise denied("MEMORY_REVISION_CONFLICT")
        if path is not None:
            name(path)
        with self.fs.verify(binding) as tree:
            with self.transaction():
                self.assert_binding(binding, run_id)
                tree.assert_unchanged()
                if path is not None:
                    if path not in tree.files:
                        raise AppError(404, "MEMORY_FILE_NOT_FOUND", "记忆文件不存在")
                    data = tree.files[path]
                    return {"path": path, "content": data.decode("utf-8"), "revision": binding["revision"],
                            "sha256": hashlib.sha256(data).hexdigest(), "native_epoch": binding["native_epoch"],
                            "updated_at": datetime.fromtimestamp(tree.infos[path].st_mtime, timezone.utc).isoformat()}
                return {"revision": binding["revision"], "native_epoch": binding["native_epoch"],
                        "files": [{"path": p, "size": len(v), "updated_at": datetime.fromtimestamp(tree.infos[p].st_mtime, timezone.utc).isoformat()} for p, v in sorted(tree.files.items())]}

    def stage(self, binding, run_id, operation_id, path, content, expected_revision):
        name(path)
        data = content_bytes(content)
        if type(expected_revision) is not int or binding["revision"] != expected_revision:
            raise denied("MEMORY_REVISION_CONFLICT")
        self.assert_binding(binding, run_id)
        with self.fs.verify(binding) as tree:
            candidate = {**tree.files, path: data}
            check_files(candidate)
            with self.transaction():
                self.assert_binding(binding, run_id)
                tree.assert_unchanged()
                op = self._new_operation(binding, operation_id, "upsert", path, content, run_id)
                old_digest = hashlib.sha256(tree.files[path]).hexdigest() if path in tree.files else None
                self.db.execute("UPDATE memory_operations SET old_content_sha256=? WHERE id=?", (old_digest, op["id"]))
                return {**op, "old_content_sha256": old_digest}

    def _approval(self, op, approval_id, policy_binding):
        if type(policy_binding) is not str or len(policy_binding.encode("utf-8")) > 8192:
            raise denied("MEMORY_APPROVAL_INVALID")
        approval = self.service.require("approvals", approval_id)
        expected = {"path": op["path"], "content": op["content"], "expected_revision": op["binding"]["revision"],
                    "operation_id": op["id"], "native_epoch": op["binding"]["native_epoch"], "policy_binding": policy_binding}
        if (approval["run_id"] != op["run_id"] or approval["user_id"] != op["user_id"] or approval["status"] != "allowed"
                or approval.get("decision_source") != "user" or approval.get("decided_by") != op["user_id"]
                or json.loads(approval["arguments"]) != expected
                or datetime.fromisoformat(approval["expires_at"]) <= datetime.now(timezone.utc)):
            raise denied("MEMORY_APPROVAL_INVALID", "记忆写入缺少有效人工批准")
        return hashlib.sha256(canonical({"arguments": expected, "approval_id": approval_id, "policy_binding": policy_binding,
                                         "decided_at": approval.get("decided_at"), "decided_by": approval.get("decided_by")})).hexdigest()

    def approve(self, operation_id, approval_id, policy_binding):
        with self.transaction():
            op = self._operation(self.db.one("SELECT * FROM memory_operations WHERE id=?", (operation_id,)))
            if not op or op["status"] not in {"proposed", "approved_pending_commit"}:
                raise denied("MEMORY_OPERATION_CONFLICT")
            self.assert_binding(op["binding"], op["run_id"])
            proof = self._approval(op, approval_id, policy_binding)
            if op["approval_digest"] and op["approval_digest"] != proof:
                raise denied("MEMORY_APPROVAL_INVALID")
            self.db.execute("UPDATE memory_operations SET status='approved_pending_commit',approval_id=?,policy_binding=?,approval_digest=?,updated_at=? WHERE id=?", (approval_id, policy_binding, proof, now(), operation_id))
        return self.pending(op["run_id"])

    def _prepare(self, op):
        binding = op["binding"]
        with self.fs.verify(binding) as tree:
            candidate = dict(tree.files)
            if op["kind"] == "clear":
                candidate = {}
            elif op["kind"] == "delete":
                if op["path"] not in candidate:
                    raise AppError(404, "MEMORY_FILE_NOT_FOUND", "记忆文件不存在")
                del candidate[op["path"]]
            else:
                candidate[op["path"]] = content_bytes(op["content"])
            changed = digest(candidate) != binding["digest"]
            if changed and op["reserved_bytes"] + sum(map(len, candidate.values())) + MAX_MANIFEST > 2 * MAX_TOTAL:
                raise denied("MEMORY_STORAGE_FULL", "记忆候选与构建树超过暂存预算")
            built = self._reserve_tree({**binding, "snapshot_id": uid()}, candidate) if changed else binding
            tree.assert_unchanged()
        return built, changed

    def _publish(self, op, built, changed, authorize=None, finalize=None):
        base = op["binding"]
        with self.fs.verify(base) as old, self.fs.verify(built) as new:
            self.local.verified = (base, old)
            try:
                with self.transaction():
                    self.authorized(op["user_id"], op["conversation_id"], write=True, run_id=op["run_id"])
                    current = self.db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (op["conversation_id"],))
                    actual = self.db.one("SELECT * FROM memory_operations WHERE id=?", (op["id"],))
                    conv = self.service.require("conversations", op["conversation_id"])
                    if (current["mutation_id"] != op["id"] or current["active_snapshot_id"] != base["snapshot_id"]
                            or current["revision"] != base["revision"] or current["digest"] != base["digest"]
                            or conv["native_epoch"] != base["native_epoch"] or actual["status"] != "prepared"):
                        raise denied("MEMORY_REVISION_CONFLICT")
                    for key in ("request_digest", "path", "content", "kind", "approval_id", "policy_binding", "approval_digest"):
                        if actual[key] != op[key]:
                            raise denied("MEMORY_OPERATION_CONFLICT")
                    if json.loads(actual["binding"]) != base:
                        raise denied("MEMORY_OPERATION_CONFLICT")
                    if op["run_id"]:
                        if self._approval(op, op["approval_id"], op["policy_binding"]) != op["approval_digest"]:
                            raise denied("MEMORY_APPROVAL_INVALID")
                        if authorize is None or finalize is None:
                            raise denied("MEMORY_COMMIT_DENIED")
                        if authorize() is False:
                            raise denied("MEMORY_COMMIT_DENIED")
                    elif conv["active_run_id"] or (self.service.manager and op["conversation_id"] in self.service.manager.context_requests):
                        raise denied("MEMORY_BUSY")
                    old.assert_unchanged()
                    new.assert_unchanged()
                    reset = changed or op["kind"] == "clear"
                    result = {"operation_id": op["operation_key"], "mutation_status": "committed" if reset else "unchanged",
                              "revision": base["revision"] + int(changed), "native_epoch": base["native_epoch"] + int(reset), "context_reset": reset}
                    if not op["run_id"]:
                        origin = "legacy_snapshot" if current["origin"] == "legacy_pending" else current["origin"]
                        result.update(conversation_id=op["conversation_id"], assistant_id=conv["assistant_id"], scope="conversation",
                                      origin=origin, read_only=False, busy=False, max_file_bytes=MAX_FILE, max_total_bytes=MAX_TOTAL,
                                      files=[{"path": p, "size": len(v), "updated_at": datetime.fromtimestamp(new.infos[p].st_mtime, timezone.utc).isoformat()} for p, v in sorted(new.files.items())])
                        if op["content"] is not None:
                            result.update(path=op["path"], content=op["content"], sha256=hashlib.sha256(content_bytes(op["content"])).hexdigest(), updated_at=now())
                    self.db.execute("UPDATE conversation_memory_scopes SET active_snapshot_id=?,manifest_digest=?,revision=?,digest=?,mutation_id=NULL,updated_at=? WHERE conversation_id=?", (built["snapshot_id"], built["manifest_digest"], result["revision"], built["digest"], now(), op["conversation_id"]))
                    if reset:
                        self.db.execute("UPDATE conversations SET native_epoch=?,harness_session_id=NULL,context_usage=NULL WHERE id=?", (result["native_epoch"], op["conversation_id"]))
                    self.db.execute("UPDATE memory_snapshot_records SET state='published' WHERE id=?", (built["snapshot_id"],))
                    self.db.execute("UPDATE memory_operations SET status='committed',result=?,updated_at=? WHERE id=?", (dump(result), now(), op["id"]))
                    if finalize:
                        finalize(result)
                        terminal = self.service.require("runs", op["run_id"])
                        owner = self.service.require("conversations", op["conversation_id"])
                        if terminal["status"] != "succeeded" or owner["active_run_id"] is not None:
                            raise denied("MEMORY_COMMIT_DENIED", "记忆提交未能与任务成功终态共同完成")
                    return result
            finally:
                self.local.verified = None

    def commit(self, run_id, *, authorize, finalize):
        op = self.pending(run_id)
        if not op or op["status"] != "approved_pending_commit":
            raise denied("MEMORY_COMMIT_DENIED")
        with self.transaction():
            self.assert_binding(op["binding"], run_id)
            self._gate(op)
            self.db.execute("UPDATE memory_operations SET status='preparing',owner_pid=?,owner_start=?,updated_at=? WHERE id=?", (os.getpid(), process_start(os.getpid()) or "unknown", now(), op["id"]))
        try:
            built, changed = self._prepare(op)
            with self.transaction():
                self.db.execute("UPDATE memory_operations SET status='prepared',snapshot_id=? WHERE id=? AND status='preparing'", (built["snapshot_id"], op["id"]))
            return self._publish(op, built, changed, authorize, finalize)
        except BaseException:
            self.abort_operation(op["id"], "commit_failed")
            raise

    def edit(self, user, cid, revision, path, content, operation_id):
        self.authorized(user["id"], cid, write=True)
        # Replays are authorized but compared against their original base, not
        # today's pointer. A lost response must not advance the epoch twice.
        previous = self.db.one("SELECT * FROM memory_operations WHERE conversation_id=? AND operation_key=?", (cid, operation_id))
        kind = "clear" if path is None else ("delete" if content is None else "upsert")
        binding = json.loads(previous["binding"]) if previous else self.snapshot(user["id"], cid)
        if type(revision) is not int or revision != binding["revision"]:
            raise denied("MEMORY_REVISION_CONFLICT")
        with self.transaction():
            self.authorized(user["id"], cid, write=True)
            op = self._new_operation(binding, operation_id, kind, path, content)
            if op["status"] == "committed":
                return op["result"]
            if previous or self.native.busy(user["id"], cid):
                raise denied("MEMORY_BUSY")
            self.assert_binding(binding)
            self._gate(op)
            self.db.execute("UPDATE memory_operations SET status='preparing' WHERE id=?", (op["id"],))
        try:
            built, changed = self._prepare(op)
            with self.transaction():
                self.db.execute("UPDATE memory_operations SET status='prepared',snapshot_id=? WHERE id=? AND status='preparing'", (built["snapshot_id"], op["id"]))
            return self._publish(op, built, changed)
        except BaseException:
            self.abort_operation(op["id"], "edit_failed")
            raise

    def abort_operation(self, operation_id, reason):
        with self.transaction():
            row = self.db.one("SELECT * FROM memory_operations WHERE id=?", (operation_id,))
            if not row or row["status"] == "committed":
                return
            self.db.execute("UPDATE memory_operations SET status='aborted',reason=?,updated_at=? WHERE id=?", (reason, now(), operation_id))
            self.db.execute("UPDATE conversation_memory_scopes SET mutation_id=NULL WHERE conversation_id=? AND mutation_id=?", (row["conversation_id"], operation_id))

    def discard(self, run_id, reason="aborted"):
        op = self.pending(run_id)
        if op:
            self.abort_operation(op["id"], reason)

    def recover(self):
        # No filesystem cleanup or adoption. Uncertain live owners remain
        # blocked, and all orphan/candidate allocations continue to count.
        for row in self.db.all("SELECT * FROM memory_operations WHERE status NOT IN ('committed','aborted')"):
            run = self.db.one("SELECT status FROM runs WHERE id=?", (row["run_id"],)) if row["run_id"] else None
            live_start = process_start(row["owner_pid"])
            gone = row["owner_start"] not in {"unknown", "unverifiable"} and live_start != "unverifiable" and live_start != row["owner_start"]
            terminal = run and run["status"] in {"failed", "cancelled", "interrupted"}
            if gone or terminal:
                self.abort_operation(row["id"], "recovered_uncommitted")
