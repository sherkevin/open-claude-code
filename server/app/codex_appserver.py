"""Default-off owner-bound host for the pinned Codex App Server stdio contract.

This module is a thin product adapter, not a Codex rewrite and not a generic
proxy.  It speaks the native JSON-RPC ``app-server --stdio`` protocol that the
fixed Codex 0.165.0 schema baseline documents, starts one short-lived server
process per admitted operation, and binds each server thread to exactly one
enterprise conversation.  It never selects a model, never sends a model turn by
itself, never reads the caller's Codex configuration or credentials, and never
grants a server request.  Tool approvals remain outside this slice: every
server request (known approval or unknown method) is answered with a JSON-RPC
error and recorded.

A turn is admitted only through an explicit owner request.  The model turn and
its real provider authorization are out of scope for this slice; the host uses
the fixed ``read-only`` sandbox and ``never`` approval policy for the thread.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import signal
import stat
import subprocess
import threading
import time

BINDING_SCHEMA = "kaiwu-codex-appserver-binding/v1"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
MAX_STATE_BYTES = 1048576
MAX_FRAME_CHARS = 1048576
MAX_TURN_NOTIFICATIONS = 8192
RECENT_REQUESTS = 8
MAX_ADMITTED_REQUESTS = 1024
RECENT_TEXT_CHARS = 4096

APPROVAL_METHODS = frozenset({
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
})
THREAD_SCOPED_NOTIFICATIONS = frozenset({
    "thread/started", "thread/status/changed",
})
TURN_SCOPED_NOTIFICATIONS = frozenset({
    "turn/started", "turn/completed", "item/agentMessage/delta",
})
TERMINAL_TURN_STATUSES = frozenset({
    "completed", "failed", "interrupted", "cancelled", "canceled",
})


class CodexHostError(Exception):
    pass


class CodexHostDisabled(CodexHostError):
    pass


class CodexHostUnavailable(CodexHostError):
    pass


class CodexHostRejected(CodexHostError):
    pass


class CodexHostBusy(CodexHostError):
    pass


class CodexHostConflict(CodexHostError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: str | bytes) -> str:
    data = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _valid_id(value) -> bool:
    return isinstance(value, str) and bool(SAFE_ID.fullmatch(value))


def _same_path(value, workspace: Path) -> bool:
    if not isinstance(value, str):
        return False
    if value == str(workspace):
        return True
    try:
        return os.path.realpath(value) == os.path.realpath(workspace)
    except OSError:
        return False


@dataclass(frozen=True)
class CodexHostConfig:
    """Server-owned launch and budget settings; never accepted from a model."""

    enabled: bool = False
    command: tuple[str, ...] = ()
    data_dir: Path | str = ""
    expected_executable_sha256: str = ""
    response_timeout_seconds: float = 30.0
    turn_timeout_seconds: float = 300.0
    reconcile_timeout_seconds: float = 10.0
    max_input_chars: int = 20000
    max_output_chars: int = 131072
    max_receipt_chars: int = 65536
    max_buffered_notifications: int = 4096
    client_name: str = "kaiwu_codex_host"
    client_version: str = "0.1.0"

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("invalid Codex host enabled flag")
        if not isinstance(self.data_dir, (str, Path)) or not str(self.data_dir):
            raise ValueError("Codex host needs a data directory")
        data_dir = Path(self.data_dir)
        if not data_dir.is_absolute():
            raise ValueError("Codex host data directory must be absolute")
        object.__setattr__(self, "data_dir", data_dir)
        if not isinstance(self.command, tuple) or any(
                not isinstance(item, str) or not item or "\x00" in item for item in self.command):
            raise ValueError("invalid Codex host command")
        if self.enabled:
            if not self.command:
                raise ValueError("enabled Codex host needs a command")
            if not os.path.isabs(self.command[0]):
                raise ValueError("Codex host executable must be an absolute path")
        if self.expected_executable_sha256:
            if not SHA256_HEX.fullmatch(self.expected_executable_sha256):
                raise ValueError("Codex host executable digest must be lowercase SHA-256")
        for name, value, low, high in (
                ("response_timeout_seconds", self.response_timeout_seconds, 0.1, 600),
                ("turn_timeout_seconds", self.turn_timeout_seconds, 0.1, 3600),
                ("reconcile_timeout_seconds", self.reconcile_timeout_seconds, 0.1, 600),
                ("max_input_chars", self.max_input_chars, 1, 131072),
                ("max_output_chars", self.max_output_chars, 1024, 1048576),
                ("max_receipt_chars", self.max_receipt_chars, 1024, 200000),
                ("max_buffered_notifications", self.max_buffered_notifications, 16, 65536),
        ):
            if type(value) not in {int, float} or not low <= value <= high:
                raise ValueError(f"invalid Codex host {name}")
        if not isinstance(self.client_name, str) or not self.client_name:
            raise ValueError("invalid Codex host client name")
        if not isinstance(self.client_version, str) or not self.client_version:
            raise ValueError("invalid Codex host client version")

    @classmethod
    def from_settings(cls, settings) -> "CodexHostConfig":
        binary = str(getattr(settings, "codex_appserver_binary", "") or "").strip()
        command = (binary, "app-server", "--stdio") if binary else ()
        return cls(
            enabled=bool(getattr(settings, "codex_appserver_enabled", False)),
            command=command,
            data_dir=settings.data_dir,
            expected_executable_sha256=str(
                getattr(settings, "codex_appserver_binary_sha256", "") or ""),
            response_timeout_seconds=getattr(
                settings, "codex_appserver_response_timeout_seconds", 30.0),
            turn_timeout_seconds=getattr(
                settings, "codex_appserver_turn_timeout_seconds", 300.0),
            max_input_chars=getattr(settings, "codex_appserver_max_input_chars", 20000),
            max_output_chars=getattr(settings, "codex_appserver_max_output_chars", 131072),
        )


@dataclass(frozen=True)
class _Layout:
    root: Path
    state: Path
    workspaces: Path
    home: Path
    codex_home: Path
    tmp: Path
    cache: Path
    config: Path
    data: Path


class _Session:
    """One isolated app-server process with a bounded, identity-checked reader."""

    def __init__(self, config: CodexHostConfig, workspace: Path, environment: dict[str, str]):
        self.config = config
        self.workspace = workspace
        self._pending: dict[object, queue.Queue] = {}
        self._pending_guard = threading.Lock()
        self._write_guard = threading.Lock()
        self._next_id = 0
        self.events: queue.Queue = queue.Queue(maxsize=config.max_buffered_notifications)
        self._overflow = False
        self._ended = threading.Event()
        self._failure: str | None = None
        self._closed = False
        self._close_guard = threading.Lock()
        self._rejections = 0
        self._rejection_guard = threading.Lock()
        try:
            self.process = subprocess.Popen(
                list(config.command),
                cwd=str(workspace),
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="strict",
                bufsize=1,
                close_fds=True,
                start_new_session=True,
            )
        except OSError:
            raise CodexHostUnavailable("app-server could not be started") from None
        if not self.process.stdin or not self.process.stdout:
            self.close()
            raise CodexHostUnavailable("app-server stdio unavailable")
        self._reader = threading.Thread(target=self._read, name="codex-appserver-reader",
                                        daemon=True)
        self._reader.start()

    @property
    def rejected_server_requests(self) -> int:
        with self._rejection_guard:
            return self._rejections

    def _fail(self, reason: str) -> None:
        if self._failure is None:
            self._failure = reason
        self._ended.set()
        with self._pending_guard:
            waiters = list(self._pending.values())
            self._pending.clear()
        for waiter in waiters:
            try:
                waiter.put_nowait(None)
            except queue.Full:
                # A real response already won the race; the caller will use it.
                pass

    def _write(self, value: dict) -> None:
        if self._closed or self._ended.is_set() or self._failure is not None:
            raise CodexHostUnavailable("app-server is not writable")
        line = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with self._write_guard:
                self.process.stdin.write(line)
                self.process.stdin.flush()
        except (OSError, ValueError):
            self._fail("write_failed")
            raise CodexHostUnavailable("app-server write failed") from None

    def _reject_server_request(self, message: dict) -> None:
        request_id = message.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
            self._fail("invalid_server_request")
            return
        method = message.get("method")
        # Both known approval requests and unknown methods are refused.  This
        # host slice has no approved command, file-change or permission
        # capability, so an error response is the only safe answer.
        text = ("host approval unavailable" if method in APPROVAL_METHODS
                else "unknown server request rejected")
        with self._rejection_guard:
            self._rejections += 1
        try:
            self._write({"id": request_id, "error": {"code": -32601, "message": text}})
        except CodexHostError:
            pass

    def _note_notification(self, message: dict) -> None:
        method = message.get("method")
        if not isinstance(method, str) or not method:
            self._fail("invalid_notification")
            return
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            self._fail("invalid_notification")
            return
        thread_id = params.get("threadId")
        if thread_id is not None and not isinstance(thread_id, str):
            self._fail("invalid_notification")
            return
        record: dict[str, object] = {"method": method, "threadId": thread_id}
        if method == "item/agentMessage/delta":
            turn_id = params.get("turnId")
            delta = params.get("delta", params.get("text"))
            if not isinstance(turn_id, str) or not isinstance(delta, str):
                self._fail("invalid_notification")
                return
            record.update(turnId=turn_id, delta=delta)
        elif method in {"turn/started", "turn/completed"}:
            turn = params.get("turn")
            if not isinstance(turn, dict):
                self._fail("invalid_notification")
                return
            turn_id = turn.get("id")
            status = turn.get("status")
            if not isinstance(turn_id, str) or (status is not None and not isinstance(status, str)):
                self._fail("invalid_notification")
                return
            record["turn"] = {"id": turn_id, "status": status}
        elif method == "thread/started":
            thread = params.get("thread")
            if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                self._fail("invalid_notification")
                return
            if thread_id is not None and thread_id != thread["id"]:
                self._fail("invalid_notification")
                return
            record["threadId"] = thread["id"]
        # Other notification payloads are intentionally not retained (command
        # output and file content never enter host memory or receipts).
        try:
            self.events.put_nowait(record)
        except queue.Full:
            self._overflow = True

    def _read(self) -> None:
        try:
            for line in self.process.stdout:
                if len(line) > MAX_FRAME_CHARS:
                    self._fail("oversized_frame")
                    return
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    self._fail("malformed_frame")
                    return
                if not isinstance(message, dict):
                    self._fail("invalid_frame")
                    return
                if "method" in message:
                    if not isinstance(message.get("method"), str) or not message["method"]:
                        self._fail("invalid_frame")
                        return
                    if "id" in message:
                        self._reject_server_request(message)
                    else:
                        self._note_notification(message)
                elif "id" in message:
                    with self._pending_guard:
                        waiter = self._pending.pop(message.get("id"), None)
                    if waiter is None:
                        self._fail("unexpected_response")
                        return
                    try:
                        waiter.put_nowait(message)
                    except queue.Full:
                        # A concurrent failure already woke the caller.
                        self._fail("duplicate_response")
                        return
                else:
                    self._fail("invalid_frame")
                    return
        except Exception:
            # A reader must never die silently: fail closed so every waiting
            # request and the active turn observe an unavailable host.
            self._fail("reader_failed")
        finally:
            self._ended.set()
            with self._pending_guard:
                waiters = list(self._pending.values())
                self._pending.clear()
            for waiter in waiters:
                try:
                    waiter.put_nowait(None)
                except queue.Full:
                    pass

    def request(self, method: str, params: dict, timeout: float) -> dict:
        if self._ended.is_set() or self._failure is not None or self._closed:
            raise CodexHostUnavailable("app-server is unavailable")
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_guard:
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = waiter
        try:
            self._write({"id": request_id, "method": method, "params": params})
            try:
                message = waiter.get(timeout=timeout)
            except queue.Empty:
                raise CodexHostUnavailable(f"{method} timed out") from None
        finally:
            with self._pending_guard:
                self._pending.pop(request_id, None)
        if message is None:
            raise CodexHostUnavailable(f"{method} ended")
        if "error" in message:
            raise CodexHostRejected(f"{method} rejected")
        result = message.get("result")
        if result is None:
            result = {}
        if not isinstance(result, dict):
            raise CodexHostRejected(f"{method} returned an invalid result")
        return result

    def notify(self, method: str, params: dict | None = None) -> None:
        value = {"method": method}
        if params is not None:
            value["params"] = params
        self._write(value)

    def initialize(self) -> None:
        self.request("initialize", {
            "clientInfo": {"name": self.config.client_name,
                           "version": self.config.client_version},
        }, self.config.response_timeout_seconds)
        self.notify("initialized")

    @staticmethod
    def _thread_of(result: dict, workspace: Path, *, require_cwd: bool) -> str:
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise CodexHostRejected("thread identity missing")
        thread_id = thread.get("id")
        if not _valid_id(thread_id):
            raise CodexHostRejected("thread identity invalid")
        cwd = thread.get("cwd")
        if cwd is not None and not _same_path(cwd, workspace):
            raise CodexHostRejected("thread workspace mismatch")
        if require_cwd and cwd is None:
            raise CodexHostRejected("thread workspace missing")
        return thread_id

    def start_thread(self, workspace: Path) -> str:
        result = self.request("thread/start", {
            "cwd": str(workspace),
            "sandbox": "read-only",
            "approvalPolicy": "never",
            "ephemeral": False,
        }, self.config.response_timeout_seconds)
        thread_id = self._thread_of(result, workspace, require_cwd=True)
        # The native server materializes a new, empty thread on the first
        # metadata write. It must be resumable after this short-lived process
        # closes, before we advertise a durable binding to the caller.
        self.request("thread/name/set", {"threadId": thread_id, "name": "开物会话"},
                     self.config.response_timeout_seconds)
        return thread_id

    def resume_thread(self, thread_id: str, workspace: Path) -> None:
        result = self.request("thread/resume", {
            "threadId": thread_id,
            "cwd": str(workspace),
            "sandbox": "read-only",
            "approvalPolicy": "never",
        }, self.config.response_timeout_seconds)
        if self._thread_of(result, workspace, require_cwd=False) != thread_id:
            raise CodexHostRejected("resumed thread identity mismatch")

    def read_thread(self, thread_id: str, include_turns: bool = True) -> dict:
        result = self.request("thread/read", {
            "threadId": thread_id,
            "includeTurns": include_turns,
        }, self.config.response_timeout_seconds)
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise CodexHostRejected("thread read identity mismatch")
        return thread

    def start_turn(self, thread_id: str, text: str) -> str:
        result = self.request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
            "approvalPolicy": "never",
        }, self.config.response_timeout_seconds)
        turn = result.get("turn")
        if not isinstance(turn, dict) or not _valid_id(turn.get("id")):
            raise CodexHostRejected("turn identity missing")
        return turn["id"]

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self.request("turn/interrupt", {
            "threadId": thread_id,
            "turnId": turn_id,
        }, self.config.response_timeout_seconds)

    def wait_turn(self, thread_id: str, turn_id: str, *, first_output=None) -> dict:
        """Wait for the exact bound turn; never forward another identity."""
        deadline = time.monotonic() + self.config.turn_timeout_seconds
        text: list[str] = []
        size = 0
        events = 0
        while True:
            if self._failure is not None:
                raise CodexHostUnavailable("app-server failed during the turn")
            if self._overflow:
                raise CodexHostUnavailable("app-server notification buffer overflow")
            if self._ended.is_set() and self.events.empty():
                raise CodexHostUnavailable("app-server ended during the turn")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexHostUnavailable("turn timed out")
            try:
                record = self.events.get(timeout=remaining)
            except queue.Empty:
                raise CodexHostUnavailable("turn timed out") from None
            events += 1
            if events > MAX_TURN_NOTIFICATIONS:
                raise CodexHostUnavailable("too many turn notifications")
            method = record["method"]
            seen_thread = record.get("threadId")
            if method in THREAD_SCOPED_NOTIFICATIONS:
                if seen_thread != thread_id:
                    raise CodexHostUnavailable("notification thread identity mismatch")
            elif method in TURN_SCOPED_NOTIFICATIONS:
                # The exact turn id below is the binding; a present thread id
                # must still match.  Some server revisions omit threadId on
                # turn events, so its absence alone is not a mismatch.
                if seen_thread is not None and seen_thread != thread_id:
                    raise CodexHostUnavailable("notification thread identity mismatch")
            elif seen_thread is not None and seen_thread != thread_id:
                raise CodexHostUnavailable("notification thread identity mismatch")
            if method == "item/agentMessage/delta":
                if record.get("turnId") != turn_id:
                    raise CodexHostUnavailable("notification turn identity mismatch")
                delta = record["delta"]
                size += len(delta)
                if size > self.config.max_output_chars:
                    raise CodexHostUnavailable("turn output exceeds the host budget")
                text.append(delta)
                if first_output is not None and delta:
                    first_output(delta[:1024])
                    first_output = None
            elif method == "turn/started":
                if record["turn"]["id"] != turn_id:
                    raise CodexHostUnavailable("turn identity mismatch")
            elif method == "turn/completed":
                if record["turn"]["id"] != turn_id:
                    raise CodexHostUnavailable("turn identity mismatch")
                status = str(record["turn"].get("status") or "").lower()
                if status not in TERMINAL_TURN_STATUSES:
                    raise CodexHostUnavailable("turn completion status invalid")
                return {"status": status, "text": "".join(text), "events": events,
                        "rejected_server_requests": self.rejected_server_requests}

    def close(self) -> None:
        with self._close_guard:
            if self._closed:
                return
            self._closed = True
        process = getattr(self, "process", None)
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if process.poll() is None:
            try:
                if os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        reader = getattr(self, "_reader", None)
        if reader is not None:
            reader.join(timeout=5)

    def __enter__(self) -> "_Session":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class CodexHost:
    """Owner-bound Thread/Turn lifecycle over the pinned stdio contract.

    One host instance is intended per product server process.  Cross-process
    turn execution is fenced with a per-binding ``flock``; a second process
    receives ``CodexHostBusy`` instead of racing.  Cancellation only reaches a
    turn owned by this process; after a restart the durable active-turn marker
    forces an explicit ``reconcile`` and never replays the original input.
    """

    def __init__(self, config: CodexHostConfig):
        if type(config) is not CodexHostConfig:
            raise TypeError("invalid Codex host config")
        self.config = config
        self._guard = threading.Lock()
        self._sessions: dict[str, _Session] = {}

    # ---- configuration and private paths ---------------------------------

    def require_enabled(self) -> None:
        if not self.config.enabled:
            raise CodexHostDisabled("Codex app-server host is disabled")

    def _layout(self, create: bool = True) -> _Layout:
        root = self.config.data_dir / "codex-appserver"
        layout = _Layout(
            root=root,
            state=root / "state",
            workspaces=root / "workspaces",
            home=root / "home",
            codex_home=root / "codex-home",
            tmp=root / "tmp",
            cache=root / "cache",
            config=root / "config",
            data=root / "data",
        )
        if create:
            self._ensure_dir(root, root.parent)
            for path in (layout.state, layout.workspaces, layout.home, layout.codex_home,
                         layout.tmp, layout.cache, layout.config, layout.data):
                self._ensure_dir(path, root)
        else:
            self._check_existing_dir(root, root.parent)
        return layout

    @staticmethod
    def _check_existing_dir(path: Path, parent: Path) -> bool:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return False
        except OSError:
            raise CodexHostUnavailable("app-server directory unavailable") from None
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise CodexHostUnavailable("app-server directory identity invalid")
        real = os.path.realpath(path)
        parent_real = os.path.realpath(parent)
        if real != parent_real and os.path.commonpath([real, parent_real]) != parent_real:
            raise CodexHostUnavailable("app-server directory escaped its root")
        return True

    def _ensure_dir(self, path: Path, parent: Path) -> None:
        if not self._check_existing_dir(path, parent):
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                pass
            except OSError:
                raise CodexHostUnavailable("app-server directory could not be created") from None
            try:
                os.chmod(path, 0o700)
            except OSError:
                raise CodexHostUnavailable("app-server directory could not be secured") from None
            if not self._check_existing_dir(path, parent):
                raise CodexHostUnavailable("app-server directory was not created")

    @staticmethod
    def _key(user_id: str, conversation_id: str) -> str:
        if not _valid_id(user_id) or not _valid_id(conversation_id):
            raise CodexHostRejected("invalid owner identity")
        return hashlib.sha256(
            f"{user_id}\x00{conversation_id}".encode("utf-8")).hexdigest()

    def _lock_path(self, key: str, suffix: str) -> Path:
        return (self.config.data_dir / "codex-appserver" / "state"
                / f"{key}.{suffix}.lock")

    def _workspace_path(self, user_id: str, conversation_id: str) -> Path:
        return (self.config.data_dir / "codex-appserver" / "workspaces"
                / _digest(user_id) / _digest(conversation_id))

    def _workspace(self, user_id: str, conversation_id: str) -> Path:
        layout = self._layout(create=True)
        owner = layout.workspaces / _digest(user_id)
        target = owner / _digest(conversation_id)
        self._ensure_dir(owner, layout.workspaces)
        self._ensure_dir(target, owner)
        return target

    def _environment(self) -> dict[str, str]:
        layout = self._layout(create=True)
        # Deliberately not os.environ.copy(): no provider, proxy, telemetry or
        # real Codex credential can leak from the product server.
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": str(layout.home),
            "CODEX_HOME": str(layout.codex_home),
            "TMPDIR": str(layout.tmp),
            "XDG_CACHE_HOME": str(layout.cache),
            "XDG_CONFIG_HOME": str(layout.config),
            "XDG_DATA_HOME": str(layout.data),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "NO_COLOR": "1",
        }

    def _verify_executable(self) -> None:
        expected = self.config.expected_executable_sha256
        if not expected:
            return
        try:
            # The path is server-owned.  Resolve a deployment symlink once,
            # then pin the content with O_NOFOLLOW on the final regular file.
            target = os.path.realpath(self.config.command[0])
            descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            raise CodexHostUnavailable("pinned app-server executable unavailable") from None
        digest = hashlib.sha256()
        try:
            with os.fdopen(descriptor, "rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            raise CodexHostUnavailable("pinned app-server executable unreadable") from None
        if digest.hexdigest() != expected:
            raise CodexHostUnavailable("pinned app-server executable digest mismatch")

    # ---- durable owner binding -------------------------------------------

    def _load(self, key: str) -> dict | None:
        # Validate the root (when it exists) before following any state path.
        self._layout(create=False)
        state_dir = self.config.data_dir / "codex-appserver" / "state"
        if not self._check_existing_dir(state_dir, self.config.data_dir / "codex-appserver"):
            return None
        path = state_dir / f"{key}.json"
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError:
            raise CodexHostUnavailable("app-server binding unavailable") from None
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > MAX_STATE_BYTES):
                raise CodexHostUnavailable("app-server binding identity invalid")
            with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                descriptor = -1
                value = json.load(source)
        except CodexHostError:
            raise
        except (OSError, ValueError, UnicodeError):
            raise CodexHostUnavailable("app-server binding unreadable") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(value, dict) or value.get("schema") != BINDING_SCHEMA:
            raise CodexHostUnavailable("app-server binding schema invalid")
        return value

    def _save(self, key: str, value: dict) -> None:
        layout = self._layout(create=True)
        path = layout.state / f"{key}.json"
        value = {**value, "schema": BINDING_SCHEMA, "updated_at": _now()}
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_STATE_BYTES:
            raise CodexHostUnavailable("app-server binding exceeds its budget")
        temporary = layout.state / f".{key}.{os.urandom(8).hex()}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                descriptor = -1
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
            # Persist the rename as well as the file contents before granting
            # a request that may cross a process/network boundary.
            directory_fd = os.open(layout.state, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            raise CodexHostUnavailable("app-server binding could not be saved") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @contextmanager
    def _turn_lock(self, key: str, request_id: str | None = None, *, preparing=False):
        self._layout(create=True)
        path = self._lock_path(key, "turn")
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 0o600)
        except OSError:
            raise CodexHostUnavailable("app-server turn lock unavailable") from None
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise CodexHostUnavailable("app-server turn lock identity invalid")
            def publish(phase, text=None):
                data = {"request_id": request_id, "phase": phase}
                if text is not None:
                    data["text"] = text
                payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
                with self._state_lock(key):
                    os.pwrite(descriptor, payload, 0)
                    os.ftruncate(descriptor, len(payload))

            with self._state_lock(key):
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise CodexHostBusy("another Codex turn owns this conversation") from None
                os.ftruncate(descriptor, 0)
            try:
                if preparing:
                    publish("preparing")
                yield publish
            finally:
                # Advisory progress exists only while the operation owns this
                # flock. Durable admission, receipts and recovery live elsewhere.
                with self._state_lock(key):
                    os.ftruncate(descriptor, 0)
        finally:
            os.close(descriptor)

    def _operation(self, key: str):
        # The private owner directory has already been validated by _load.
        try:
            fd = os.open(self._lock_path(key, "turn"), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError:
            raise CodexHostUnavailable("Codex operation state unavailable") from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise CodexHostUnavailable("Codex operation state invalid")
            with self._state_lock(key):
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    raw = os.pread(fd, 16384, 0)
                    if not raw:
                        return None
                    try:
                        value = json.loads(raw)
                    except (ValueError, UnicodeError):
                        raise CodexHostUnavailable("Codex operation state invalid") from None
                    if (not isinstance(value, dict) or set(value) - {"request_id", "phase", "text"}
                            or value.get("phase") not in ("preparing", "thread_ready", "running", "output")
                            or (value.get("request_id") is not None and not _valid_id(value["request_id"]))
                            or ("text" in value and (not isinstance(value["text"], str)
                                                     or len(value["text"]) > 1024))):
                        raise CodexHostUnavailable("Codex operation state invalid")
                    return value
                return None
        finally:
            os.close(fd)

    @contextmanager
    def _state_lock(self, key: str):
        self._layout(create=True)
        path = self._lock_path(key, "state")
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 0o600)
        except OSError:
            raise CodexHostUnavailable("app-server state lock unavailable") from None
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise CodexHostUnavailable("app-server state lock identity invalid")
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise CodexHostUnavailable("app-server state lock busy") from None
                    time.sleep(0.01)
            yield
        finally:
            os.close(descriptor)

    def _validate_binding(self, value: dict, user_id: str, conversation_id: str,
                          workspace: Path) -> None:
        if value.get("user_id") != user_id or value.get("conversation_id") != conversation_id:
            raise CodexHostUnavailable("app-server binding owner mismatch")
        if value.get("workspace") != str(workspace):
            raise CodexHostUnavailable("app-server binding workspace mismatch")
        if not _valid_id(value.get("thread_id")):
            raise CodexHostUnavailable("app-server binding thread invalid")
        for field_name in ("active_turn_id", "last_turn_id", "uncertain_request_id"):
            item = value.get(field_name)
            if item is not None and not _valid_id(item):
                raise CodexHostUnavailable("app-server binding identity invalid")
        for field_name in ("active_request_id", "last_request_id"):
            item = value.get(field_name)
            if item is not None and not isinstance(item, str):
                raise CodexHostUnavailable("app-server binding request invalid")
        if not isinstance(value.get("last_text", ""), str):
            raise CodexHostUnavailable("app-server binding text invalid")
        if value.get("last_status") is not None and not isinstance(value["last_status"], str):
            raise CodexHostUnavailable("app-server binding status invalid")
        admitted = value.get("admitted_requests")
        if (not isinstance(admitted, dict) or len(admitted) > MAX_ADMITTED_REQUESTS
                or any(not isinstance(k, str) or not REQUEST_ID.fullmatch(k)
                       or not isinstance(v, str) or not SHA256_HEX.fullmatch(v)
                       for k, v in admitted.items())):
            raise CodexHostUnavailable("app-server admitted request ledger invalid")
        for name, digest_name in (("active_request_id", "active_text_digest"),
                                  ("uncertain_request_id", "uncertain_text_digest"),
                                  ("last_request_id", "last_text_digest")):
            request = value.get(name)
            if request is not None and (request not in admitted or admitted[request] != value.get(digest_name)):
                raise CodexHostUnavailable("app-server request ledger mismatch")
        recent = value.get("recent_requests", [])
        if not isinstance(recent, list) or len(recent) > RECENT_REQUESTS * 2:
            raise CodexHostUnavailable("app-server binding request history invalid")
        for entry in recent:
            if (not isinstance(entry, dict)
                    or not isinstance(entry.get("request_id"), str)
                    or not isinstance(entry.get("text_digest"), str)
                    or not SHA256_HEX.fullmatch(entry["text_digest"])
                    or not _valid_id(entry.get("turn_id"))
                    or not isinstance(entry.get("status"), str)
                    or not isinstance(entry.get("text", ""), str)
                    or not isinstance(entry.get("text_truncated", False), bool)
                    or admitted.get(entry["request_id"]) != entry["text_digest"]):
                raise CodexHostUnavailable("app-server binding request history invalid")

    @staticmethod
    def _new_binding(user_id: str, conversation_id: str, workspace: Path,
                     thread_id: str) -> dict:
        return {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "workspace": str(workspace),
            "thread_id": thread_id,
            "created_at": _now(),
            "active_turn_id": None,
            "active_request_id": None,
            "active_text_digest": None,
            "active_since": None,
            "interrupt_requested_at": None,
            "uncertain_request_id": None,
            "uncertain_text_digest": None,
            "last_request_id": None,
            "last_turn_id": None,
            "last_status": None,
            "last_text": "",
            "last_text_truncated": False,
            "last_text_digest": None,
            "last_rejected_server_requests": 0,
            "recent_requests": [],
            "admitted_requests": {},
            "reconciled_at": None,
        }

    @staticmethod
    def _view(binding: dict | None) -> dict:
        if binding is None:
            return {"bound": False, "thread_id": None, "active_turn": None,
                    "recovery_required": False, "last_turn": None}
        active = binding.get("active_turn_id")
        uncertain = binding.get("uncertain_request_id")
        last_turn = None
        if binding.get("last_turn_id"):
            last_turn = {
                "turn_id": binding["last_turn_id"],
                "request_id": binding.get("last_request_id"),
                "status": binding.get("last_status"),
                "text_truncated": bool(binding.get("last_text_truncated")),
            }
        return {
            "bound": True,
            "thread_id": binding["thread_id"],
            "active_turn": ({
                "turn_id": active,
                "request_id": binding.get("active_request_id"),
                "since": binding.get("active_since"),
                "interrupt_requested": bool(binding.get("interrupt_requested_at")),
            } if active else None),
            "recovery_required": bool(active or uncertain),
            "last_turn": last_turn,
        }

    # ---- process ownership ------------------------------------------------

    @contextmanager
    def _open_session(self, workspace: Path):
        self._verify_executable()
        session = _Session(self.config, workspace, self._environment())
        try:
            yield session
        finally:
            session.close()

    def _register(self, key: str, session: _Session) -> None:
        with self._guard:
            if key in self._sessions:
                raise CodexHostBusy("another Codex turn owns this conversation")
            self._sessions[key] = session

    def _unregister(self, key: str, session: _Session) -> None:
        with self._guard:
            if self._sessions.get(key) is session:
                del self._sessions[key]

    def _session_for(self, key: str) -> _Session | None:
        with self._guard:
            return self._sessions.get(key)

    def close_all(self) -> None:
        """Best-effort shutdown; an interrupted turn stays durably uncertain."""
        with self._guard:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.close()

    @staticmethod
    def _turns(thread: dict) -> list[dict]:
        turns = thread.get("turns")
        if turns is None:
            return []
        if not isinstance(turns, list) or len(turns) > 4096:
            raise CodexHostUnavailable("thread turn list invalid")
        result = []
        for turn in turns:
            if not isinstance(turn, dict) or not _valid_id(turn.get("id")):
                raise CodexHostUnavailable("thread turn identity invalid")
            status = turn.get("status")
            if status is not None and not isinstance(status, str):
                raise CodexHostUnavailable("thread turn status invalid")
            result.append({"id": turn["id"], "status": status})
        return result

    def _poll_terminal(self, session: _Session, thread_id: str, turn_id: str,
                       deadline: float) -> str | None:
        while time.monotonic() < deadline:
            turns = self._turns(session.read_thread(thread_id, True))
            for turn in turns:
                if turn["id"] == turn_id:
                    status = str(turn["status"] or "").lower()
                    if status in TERMINAL_TURN_STATUSES:
                        return status
                    break
            time.sleep(0.1)
        return None

    # ---- public product operations ---------------------------------------

    def status(self, user_id: str, conversation_id: str) -> dict:
        """Read the durable owner binding without starting a server process."""
        self.require_enabled()
        key = self._key(user_id, conversation_id)
        binding = self._load(key)
        if binding is not None:
            # A status read never creates a workspace or starts a process.
            workspace = self._workspace_path(user_id, conversation_id)
            self._validate_binding(binding, user_id, conversation_id, workspace)
        view = self._view(binding)
        operation = self._operation(key)
        if operation is not None:
            view["operation"] = operation
        if view["last_turn"]:
            view["last_turn"]["text"] = binding.get("last_text", "")
        if view["active_turn"]:
            view["active_turn"]["can_interrupt"] = self._session_for(key) is not None
        return view

    def ensure_thread(self, user_id: str, conversation_id: str) -> dict:
        """Start or resume the one thread bound to this owner conversation."""
        self.require_enabled()
        key = self._key(user_id, conversation_id)
        with self._turn_lock(key, preparing=True) as publish:
            binding = self._load(key)
            workspace = self._workspace(user_id, conversation_id)
            if binding is not None:
                self._validate_binding(binding, user_id, conversation_id, workspace)
                if binding.get("active_turn_id") or binding.get("uncertain_request_id"):
                    raise CodexHostConflict("Codex turn recovery is required first")
                with self._open_session(workspace) as session:
                    session.initialize()
                    session.resume_thread(binding["thread_id"], workspace)
                return self._view(binding)
            with self._open_session(workspace) as session:
                session.initialize()
                binding = self._start_binding(session, key, user_id, conversation_id, workspace)
                publish("thread_ready")
            return self._view(binding)

    def _start_binding(self, session, key, user_id, conversation_id, workspace):
        thread_id = session.start_thread(workspace)
        binding = self._new_binding(user_id, conversation_id, workspace, thread_id)
        with self._state_lock(key):
            if self._load(key) is not None:
                raise CodexHostConflict("Codex thread binding changed")
            self._save(key, binding)
            saved = self._load(key)
            if saved is None:
                raise CodexHostUnavailable("Codex thread binding disappeared")
            return saved

    def run_turn(self, user_id: str, conversation_id: str, request_id: str,
                 text: str, prepare_if_needed: bool = False) -> dict:
        """Run exactly one explicit owner turn and return its bound receipt.

        The caller is responsible for model/provider authorization.  This host
        does not choose a model and does not retry or replay an uncertain turn.
        """
        self.require_enabled()
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise CodexHostRejected("invalid Codex request identity")
        if not isinstance(text, str) or not text.strip():
            raise CodexHostRejected("empty Codex turn input")
        if len(text) > self.config.max_input_chars:
            raise CodexHostRejected("Codex turn input exceeds the host budget")
        if "\x00" in text:
            raise CodexHostRejected("invalid Codex turn input")
        digest = _digest(text)
        key = self._key(user_id, conversation_id)
        with self._turn_lock(key, request_id, preparing=True) as publish:
            binding = self._load(key)
            if binding is None and not prepare_if_needed:
                raise CodexHostConflict("Codex thread is not prepared")
            workspace = self._workspace(user_id, conversation_id)
            if binding is not None:
                self._validate_binding(binding, user_id, conversation_id, workspace)
                if binding.get("active_turn_id") or binding.get("uncertain_request_id"):
                    raise CodexHostConflict("Codex turn recovery is required first")
                if binding.get("last_request_id") == request_id:
                    if binding.get("last_text_digest") != digest:
                        raise CodexHostConflict("Codex request identity was reused")
                    return self._receipt(binding, replayed=True)
                replayed = self._recent_receipt(binding, request_id, digest)
                if replayed is not None:
                    return replayed
                if request_id in binding["admitted_requests"]:
                    raise CodexHostConflict("Codex request was already admitted; receipt is not retained")
                if len(binding["admitted_requests"]) >= MAX_ADMITTED_REQUESTS:
                    raise CodexHostConflict("Codex thread request ledger is full; prepare a new conversation")
            # Verify the pinned executable before creating the process so a
            # digest rejection cannot leak a launched child.
            self._verify_executable()
            session = _Session(self.config, workspace, self._environment())
            try:
                session.initialize()
                if binding is None:
                    binding = self._start_binding(session, key, user_id, conversation_id, workspace)
                else:
                    session.resume_thread(binding["thread_id"], workspace)
                publish("thread_ready")
                # Commit the exact request before turn/start. A hard crash,
                # transport failure or lost response cannot make it look unsent.
                with self._state_lock(key):
                    current = self._load(key)
                    if current is None:
                        raise CodexHostUnavailable("Codex thread binding disappeared")
                    self._validate_binding(current, user_id, conversation_id, workspace)
                    if current != binding:
                        raise CodexHostConflict("Codex thread binding changed before admission")
                    current["admitted_requests"][request_id] = digest
                    current["uncertain_request_id"] = request_id
                    current["uncertain_text_digest"] = digest
                    self._save(key, current)
                turn_id = session.start_turn(binding["thread_id"], text)
                # Register before the durable active marker so an interrupt
                # that observes the marker can also reach this process.
                self._register(key, session)
                with self._state_lock(key):
                    current = self._load(key)
                    if current is None:
                        raise CodexHostUnavailable("Codex thread binding disappeared")
                    if (current.get("uncertain_request_id") != request_id
                            or current.get("uncertain_text_digest") != digest):
                        raise CodexHostConflict("Codex request admission changed")
                    current.update(
                        active_turn_id=turn_id,
                        active_request_id=request_id,
                        active_text_digest=digest,
                        active_since=_now(),
                        interrupt_requested_at=None,
                        uncertain_request_id=None,
                        uncertain_text_digest=None,
                    )
                    self._save(key, current)
                publish("running")
                outcome = session.wait_turn(binding["thread_id"], turn_id,
                    first_output=lambda text: publish("output", text))
                stored_text = outcome["text"][:self.config.max_receipt_chars]
                with self._state_lock(key):
                    current = self._load(key)
                    if current is None:
                        raise CodexHostUnavailable("Codex thread binding disappeared")
                    recent = [entry for entry in current.get("recent_requests", [])
                              if entry.get("request_id") != request_id]
                    recent.append({
                        "request_id": request_id,
                        "text_digest": digest,
                        "turn_id": turn_id,
                        "status": outcome["status"],
                        "text": outcome["text"][:RECENT_TEXT_CHARS],
                        "text_truncated": len(outcome["text"]) > RECENT_TEXT_CHARS,
                        "rejected_server_requests": outcome["rejected_server_requests"],
                    })
                    if (current.get("active_turn_id") != turn_id
                            or current.get("active_request_id") != request_id
                            or current.get("active_text_digest") != digest):
                        raise CodexHostConflict("Codex active receipt identity changed")
                    current.update(
                        active_turn_id=None,
                        active_request_id=None,
                        active_text_digest=None,
                        active_since=None,
                        interrupt_requested_at=None,
                        uncertain_request_id=None,
                        uncertain_text_digest=None,
                        last_request_id=request_id,
                        last_turn_id=turn_id,
                        last_status=outcome["status"],
                        last_text=stored_text,
                        last_text_truncated=len(outcome["text"]) > len(stored_text),
                        last_text_digest=digest,
                        last_rejected_server_requests=outcome["rejected_server_requests"],
                        recent_requests=recent[-RECENT_REQUESTS:],
                    )
                    self._save(key, current)
                return self._receipt(current, replayed=False)
            finally:
                self._unregister(key, session)
                session.close()

    def interrupt(self, user_id: str, conversation_id: str,
                  turn_id: str | None = None) -> dict:
        """Cancel the active turn only through the process that owns it."""
        self.require_enabled()
        key = self._key(user_id, conversation_id)
        binding = self._load(key)
        if binding is None:
            raise CodexHostConflict("no active Codex turn")
        workspace = self._workspace(user_id, conversation_id)
        self._validate_binding(binding, user_id, conversation_id, workspace)
        active = binding.get("active_turn_id")
        if not active:
            raise CodexHostConflict("no active Codex turn")
        if turn_id is not None and (not _valid_id(turn_id) or turn_id != active):
            raise CodexHostConflict("Codex turn identity mismatch")
        session = self._session_for(key)
        if session is None:
            raise CodexHostUnavailable(
                "active turn is not owned by this process; reconcile is required")
        session.interrupt_turn(binding["thread_id"], active)
        with self._state_lock(key):
            current = self._load(key)
            if current is not None and current.get("active_turn_id") == active:
                current["interrupt_requested_at"] = _now()
                self._save(key, current)
        return {
            "thread_id": binding["thread_id"],
            "turn_id": active,
            "status": "interrupt_requested",
            "rejected_server_requests": session.rejected_server_requests,
        }

    def reconcile(self, user_id: str, conversation_id: str) -> dict:
        """Resolve a durable uncertain turn without replaying its input."""
        self.require_enabled()
        key = self._key(user_id, conversation_id)
        with self._turn_lock(key):
            binding = self._load(key)
            if binding is None:
                return self._view(None)
            workspace = self._workspace(user_id, conversation_id)
            self._validate_binding(binding, user_id, conversation_id, workspace)
            active = binding.get("active_turn_id")
            uncertain = binding.get("uncertain_request_id")
            if not active and not uncertain:
                return self._view(binding)
            if not active:
                # The pinned protocol does not prove which turn belongs to a
                # request that lost its start receipt. Never guess by position
                # in thread history or interrupt an unrelated turn.
                raise CodexHostConflict("Codex turn ID is unconfirmed; independent reconciliation is required")
            with self._open_session(workspace) as session:
                session.initialize()
                session.resume_thread(binding["thread_id"], workspace)
                turns = self._turns(session.read_thread(binding["thread_id"], True))
                target = active
                matches = [turn for turn in turns if turn["id"] == target]
                if len(matches) != 1:
                    raise CodexHostUnavailable("Codex exact turn is absent or ambiguous; reconcile again")
                status = str(matches[0]["status"] or "").lower()
                if status not in TERMINAL_TURN_STATUSES:
                    session.interrupt_turn(binding["thread_id"], target)
                    deadline = time.monotonic() + self.config.reconcile_timeout_seconds
                    observed = self._poll_terminal(session, binding["thread_id"], target, deadline)
                    if observed is None:
                        raise CodexHostUnavailable("Codex turn state is still uncertain; reconcile again")
                    status = observed
            with self._state_lock(key):
                current = self._load(key)
                if current is None:
                    raise CodexHostUnavailable("Codex thread binding disappeared")
                if (current.get("active_turn_id") != target
                        or current.get("active_request_id") != binding.get("active_request_id")
                        or current.get("active_text_digest") != binding.get("active_text_digest")):
                    raise CodexHostConflict("Codex recovery receipt identity changed")
                # Reconciliation confirms terminal state, not the missing
                # answer or approval count. Never reuse a prior answer.
                current.update(
                    last_request_id=current["active_request_id"],
                    last_text_digest=current["active_text_digest"],
                    last_turn_id=target,
                    last_status=status,
                    last_text="",
                    last_text_truncated=True,
                    last_rejected_server_requests=None,
                    active_turn_id=None,
                    active_request_id=None,
                    active_text_digest=None,
                    active_since=None,
                    interrupt_requested_at=None,
                    uncertain_request_id=None,
                    uncertain_text_digest=None,
                    reconciled_at=_now(),
                )
                self._save(key, current)
            return self._view(current)

    def _recent_receipt(self, binding: dict, request_id: str, digest: str) -> dict | None:
        for entry in reversed(binding.get("recent_requests", [])):
            if entry.get("request_id") != request_id:
                continue
            if entry.get("text_digest") != digest:
                raise CodexHostConflict("Codex request identity was reused")
            return {
                "thread_id": binding["thread_id"],
                "turn_id": entry["turn_id"],
                "status": entry["status"],
                "text": entry.get("text", ""),
                "text_truncated": bool(entry.get("text_truncated")),
                "replayed": True,
                "rejected_server_requests": entry.get("rejected_server_requests"),
            }
        return None

    def _receipt(self, binding: dict, replayed: bool) -> dict:
        return {
            "thread_id": binding["thread_id"],
            "turn_id": binding.get("last_turn_id"),
            "status": binding.get("last_status"),
            "text": binding.get("last_text", ""),
            "text_truncated": bool(binding.get("last_text_truncated")),
            "replayed": replayed,
            "rejected_server_requests": binding.get("last_rejected_server_requests"),
        }
