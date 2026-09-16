"""FileStore and SqliteStore against the shared conformance suite, and then
against what only a real filesystem or a real database can do to them.

The second half exists because an adversarial review broke both backends in
ways no in-process conformance test reaches: expiry that deleted a fresh
write, two legal keys wanting one path, a damaged file that blinded every
listing, a read that waited five seconds on another process's transaction.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import sqlite3
import sys
import time

import pytest

from atomdoc import (ABSENT, DocumentNotFound, FileStore, SqliteStore, StaleWrite,
                     StoreError)

from store_conformance import StoreConformance

pytestmark = pytest.mark.asyncio


class TestFileStore(StoreConformance):
    @pytest.fixture
    def store(self, tmp_path):
        return FileStore(tmp_path / "documents")

    @pytest.fixture
    def reopen(self, tmp_path):
        async def again():
            return FileStore(tmp_path / "documents")
        return again

    @pytest.fixture
    def reopen_spec(self, tmp_path):
        return FileStore, (str(tmp_path / "documents"),)


class TestSqliteStore(StoreConformance):
    @pytest.fixture
    def store(self, tmp_path):
        return SqliteStore(tmp_path / "documents.db")

    @pytest.fixture
    def reopen(self, tmp_path):
        async def again():
            return SqliteStore(tmp_path / "documents.db")
        return again

    @pytest.fixture
    def reopen_spec(self, tmp_path):
        return SqliteStore, (str(tmp_path / "documents.db"),)


# ── both: many processes, one counter ─────────────────────────────────────────

def _count_up(factory, args, times):
    async def run():
        store = factory(*args)
        won = 0
        try:
            while won < times:
                data, token = await store.read("counter")
                try:
                    await store.put("counter", str(int(data) + 1).encode(), if_match=token)
                    won += 1
                except StaleWrite:
                    pass
        finally:
            await store.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["file", "sqlite"])
async def test_compare_and_set_across_processes_loses_nothing(tmp_path, kind):
    """The lock is an flock (or SQLite's) between processes and a thread lock
    within one: an flock belongs to an open file, so a store sharing one
    descriptor across threads once let every concurrent writer win."""
    factory, args = ((FileStore, (str(tmp_path / "f"),)) if kind == "file"
                     else (SqliteStore, (str(tmp_path / "s.db"),)))
    store = factory(*args)
    await store.put("counter", b"0")
    await store.aclose()
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_count_up, args=(factory, args, 25)) for _ in range(4)]
    for p in procs:
        p.start()
    await asyncio.to_thread(lambda: [p.join(60) for p in procs])
    assert all(p.exitcode == 0 for p in procs)
    store = factory(*args)
    assert await store.get("counter") == b"100"
    await store.aclose()


# ── FileStore ─────────────────────────────────────────────────────────────────

@pytest.fixture
def files(tmp_path):
    return FileStore(tmp_path / "documents", fsync=False)


async def test_a_read_never_deletes(files):
    """The rule that closes the race below, checked where no timing is needed:
    an expired file is invisible to every reader, and only `purge` — under the
    writers' lock — removes it."""
    await files.put("doc", b"old", ttl=0.01)
    await asyncio.sleep(0.05)
    for call in (files.get, files.read, files.head):
        with pytest.raises(DocumentNotFound):
            await call("doc")
    assert [k async for k in files.keys()] == []
    assert files._path("doc").exists(), "a reader removed a file a writer may be replacing"
    assert await files.purge() == 1


async def test_expiry_never_deletes_a_fresh_write(files):
    """Deleting an expired file when it was read raced a writer replacing it:
    1.7% of rounds lost a document that had no expiry at all."""
    for i in range(200):
        await files.put("doc", b"old", ttl=0.001)
        await asyncio.sleep(0.002)
        fresh = f"new {i}".encode()
        readers = [files.get("doc") for _ in range(6)]
        listers = [_listed(files) for _ in range(2)]
        await asyncio.gather(*readers[:3], files.put("doc", fresh), *readers[3:], *listers,
                             return_exceptions=True)
        assert await files.get("doc") == fresh


async def _listed(store):
    return [k async for k in store.keys()]


@pytest.mark.parametrize("first,second", [("a", "a.doc/b"), ("a.doc/b", "a"),
                                          ("A", "A/b"), ("aDoc", "a/b"),
                                          (".tmp/x", ".lock/y")])
async def test_no_two_keys_want_one_path(files, first, second):
    await files.put(first, b"1")
    await files.put(second, b"2")
    assert (await files.get(first), await files.get(second)) == (b"1", b"2")
    assert sorted([k async for k in files.keys()]) == sorted([first, second])


async def test_the_store_sweeps_nothing_that_is_a_key(tmp_path):
    root = tmp_path / "documents"
    store = FileStore(root, fsync=False)
    await store.put(".tmp/x", b"kept")
    await store.put("tmp/x", b"kept")
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (time.time() - 3600, time.time() - 3600))
    await store.aclose()
    store = FileStore(root)
    assert await store.purge() == 0
    assert await store.get(".tmp/x") == b"kept" and await store.get("tmp/x") == b"kept"


@pytest.mark.parametrize("key", ["A" * 128, "A" * 128 + "/" + "B" * 128,
                                 "/".join(["Ab" * 64] * 3), "a" * 128])
async def test_the_longest_legal_segments(files, key):
    """Encoding case once per character doubled a segment, so a legal
    128-capital segment would not fit a 255-byte filename."""
    await files.put(key, b"x")
    assert await files.get(key) == b"x"
    assert [k async for k in files.keys()] == [key]


@pytest.mark.parametrize("damage", [b"", b"not ours at all", b"atomdoc-store-1 ",
                                    b"atomdoc-store-1 tok", b"atomdoc-store-1 tok abc\nbody",
                                    b"atomdoc-store-1 \xff\xfe 1.0\nbody"])
async def test_a_damaged_file(files, damage):
    await files.put("good", b"fine")
    await files.put("x", b"fine")
    files._path("x").write_bytes(damage)
    with pytest.raises(StoreError) as caught:
        await files.get("x")
    assert type(caught.value) is StoreError                 # not a raw ValueError
    assert [k async for k in files.keys()] == ["good"]      # one bad file blinds nothing
    with pytest.raises(StaleWrite):
        await files.delete("x", if_match="some token")      # there is no version to match
    assert await files.delete("x") is False                 # but it can be cleared
    await files.put("x", b"again", if_match=ABSENT)
    assert await files.get("x") == b"again"


async def test_head_under_concurrent_deletes(files):
    for _ in range(150):
        results = await asyncio.gather(files.put("k", b"x" * 64), files.head("k"),
                                       files.delete("k"), files.head("k"),
                                       return_exceptions=True)
        for r in results:
            assert not isinstance(r, Exception) or isinstance(r, DocumentNotFound), repr(r)


async def test_a_removed_lock_file_still_excludes(tmp_path):
    """Two store objects hold two open files, so only the flock stands between
    them. Replace the lock file under the first and it must follow."""
    root = tmp_path / "documents"
    first = FileStore(root, fsync=False)
    (root / "^lock").unlink()
    second = FileStore(root, fsync=False)
    for round_ in range(20):
        token = await first.put("k", str(round_).encode())
        results = await asyncio.gather(
            *(s.put("k", b"w", if_match=token) for s in (first, second) for _ in range(3)),
            return_exceptions=True)
        assert sum(not isinstance(r, Exception) for r in results) == 1, results
        assert all(isinstance(r, (str, StaleWrite)) for r in results)


async def test_a_crashed_write_is_swept(tmp_path):
    root = tmp_path / "documents"
    store = FileStore(root)
    await store.put("k", b"x")
    old, fresh = root / "^tmp" / "dead-1", root / "^tmp" / "live-1"
    old.write_bytes(b"x" * 1000)
    fresh.write_bytes(b"x" * 1000)
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    await store.aclose()
    store = FileStore(root)
    assert not old.exists(), "a crashed write's temp file outlived a reopen"
    assert fresh.exists(), "a temp file a live write may still own was swept"
    assert [k async for k in store.keys()] == ["k"]


async def test_purge_reclaims_only_what_is_still_expired(files):
    await files.put("gone", b"x", ttl=0.01)
    await files.put("back", b"x", ttl=0.01)
    await asyncio.sleep(0.05)
    await files.put("back", b"alive")
    assert await files.purge() == 1
    assert not files._path("gone").exists()
    assert await files.get("back") == b"alive"


async def test_an_unusable_root_is_a_store_error(tmp_path):
    (tmp_path / "a-file").write_bytes(b"")
    with pytest.raises(StoreError):
        FileStore(tmp_path / "a-file")


# ── SqliteStore ───────────────────────────────────────────────────────────────

async def test_a_read_does_not_wait_on_another_writer(tmp_path):
    """A read used to delete the expired row it found, which needed the write
    lock, and waited out the busy timeout — freezing the whole store object —
    whenever another process held it."""
    path = tmp_path / "s.db"
    store = SqliteStore(path)
    await store.put("live", b"1")
    await store.put("dead", b"1", ttl=0.01)
    await asyncio.sleep(0.05)
    other = sqlite3.connect(path, isolation_level=None, timeout=30)
    other.execute("BEGIN IMMEDIATE")
    other.execute("INSERT INTO entries VALUES ('z', 't', 0, NULL, 1)")
    try:
        started = time.monotonic()
        with pytest.raises(DocumentNotFound):
            await store.get("dead")
        assert [k async for k in store.keys()] == ["live"]
        assert await store.get("live") == b"1"
        assert time.monotonic() - started < 1.0
    finally:
        other.execute("ROLLBACK")
        other.close()
        await store.aclose()


async def test_a_damaged_database_is_a_store_error(tmp_path):
    path = tmp_path / "s.db"
    path.write_bytes(b"this is not a database" * 100)
    with pytest.raises(StoreError):
        SqliteStore(path)


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="permissions do not bind root")
async def test_a_read_only_database_is_a_store_error(tmp_path):
    folder = tmp_path / "ro"
    folder.mkdir()
    store = SqliteStore(folder / "s.db")
    await store.put("a", b"1")
    await store.aclose()
    for f in folder.iterdir():
        f.chmod(0o444)
    folder.chmod(0o555)
    try:
        try:
            store = SqliteStore(folder / "s.db")
        except StoreError:
            return
        with pytest.raises(StoreError):
            await store.put("a", b"2")
        await store.aclose()
    finally:
        folder.chmod(0o755)


async def test_sqlite_purge_clears_both_tables(tmp_path):
    store = SqliteStore(tmp_path / "s.db")
    await store.put("gone", b"x" * 100, ttl=0.01)
    await store.put("kept", b"y")
    await asyncio.sleep(0.05)
    assert await store.purge() == 1
    db = store._conn
    assert db.execute("SELECT key FROM bodies").fetchall() == [("kept",)]
    assert db.execute("SELECT key FROM entries").fetchall() == [("kept",)]
    await store.aclose()
