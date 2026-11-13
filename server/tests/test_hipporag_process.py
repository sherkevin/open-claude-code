"""Process lifecycle contracts, with synthetic local factories and no models."""
import copy
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.hipporag import HippoRAGError, MAX_INDEX_BYTES, MAX_INDEX_FILES, PINNED_COMMIT
from app.hipporag_process import HippoRAGProcessAdapter, create_official_adapter


IDENTITY = {"upstream_commit": PINNED_COMMIT, **{
    key: char * 64 for key, char in zip(
        ("parser_digest", "extraction_digest", "embedding_digest", "chunking_digest", "acl_digest"), "abcde")}}
ROWS = [{"id": "chunk-a", "kb_id": "kb-a", "document_id": "document-a", "version": 1,
         "sha256": "f" * 64, "text": "Synthetic pump supplies valve."},
        {"id": "chunk-b", "kb_id": "kb-a", "document_id": "document-b", "version": 1,
         "sha256": "e" * 64, "text": "Synthetic pump supplies valve."}]

# A trusted, temporary module imports the real thin adapter, not pytest or a model.
FACTORY_SOURCE = r'''
import json, os, signal, socket, subprocess, sys, time
from pathlib import Path
from types import SimpleNamespace
from app.hipporag import HippoRAGAdapter

def Chunk(**kw):
    return SimpleNamespace(**kw)

class Engine:
    def __init__(self, *, save_dir):
        self.directory = Path(save_dir)
        self.path = self.directory / "fixture.json"
        self.chunks = [Chunk(**item) for item in json.loads(self.path.read_text())] if self.path.exists() else []
    def index(self, chunks):
        self.chunks = chunks
        self.path.write_text(json.dumps([vars(chunk) for chunk in chunks]))
    def retrieve(self, queries, num_to_retrieve):
        chunks = self.chunks[:num_to_retrieve]
        return [SimpleNamespace(question=queries[0], docs=[c.content for c in chunks],
            doc_scores=[0.5 for c in chunks], doc_metadata=[{**c.metadata, "source_id":c.source_id} for c in chunks],
            graph_seeds=[("pump", "supplies", "valve")])]
    def close(self):
        pass

class EvilPickle:
    def __init__(self, marker):
        self.marker = marker
    def __reduce__(self):
        (self.marker / "pickle-was-used").write_text("unsafe")
        return (str, ("bad",))

class Fixture(HippoRAGAdapter):
    def __init__(self, marker_dir, mode="normal", build_delay=0, close_delay=0):
        super().__init__(engine_factory=Engine, chunk_factory=Chunk)
        self.marker = Path(marker_dir)
        self.mode = mode
        self.build_delay = build_delay
        self.close_delay = close_delay
        self.marker.joinpath("pid").write_text(str(os.getpid()))
        self.marker.joinpath("environment.json").write_text(json.dumps({"env":dict(os.environ),
            "no_site":sys.flags.no_site, "isolated":sys.flags.isolated,
            "executable":sys.executable, "path":sys.path}))
        print("third-party library noise\n" * 100)
    def build(self, *args, **kwargs):
        time.sleep(self.build_delay)
        return super().build(*args, **kwargs)
    def search(self, query, **kwargs):
        self.marker.joinpath("started").write_text(query)
        if query in {"hang", "tree"}:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            if query == "tree":
                code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(300)"
                child = subprocess.Popen([sys.executable, "-I", "-S", "-c", code])
                self.marker.joinpath("descendant").write_text(str(child.pid))
            time.sleep(300)
        if query == "slow":
            time.sleep(0.5)
        if query == "exit":
            os._exit(7)
        if query == "exception":
            raise RuntimeError("sensitive-query-and-credential-must-not-escape")
        if query == "network":
            try:
                socket.getaddrinfo("localhost", 80)
            except PermissionError:
                self.marker.joinpath("network-denied").touch()
            else:
                self.marker.joinpath("network-allowed").touch()
        scores, stats = super().search(query, **kwargs)
        if self.mode == "unknown-score":
            scores = {"unauthorized-chunk": 0.8}
            stats["returned_count"] = 1
        if self.mode == "score-nan":
            scores[next(iter(scores))] = float("nan")
        if self.mode == "score-bool":
            scores[next(iter(scores))] = True
        if self.mode == "score-too-large":
            scores[next(iter(scores))] = 1e20
        if self.mode == "stats-extra":
            stats["private_debug"] = "must-not-escape"
        if self.mode == "stats-wrong-identity":
            stats["identity_digest"] = "0" * 64
        if self.mode == "stats-count":
            stats["returned_count"] = True
        if self.mode == "large":
            stats["excess"] = "x" * 300000
        if self.mode == "pickle":
            scores = EvilPickle(self.marker)
        if self.mode.startswith("wire-"):
            # Deliberately corrupt the IPC from a trusted adversarial fixture.
            # No production factory accesses the dispatcher's frame.
            request = sys._getframe(1).f_locals["request"]
            value = {"version":1, "seq":request["seq"], "id":request["id"], "ok":False, "error":"worker_failed"}
            raw = json.dumps(value)
            if self.mode == "wire-malformed":
                raw = "not-json"
            elif self.mode == "wire-duplicate":
                raw = raw[:-1] + ',"ok":true}'
            elif self.mode == "wire-nan":
                raw = raw[:-1] + ',"number":NaN}'
            elif self.mode == "wire-unknown-error":
                value["error"] = "sensitive-query-and-credential-must-not-escape"
                raw = json.dumps(value)
            elif self.mode == "wire-list-error":
                value["error"] = []
                raw = json.dumps(value)
            elif self.mode == "wire-stale":
                value["seq"] -= 1
                raw = json.dumps(value)
            elif self.mode == "wire-extra":
                value["debug"] = "secret"
                raw = json.dumps(value)
            elif self.mode == "wire-oversize":
                raw = " " * 300000
            path = Path("response.tmp")
            if self.mode == "wire-symlink":
                target = self.marker / "untrusted-response"
                target.write_text(raw)
                target.chmod(0o600)
                path.symlink_to(target)
            elif self.mode == "wire-fifo":
                os.mkfifo(path, 0o600)
            else:
                path.write_text(raw)
                path.chmod(0o644 if self.mode == "wire-mode" else 0o600)
            path.replace("response.json")
            time.sleep(300)
        return scores, stats
    def close(self):
        self.marker.joinpath("closed").touch()
        time.sleep(self.close_delay)
        return super().close()

def factory(**kwargs):
    return Fixture(**kwargs)
'''


@pytest.fixture
def setup(tmp_path):
    (tmp_path / "process_fixture.py").write_text(FACTORY_SOURCE)
    adapters = []

    def make(*, marker=None, **options):
        marker = marker or tmp_path / f"markers-{len(adapters)}"
        marker.mkdir()
        factory_kwargs = options.pop("factory_kwargs", {})
        adapter = HippoRAGProcessAdapter(work_root=tmp_path, factory_module="process_fixture", factory_name="factory",
            module_paths=[tmp_path], factory_kwargs={"marker_dir": str(marker), **factory_kwargs},
            build_timeout_seconds=10, search_timeout_seconds=2, control_timeout_seconds=1, **options)
        adapters.append(adapter)
        return adapter, marker

    yield make
    for adapter in adapters:
        try:
            adapter.close()
        except HippoRAGError:
            pass


def fail(code, function):
    with pytest.raises(HippoRAGError) as result:
        function()
    assert result.value.code == code
    assert "sensitive" not in str(result.value)
    return result.value


def build(adapter, path):
    return adapter.build(copy.deepcopy(ROWS), identity=IDENTITY, index_dir=path)


def wait_file(path, seconds=3):
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"Missing marker {path.name}"
        time.sleep(0.01)


def dead(pid):
    path = Path(f"/proc/{pid}/stat")
    try:
        return path.read_text().split(")", 1)[1].strip().startswith("Z")
    except FileNotFoundError:
        return True


def assert_reaped(pid):
    assert dead(pid)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def test_default_no_factory_never_launches_or_loads_model(tmp_path):
    adapter = HippoRAGProcessAdapter(work_root=tmp_path)
    fail("unavailable", lambda: build(adapter, tmp_path / "index"))
    assert adapter.worker_pid is None
    assert list(tmp_path.iterdir()) == []
    adapter.close()


def test_real_adapter_duplicate_sources_and_worker_reuse(setup, tmp_path):
    adapter, marker = setup()
    stats = build(adapter, tmp_path / "index")
    pid = adapter.worker_pid
    assert pid != os.getpid()
    assert stats["source_count"] == 2 and stats["unique_text_count"] == 1
    scores, stats = adapter.search("pump", rows=ROWS)
    assert scores == {"chunk-a": 0.5, "chunk-b": 0.5}
    assert stats["graph_seed_count"] == 1
    assert adapter.worker_pid == pid
    assert (marker / "started").read_text() == "pump"
    adapter.close()
    adapter.close()
    assert (marker / "closed").exists()
    assert adapter.worker_pid is None
    assert_reaped(pid)
    assert not list(tmp_path.glob("hipporag-worker-*"))


def test_worker_environment_is_clean_and_explicit_paths_do_not_run_pth(setup, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-copy-this-synthetic-marker")
    monkeypatch.setenv("TOKENHUB_API_KEY", "do-not-copy")
    monkeypatch.setenv("HTTPS_PROXY", "http://not-a-proxy.invalid")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/path")
    pth_marker = tmp_path / "pth-executed"
    (tmp_path / "untrusted.pth").write_text(f"import pathlib; pathlib.Path({str(pth_marker)!r}).touch()\n")
    adapter, marker = setup()
    build(adapter, tmp_path / "index")
    info = json.loads((marker / "environment.json").read_text())
    assert info["isolated"] == 1 and info["no_site"] == 1
    assert not {"OPENAI_API_KEY", "TOKENHUB_API_KEY", "HTTPS_PROXY", "PYTHONPATH"} & info["env"].keys()
    assert Path(info["env"]["HOME"]).parent.parent == tmp_path
    assert Path(info["env"]["TMPDIR"]).parent.parent == tmp_path
    assert info["env"]["HF_HUB_OFFLINE"] == "1"
    assert not pth_marker.exists()
    adapter.search("network", rows=ROWS)
    assert (marker / "network-denied").exists()


def test_network_opt_in_is_only_host_constructor_configuration(setup, tmp_path):
    adapter, marker = setup(allow_network=True)
    build(adapter, tmp_path / "index")
    adapter.search("network", rows=ROWS)
    assert (marker / "network-allowed").exists()
    with pytest.raises(TypeError):
        adapter.search("pump", rows=ROWS, allow_network=True)


def test_alternate_python_environment_preserves_executable_symlink(setup, tmp_path):
    executable = tmp_path / "alternate-env" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(sys.executable)
    (executable.parent.parent / "pyvenv.cfg").write_text(f"home = {Path(sys.executable).parent}\ninclude-system-site-packages = false\n")
    adapter, marker = setup(python_executable=executable)
    build(adapter, tmp_path / "index")
    info = json.loads((marker / "environment.json").read_text())
    assert info["executable"] == str(executable)
    assert adapter.search("pump", rows=ROWS)[0]


def test_relative_interpreter_spelling_with_parent_traversal_keeps_the_venv(setup, tmp_path):
    """`sys.executable` of `venv/bin/python3` run as `server/../.venv/bin/python3`.

    The `..` is legal and statically normalizable; resolving the symlink instead
    would launch the base interpreter and silently leave the virtual environment.
    """
    project = tmp_path / "project"
    base = os.path.realpath(sys.executable)
    executable = project / ".venv" / "bin" / "python3"
    executable.parent.mkdir(parents=True)
    (project / "server").mkdir()
    executable.symlink_to(base)
    (project / ".venv" / "pyvenv.cfg").write_text(
        f"home = {Path(base).parent}\ninclude-system-site-packages = false\n")
    spelling = f"{project}/server/../.venv/bin/python3"
    adapter, marker = setup(python_executable=spelling)
    build(adapter, tmp_path / "index")
    info = json.loads((marker / "environment.json").read_text())
    assert info["executable"] == str(executable)
    assert os.path.realpath(info["executable"]) == base
    assert adapter.search("pump", rows=ROWS)[0] == {"chunk-a": 0.5, "chunk-b": 0.5}


def test_path_normalization_collapses_legal_parent_traversal_and_stays_fail_closed(tmp_path):
    import app.hipporag_process as process
    legal = f"{tmp_path}/server/../.venv/bin/python3"
    assert process._absolute(legal) == f"{tmp_path}/.venv/bin/python3"
    assert process._absolute(f"{tmp_path}/work/..") == str(tmp_path)
    assert process._absolute("bin/python3") == os.path.join(os.getcwd(), "bin", "python3")
    for value in ("", "/tmp/\x00python3", "/" + "a" * 5000, b"/tmp", None, 5, object(),
                  "/../etc/passwd", "../escape"):
        fail("invalid_input", lambda value=value: process._absolute(value))
    # Absolute spellings that stay inside the filesystem stay legal; only a
    # `..` escaping the root (or the starting point of a relative path) fails.
    assert process._absolute(f"{tmp_path}/parent/../sibling") == f"{tmp_path}/sibling"
    assert process._absolute(f"{tmp_path}/a/b/../../c") == f"{tmp_path}/c"
    # A symlinked leaf is kept as written: normalization is textual, not resolve().
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python3").symlink_to(sys.executable)
    assert process._absolute(f"{tmp_path}/bin/python3") == f"{tmp_path}/bin/python3"
    # `link/../x` means two different things textually and physically, so refuse
    # the ambiguous spelling rather than pick one silently.
    (tmp_path / "elsewhere" / "deep").mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "elsewhere" / "deep")
    fail("invalid_input", lambda: process._absolute(f"{tmp_path}/link/../x"))
    assert process._absolute(f"{tmp_path}/link/x") == f"{tmp_path}/link/x"
    # Host-supplied roots and index paths accept the same legal traversal.
    (tmp_path / "work").mkdir()
    adapter = HippoRAGProcessAdapter(work_root=f"{tmp_path}/work/..",
        factory_module="test_module", factory_name="factory")
    assert adapter._root == tmp_path
    adapter.close()


def test_index_dir_with_legal_parent_traversal_builds_the_normalized_directory(setup, tmp_path):
    adapter, marker = setup()
    (tmp_path / "parent").mkdir()
    stats = build(adapter, f"{tmp_path}/parent/../index")
    assert stats["source_count"] == 2
    assert (tmp_path / "index").is_dir() and not (tmp_path / "parent" / "index").exists()
    assert adapter.search("pump", rows=ROWS)[0] == {"chunk-a": 0.5, "chunk-b": 0.5}


def test_hard_timeout_reaps_worker_and_kills_owned_descendants(setup, tmp_path):
    adapter, marker = setup()
    build(adapter, tmp_path / "index")
    pid = adapter.worker_pid
    start = time.monotonic()
    fail("timeout", lambda: adapter.search("tree", rows=ROWS, timeout_seconds=0.3))
    assert time.monotonic() - start < 2
    assert_reaped(pid)
    descendant = int((marker / "descendant").read_text())
    end = time.monotonic() + 1
    while not dead(descendant) and time.monotonic() < end:
        time.sleep(0.01)
    assert dead(descendant)
    assert adapter.worker_pid is None
    fail("index_not_ready", lambda: adapter.search("pump", rows=ROWS))
    build(adapter, tmp_path / "explicit-new-index")
    assert adapter.search("pump", rows=ROWS)[0]


def test_active_cancel_reaps_only_its_adapter(setup, tmp_path):
    first, marker = setup()
    second, _ = setup()
    build(first, tmp_path / "index-one")
    build(second, tmp_path / "index-two")
    first_pid, second_pid = first.worker_pid, second.worker_pid
    event = threading.Event()
    result = []

    def run():
        result.append(fail("cancelled", lambda: first.search("hang", rows=ROWS, cancel_event=event)))

    thread = threading.Thread(target=run)
    thread.start()
    wait_file(marker / "started")
    event.set()
    thread.join(3)
    assert not thread.is_alive() and len(result) == 1
    assert_reaped(first_pid)
    assert second.worker_pid == second_pid
    assert second.search("pump", rows=ROWS)[0]


@pytest.mark.parametrize("waiting_kind", ["cancel", "timeout"])
def test_waiting_call_cannot_cancel_active_request(setup, tmp_path, waiting_kind):
    adapter, marker = setup()
    build(adapter, tmp_path / "index")
    pid, result = adapter.worker_pid, []
    thread = threading.Thread(target=lambda: result.append(adapter.search("slow", rows=ROWS)))
    thread.start()
    wait_file(marker / "started")
    event = threading.Event()
    if waiting_kind == "cancel":
        event.set()
    fail("cancelled" if waiting_kind == "cancel" else "timeout", lambda: adapter.search("pump", rows=ROWS,
        cancel_event=event, timeout_seconds=0.05))
    thread.join(3)
    assert not thread.is_alive() and result[0][0]
    assert adapter.worker_pid == pid
    assert adapter.search("pump", rows=ROWS)[0]


def test_cancel_before_build_does_not_spawn(setup, tmp_path):
    adapter, _ = setup()
    event = threading.Event()
    event.set()
    fail("cancelled", lambda: adapter.build(ROWS, identity=IDENTITY, index_dir=tmp_path / "index", cancel_event=event))
    assert adapter.worker_pid is None
    assert not (tmp_path / "index").exists()


def test_cancel_policy_exception_fails_closed(setup, tmp_path):
    adapter, _ = setup()
    build(adapter, tmp_path / "index")
    pid = adapter.worker_pid

    class Policy:
        def is_set(self):
            raise RuntimeError("ACL state unavailable")

    fail("cancelled", lambda: adapter.search("pump", rows=ROWS, cancel_event=Policy()))
    assert adapter.worker_pid == pid


def test_build_timeout_requires_explicit_new_operation(setup, tmp_path):
    adapter, _ = setup(factory_kwargs={"build_delay": 0.5})
    fail("timeout", lambda: adapter.build(ROWS, identity=IDENTITY, index_dir=tmp_path / "index", timeout_seconds=0.2))
    assert adapter.worker_pid is None
    fail("index_not_ready", lambda: adapter.search("pump", rows=ROWS))
    build(adapter, tmp_path / "index-next")


def test_close_hang_is_killed_with_bounded_wait(setup, tmp_path):
    adapter, _ = setup(factory_kwargs={"close_delay": 300})
    build(adapter, tmp_path / "index")
    pid = adapter.worker_pid
    start = time.monotonic()
    fail("timeout", adapter.close)
    assert time.monotonic() - start < 2.5
    assert_reaped(pid)
    adapter.close()


@pytest.mark.parametrize("query,code", [("exit", "worker_failed"), ("exception", "worker_failed")])
def test_worker_crash_and_exception_are_sanitized(setup, tmp_path, query, code):
    adapter, _ = setup()
    build(adapter, tmp_path / "index")
    pid = adapter.worker_pid
    fail(code, lambda: adapter.search(query, rows=ROWS))
    assert adapter.worker_pid is None
    assert_reaped(pid)


@pytest.mark.parametrize("mode", ["unknown-score", "score-nan", "score-bool", "score-too-large", "stats-extra",
    "stats-wrong-identity", "stats-count", "large", "pickle", "wire-malformed", "wire-duplicate", "wire-nan",
    "wire-unknown-error", "wire-list-error", "wire-stale", "wire-extra", "wire-oversize",
    "wire-symlink", "wire-fifo", "wire-mode"])
def test_child_output_is_strict_json_and_bound_to_authorized_sources(setup, tmp_path, mode):
    adapter, _ = setup(factory_kwargs={"mode": mode})
    build(adapter, tmp_path / "index")
    pid = adapter.worker_pid
    fail("worker_protocol", lambda: adapter.search("pump", rows=ROWS))
    assert adapter.worker_pid is None
    assert_reaped(pid)
    assert not list(tmp_path.rglob("pickle-was-used"))


def test_rows_identity_and_input_bounds_are_checked_before_dispatch(setup, tmp_path):
    adapter, marker = setup()
    build(adapter, tmp_path / "index")
    pid = adapter.worker_pid
    changed = copy.deepcopy(ROWS)
    changed[0]["version"] += 1
    fail("identity_mismatch", lambda: adapter.search("pump", rows=changed))
    fail("identity_mismatch", lambda: adapter.search("pump", rows=ROWS, identity={**IDENTITY, "acl_digest": "f" * 64}))
    for kwargs in ({"limit": True}, {"limit": 101}, {"timeout_seconds": 0}, {"timeout_seconds": float("nan")}):
        fail("invalid_input", lambda: adapter.search("pump", rows=ROWS, **kwargs))
    fail("invalid_input", lambda: adapter.search("x" * 2001, rows=ROWS))
    assert not (marker / "started").exists()
    assert adapter.worker_pid == pid


def test_ignored_sql_columns_are_not_transmitted(setup, tmp_path):
    adapter, _ = setup()
    rows = copy.deepcopy(ROWS)
    rows[0]["vector"] = object()
    rows[0]["terms"] = object()
    adapter.build(rows, identity=IDENTITY, index_dir=tmp_path / "index")
    assert adapter.search("pump", rows=rows)[0]


def test_seal_and_restore_new_worker_without_reindexing(setup, tmp_path):
    adapter, _ = setup()
    original = tmp_path / "index"
    build(adapter, original)
    pid = adapter.worker_pid
    receipt = adapter.seal()
    assert receipt["source_dir"] == str(original)
    assert adapter.worker_pid is None
    assert_reaped(pid)
    fail("index_not_ready", lambda: adapter.search("pump", rows=ROWS))
    recovered, marker = setup()
    stats = recovered.restore(ROWS, identity=IDENTITY, source_dir=original, receipt=receipt,
                              index_dir=tmp_path / "restored")
    assert stats["source_count"] == 2
    assert recovered.search("pump", rows=ROWS)[0] == {"chunk-a": 0.5, "chunk-b": 0.5}
    assert (marker / "started").exists()
    fail("timeout", lambda: recovered.search("hang", rows=ROWS, timeout_seconds=0.1))
    recovered.restore(ROWS, identity=IDENTITY, source_dir=original, receipt=receipt, index_dir=tmp_path / "restored-again")
    assert recovered.search("pump", rows=ROWS)[0]


def test_restore_bad_receipt_cannot_launch_factory(setup, tmp_path):
    adapter, marker = setup()
    fail("index_untrusted", lambda: adapter.restore(ROWS, identity=IDENTITY, source_dir=tmp_path / "source",
         receipt={"not": "trusted"}, index_dir=tmp_path / "dest"))
    assert adapter.worker_pid is None and not (marker / "pid").exists()


@pytest.mark.parametrize("key,value", [("file_count", MAX_INDEX_FILES + 1),
    ("total_bytes", MAX_INDEX_BYTES + 1), ("file_count", True), ("owner_uid", True)])
def test_receipt_limits_match_core_adapter(setup, tmp_path, key, value):
    adapter, _ = setup()
    build(adapter, tmp_path / "index")
    receipt = adapter.seal()
    receipt[key] = value
    recovered, marker = setup()
    fail("index_untrusted", lambda: recovered.restore(ROWS, identity=IDENTITY, source_dir=tmp_path / "index",
         receipt=receipt, index_dir=tmp_path / "dest"))
    assert recovered.worker_pid is None and not (marker / "pid").exists()


def test_rejected_oversize_request_does_not_desynchronize_sequence(setup, tmp_path, monkeypatch):
    adapter, _ = setup()
    build(adapter, tmp_path / "index")
    import app.hipporag_process as process
    maximum = process.MAX_REQUEST_BYTES
    monkeypatch.setattr(process, "MAX_REQUEST_BYTES", 100)
    fail("invalid_input", lambda: adapter.search("pump", rows=ROWS))
    monkeypatch.setattr(process, "MAX_REQUEST_BYTES", maximum)
    assert adapter.search("pump", rows=ROWS)[0]


@pytest.mark.parametrize("kwargs", [{"allow_network": 1}, {"factory_kwargs": {"bad": float("nan")}},
    {"factory_kwargs": {"bad": object()}}, {"module_paths": ["/missing-explicit-module-path"]},
    {"factory_name": "factory;bad"}, {"factory_module": "bad/module"}, {"search_timeout_seconds": -1}])
def test_invalid_profile_is_rejected_without_side_effects(tmp_path, kwargs):
    defaults = {"work_root": tmp_path, "factory_module": "test_module", "factory_name": "factory"}
    fail("invalid_input", lambda: HippoRAGProcessAdapter(**{**defaults, **kwargs}))
    assert list(tmp_path.iterdir()) == []


def test_explicit_official_composition_factory_does_not_choose_a_model(tmp_path):
    fail("invalid_input", lambda: create_official_adapter(engine_factory="not-a-reference", chunk_factory="x:y"))


def test_default_control_close_is_idempotent_without_worker(tmp_path):
    adapter = HippoRAGProcessAdapter(work_root=tmp_path)
    adapter.close()
    adapter.close()
    fail("index_not_ready", lambda: build(adapter, tmp_path / "index"))
    assert not list(tmp_path.iterdir())


def test_close_retries_owned_worker_when_first_bounded_reap_times_out(tmp_path, monkeypatch):
    import app.hipporag_process as process_module
    adapter = HippoRAGProcessAdapter(work_root=tmp_path)
    directory = tmp_path / "owned-worker"
    directory.mkdir()
    signals = []

    class DelayedReap:
        pid = 123456789
        returncode = None
        waits = 0

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("synthetic-worker", timeout)
            self.returncode = -9
            return -9

    owned = DelayedReap()
    adapter._process, adapter._directory = owned, directory
    monkeypatch.setattr(process_module, "_child_status", lambda _: "exited")
    monkeypatch.setattr(process_module.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    fail("worker_failed", adapter.close)
    assert adapter._process is owned and directory.exists()
    adapter.close()
    assert owned.waits == 2 and adapter._process is None
    assert not directory.exists()
    assert len(signals) == 4


def test_external_reap_during_term_grace_does_not_trigger_late_group_kill(tmp_path, monkeypatch):
    import app.hipporag_process as process_module
    adapter = HippoRAGProcessAdapter(work_root=tmp_path)
    signals = []
    statuses = iter(["running", "reaped", "reaped"])

    class ExternallyReaped:
        pid = 123456789
        returncode = None

        def wait(self, timeout):
            return 0

    adapter._process = ExternallyReaped()
    monkeypatch.setattr(process_module, "_child_status", lambda _: next(statuses))
    monkeypatch.setattr(process_module.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    adapter._stop()
    assert [sig for _, sig in signals] == [process_module.signal.SIGTERM]
    assert adapter._process is None
