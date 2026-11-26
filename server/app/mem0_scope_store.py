"""Private, persistent local scope binding; not a product or purge endpoint.

Callers must supply a host-authorized TrustedScope. Lifecycle state is separate
from SDK data so retiring a scope cannot be undone by opening a missing path.
Linux cooperative locking only; the host data directory and same-UID processes
are trusted. No SDK, provider, customer data or physical deletion here.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import uuid

from .mem0_index import (
    Mem0IndexConfig, Mem0IndexDisabled, Mem0IndexUnavailable,
    TrustedScope, canonical,
)


@dataclass(frozen=True)
class ScopePaths:
    vectors: Path
    history: Path


class ScopeStore:
    def __init__(self, data_dir: Path, config: Mem0IndexConfig = Mem0IndexConfig()):
        if type(config) is not Mem0IndexConfig:
            raise TypeError("invalid Mem0 config")
        self.config = config
        self.root = Path(data_dir).absolute() / ".mem0-scopes-v1"

    def _key(self, scope: TrustedScope) -> str:
        if not self.config.enabled:
            raise Mem0IndexDisabled("Mem0 scope store disabled")
        if type(scope) is not TrustedScope:
            raise Mem0IndexUnavailable("host scope required")
        # Stable across approved memory revisions/epochs. Retirement is terminal
        # for this identity; a new generation requires a separate policy.
        return hashlib.sha256(canonical(
            [1, scope.user_id, self.config.agent_id, scope.conversation_id]
        )).hexdigest()

    @staticmethod
    def _private_dir(info):
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise Mem0IndexUnavailable("invalid private scope directory")

    @contextmanager
    def _locked(self, *, create=False):
        if not self.config.enabled:
            raise Mem0IndexDisabled("Mem0 scope store disabled")
        fd = None
        try:
            if self.root.parent.resolve(strict=True) != self.root.parent:
                raise Mem0IndexUnavailable("invalid host data path")
            if create:
                self.root.mkdir(mode=0o700, exist_ok=True)
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            self._private_dir(os.fstat(fd))
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Do not operate on a substituted root.
            info = self.root.lstat()
            if (info.st_dev, info.st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
                raise Mem0IndexUnavailable("scope root changed")
            yield fd
        except OSError:
            raise Mem0IndexUnavailable("scope store unavailable or busy") from None
        finally:
            if fd is not None:
                os.close(fd)

    @staticmethod
    def _read(fd, key):
        try:
            marker = os.open(key + ".json", os.O_RDONLY | os.O_NOFOLLOW |
                             os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(marker)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > 1024):
                raise Mem0IndexUnavailable("invalid scope binding")
            with os.fdopen(marker, "rb", closefd=False) as stream:
                raw = stream.read(1025)
            try:
                item = json.loads(raw)
            except (ValueError, UnicodeError):
                raise Mem0IndexUnavailable("invalid scope binding") from None
            if (type(item) is not dict or set(item) not in ({"key", "state", "dev", "ino"},
                                         {"key", "state", "dev", "ino", "clear_proof"})
                    or item["key"] != key or item["state"] not in ("active", "retired")
                    or type(item["dev"]) is not int or type(item["ino"]) is not int
                    or ("clear_proof" in item and (item["state"] != "retired"
                        or type(item["clear_proof"]) is not str
                        or re.fullmatch(r"[0-9a-f]{64}", item["clear_proof"]) is None))):
                raise Mem0IndexUnavailable("invalid scope binding")
            return item
        finally:
            os.close(marker)

    @staticmethod
    def _write(fd, key, item):
        temp = ".binding-" + uuid.uuid4().hex
        marker = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
        with os.fdopen(marker, "wb") as stream:
            stream.write(canonical(item))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, key + ".json", src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)

    def _paths(self, fd, key, item):
        if item is None or item["state"] != "active":
            raise Mem0IndexUnavailable("scope not active")
        directory = os.open(key, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                            os.O_CLOEXEC, dir_fd=fd)
        try:
            info = os.fstat(directory)
            self._private_dir(info)
            if (info.st_dev, info.st_ino) != (item["dev"], item["ino"]):
                raise Mem0IndexUnavailable("scope directory identity changed")
            # Do not follow pre-existing symlinks or hard-linked SQLite files.
            for name in ("vectors", "history.sqlite", "history.sqlite-wal",
                         "history.sqlite-shm", "history.sqlite-journal"):
                try:
                    child = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    if name in ("vectors", "history.sqlite"):
                        raise Mem0IndexUnavailable("scope storage missing") from None
                    continue
                if name == "vectors":
                    self._private_dir(child)
                elif (not stat.S_ISREG(child.st_mode) or child.st_uid != os.geteuid()
                      or child.st_nlink != 1 or stat.S_IMODE(child.st_mode) & 0o077):
                    raise Mem0IndexUnavailable("invalid scope storage file")
            path = self.root / key
            return ScopePaths(path / "vectors", path / "history.sqlite")
        finally:
            os.close(directory)

    def provision(self, scope: TrustedScope) -> None:
        """Explicit trusted maintenance only; no adoption of orphan directories."""
        key = self._key(scope)
        with self._locked(create=True) as fd:
            item = self._read(fd, key)
            if item is not None:
                self._paths(fd, key, item)
                return
            # Existing directory with missing marker is an uncertain operation.
            os.mkdir(key, mode=0o700, dir_fd=fd)
            info = os.stat(key, dir_fd=fd, follow_symlinks=False)
            self._private_dir(info)
            # Restrict SDK paths ahead of time; SDK defaults may be less strict.
            directory = os.open(key, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                                os.O_CLOEXEC, dir_fd=fd)
            try:
                os.mkdir("vectors", mode=0o700, dir_fd=directory)
                os.close(os.open("history.sqlite", os.O_CREAT | os.O_EXCL |
                                 os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory))
                os.fsync(directory)
            finally:
                os.close(directory)
            self._write(fd, key, {"key": key, "state": "active",
                                  "dev": info.st_dev, "ino": info.st_ino})

    @contextmanager
    def lease(self, scope: TrustedScope):
        """Resolve only. Caller must close any SDK before leaving this lease."""
        key = self._key(scope)
        with self._locked() as fd:
            yield self._paths(fd, key, self._read(fd, key))

    def retire(self, scope: TrustedScope) -> None:
        """Terminal metadata transition only. Does not read or delete SDK data.

        Not an authorization check; a future product caller must establish
        independent retirement/deletion authority.
        """
        key = self._key(scope)
        with self._locked() as fd:
            item = self._read(fd, key)
            if item is not None and item["state"] == "retired":
                return
            self._paths(fd, key, item)
            self._write(fd, key, {**item, "state": "retired"})


    def _retire_with_clear_proof(self, scope: TrustedScope, proof: str) -> str:
        """Internal transition; caller must hold a current authority transaction.

        Persist the admission proof in the same atomic marker as retirement.
        No SDK access or removal of bytes. An older unproven retirement cannot
        be upgraded into an authorized receipt after the fact.
        """
        if type(proof) is not str or re.fullmatch(r"[0-9a-f]{64}", proof) is None:
            raise Mem0IndexUnavailable("invalid retirement proof")
        key = self._key(scope)
        with self._locked() as fd:
            item = self._read(fd, key)
            if item is not None and item["state"] == "retired":
                if item.get("clear_proof") != proof:
                    raise Mem0IndexUnavailable("retirement proof mismatch")
                # Recover a lost acknowledgment only after confirming the
                # observed marker is durable, including the directory entry.
                os.fsync(fd)
                return proof
            self._paths(fd, key, item)
            self._write(fd, key, {**item, "state": "retired", "clear_proof": proof})
            observed = self._read(fd, key)
            if observed != {**item, "state": "retired", "clear_proof": proof}:
                raise Mem0IndexUnavailable("retirement receipt unconfirmed")
            return proof
