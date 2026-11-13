"""Opt-in, cancellable HippoRAG worker with bounded JSON-only IPC.

The factory, interpreter and module paths are trusted host configuration, never
request parameters. Each adapter owns one fresh interpreter and process group.
The operation deadline includes lock waiting and IPC; termination/reaping has an
additional one-second budget, not a real-time scheduling guarantee. Input
validation and filesystem cleanup are outside that budget. The clean environment
and Python network audit are not an OS sandbox.
No model, endpoint or persisted index is selected automatically.

This worker requires Linux/POSIX process groups and waitid(WNOWAIT). The host
must not reap this adapter's private child PIDs in a competing SIGCHLD handler.
"""
from __future__ import annotations

import importlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

# -I -S omits both application imports and site/.pth startup execution.
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).absolute().parent.parent))

from app.hipporag import (
    HippoRAGAdapter, HippoRAGError, MAX_INDEX_BYTES, MAX_INDEX_FILES, MAX_QUERY_BYTES, MAX_RESULTS, MAX_ROWS,
    MAX_RECEIPT_BYTES, _digest, _identity, _snapshot,
)

MAX_REQUEST_BYTES = 24_000_000
MAX_RESPONSE_BYTES = 262_144
MAX_PROFILE_BYTES = 65_536
_MAX_PATH_BYTES = 4096
_POLL_SECONDS = 0.01
_TEARDOWN_SECONDS = 1.0
_STATS_FIELDS = {"backend", "algorithm", "identity_digest", "source_count",
                 "unique_text_count", "returned_count", "graph_seed_count"}
_RECEIPT_FIELDS = {"schema", "source_dir", "owner_uid", "identity_digest",
                   "snapshot_digest", "tree_digest", "file_count", "total_bytes"}
_ERROR_CODES = {"unavailable", "invalid_input", "identity_mismatch", "index_untrusted",
                "index_failed", "index_not_ready", "result_invalid", "retrieval_failed",
                "timeout", "cancelled", "worker_failed", "worker_protocol"}
_MODULE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*")
_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_HASH = re.compile(r"[0-9a-f]{64}")


def _primitive(value, depth=0, budget=None):
    if budget is None:
        budget = [250_000]
    budget[0] -= 1
    if depth > 16 or budget[0] < 0:
        raise ValueError()
    if value is None or type(value) in (str, bool, int):
        if type(value) is int and value.bit_length() > 128:
            raise ValueError()
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _primitive(item, depth + 1, budget)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _primitive(item, depth + 1, budget)
        return
    raise ValueError()


def _encode(value, maximum):
    _primitive(value)
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > maximum:
        raise ValueError()
    return data


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


def _invalid_constant(_):
    raise ValueError()


def _read(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_size > maximum):
            raise ValueError()
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(data) != info.st_size or any(getattr(info, key) != getattr(after, key) for key in fields):
            raise ValueError()
    value = json.loads(data, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
    _primitive(value)
    return value


def _write(path, value, maximum):
    data = _encode(value, maximum)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 86_400:
        raise HippoRAGError("invalid_input")
    return float(value)


def _absolute(value):
    """Lexically absolute path; symlinks stay unresolved.

    ``Path.resolve``/``realpath`` would follow ``.venv/bin/python3`` out of the
    virtual environment and lose its ``sys.prefix``, so normalization is
    textual: a trusted spelling such as ``server/../.venv/bin/python3`` becomes
    ``.venv/bin/python3`` and the venv interpreter name is preserved. Empty,
    NUL, oversize and non-string values still fail closed, a ``..`` that
    escapes its root or current directory is still refused, and so is a ``..``
    whose physical meaning differs from the textual one (it crosses a symlink).
    """
    try:
        raw = os.fspath(value)
        if type(raw) is not str or not raw or "\x00" in raw or len(raw.encode("utf-8")) > _MAX_PATH_BYTES:
            raise ValueError()
        depth = 0
        for part in raw.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if depth == 0:
                    raise ValueError()
                depth -= 1
            else:
                depth += 1
        path = os.path.abspath(raw)
        if len(path.encode("utf-8")) > _MAX_PATH_BYTES or os.path.realpath(raw) != os.path.realpath(path):
            raise ValueError()
        return path
    except (TypeError, ValueError, OSError, UnicodeError):
        raise HippoRAGError("invalid_input") from None


def _rows(rows):
    sources, groups, digest = _snapshot(rows)
    texts = {row["id"]: row["text"] for row in rows}
    normalized = [{**{key: value for key, value in source.items() if key != "text_sha256"},
                   "text": texts[source["id"]]} for source in sources]
    return normalized, {"snapshot": digest, "ids": set(texts), "sources": len(sources), "texts": len(groups)}


def _expected(rows, identity):
    normalized, expected = _rows(rows)
    identity = _identity(identity)
    expected["identity"] = identity
    expected["digest"] = _digest({"identity": identity, "snapshot": expected["snapshot"]})
    return normalized, expected


def _stats(value, expected, limit=MAX_RESULTS):
    if (type(value) is not dict or set(value) != _STATS_FIELDS
            or value["backend"] != "official" or value["algorithm"] != "hipporag2-official"
            or value["identity_digest"] != expected["digest"]):
        raise ValueError()
    bounds = {"source_count": MAX_ROWS, "unique_text_count": MAX_ROWS,
              "returned_count": limit, "graph_seed_count": 10_000}
    if any(type(value[key]) is not int or not 0 <= value[key] <= bound for key, bound in bounds.items()):
        raise ValueError()
    if value["source_count"] != expected["sources"] or value["unique_text_count"] != expected["texts"]:
        raise ValueError()
    return value


def _receipt(value, expected, source_dir):
    if (type(value) is not dict or set(value) != _RECEIPT_FIELDS
            or value["schema"] != "workspace-hipporag-receipt/v1"
            or value["source_dir"] != source_dir or value["owner_uid"] != os.geteuid()
            or type(value["owner_uid"]) is not int
            or value["identity_digest"] != expected["digest"]
            or value["snapshot_digest"] != expected["snapshot"]
            or type(value["tree_digest"]) is not str or not _HASH.fullmatch(value["tree_digest"])
            or type(value["file_count"]) is not int or not 1 <= value["file_count"] <= MAX_INDEX_FILES
            or type(value["total_bytes"]) is not int or not 1 <= value["total_bytes"] <= MAX_INDEX_BYTES):
        raise ValueError()
    _encode(value, MAX_RECEIPT_BYTES)
    return value


def _reference(value):
    if type(value) is not str or value.count(":") != 1:
        raise HippoRAGError("invalid_input")
    module, name = value.split(":")
    if not _MODULE.fullmatch(module) or not _NAME.fullmatch(name):
        raise HippoRAGError("invalid_input")
    target = getattr(importlib.import_module(module), name)
    if not callable(target):
        raise HippoRAGError("unavailable")
    return target


def create_official_adapter(*, engine_factory, chunk_factory):
    """Compose explicit trusted import references; never pick a model implicitly."""
    return HippoRAGAdapter(engine_factory=_reference(engine_factory), chunk_factory=_reference(chunk_factory))


def _child_status(process):
    """Observe without reaping: an unreaped group leader prevents PID reuse."""
    if process.returncode is not None:
        return "reaped"
    try:
        result = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return "reaped"
    return "running" if result is None else "exited"


class HippoRAGProcessAdapter:
    """One owned worker per index; interruption requires explicit build/restore.

    ``module_paths`` supplies dependency directories without executing .pth files.
    ``work_root`` must be explicit; worker home/tmp/cache are private descendants.
    ``allow_network`` is a trusted host setting, not an operation/request option.
    """

    supports_cancellation = True

    def __init__(self, *, work_root, python_executable=None, factory_module=None, factory_name=None,
                 module_paths=(), factory_kwargs=None, allow_network=False,
                 build_timeout_seconds=120, search_timeout_seconds=30, control_timeout_seconds=5):
        if (type(allow_network) is not bool or not isinstance(module_paths, (list, tuple))
                or len(module_paths) > 32 or (factory_module is None) != (factory_name is None)):
            raise HippoRAGError("invalid_input")
        if factory_module is not None and (type(factory_module) is not str or not _MODULE.fullmatch(factory_module)
                or type(factory_name) is not str or not _NAME.fullmatch(factory_name)):
            raise HippoRAGError("invalid_input")
        self._root = Path(_absolute(work_root))
        if not self._root.is_dir() or any(path.is_symlink() for path in (self._root, *self._root.parents)):
            raise HippoRAGError("invalid_input")
        self._python = _absolute(python_executable or sys.executable)
        if not os.path.isfile(self._python) or not os.access(self._python, os.X_OK):
            raise HippoRAGError("invalid_input")
        paths = [_absolute(path) for path in module_paths]
        if any(not Path(path).is_dir() for path in paths):
            raise HippoRAGError("invalid_input")
        if factory_kwargs is None:
            factory_kwargs = {}
        if type(factory_kwargs) is not dict:
            raise HippoRAGError("invalid_input")
        self._profile = {"factory_module": factory_module, "factory_name": factory_name,
                         "module_paths": paths, "factory_kwargs": factory_kwargs, "allow_network": allow_network}
        try:
            self._profile = json.loads(_encode(self._profile, MAX_PROFILE_BYTES))
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise HippoRAGError("invalid_input") from None
        self._build_timeout = _seconds(build_timeout_seconds)
        self._search_timeout = _seconds(search_timeout_seconds)
        self._control_timeout = _seconds(control_timeout_seconds)
        self._lock = threading.Lock()
        self._process = None
        self._directory = None
        self._sequence = 0
        self._state = "new"
        self._expected = None
        self._index_dir = None

    @property
    def worker_pid(self):
        process = self._process
        return process.pid if process is not None and _child_status(process) == "running" else None

    def _check(self, event, deadline):
        if event is not None:
            try:
                cancelled = event.is_set()
            except Exception:
                cancelled = True
            if cancelled:
                raise HippoRAGError("cancelled")
        if time.monotonic() >= deadline:
            raise HippoRAGError("timeout")

    def _acquire(self, event, deadline):
        while True:
            self._check(event, deadline)
            if self._lock.acquire(timeout=min(_POLL_SECONDS, max(0, deadline - time.monotonic()))):
                try:
                    self._check(event, deadline)
                except BaseException:
                    self._lock.release()
                    raise
                return

    def _stop(self):
        process, directory = self._process, self._directory
        self._process = None
        self._directory = None
        if process is not None:
            end = time.monotonic() + _TEARDOWN_SECONDS
            if _child_status(process) != "reaped":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                grace = min(end, time.monotonic() + 0.15)
                while _child_status(process) == "running" and time.monotonic() < grace:
                    time.sleep(min(_POLL_SECONDS, max(0, grace - time.monotonic())))
                # Kill descendants before reaping the leader, even if it exited
                # on TERM already. Never signal a potentially reused group ID.
                if _child_status(process) != "reaped":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            try:
                process.wait(timeout=max(0.001, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                # Retain ownership so close() can attempt reaping again.
                self._process = process
                self._directory = directory
                raise HippoRAGError("worker_failed") from None
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)

    def _start(self):
        if self._profile["factory_module"] is None:
            raise HippoRAGError("unavailable")
        directory = Path(tempfile.mkdtemp(prefix="hipporag-worker-", dir=self._root))
        self._directory = directory
        for name in ("home", "tmp", "cache"):
            (directory / name).mkdir(mode=0o700)
        _write(directory / "profile.json", self._profile, MAX_PROFILE_BYTES)
        environment = {
            "PATH": "/usr/bin:/bin", "HOME": str(directory / "home"), "TMPDIR": str(directory / "tmp"),
            "XDG_CACHE_HOME": str(directory / "cache"), "HF_HOME": str(directory / "cache" / "hf"),
            "PYTHONPYCACHEPREFIX": str(directory / "cache" / "pycache"), "PYTHONDONTWRITEBYTECODE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1", "AWS_EC2_METADATA_DISABLED": "true",
            "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        }
        if not self._profile["allow_network"]:
            environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        try:
            self._process = subprocess.Popen(
                [self._python, "-I", "-S", "-B", str(Path(__file__).absolute()), "--worker", str(directory)],
                cwd=directory, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
            )
        except (OSError, ValueError):
            self._stop()
            raise HippoRAGError("worker_failed") from None
        self._sequence = 0

    def _exchange(self, operation, payload, event, deadline):
        self._check(event, deadline)
        if self._process is None or _child_status(self._process) != "running":
            raise HippoRAGError("worker_failed")
        sequence = self._sequence + 1
        identifier = uuid.uuid4().hex
        request = {"version": 1, "id": identifier, "seq": sequence,
                   "operation": operation, "payload": payload}
        response_path = self._directory / "response.json"
        try:
            response_path.unlink(missing_ok=True)
            _write(self._directory / "request.json", request, MAX_REQUEST_BYTES)
            self._sequence = sequence
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise HippoRAGError("invalid_input") from None
        while True:
            self._check(event, deadline)
            try:
                response = _read(response_path, MAX_RESPONSE_BYTES)
            except FileNotFoundError:
                # close() may publish and exit between open() and status check.
                if _child_status(self._process) != "running" and not response_path.exists():
                    raise HippoRAGError("worker_failed")
                time.sleep(min(_POLL_SECONDS, max(0, deadline - time.monotonic())))
                continue
            except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                raise HippoRAGError("worker_protocol") from None
            if (type(response) is not dict or type(response.get("version")) is not int or response["version"] != 1
                    or response.get("id") != identifier or type(response.get("seq")) is not int
                    or response["seq"] != self._sequence or type(response.get("ok")) is not bool):
                raise HippoRAGError("worker_protocol")
            if not response["ok"]:
                if (set(response) != {"version", "id", "seq", "ok", "error"}
                        or type(response.get("error")) is not str or response["error"] not in _ERROR_CODES):
                    raise HippoRAGError("worker_protocol")
                raise HippoRAGError(response["error"])
            if set(response) != {"version", "id", "seq", "ok", "result"}:
                raise HippoRAGError("worker_protocol")
            self._check(event, deadline)
            return response["result"]

    def _operation(self, operation, payload, expected, event, timeout, default):
        deadline = time.monotonic() + _seconds(default if timeout is None else timeout)
        self._acquire(event, deadline)
        dispatched = False
        try:
            if self._state == "closed":
                raise HippoRAGError("index_not_ready")
            if operation in {"build", "restore"}:
                if self._state == "ready":
                    raise HippoRAGError("index_not_ready")
                self._stop()
                self._check(event, deadline)
                self._start()
                dispatched = True
            else:
                if self._state != "ready":
                    raise HippoRAGError("index_not_ready")
                if expected is not None and expected["digest"] != self._expected["digest"]:
                    raise HippoRAGError("identity_mismatch")
            self._check(event, deadline)
            dispatched = True
            result = self._exchange(operation, payload, event, deadline)
            try:
                if operation in {"build", "restore"}:
                    _stats(result, expected, 0)
                    self._expected = expected
                    self._index_dir = payload["index_dir"]
                    self._state = "ready"
                elif operation == "search":
                    if type(result) is not dict or set(result) != {"scores", "stats"}:
                        raise ValueError()
                    scores = result["scores"]
                    stats = _stats(result["stats"], expected, payload["limit"])
                    if (type(scores) is not dict or len(scores) > payload["limit"]
                            or not set(scores).issubset(expected["ids"])
                            or stats["returned_count"] != len(scores)
                            or any(type(score) not in (int, float) or not math.isfinite(score)
                                   or abs(score) > 1_000_000_000_000 for score in scores.values())):
                        raise ValueError()
                    result = (scores, stats)
                elif operation == "seal":
                    _receipt(result, self._expected, self._index_dir)
                    self._stop()
                    self._state = "sealed"
                elif operation == "close":
                    if result is not None:
                        raise ValueError()
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise HippoRAGError("worker_protocol") from None
            return result
        except HippoRAGError as error:
            if dispatched and (operation in {"build", "restore", "seal", "close"}
                    or error.code in {"timeout", "cancelled", "worker_failed", "worker_protocol"}):
                self._state = "broken"
                self._stop()
            raise
        except Exception:
            if dispatched:
                self._state = "broken"
                self._stop()
            raise HippoRAGError("worker_failed") from None
        finally:
            self._lock.release()

    def build(self, rows, *, identity, index_dir, cancel_event=None, timeout_seconds=None):
        normalized, expected = _expected(rows, identity)
        return self._operation("build", {"rows": normalized, "identity": expected["identity"],
            "index_dir": _absolute(index_dir)}, expected, cancel_event, timeout_seconds, self._build_timeout)

    def search(self, query, *, rows, identity=None, limit=6, cancel_event=None, timeout_seconds=None):
        if (type(query) is not str or not query.strip() or len(query) > 2000
                or type(limit) is not int or not 1 <= limit <= MAX_RESULTS):
            raise HippoRAGError("invalid_input")
        try:
            if len(query.encode("utf-8")) > MAX_QUERY_BYTES:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise HippoRAGError("invalid_input") from None
        chosen = identity if identity is not None else self._expected["identity"] if self._expected else None
        if chosen is None:
            raise HippoRAGError("index_not_ready")
        normalized, expected = _expected(rows, chosen)
        return self._operation("search", {"query": query, "rows": normalized, "identity": expected["identity"],
            "limit": limit}, expected, cancel_event, timeout_seconds, self._search_timeout)

    def seal(self, *, cancel_event=None, timeout_seconds=None):
        return self._operation("seal", {}, None, cancel_event, timeout_seconds, self._control_timeout)

    def restore(self, rows, *, identity, source_dir, receipt, index_dir, cancel_event=None, timeout_seconds=None):
        normalized, expected = _expected(rows, identity)
        source_dir = _absolute(source_dir)
        try:
            _receipt(receipt, expected, source_dir)
            receipt = json.loads(_encode(receipt, MAX_RECEIPT_BYTES))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise HippoRAGError("index_untrusted") from None
        return self._operation("restore", {"rows": normalized, "identity": expected["identity"],
            "source_dir": source_dir, "receipt": receipt, "index_dir": _absolute(index_dir)},
            expected, cancel_event, timeout_seconds, self._build_timeout)

    def close(self):
        deadline = time.monotonic() + self._control_timeout
        self._acquire(None, deadline)
        try:
            if self._state == "closed":
                # A previous bounded wait may have timed out after SIGKILL.
                # Retained ownership must remain retryable, even when closed.
                self._stop()
                return
            try:
                if self.worker_pid is not None:
                    result = self._exchange("close", {}, None, deadline)
                    if result is not None:
                        raise HippoRAGError("worker_protocol")
            finally:
                self._state = "closed"
                self._stop()
        finally:
            self._lock.release()


def _worker(directory):
    directory = Path(directory)
    profile = _read(directory / "profile.json", MAX_PROFILE_BYTES)
    if set(profile) != {"factory_module", "factory_name", "module_paths", "factory_kwargs", "allow_network"}:
        raise ValueError()
    if not profile["allow_network"]:
        def audit(event, args):
            if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.sendmsg"}:
                raise PermissionError("HippoRAG worker network access is disabled")
        sys.addaudithook(audit)
    # Explicit trusted roots only; site.addsitedir would also execute .pth code.
    for path in reversed(profile["module_paths"]):
        sys.path.insert(0, path)
    adapter = None
    sequence = 0
    while True:
        try:
            request = _read(directory / "request.json", MAX_REQUEST_BYTES)
        except FileNotFoundError:
            time.sleep(_POLL_SECONDS)
            continue
        # Consume before replying so the next request cannot replace an inode
        # that this loop is still reading (and change its link-count mid-read).
        (directory / "request.json").unlink()
        if (type(request) is not dict or set(request) != {"version", "id", "seq", "operation", "payload"}
                or type(request["version"]) is not int or request["version"] != 1
                or type(request["seq"]) is not int or type(request["id"]) is not str
                or not re.fullmatch(r"[0-9a-f]{32}", request["id"]) or type(request["payload"]) is not dict):
            raise ValueError()
        if request["seq"] != sequence + 1:
            raise ValueError()
        sequence = request["seq"]
        operation, payload = request["operation"], request["payload"]
        response = {"version": 1, "id": request["id"], "seq": sequence}
        try:
            fields = {"build": {"rows", "identity", "index_dir"},
                      "restore": {"rows", "identity", "source_dir", "receipt", "index_dir"},
                      "search": {"query", "rows", "identity", "limit"}, "seal": set(), "close": set()}
            if operation not in fields or set(payload) != fields[operation]:
                raise HippoRAGError("worker_protocol")
            if adapter is None:
                factory = _reference(f"{profile['factory_module']}:{profile['factory_name']}")
                adapter = factory(**profile["factory_kwargs"])
            if operation in {"build", "restore"}:
                result = getattr(adapter, operation)(payload["rows"], **{key: value for key, value in payload.items() if key != "rows"})
            elif operation == "search":
                scores, stats = adapter.search(payload["query"], **{key: value for key, value in payload.items() if key != "query"})
                result = {"scores": scores, "stats": stats}
            else:
                result = getattr(adapter, operation)()
            _encode(result, MAX_RESPONSE_BYTES - 512)
            response.update(ok=True, result=result)
        except HippoRAGError as error:
            response.update(ok=False, error=error.code if error.code in _ERROR_CODES else "worker_failed")
        except (ValueError, TypeError, RecursionError, UnicodeError):
            response.update(ok=False, error="worker_protocol")
        except Exception:
            response.update(ok=False, error="worker_failed")
        _write(directory / "response.json", response, MAX_RESPONSE_BYTES)
        if operation == "close":
            return


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit(2)
    try:
        _worker(sys.argv[2])
    except BaseException:
        raise SystemExit(1) from None
