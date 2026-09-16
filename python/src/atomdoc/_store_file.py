"""A document store in a directory.

One file per document, replaced atomically, with the token and expiry in a
short header so a listing does not read every body. Suitable wherever there is
a real filesystem: a laptop, a VPS, a single container, and several processes
over the same directory — the lock is an `flock`, so any number of writers may
share one store. Not suitable on a network filesystem, where that lock is not
reliable, which rules out Modal volumes.

`durable=True` means a returned `put` survives the process. It does not mean it
survives the power going out: `os.fsync` on macOS is not `F_FULLFSYNC` and does
not flush the drive's own cache.

The expiry lives in the file's header, so `touch` rewrites the file: race-free
against a concurrent save, which is the point of it, but proportional to the
document's size — about a millisecond a megabyte. A store renewing leases on
very large documents wants `SqliteStore`, where a renewal touches one row.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import secrets
import threading
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

from ._store import (ABSENT, DocumentNotFound, Entry, StaleWrite, StoreBase,
                     StoreCapabilities, StoreClosed, StoreError, _Absent)

__all__ = ["FileStore"]

_MAGIC = b"atomdoc-store-1 "
# `^` cannot occur in a key, and the encoder below only ever emits it before a
# hex digit, so no encoded name can end in `^.doc`. That is what keeps `a` (a
# file) and `a.doc/b` (a directory named `a.doc`) from wanting the same path.
_LEAF = "^.doc"
# The store's own files are named so that no key can reach them: an encoded
# segment containing `^` always has something before it. A `.tmp` here would
# let the key `.tmp/x` be written into the temp directory and swept as litter.
_LOCK = "^lock"
_TMP = "^tmp"
_HEADER_MAX = 128           # magic + token + expiry, comfortably
_TMP_GRACE = 300.0          # a temp file older than this is a crashed write's


def _encode(segment: str) -> str:
    """A path component that survives a case-folding filesystem.

    An all-lowercase segment is itself. Anything else is lowercased and given
    a `^<hex>` suffix marking which positions were capitals, so two keys
    differing only in case stay two documents. Encoding the case *once* rather
    than per character is what keeps a 128-character segment — the longest a
    key may hold — inside a 255-byte filename.
    """
    mask = 0
    for i, c in enumerate(segment):
        if c.isupper():
            mask |= 1 << i
    return segment if mask == 0 else f"{segment.lower()}^{mask:x}"


def _decode(name: str) -> str:
    base, sep, mask = name.rpartition("^")
    if not sep:
        return name
    bits = int(mask, 16)
    return "".join(c.upper() if bits >> i & 1 else c for i, c in enumerate(base))


def _encode_path(key: str) -> str:
    return "/".join(_encode(segment) for segment in key.split("/"))


def _decode_path(path: str) -> str:
    return "/".join(_decode(name) for name in path.split("/"))


def _frame(token: str, expires: float | None, data: bytes) -> bytes:
    head = f"{token} {'-' if expires is None else repr(expires)}\n".encode()
    return _MAGIC + head + data


def _unframe(raw: bytes, path: Path) -> tuple[str, float | None, bytes]:
    """The header, and whatever of the body came with it. Every way this can
    fail on a truncated or foreign file is a `StoreError`: one bad file must
    not escape as a `ValueError` nor take a whole listing down with it."""
    if not raw.startswith(_MAGIC):
        raise StoreError(f"{path} is not a document this store wrote")
    head, sep, body = raw[len(_MAGIC):].partition(b"\n")
    if not sep:
        raise StoreError(f"{path} is truncated inside its header")
    try:
        token, _, expiry = head.decode().partition(" ")
        return token, None if expiry == "-" else float(expiry), body
    except (ValueError, UnicodeDecodeError) as e:
        raise StoreError(f"{path} has a header this store cannot read: {e}") from e


@dataclass(frozen=True)
class _Stored:
    data: bytes
    token: str
    expires: float | None
    modified: float
    size: int


class FileStore(StoreBase):
    """Documents as files under `root`."""

    capabilities = StoreCapabilities(atomic_put=True, durable=True, honors_ttl=True,
                                     compare_and_set=True, create_if_absent=True,
                                     token_is_write_unique=True, strong_list=True)

    def __init__(self, root: str | os.PathLike[str], *, fsync: bool = True) -> None:
        self.root = Path(root)
        self._fsync = fsync
        self._writes = 0
        self._nonce = secrets.token_hex(4)          # this store object, on this machine
        self._thread_lock = threading.Lock()
        self._tmp_dir = self.root / _TMP
        self._lock_path = self.root / _LOCK
        try:
            self._tmp_dir.mkdir(parents=True, exist_ok=True)
            self._lock_fd: int | None = os.open(self._lock_path,
                                                os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            raise StoreError(f"{self.root} is not usable as a store: {e}") from e
        self._sweep_temps()

    def __repr__(self) -> str:
        return f"FileStore({str(self.root)!r})"

    def _sweep_temps(self) -> None:
        """A `put` killed between opening its temp file and renaming it leaves
        one behind. Nothing else ever will, so anything here that a live write
        cannot still be using is a crash's litter."""
        cutoff = time.time() - _TMP_GRACE
        try:
            candidates = list(self._tmp_dir.iterdir())
        except OSError:
            return
        for path in candidates:
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    # ── the lock ──────────────────────────────────────────────────────────

    class _Locked:
        """An `flock` belongs to an open file description, not to a thread: a
        second thread of this process would be granted the lock its sibling
        already holds. So a thread lock excludes this process's own writers,
        and the `flock` excludes everyone else's."""

        def __init__(self, store: FileStore) -> None:
            self._store = store

        def __enter__(self) -> None:
            store = self._store
            store._thread_lock.acquire()
            try:
                while True:
                    fd = store._open()
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    if store._lock_is_current(fd):
                        self._fd = fd
                        return
                    # Someone removed or replaced the lock file. Every other
                    # process will open the new one, so locking the old inode
                    # would exclude nobody: move to the new one and retry.
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    store._reopen_lock()
            except BaseException:
                store._thread_lock.release()
                raise

        def __exit__(self, *exc) -> None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                self._store._thread_lock.release()

    def _locked(self) -> _Locked:
        """One writer at a time, in this process and across processes."""
        return self._Locked(self)

    def _lock_is_current(self, fd: int) -> bool:
        try:
            return os.fstat(fd).st_ino == os.stat(self._lock_path).st_ino
        except OSError:
            return False

    def _reopen_lock(self) -> None:
        try:
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            raise StoreError(f"{self.root}: the lock file cannot be recreated: {e}") from e
        old, self._lock_fd = self._lock_fd, fd
        if old is not None:
            os.close(old)

    def _open(self) -> int:
        if self._lock_fd is None:
            raise StoreClosed(f"{self!r} is closed")
        return self._lock_fd

    # ── paths ─────────────────────────────────────────────────────────────

    def _path(self, key: str) -> Path:
        return self.root / (_encode_path(key) + _LEAF)

    def _key_of(self, path: Path) -> str:
        rel = path.relative_to(self.root).as_posix()
        return _decode_path(rel[: -len(_LEAF)])

    def _walk(self, prefix: str = "") -> Iterator[Path]:
        base = self.root
        if "/" in prefix:                           # the directories are known: start there
            base = self.root / _encode_path(prefix.rsplit("/", 1)[0])
        try:
            paths = base.rglob(f"*{_LEAF}")
            for path in paths:
                if path.is_file():
                    yield path
        except OSError:
            return

    # ── one blocking operation, run off the loop ──────────────────────────

    def _read_sync(self, key: str, *, body: bool = True) -> _Stored:
        path = self._path(key)
        try:
            with open(path, "rb") as fh:
                head = fh.read(_HEADER_MAX)
                token, expires, first = _unframe(head, path)
                stat = os.fstat(fh.fileno())
                start = len(head) - len(first)      # where the body begins
                if body:
                    fh.seek(start)
                    data = fh.read(stat.st_size - start)   # one buffer, sized once
                else:
                    data = b""
        except FileNotFoundError:
            raise DocumentNotFound(key) from None
        except OSError as e:                        # never leak an OSError
            raise StoreError(f"{key!r} could not be read: {e}") from e
        if expires is not None and expires <= time.time():
            raise DocumentNotFound(key)             # gone when looked at; swept by purge()
        return _Stored(data, token, expires, stat.st_mtime, stat.st_size - start)

    def _held(self, key: str) -> str | None:
        """The token a conditional call must match, or None if there is nothing
        there to match — including a file too damaged to name a version."""
        try:
            return self._read_sync(key, body=False).token
        except DocumentNotFound:
            return None
        except StoreError:
            return None

    def _write_sync(self, key: str, data: bytes, if_match: str | _Absent | None,
                    ttl: float | None) -> str:
        path = self._path(key)
        with self._locked():
            if if_match is not None:
                held = self._held(key)
                if if_match is ABSENT:
                    if held is not None:
                        raise StaleWrite(key, ABSENT, held)
                elif held != if_match:
                    raise StaleWrite(key, if_match, held)
            self._writes += 1
            token = f"{time.time_ns():x}-{self._nonce}-{self._writes}"   # a write, not a value
            expires = None if ttl is None else time.time() + ttl
            tmp = self._tmp_dir / f"{os.getpid():x}-{self._nonce}-{self._writes}"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "wb") as fh:
                    fh.write(_frame(token, expires, data))
                    fh.flush()
                    if self._fsync:
                        os.fsync(fh.fileno())
                os.replace(tmp, path)               # atomic for any reader
                if self._fsync:
                    self._sync_dir(path.parent)     # the rename itself
            except OSError as e:
                try:
                    tmp.unlink(missing_ok=True)     # cleanup must not raise over the cause
                except OSError:
                    pass
                raise StoreError(f"{key!r} could not be written: {e}") from e
            return token

    def _touch_sync(self, key: str, ttl: float | None, if_match: str | None) -> Entry:
        with self._locked():
            stored = self._read_sync(key)           # the header lives in the file
            if if_match is not None and stored.token != if_match:
                raise StaleWrite(key, if_match, stored.token)
            expires = None if ttl is None else time.time() + ttl
            path = self._path(key)
            tmp = self._tmp_dir / f"{os.getpid():x}-{self._nonce}-t{self._writes}"
            try:
                with open(tmp, "wb") as fh:
                    fh.write(_frame(stored.token, expires, stored.data))
                    fh.flush()
                    if self._fsync:
                        os.fsync(fh.fileno())
                os.replace(tmp, path)
                if self._fsync:
                    self._sync_dir(path.parent)
                modified = path.stat().st_mtime
            except OSError as e:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise StoreError(f"{key!r} could not be written: {e}") from e
            return Entry(key=key, token=stored.token, size=stored.size,
                         modified=modified, expires_at=expires)

    def _delete_sync(self, key: str, if_match: str | _Absent | None) -> bool:
        with self._locked():
            held = self._held(key)
            if if_match is ABSENT and held is not None:
                raise StaleWrite(key, ABSENT, held)
            if isinstance(if_match, str) and held != if_match:
                raise StaleWrite(key, if_match, held)
            removed = self._remove(self._path(key))   # expired or damaged files go too
            return removed and held is not None       # but only a live one "was there"

    def _remove(self, path: Path) -> bool:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as e:
            raise StoreError(f"{path} could not be removed: {e}") from e
        for parent in path.parents:                 # tidy empty directories
            if parent == self.root:
                break
            try:
                parent.rmdir()
            except OSError:
                break
        return True

    @staticmethod
    def _sync_dir(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _list_sync(self, prefix: str) -> list[Entry]:
        now, entries, seen = time.time(), [], set()
        for path in self._walk(prefix):
            try:
                key = self._key_of(path)
            except ValueError:
                continue
            if not key.startswith(prefix) or key in seen:
                continue                            # a rename mid-walk must not list a key twice
            seen.add(key)
            try:
                stat = path.stat()
                with open(path, "rb") as fh:
                    head = fh.read(_HEADER_MAX)     # the header, not the document
                token, expires, first = _unframe(head, path)
            except (OSError, StoreError):
                continue                            # vanished under us, damaged, or not ours
            if expires is not None and expires <= now:
                continue                            # invisible; purge() reclaims the space
            size = stat.st_size - (len(head) - len(first))
            entries.append(Entry(key=key, token=token, size=size,
                                 modified=stat.st_mtime, expires_at=expires))
        return entries

    def _purge_sync(self) -> int:
        """Reclaim expired files. Each is re-read and removed under the lock
        every writer takes, so a file that expired a moment ago cannot be
        replaced by a fresh write between the look and the unlink — the race
        that makes deleting on the read path silent data loss."""
        removed = 0
        for path in list(self._walk()):
            with self._locked():
                try:
                    with open(path, "rb") as fh:
                        _, expires, _ = _unframe(fh.read(_HEADER_MAX), path)
                except (OSError, StoreError):
                    continue                        # gone, or damaged: not ours to discard
                if expires is not None and expires <= time.time():
                    removed += int(self._remove(path))
        self._sweep_temps()
        return removed

    # ── the interface ─────────────────────────────────────────────────────

    async def _read(self, key: str) -> tuple[bytes, str]:
        self._open()
        stored = await asyncio.to_thread(self._read_sync, key)
        return stored.data, stored.token

    async def _head(self, key: str) -> Entry:
        self._open()
        stored = await asyncio.to_thread(self._read_sync, key, body=False)
        return Entry(key=key, token=stored.token, size=stored.size,
                     modified=stored.modified, expires_at=stored.expires)

    async def _put(self, key: str, data: bytes, if_match: str | _Absent | None,
                   ttl: float | None) -> str:
        return await asyncio.to_thread(self._write_sync, key, data, if_match, ttl)

    async def _touch(self, key: str, ttl: float | None, if_match: str | None) -> Entry:
        return await asyncio.to_thread(self._touch_sync, key, ttl, if_match)

    async def _delete(self, key: str, if_match: str | _Absent | None) -> bool:
        return await asyncio.to_thread(self._delete_sync, key, if_match)

    async def _list(self, prefix: str) -> AsyncIterator[Entry]:
        self._open()
        for entry in await asyncio.to_thread(self._list_sync, prefix):
            yield entry

    async def purge(self) -> int:
        """Delete every expired file, returning how many. Expiry is honoured on
        read and on listing regardless; this is for reclaiming the space, and
        for clearing temp files a crashed write left behind."""
        return await asyncio.to_thread(self._purge_sync)

    async def _aclose(self) -> None:
        fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            os.close(fd)
