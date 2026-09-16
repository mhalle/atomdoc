"""A document store in a SQLite database.

One row per document, in WAL mode, which gives concurrent readers with one
writer — the shape this store already has, since a live document is owned by
one process. Over the file store it buys a cheap prefix range scan, expiry as
a column a sweep can act on, and a transaction around compare-and-set. Like
the file store it wants a real filesystem: SQLite's locking is not reliable on
a network one.

Reads never write. An expired row is invisible to every reader at once and is
removed by `purge()`; deleting it opportunistically on the read path made a
plain `get` need the write lock, and wait out another process's transaction
for it.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sqlite3
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

from ._store import (ABSENT, DocumentNotFound, Entry, StaleWrite, StoreBase,
                     StoreCapabilities, StoreClosed, StoreError, StoreUnavailable,
                     _Absent)

__all__ = ["SqliteStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    key      TEXT PRIMARY KEY,
    token    TEXT NOT NULL,
    written  REAL NOT NULL,
    expires  REAL,
    size     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bodies (
    key      TEXT PRIMARY KEY,
    data     BLOB NOT NULL
);
"""
# Two tables because SQLite rewrites a whole row on any UPDATE: with the body
# beside the expiry, renewing a lease on a 100 MB document copied 100 MB. And
# not WITHOUT ROWID, which keeps the row in the key's b-tree — reading the
# token beside a 100 MB body then took 29 ms rather than 0.01.
_BUSY_TIMEOUT = 5.0         # seconds a writer waits for another process's transaction


@contextmanager
def _wrapped(what: str) -> Iterator[None]:
    """Every sqlite3 failure as a StoreError; a lock held elsewhere as the one
    a caller may retry."""
    try:
        yield
    except sqlite3.OperationalError as e:
        if "locked" in str(e) or "busy" in str(e):
            raise StoreUnavailable(f"{what}: {e}") from e
        raise StoreError(f"{what}: {e}") from e
    except sqlite3.Error as e:
        raise StoreError(f"{what}: {e}") from e


class SqliteStore(StoreBase):
    """Documents as rows in `path`, or in memory when `path` is ':memory:'."""

    capabilities = StoreCapabilities(atomic_put=True, durable=True, honors_ttl=True,
                                     compare_and_set=True, create_if_absent=True,
                                     token_is_write_unique=True, strong_list=True)

    def __init__(self, path: str | os.PathLike[str] = ":memory:") -> None:
        self.path = str(path)
        self._writes = 0
        self._nonce = secrets.token_hex(4)                     # this store object, anywhere
        with _wrapped(f"{self.path} is not usable as a store"):
            self._db: sqlite3.Connection | None = sqlite3.connect(
                self.path, check_same_thread=False, isolation_level=None,
                timeout=_BUSY_TIMEOUT)
            try:
                self._db.execute("PRAGMA journal_mode=WAL")    # readers do not block the writer
                self._db.execute("PRAGMA synchronous=FULL")    # a returned put has landed
                self._db.executescript(_SCHEMA)
            except BaseException:
                self._db.close()
                raise
        self._lock = asyncio.Lock()                            # one connection, one caller at a time

    def __repr__(self) -> str:
        return f"SqliteStore({self.path!r})"

    @property
    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            raise StoreClosed(f"{self!r} is closed")
        return self._db

    # ── blocking work, run off the loop ───────────────────────────────────

    def _row(self, key: str, *, body: bool) -> tuple[bytes, str, float | None, float, int]:
        row = self._conn.execute(
            "SELECT token, expires, written, size FROM entries WHERE key = ?",
            (key,)).fetchone()
        if row is None:
            raise DocumentNotFound(key)
        token, expires, written, size = row
        if expires is not None and expires <= time.time():
            raise DocumentNotFound(key)                        # invisible now; purge() removes it
        if not body:
            return b"", token, expires, written, size
        found = self._conn.execute("SELECT data FROM bodies WHERE key = ?", (key,)).fetchone()
        if found is None:
            raise StoreError(f"{key!r} has an entry and no body")
        return bytes(found[0]), token, expires, written, size

    def _read_sync(self, key: str, body: bool = True):
        with _wrapped(f"{key!r} could not be read"):
            if not body:
                return self._row(key, body=False)
            self._conn.execute("BEGIN")                        # entry and body from one snapshot
            try:
                return self._row(key, body=True)
            finally:
                self._conn.execute("COMMIT")

    def _held(self, key: str) -> str | None:
        try:
            return self._row(key, body=False)[1]
        except DocumentNotFound:
            return None

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._conn
        db.execute("BEGIN IMMEDIATE")                          # the write lock, before we look
        try:
            yield db
        except BaseException:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass                                           # the cause matters more
            raise
        db.execute("COMMIT")

    def _write_sync(self, key: str, data: bytes, if_match: str | _Absent | None,
                    ttl: float | None) -> str:
        with _wrapped(f"{key!r} could not be written"), self._transaction() as db:
            if if_match is not None:
                held = self._held(key)
                if if_match is ABSENT:
                    if held is not None:
                        raise StaleWrite(key, ABSENT, held)
                elif held != if_match:
                    raise StaleWrite(key, if_match, held)
            self._writes += 1
            token = f"{time.time_ns():x}-{self._nonce}-{self._writes}"   # a write, not a value
            now = time.time()
            db.execute(
                "INSERT INTO entries (key, token, written, expires, size) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "token=excluded.token, written=excluded.written, "
                "expires=excluded.expires, size=excluded.size",        # no ttl clears it
                (key, token, now, None if ttl is None else now + ttl, len(data)))
            db.execute(
                "INSERT INTO bodies (key, data) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data", (key, data))
            return token

    def _touch_sync(self, key: str, ttl: float | None, if_match: str | None) -> Entry:
        with _wrapped(f"{key!r} could not be touched"), self._transaction() as db:
            _, token, _, written, size = self._row(key, body=False)
            if if_match is not None and token != if_match:
                raise StaleWrite(key, if_match, token)
            expires = None if ttl is None else time.time() + ttl
            db.execute("UPDATE entries SET expires = ? WHERE key = ?", (expires, key))
            return Entry(key=key, token=token, size=size, modified=written,
                         expires_at=expires)

    def _delete_sync(self, key: str, if_match: str | _Absent | None) -> bool:
        with _wrapped(f"{key!r} could not be removed"), self._transaction() as db:
            held = self._held(key)
            if if_match is ABSENT and held is not None:
                raise StaleWrite(key, ABSENT, held)
            if isinstance(if_match, str) and held != if_match:
                raise StaleWrite(key, if_match, held)
            db.execute("DELETE FROM bodies WHERE key = ?", (key,))
            removed = db.execute("DELETE FROM entries WHERE key = ?", (key,)).rowcount
            return removed > 0 and held is not None            # an expired row was not "there"

    def _list_sync(self, prefix: str) -> list[Entry]:
        now = time.time()
        query = ("SELECT key, token, size, written, expires FROM entries "
                 "WHERE (expires IS NULL OR expires > ?)")
        with _wrapped(f"{prefix!r} could not be listed"):
            if prefix and ord(prefix[-1]) < 0x10FFFF:          # a range scan, not a table scan
                rows = self._conn.execute(
                    query + " AND key >= ? AND key < ? ORDER BY key",
                    (now, prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1))).fetchall()
            else:
                rows = [row for row in self._conn.execute(query + " ORDER BY key", (now,))
                        if row[0].startswith(prefix)]
        return [Entry(key=key, token=token, size=size, modified=written, expires_at=expires)
                for key, token, size, written, expires in rows]

    def _purge_sync(self) -> int:
        with _wrapped("expired documents could not be purged"), self._transaction() as db:
            now = time.time()
            db.execute("DELETE FROM bodies WHERE key IN (SELECT key FROM entries "
                       "WHERE expires IS NOT NULL AND expires <= ?)", (now,))
            return db.execute("DELETE FROM entries WHERE expires IS NOT NULL "
                              "AND expires <= ?", (now,)).rowcount

    # ── the interface ─────────────────────────────────────────────────────

    async def _read(self, key: str) -> tuple[bytes, str]:
        async with self._lock:
            data, token, _, _, _ = await asyncio.to_thread(self._read_sync, key)
        return data, token

    async def _head(self, key: str) -> Entry:
        async with self._lock:
            _, token, expires, written, size = await asyncio.to_thread(
                self._read_sync, key, False)
        return Entry(key=key, token=token, size=size, modified=written, expires_at=expires)

    async def _put(self, key: str, data: bytes, if_match: str | _Absent | None,
                   ttl: float | None) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._write_sync, key, data, if_match, ttl)

    async def _touch(self, key: str, ttl: float | None, if_match: str | None) -> Entry:
        async with self._lock:
            return await asyncio.to_thread(self._touch_sync, key, ttl, if_match)

    async def _delete(self, key: str, if_match: str | _Absent | None) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._delete_sync, key, if_match)

    async def _list(self, prefix: str) -> AsyncIterator[Entry]:
        async with self._lock:
            entries = await asyncio.to_thread(self._list_sync, prefix)
        for entry in entries:
            yield entry

    async def purge(self) -> int:
        """Remove every expired row, returning how many. Expiry is honoured on
        read and on listing regardless; this is for reclaiming the space."""
        async with self._lock:
            return await asyncio.to_thread(self._purge_sync)

    async def _aclose(self) -> None:
        async with self._lock:
            db, self._db = self._db, None
            if db is not None:
                db.close()
