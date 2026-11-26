"""Synthetic scope lifecycle tests; no product or provider access."""
from dataclasses import replace
from pathlib import Path
import multiprocessing
import os

import pytest

from app.mem0_index import Mem0IndexConfig, Mem0IndexDisabled, Mem0IndexUnavailable, TrustedScope
from app.mem0_scope_store import ScopeStore


def scope(user="a", cid="b", revision=1):
    return TrustedScope(user, cid, 1, revision, "a"*64)


def store(tmp_path):
    return ScopeStore(tmp_path, Mem0IndexConfig(enabled=True))


def test_disabled_and_read_does_not_create(tmp_path):
    with pytest.raises(Mem0IndexDisabled):
        ScopeStore(tmp_path).provision(scope())
    s = store(tmp_path)
    with pytest.raises(Mem0IndexUnavailable), s.lease(scope()):
        pass
    assert not s.root.exists()


def test_stable_binding_and_distinct_scopes(tmp_path):
    s = store(tmp_path)
    items = [scope(), scope("c"), scope(cid="c")]
    paths = []
    for item in items:
        s.provision(item)
        with s.lease(item) as p:
            paths.append(p)
    assert len(set(paths)) == 3
    s2 = store(tmp_path)
    next_revision = replace(items[0], revision=2, native_epoch=2)
    s2.provision(next_revision)
    with s2.lease(next_revision) as p:
        assert p == paths[0]
        assert p.history.stat().st_mode & 0o777 == 0o600


def test_retirement_survives_reopen_and_missing_directory(tmp_path):
    s = store(tmp_path)
    s.provision(scope())
    with s.lease(scope()) as p:
        p.history.write_text("synthetic content")
    s.retire(scope())
    s.retire(scope())
    assert p.history.read_text() == "synthetic content"
    p.history.unlink()
    p.vectors.rmdir()
    p.history.parent.rmdir()
    for operation in (lambda: store(tmp_path).provision(scope()),
                      lambda: store(tmp_path).provision(scope(revision=2))):
        with pytest.raises(Mem0IndexUnavailable):
            operation()
    with pytest.raises(Mem0IndexUnavailable), store(tmp_path).lease(scope()):
        pass
    assert not p.history.parent.exists()


def test_missing_marker_or_replaced_directory_fails_closed(tmp_path):
    s = store(tmp_path)
    s.provision(scope())
    with s.lease(scope()) as p:
        directory = p.history.parent
    marker = directory.with_suffix(".json")
    marker.unlink()
    with pytest.raises(Mem0IndexUnavailable):
        s.provision(scope())
    s.provision(scope("other"))
    with s.lease(scope("other")) as q:
        d = q.history.parent
    d.rename(d.with_name("old"))
    d.mkdir(mode=0o700)
    with pytest.raises(Mem0IndexUnavailable), s.lease(scope("other")):
        pass


@pytest.mark.parametrize("target", ["root", "directory", "marker", "vectors", "history"])
def test_symlink_rejected(tmp_path, target):
    s = store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    if target == "root":
        s.root.symlink_to(outside, target_is_directory=True)
        with pytest.raises(Mem0IndexUnavailable):
            s.provision(scope())
    else:
        s.provision(scope())
        with s.lease(scope()) as p:
            d = p.history.parent
        path = {"directory": d, "marker": d.with_suffix(".json"),
                "vectors": p.vectors, "history": p.history}[target]
        path.rename(path.with_name(path.name + ".old"))
        path.symlink_to(outside)
        with pytest.raises(Mem0IndexUnavailable), s.lease(scope()):
            pass
    assert list(outside.iterdir()) == []


def _competing(root, queue):
    s = store(Path(root))
    try:
        s.retire(scope())
    except Mem0IndexUnavailable:
        queue.put("busy")
    else:
        queue.put("retired")


def test_independent_process_cannot_retire_while_leased(tmp_path):
    s = store(tmp_path)
    s.provision(scope())
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    with s.lease(scope()):
        proc = ctx.Process(target=_competing, args=(str(tmp_path), queue))
        proc.start()
        proc.join(10)
        assert proc.exitcode == 0
        assert queue.get(timeout=2) == "busy"
    queue.close()
    queue.join_thread()
    s.retire(scope())
    with pytest.raises(Mem0IndexUnavailable), s.lease(scope()):
        pass


def test_hard_link_and_public_permissions_rejected(tmp_path):
    s = store(tmp_path)
    s.provision(scope())
    with s.lease(scope()) as p:
        pass
    extra = tmp_path / "extra"
    os.link(p.history, extra)
    with pytest.raises(Mem0IndexUnavailable), s.lease(scope()):
        pass
    extra.unlink()
    p.history.chmod(0o644)
    with pytest.raises(Mem0IndexUnavailable), s.lease(scope()):
        pass
