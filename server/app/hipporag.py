"""Opt-in source binding for the official HippoRAG algorithm.

Factories are supplied by a trusted caller; importing this module never imports
HippoRAG or starts a model. The caller authorizes rows before and after search.
Build owns a fresh index. Restore requires an externally trusted receipt and
copies verified bytes into another fresh directory before loading any pickle.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
import uuid
from collections.abc import Mapping
from functools import wraps
from numbers import Real
from pathlib import Path


PINNED_COMMIT = "d5c8329422e0a0b834a15874545cb6a74b4f9b26"
MAX_ROWS = 5000
MAX_TEXT_BYTES = 65536
MAX_SNAPSHOT_BYTES = 16_000_000
MAX_QUERY_BYTES = 8192
MAX_RESULTS = 100
MAX_MANIFEST_BYTES = 8_000_000
MAX_INDEX_FILES = 10000
MAX_INDEX_ENTRIES = 20000
MAX_INDEX_FILE_BYTES = 128_000_000
MAX_INDEX_BYTES = 512_000_000
MAX_INDEX_DEPTH = 16
MAX_SEAL_BYTES = 4_000_000
MAX_RECEIPT_BYTES = 8192
_SEAL_NAME = "seal.json"
_RECEIPT_FIELDS = {"schema", "source_dir", "owner_uid", "identity_digest", "snapshot_digest",
                   "tree_digest", "file_count", "total_bytes"}
_DIGEST = re.compile(r"[0-9a-f]{64}")
_IDENTITY_FIELDS = {"parser_digest", "extraction_digest", "embedding_digest", "chunking_digest", "acl_digest"}
_SOURCE_STRINGS = ("name", "section", "imported_at", "knowledge_base")
_SOURCE_NUMBERS = ("page", "paragraph", "start", "end")
_ROW_FIELDS = {"id", "kb_id", "document_id", "version", "sha256", "text", *_SOURCE_STRINGS, *_SOURCE_NUMBERS,
               "terms", "vector", "embedding_model", "lexical_score", "vector_score"}
_ERRORS = {
    "unavailable": "Official HippoRAG factories are not configured",
    "invalid_input": "HippoRAG input is invalid or exceeds the supported limits",
    "identity_mismatch": "HippoRAG identity or authorized source snapshot changed",
    "index_untrusted": "HippoRAG index directory or manifest is not trusted",
    "index_failed": "HippoRAG index construction failed",
    "index_not_ready": "HippoRAG index is not ready",
    "result_invalid": "HippoRAG returned an invalid or unbound result",
    "retrieval_failed": "HippoRAG retrieval failed",
    "timeout": "HippoRAG worker exceeded its operation deadline",
    "cancelled": "HippoRAG operation was cancelled",
    "worker_failed": "HippoRAG worker failed",
    "worker_protocol": "HippoRAG worker returned an invalid protocol message",
}


class HippoRAGError(Exception):
    def __init__(self, code):
        if code not in _ERRORS:
            raise ValueError("Unknown HippoRAG error code")
        self.code = code
        super().__init__(_ERRORS[code])


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise HippoRAGError("invalid_input") from None


def _encoded(value):
    try:
        return value.encode("utf-8")
    except UnicodeError:
        raise HippoRAGError("invalid_input") from None


def _locked(method):
    @wraps(method)
    def operation(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return operation


def _digest(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _identity(value):
    if (type(value) is not dict or set(value) != _IDENTITY_FIELDS | {"upstream_commit"}
            or value["upstream_commit"] != PINNED_COMMIT
            or any(type(value[key]) is not str or not _DIGEST.fullmatch(value[key]) for key in _IDENTITY_FIELDS)):
        raise HippoRAGError("invalid_input")
    return dict(value)


def _snapshot(rows):
    if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= MAX_ROWS:
        raise HippoRAGError("invalid_input")
    sources, texts, documents, total = {}, {}, {}, 0
    for row in rows:
        if not isinstance(row, Mapping) or set(row) - _ROW_FIELDS:
            raise HippoRAGError("invalid_input")
        entry = {}
        for key in ("id", "kb_id", "document_id"):
            value = row.get(key)
            if type(value) is not str or not value or len(value) > 200 or any(ord(char) < 32 for char in value):
                raise HippoRAGError("invalid_input")
            entry[key] = value
        if entry["id"] in sources:
            raise HippoRAGError("invalid_input")
        version, sha, text = row.get("version"), row.get("sha256"), row.get("text")
        if (type(version) is not int or not 1 <= version <= 1_000_000_000
                or type(sha) is not str or not _DIGEST.fullmatch(sha)
                or type(text) is not str or not text.strip()):
            raise HippoRAGError("invalid_input")
        size = len(_encoded(text))
        total += size
        if size > MAX_TEXT_BYTES or total > MAX_SNAPSHOT_BYTES:
            raise HippoRAGError("invalid_input")
        entry.update(version=version, sha256=sha, text_sha256=hashlib.sha256(_encoded(text)).hexdigest())
        for key in _SOURCE_STRINGS:
            value = row.get(key)
            if value is not None and (type(value) is not str or len(_encoded(value)) > 4096):
                raise HippoRAGError("invalid_input")
            total += len(_encoded(value)) if value is not None else 0
            if total > MAX_SNAPSHOT_BYTES:
                raise HippoRAGError("invalid_input")
            entry[key] = value
        for key in _SOURCE_NUMBERS:
            value = row.get(key)
            if value is not None and (type(value) is not int or not 0 <= value <= 1_000_000_000):
                raise HippoRAGError("invalid_input")
            entry[key] = value
        if entry["start"] is not None and entry["end"] is not None and entry["start"] > entry["end"]:
            raise HippoRAGError("invalid_input")
        document_key = (entry["document_id"], version)
        document_identity = (entry["kb_id"], sha, entry["name"], entry["imported_at"])
        if document_key in documents and documents[document_key] != document_identity:
            raise HippoRAGError("invalid_input")
        documents[document_key] = document_identity
        sources[entry["id"]] = entry
        texts.setdefault(text, []).append(entry["id"])
    ordered = [sources[key] for key in sorted(sources)]
    snapshot_digest = _digest(ordered)
    groups = {}
    for text, ids in texts.items():
        ids.sort()
        group_id = _digest({"snapshot": snapshot_digest, "text": text, "sources": ids})
        groups[group_id] = {"text": text, "ids": ids, "metadata": {
            "adapter_snapshot": snapshot_digest, "adapter_group": group_id, "source_id": group_id,
        }}
    return ordered, groups, snapshot_digest


def _manifest(identity, sources, groups, snapshot_digest):
    value = {
        "schema": "workspace-hipporag/v1", "state": "ready", "identity": identity,
        "identity_digest": _digest({"identity": identity, "snapshot": snapshot_digest}),
        "snapshot_digest": snapshot_digest, "sources": sources,
        "groups": {key: {"source_ids": group["ids"], "text_sha256": hashlib.sha256(_encoded(group["text"])).hexdigest()}
                   for key, group in sorted(groups.items())},
    }
    encoded = _json(value)
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise HippoRAGError("invalid_input")
    return encoded


def _absolute_directory(value):
    try:
        directory = Path(value).absolute()
        if ".." in directory.parts:
            raise ValueError()
        return directory
    except (ValueError, TypeError, OSError):
        raise HippoRAGError("invalid_input") from None


def _directory_chain(directory):
    identities = {}
    for path in (*directory.parents, directory):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError()
        identities[path] = (info.st_dev, info.st_ino)
    return identities


def _read_private(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_uid != os.geteuid() or before.st_nlink != 1 or before.st_size > maximum):
            raise ValueError()
        contents = source.read(maximum + 1)
        after = os.fstat(source.fileno())
        if len(contents) != before.st_size or _file_state(before) != _file_state(after):
            raise ValueError()
        return contents


def _file_state(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _directory_names(fd):
    names = []
    with os.scandir(fd) as children:
        for child in children:
            names.append(child.name)
            if len(names) > MAX_INDEX_ENTRIES:
                raise ValueError()
    return sorted(names)


def _tree(directory, *, normalize=False, destination=None):
    """Inspect/copy only regular owned files through no-follow directory FDs.

    seal.json is separately authenticated by the receipt's hash. Everything
    else, including empty directories and manifest.json, must appear here.
    """
    entries, file_count, total = [], 0, 0
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def walk(fd, prefix, target_fd=None, depth=0):
        nonlocal file_count, total
        before = os.fstat(fd)
        if depth > MAX_INDEX_DEPTH or before.st_uid != os.geteuid() or not stat.S_ISDIR(before.st_mode):
            raise ValueError()
        if normalize:
            os.fchmod(fd, 0o700)
            before = os.fstat(fd)
        if stat.S_IMODE(before.st_mode) != 0o700:
            raise ValueError()
        entries.append({"path": prefix or ".", "kind": "directory", "mode": 0o700, "uid": before.st_uid})
        names = _directory_names(fd)
        if len(entries) + len(names) > MAX_INDEX_ENTRIES:
            raise ValueError()
        for name in names:
            if not prefix and name == _SEAL_NAME:
                continue
            relative = f"{prefix}/{name}" if prefix else name
            if len(_encoded(relative)) > 2048 or any(ord(char) < 32 for char in name):
                raise ValueError()
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if info.st_uid != os.geteuid():
                raise ValueError()
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, flags, dir_fd=fd)
                target_child = None
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise ValueError()
                    if target_fd is not None:
                        os.mkdir(name, mode=0o700, dir_fd=target_fd)
                        target_child = os.open(name, flags, dir_fd=target_fd)
                    walk(child, relative, target_child, depth + 1)
                finally:
                    os.close(child)
                    if target_child is not None:
                        os.close(target_child)
            elif stat.S_ISREG(info.st_mode):
                source_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(source_fd, "rb") as source:
                    opened = os.fstat(source.fileno())
                    if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                            or not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid()
                            or opened.st_nlink != 1 or opened.st_size > MAX_INDEX_FILE_BYTES):
                        raise ValueError()
                    if normalize:
                        os.fchmod(source.fileno(), 0o600)
                        opened = os.fstat(source.fileno())
                    if stat.S_IMODE(opened.st_mode) != 0o600:
                        raise ValueError()
                    file_count += 1
                    if file_count >= MAX_INDEX_FILES:
                        raise ValueError()
                    output = None
                    try:
                        if target_fd is not None:
                            output = os.fdopen(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                                      0o600, dir_fd=target_fd), "wb")
                        digest, size = hashlib.sha256(), 0
                        while True:
                            block = source.read(65536)
                            if not block:
                                break
                            size += len(block)
                            total += len(block)
                            if size > MAX_INDEX_FILE_BYTES or total > MAX_INDEX_BYTES:
                                raise ValueError()
                            digest.update(block)
                            if output is not None:
                                output.write(block)
                        if output is not None:
                            output.flush()
                            os.fsync(output.fileno())
                    finally:
                        if output is not None:
                            output.close()
                    if size != opened.st_size or _file_state(opened) != _file_state(os.fstat(source.fileno())):
                        raise ValueError()
                entries.append({"path": relative, "kind": "file", "mode": 0o600, "uid": info.st_uid,
                                "size": size, "sha256": digest.hexdigest()})
            else:
                raise ValueError()
            if len(entries) > MAX_INDEX_ENTRIES:
                raise ValueError()
        after = os.fstat(fd)
        if (names != _directory_names(fd) or (before.st_dev, before.st_ino, before.st_mode, before.st_uid)
                != (after.st_dev, after.st_ino, after.st_mode, after.st_uid)):
            raise ValueError()

    source_fd, target_fd = os.open(directory, flags), None
    try:
        if destination is not None:
            target_fd = os.open(destination, flags)
        walk(source_fd, "", target_fd)
    finally:
        os.close(source_fd)
        if target_fd is not None:
            os.close(target_fd)
    return sorted(entries, key=lambda item: item["path"]), file_count, total


def _publish(directory, name, data):
    temporary = directory / f".{name}-{uuid.uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, directory / name)
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


class HippoRAGAdapter:
    """One immutable authorized snapshot; no implicit dependency or model use.

    ``engine_factory(save_dir=...)`` must return the pinned official engine;
    ``chunk_factory`` is its official Chunk class. The trusted caller verifies
    the source revision and that model configuration matches the identity; this
    adapter cannot attest an injected factory. Tests may explicitly inject
    doubles. Construction and retrieval deadlines belong to the isolated worker
    hosting these synchronous factories, not a pretend cancellable thread timer.
    """

    def __init__(self, *, engine_factory=None, chunk_factory=None):
        self._lock = threading.RLock()
        self.engine_factory, self.chunk_factory = engine_factory, chunk_factory
        self._engine = None
        self._ready = False
        self._directory = None
        self._manifest = None
        self._identity = None
        self._snapshot_digest = None
        self._groups = {}
        self._source_count = 0
        self._seal_manifest = None

    def _create_directory(self, directory):
        try:
            # A private host-created root is required, not a user-supplied cache.
            self._path_identities = {}
            for parent in directory.parents:
                info = parent.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise OSError()
                self._path_identities[parent] = (info.st_dev, info.st_ino)
            directory.mkdir(mode=0o700)
            created = directory.lstat()
            if not stat.S_ISDIR(created.st_mode):
                raise OSError()
        except OSError:
            raise HippoRAGError("index_untrusted") from None
        self._directory = directory
        self._path_identities[directory] = (created.st_dev, created.st_ino)

    @_locked
    def build(self, rows, *, identity, index_dir):
        if not callable(self.engine_factory) or not callable(self.chunk_factory):
            raise HippoRAGError("unavailable")
        if self._directory is not None or self._engine is not None:
            raise HippoRAGError("index_untrusted")
        identity = _identity(identity)
        sources, groups, snapshot_digest = _snapshot(rows)
        directory = _absolute_directory(index_dir)
        serialized = _manifest(identity, sources, groups, snapshot_digest)
        self._create_directory(directory)
        try:
            upstream = directory / "upstream"
            upstream.mkdir(mode=0o700)
            self._engine = self.engine_factory(save_dir=str(upstream))
            chunks = [self.chunk_factory(content=group["text"], source_id=key, metadata={
                "adapter_snapshot": snapshot_digest, "adapter_group": key,
            }) for key, group in sorted(groups.items())]
            self._engine.index(chunks)
            if _snapshot(rows)[2] != snapshot_digest:
                raise HippoRAGError("identity_mismatch")
            self._check_paths()
            # The path remains stable for the official engine's later writes.
            # Only a fully built snapshot receives an atomically published ready marker.
            _publish(directory, "manifest.json", serialized)
            self._identity, self._snapshot_digest = identity, snapshot_digest
            self._manifest, self._groups = serialized, groups
            self._source_count, self._ready = len(sources), True
            return self._stats()
        except Exception:
            self.close()
            raise HippoRAGError("index_failed") from None

    def _stats(self, *, returned=0, graph_seeds=0):
        return {"backend": "official", "algorithm": "hipporag2-official",
                "identity_digest": _digest({"identity": self._identity, "snapshot": self._snapshot_digest}),
                "source_count": self._source_count, "unique_text_count": len(self._groups),
                "returned_count": returned, "graph_seed_count": graph_seeds}

    def _check_paths(self):
        for path, expected in self._path_identities.items():
            current = path.lstat()
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != expected:
                raise ValueError()

    def _verify(self, rows, identity):
        if not self._ready or self._engine is None:
            raise HippoRAGError("index_not_ready")
        if identity is not None and _identity(identity) != self._identity:
            raise HippoRAGError("identity_mismatch")
        _, _, snapshot_digest = _snapshot(rows)
        if snapshot_digest != self._snapshot_digest:
            raise HippoRAGError("identity_mismatch")
        self._verify_storage()

    def _verify_storage(self):
        try:
            self._check_paths()
            if _read_private(self._directory / "manifest.json", MAX_MANIFEST_BYTES) != self._manifest:
                raise ValueError()
        except Exception:
            raise HippoRAGError("index_untrusted") from None

    @_locked
    def seal(self):
        """Flush and close this instance, returning a host-held recovery receipt.

        The receipt is not a signature. Keep it outside the cache in a trusted
        host store; never accept a receipt supplied by a cache owner or model.
        No search is allowed after sealing, even if sealing itself fails.
        """
        if not self._ready or self._engine is None:
            raise HippoRAGError("index_not_ready")
        engine, self._engine, self._ready = self._engine, None, False
        storage_error = None
        try:
            self._verify_storage()
        except HippoRAGError as exc:
            storage_error = exc
        try:
            close = getattr(engine, "close", None)
            if not callable(close):
                raise ValueError()
            close()
        except Exception:
            raise HippoRAGError("index_failed") from None
        if storage_error is not None:
            raise storage_error
        try:
            self._verify_storage()
            seal_path = self._directory / _SEAL_NAME
            if self._seal_manifest is None:
                if os.path.lexists(seal_path):
                    raise ValueError()
            elif _read_private(seal_path, MAX_SEAL_BYTES) != self._seal_manifest:
                raise ValueError()
            entries, files, size = _tree(self._directory, normalize=True)
            sealed = _json({"schema": "workspace-hipporag-tree/v1", "entries": entries})
            if len(sealed) > MAX_SEAL_BYTES or size + len(sealed) > MAX_INDEX_BYTES:
                raise ValueError()
            self._check_paths()
            _publish(self._directory, _SEAL_NAME, sealed)
            checked, _, _ = _tree(self._directory)
            if checked != entries or _read_private(seal_path, MAX_SEAL_BYTES) != sealed:
                raise ValueError()
            self._seal_manifest = sealed
            receipt = {"schema": "workspace-hipporag-receipt/v1", "source_dir": str(self._directory),
                       "owner_uid": os.geteuid(), "identity_digest": self._stats()["identity_digest"],
                       "snapshot_digest": self._snapshot_digest, "tree_digest": hashlib.sha256(sealed).hexdigest(),
                       "file_count": files + 1, "total_bytes": size + len(sealed)}
            if len(_json(receipt)) > MAX_RECEIPT_BYTES:
                raise ValueError()
            return receipt
        except Exception:
            raise HippoRAGError("index_untrusted") from None

    @_locked
    def restore(self, rows, *, identity, source_dir, receipt, index_dir):
        """Validate a host-held receipt, copy verified bytes, then open the copy.

        All recovery validation precedes engine_factory. The original source
        directory is never chmodded or opened by the official engine. This is a
        trusted-host recovery contract, not an OS sandbox against the same UID.
        """
        if not callable(self.engine_factory) or not callable(self.chunk_factory):
            raise HippoRAGError("unavailable")
        if self._directory is not None or self._engine is not None:
            raise HippoRAGError("index_untrusted")
        identity = _identity(identity)
        sources, groups, snapshot_digest = _snapshot(rows)
        source, directory = _absolute_directory(source_dir), _absolute_directory(index_dir)
        serialized = _manifest(identity, sources, groups, snapshot_digest)
        expected_identity = _digest({"identity": identity, "snapshot": snapshot_digest})
        if (type(receipt) is not dict or set(receipt) != _RECEIPT_FIELDS
                or receipt.get("schema") != "workspace-hipporag-receipt/v1"
                or type(receipt.get("source_dir")) is not str or receipt["source_dir"] != str(source)
                or type(receipt.get("owner_uid")) is not int or receipt["owner_uid"] != os.geteuid()
                or any(type(receipt.get(key)) is not str or not _DIGEST.fullmatch(receipt[key])
                       for key in ("identity_digest", "snapshot_digest", "tree_digest"))
                or type(receipt.get("file_count")) is not int or not 1 <= receipt["file_count"] <= MAX_INDEX_FILES
                or type(receipt.get("total_bytes")) is not int or not 1 <= receipt["total_bytes"] <= MAX_INDEX_BYTES):
            raise HippoRAGError("index_untrusted")
        if len(_json(receipt)) > MAX_RECEIPT_BYTES:
            raise HippoRAGError("index_untrusted")
        if receipt["identity_digest"] != expected_identity or receipt["snapshot_digest"] != snapshot_digest:
            raise HippoRAGError("identity_mismatch")
        # A nested destination would mutate the source while copying it.
        if directory == source or directory.is_relative_to(source) or source.is_relative_to(directory):
            raise HippoRAGError("index_untrusted")
        try:
            source_chain = _directory_chain(source)
            sealed = _read_private(source / _SEAL_NAME, MAX_SEAL_BYTES)
            if hashlib.sha256(sealed).hexdigest() != receipt["tree_digest"]:
                raise ValueError()
            entries, files, size = _tree(source)
            if (_json({"schema": "workspace-hipporag-tree/v1", "entries": entries}) != sealed
                    or files + 1 != receipt["file_count"] or size + len(sealed) != receipt["total_bytes"]
                    or _read_private(source / "manifest.json", MAX_MANIFEST_BYTES) != serialized):
                raise ValueError()
            self._create_directory(directory)
            copied, _, _ = _tree(source, destination=directory)
            if copied != entries or _directory_chain(source) != source_chain:
                raise ValueError()
            if _read_private(source / _SEAL_NAME, MAX_SEAL_BYTES) != sealed:
                raise ValueError()
            _publish(directory, _SEAL_NAME, sealed)
            checked, _, _ = _tree(directory)
            if checked != entries or _read_private(directory / _SEAL_NAME, MAX_SEAL_BYTES) != sealed:
                raise ValueError()
            if _read_private(directory / "manifest.json", MAX_MANIFEST_BYTES) != serialized:
                raise ValueError()
            self._check_paths()
        except Exception:
            raise HippoRAGError("index_untrusted") from None
        if _snapshot(rows)[2] != snapshot_digest:
            raise HippoRAGError("identity_mismatch")
        try:
            self._identity, self._snapshot_digest = identity, snapshot_digest
            self._manifest, self._groups, self._seal_manifest = serialized, groups, sealed
            self._source_count = len(sources)
            self._engine = self.engine_factory(save_dir=str(directory / "upstream"))
            self._verify_storage()
            self._ready = True
            return self._stats()
        except Exception:
            self.close()
            raise HippoRAGError("index_failed") from None

    @_locked
    def search(self, query, *, rows, identity=None, limit=6):
        if (type(query) is not str or not query.strip() or len(query) > 2000 or len(_encoded(query)) > MAX_QUERY_BYTES
                or type(limit) is not int or not 1 <= limit <= MAX_RESULTS):
            raise HippoRAGError("invalid_input")
        self._verify(rows, identity)
        count = min(limit, len(self._groups))
        try:
            results = self._engine.retrieve([query], num_to_retrieve=count)
        except Exception:
            raise HippoRAGError("retrieval_failed") from None
        self._verify(rows, identity)
        try:
            if type(results) is not list or len(results) != 1:
                raise ValueError()
            result = results[0]
            docs, scores, metadata = result.docs, result.doc_scores, result.doc_metadata
            if (result.question != query or type(docs) is not list or type(metadata) is not list
                    or isinstance(scores, (str, bytes, dict))
                    or not len(docs) == len(scores) == len(metadata) or len(docs) > count):
                raise ValueError()
            seeds = getattr(result, "graph_seeds", None)
            if seeds is not None and (not isinstance(seeds, (list, tuple)) or len(seeds) > 10000):
                raise ValueError()
            mapped, seen = {}, set()
            for text, score, item in zip(docs, scores, metadata):
                if type(item) is not dict or type(text) is not str:
                    raise ValueError()
                group_id = item.get("adapter_group")
                group = self._groups.get(group_id)
                if (not group or group_id in seen or item != group["metadata"] or text != group["text"]
                        or isinstance(score, bool) or not isinstance(score, Real)
                        or not math.isfinite(float(score)) or abs(float(score)) > 1_000_000_000_000):
                    raise ValueError()
                seen.add(group_id)
                for source_id in group["ids"]:
                    if len(mapped) < limit:
                        mapped[source_id] = float(score)
            # Deliberately do not return graph_seeds: they are candidates, not
            # source-verified entity relations or licensed evidence links.
            return mapped, self._stats(returned=len(mapped), graph_seeds=len(seeds) if seeds is not None else 0)
        except Exception:
            raise HippoRAGError("result_invalid") from None

    @_locked
    def close(self):
        engine, self._engine, self._ready = self._engine, None, False
        if engine is not None:
            close = getattr(engine, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
