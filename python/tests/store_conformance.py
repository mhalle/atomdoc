"""One suite every document store must pass.

A backend is honest because it is tested, not because its `capabilities` say
so: each claim here is asserted and what a backend does not claim is skipped.
Subclass with a `store` fixture and the whole suite runs against it. A store
that claims durability must also provide `reopen` (a second store object over
the same state) and `reopen_spec` (a picklable factory for one, so the claim is
checked from a fresh interpreter, where a dictionary cannot follow).

Most of these exist because a plausible wrong backend passed without them.
The comments name what each one catches.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import time
from contextlib import aclosing

import pytest

from atomdoc import (ABSENT, CapabilityError, DocumentNotFound, InvalidKey, StaleWrite,
                     StoreCapabilities, StoreClosed, ValueTooLarge)


async def collect(iterator) -> list[str]:
    return sorted([key async for key in iterator])


async def eventually(check, timeout: float = 2.0) -> None:
    """For a store whose listing lags: poll until it agrees, or give up."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            await check()
            return
        except AssertionError:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(0.02)


def _read_in_a_child(factory, args, key):
    """Runs in a spawned interpreter: nothing of the parent's memory survives."""
    async def read():
        store = factory(*args)
        try:
            return await store.get(key)
        finally:
            await store.aclose()
    return asyncio.run(read())


class StoreConformance:
    """Mix in with a `store` fixture; every backend runs the same tests."""

    pytestmark = pytest.mark.asyncio

    # How long an untouched document must survive; how big a document the
    # suite pushes through a store that declares no limit; how far a store's
    # idea of "now" may sit from the caller's.
    unasked_grace: float = 1.0
    unbounded_probe: int = 8 * 1024 * 1024
    clock_tolerance: float = 5.0

    @pytest.fixture
    def reopen(self):
        """Override to return an async callable giving a second store over the
        same durable state; without it, durability is skipped and the store
        must not claim it."""
        return None

    @pytest.fixture
    def reopen_spec(self):
        """Override with `(factory, args)`, picklable into a fresh interpreter,
        that builds a store over the same durable state."""
        return None

    @staticmethod
    async def settled(store, check) -> None:
        """A listing assertion. A store that does not claim `strong_list` is
        given time to catch up rather than failed for lagging — without this
        a backend declaring the field honestly failed seven tests, so no
        backend could."""
        if store.capabilities.strong_list:
            await check()
        else:
            await eventually(check)

    # ── the basics ────────────────────────────────────────────────────────

    async def test_round_trip(self, store):
        await store.put("a", b"one")
        assert await store.get("a") == b"one"

    async def test_bytes_are_returned_unchanged(self, store):
        payload = bytes(range(256)) + "café ☕".encode()
        await store.put("binary", payload)
        assert await store.get("binary") == payload

    async def test_missing_key_raises(self, store):
        with pytest.raises(DocumentNotFound):
            await store.get("nope")
        with pytest.raises(KeyError):                    # the same error, read plainly
            await store.get("nope")

    async def test_put_replaces(self, store):
        await store.put("a", b"one")
        await store.put("a", b"two")
        assert await store.get("a") == b"two"

    async def test_read_and_get_agree(self, store):
        await store.put("a", b"one")
        data, _ = await store.read("a")
        assert data == await store.get("a")

    async def test_an_empty_document_is_not_an_absent_one(self, store):   # B8
        await store.put("e", b"")
        assert await store.get("e") == b""
        assert await collect(store.keys()) == ["e"]

    async def test_put_copies_what_it_is_given(self, store):              # B5
        buffer = bytearray(b"first")
        await store.put("a", buffer)
        buffer[:] = b"other"
        assert await store.get("a") == b"first"
        assert type(await store.get("a")) is bytes

    async def test_head_answers_without_the_body(self, store):
        token = await store.put("a", b"12345")
        entry = await store.head("a")
        assert (entry.key, entry.size, entry.token) == ("a", 5, token)
        with pytest.raises(DocumentNotFound):
            await store.head("gone")

    # ── deleting ──────────────────────────────────────────────────────────

    async def test_delete_reports_what_it_found(self, store):
        await store.put("a", b"one")
        assert await store.delete("a") is True
        assert await store.delete("a") is False          # a state, not an event
        with pytest.raises(DocumentNotFound):
            await store.get("a")

    async def test_conditional_delete(self, store):
        if not store.capabilities.compare_and_set:
            pytest.skip("no compare_and_set")
        await store.put("a", b"one")
        _, token = await store.read("a")
        await store.put("a", b"two")                     # the reaper's token goes stale
        with pytest.raises(StaleWrite):
            await store.delete("a", if_match=token)
        assert await store.get("a") == b"two"

    # ── tokens: the rules that make compare-and-set mean anything ─────────

    async def test_put_returns_the_token_read_would_give(self, store):    # LyingTokenStore
        token = await store.put("a", b"one")
        assert isinstance(token, str) and token
        assert (await store.read("a"))[1] == token

    async def test_a_token_names_a_write_not_a_value(self, store):        # B1
        if not store.capabilities.token_is_write_unique:
            pytest.skip("token is not write-unique")
        first = await store.put("a", b"same")
        second = await store.put("a", b"same")           # identical bytes, new write
        assert first != second

    async def test_a_token_does_not_come_back_from_the_dead(self, store):  # B1, ABA
        if not (store.capabilities.compare_and_set
                and store.capabilities.token_is_write_unique):
            pytest.skip("no ABA protection claimed")
        await store.put("a", b"A")
        _, stale = await store.read("a")
        await store.put("a", b"B")
        await store.put("a", b"A")                       # the bytes came back
        with pytest.raises(StaleWrite):
            await store.put("a", b"mine", if_match=stale)

    async def test_a_token_does_not_survive_a_delete(self, store):        # B2
        if not (store.capabilities.compare_and_set
                and store.capabilities.token_is_write_unique):
            pytest.skip("no ABA protection claimed")
        await store.put("a", b"one")
        _, stale = await store.read("a")
        await store.delete("a")
        await store.put("a", b"again")
        with pytest.raises(StaleWrite):
            await store.put("a", b"mine", if_match=stale)

    async def test_compare_and_set(self, store):
        if not store.capabilities.compare_and_set:
            pytest.skip("no compare_and_set")
        await store.put("a", b"one")
        _, token = await store.read("a")
        await store.put("a", b"two", if_match=token)
        with pytest.raises(StaleWrite):
            await store.put("a", b"three", if_match=token)
        assert await store.get("a") == b"two"

    async def test_one_writer_of_many_wins(self, store):                  # B4
        if not store.capabilities.compare_and_set:
            pytest.skip("no compare_and_set")
        await store.put("a", b"0")
        _, token = await store.read("a")
        results = await asyncio.gather(
            *(store.put("a", str(i + 1).encode(), if_match=token) for i in range(5)),
            return_exceptions=True)
        assert sum(isinstance(r, StaleWrite) for r in results) == 4

    async def test_create_if_absent(self, store):
        if not store.capabilities.create_if_absent:
            pytest.skip("no create_if_absent")
        await store.put("fresh", b"one", if_match=ABSENT)
        with pytest.raises(StaleWrite):
            await store.put("fresh", b"two", if_match=ABSENT)
        await store.delete("fresh")
        await store.put("fresh", b"three", if_match=ABSENT)   # absent again

    # ── what the store does not claim, it refuses ─────────────────────────

    async def test_if_match_without_the_capability_raises(self, store):   # NoCas
        if store.capabilities.compare_and_set:
            pytest.skip("has compare_and_set")
        await store.put("a", b"one")
        with pytest.raises(CapabilityError):
            await store.put("a", b"two", if_match="whatever")

    async def test_absent_without_the_capability_raises(self, store):
        if store.capabilities.create_if_absent:
            pytest.skip("has create_if_absent")
        with pytest.raises(CapabilityError):
            await store.put("a", b"one", if_match=ABSENT)

    async def test_ttl_without_the_capability_raises(self, store):
        if store.capabilities.honors_ttl:
            pytest.skip("has honors_ttl")
        with pytest.raises(CapabilityError):
            await store.put("a", b"one", ttl=60)

    async def test_too_large_is_refused_not_truncated(self, store):
        limit = store.capabilities.max_value_bytes
        if limit is None:
            pytest.skip("no size limit")
        with pytest.raises(ValueTooLarge):
            await store.put("a", b"x" * (limit + 1))

    # ── keys ──────────────────────────────────────────────────────────────

    async def test_prefix_is_a_string_prefix_not_a_path(self, store):     # B7
        for key in ["alice/1", "alicia/1", "bob/1"]:
            await store.put(key, b"x")

        async def check():
            assert await collect(store.keys("ali")) == ["alice/1", "alicia/1"]
            assert await collect(store.keys("alice/")) == ["alice/1"]
            assert await collect(store.keys("nobody")) == []
        await self.settled(store, check)

    async def test_keys_are_case_sensitive(self, store):                  # the security one
        await store.put("user/Alice", b"hers")
        await store.put("user/alice", b"someone else")
        assert await store.get("user/Alice") == b"hers"

        async def check():
            assert len(await collect(store.keys("user/"))) == 2
        await self.settled(store, check)

    async def test_a_key_may_be_a_prefix_of_another(self, store):
        await store.put("a", b"leaf")
        await store.put("a/b", b"below")
        assert (await store.get("a"), await store.get("a/b")) == (b"leaf", b"below")

    async def test_keys_does_not_repeat_itself(self, store):
        for i in range(50):
            await store.put(f"k{i}", b"x")

        async def check():
            seen = [key async for key in store.keys()]
            assert len(seen) == len(set(seen)) == 50
        await self.settled(store, check)

    async def test_keys_forgets_deleted(self, store):
        await store.put("a", b"x")
        await store.delete("a")
        await self.settled(store, lambda: _assert_empty(store))

    async def test_list_carries_what_the_backend_knows(self, store):
        await store.put("a", b"12345")

        async def check():
            entries = [e async for e in store.list("a")]
            assert len(entries) == 1 and entries[0].size == 5 and entries[0].token
        await self.settled(store, check)

    async def test_an_illegal_key_is_refused(self, store):
        for bad in ["", "a//b", "alice/../bob", "a/./b", "with space", "nul\x00", "x" * 600]:
            with pytest.raises(InvalidKey):
                await store.put(bad, b"x")
            with pytest.raises(InvalidKey):
                await store.get(bad)

    # ── expiry ────────────────────────────────────────────────────────────

    async def test_ttl_expires(self, store):
        if not store.capabilities.honors_ttl:
            pytest.skip("no ttl")
        await store.put("brief", b"x", ttl=0.05)
        await asyncio.sleep(0.2)
        with pytest.raises(DocumentNotFound):
            await store.get("brief")

    async def test_keys_after_an_entry_expired(self, store):
        if not store.capabilities.honors_ttl:
            pytest.skip("no ttl")
        await store.put("a", b"1")
        await store.put("b", b"2", ttl=0.02)
        await asyncio.sleep(0.1)

        async def check():
            assert await collect(store.keys()) == ["a"]
        await self.settled(store, check)

    async def test_a_re_put_without_ttl_is_permanent(self, store):        # B6
        if not store.capabilities.honors_ttl:
            pytest.skip("no ttl")
        await store.put("a", b"one", ttl=0.05)
        await store.put("a", b"two")                     # no ttl: the expiry is gone
        await asyncio.sleep(0.2)
        assert await store.get("a") == b"two"

    # ── concurrency, durability, atomicity ────────────────────────────────

    async def test_concurrent_writes_to_different_keys(self, store):
        await asyncio.gather(*(store.put(f"k{i}", str(i).encode()) for i in range(20)))
        assert await store.get("k7") == b"7"

        async def check():
            assert len(await collect(store.keys())) == 20
        await self.settled(store, check)

    async def test_a_large_document(self, store):
        limit = store.capabilities.max_value_bytes
        size = 2 * 1024 * 1024 if limit is None else min(limit, 2 * 1024 * 1024)
        payload = b"x" * size
        await store.put("big", payload)
        assert await store.get("big") == payload

    async def test_survives_a_reopen(self, store, reopen):                # B9
        if reopen is None:
            assert store.capabilities.durable is False, \
                "a store claiming durability must provide a `reopen` fixture"
            pytest.skip("no reopen fixture")
        await store.put("a", b"one")
        second = await reopen()
        try:
            assert await second.get("a") == b"one"
        finally:
            await second.aclose()

    async def test_a_reader_never_sees_half_a_document(self, store):
        """Catches a backend that truncates in place *and* does its work off the
        event loop; a backend that blocks the loop through `put` cannot be
        caught here, and crash-atomicity needs a subprocess, not this."""
        if not store.capabilities.atomic_put:
            pytest.skip("no atomic_put")
        await store.put("a", b"a" * 4096)
        stop = False

        async def rewrite():
            i = 0
            while not stop:
                i += 1
                await store.put("a", (b"a" if i % 2 else b"b") * 4096)
                await asyncio.sleep(0)

        writer = asyncio.create_task(rewrite())
        try:
            for _ in range(200):
                seen = await store.get("a")
                assert seen in (b"a" * 4096, b"b" * 4096), "a partial document was visible"
                await asyncio.sleep(0)
        finally:
            stop = True
            await writer

    async def test_put_refuses_what_is_not_bytes(self, store):
        with pytest.raises(TypeError):
            await store.put("a", "text is not bytes")     # type: ignore[arg-type]


    # ── capability claims that must hang together ─────────────────────────

    async def test_capabilities_are_coherent(self, store):            # hash tokens, declared
        caps = store.capabilities
        assert isinstance(caps, StoreCapabilities)
        if caps.compare_and_set:
            assert caps.token_is_write_unique, (
                "compare_and_set over value-derived tokens permits ABA")
        assert caps.max_value_bytes is None or caps.max_value_bytes > 0
        assert caps.expires_unasked_after is None or caps.expires_unasked_after > 0

    # ── a delete is a delete for every reader ─────────────────────────────

    async def test_a_deleted_key_is_gone_from_every_reader(self, store):
        await store.put("a", b"x")
        await store.delete("a")
        for call in (store.get, store.read, store.head):
            with pytest.raises(DocumentNotFound):
                await call("a")

        async def check():
            assert [entry async for entry in store.list("a")] == []
        await self.settled(store, check)

    # ── head is worth having only if it carries the metadata ──────────────

    async def test_head_carries_the_expiry_it_was_given(self, store):  # store-clock skew
        if not store.capabilities.honors_ttl:
            pytest.skip("no ttl")
        before = time.time()
        await store.put("a", b"x", ttl=300)
        entry = await store.head("a")
        assert entry.expires_at is not None, "a ttl the store honoured is a ttl it knows"
        assert before + 300 - self.clock_tolerance <= entry.expires_at \
            <= time.time() + 300 + self.clock_tolerance, (
            "expires_at is epoch seconds on the caller's clock")

        async def check():
            listed = [e async for e in store.list("a")]
            assert [e.expires_at for e in listed] == [entry.expires_at]
        await self.settled(store, check)

    async def test_a_document_without_a_ttl_says_so(self, store):      # sticky ttl in head
        await store.put("a", b"x")
        assert (await store.head("a")).expires_at is None

    async def test_head_agrees_with_the_body(self, store):             # head reads a stale size
        payload = b"x" * 1000
        await store.put("a", payload)
        assert (await store.head("a")).size == len(payload)

    # ── expiry, in both directions ────────────────────────────────────────

    async def test_a_ttl_document_lives_until_its_ttl(self, store):    # expires far too early
        if not store.capabilities.honors_ttl:
            pytest.skip("no ttl")
        await store.put("a", b"x", ttl=300)
        assert await store.get("a") == b"x"

        async def check():
            assert await collect(store.keys("a")) == ["a"]
        await self.settled(store, check)

    async def test_a_non_positive_ttl_is_an_error(self, store):        # a check that forgot
        if not store.capabilities.honors_ttl:
            pytest.skip("no ttl")
        for bad in (0, -1.0):
            with pytest.raises(ValueError):
                await store.put("a", b"x", ttl=bad)

    async def test_an_untouched_document_stays(self, store):           # silently forgetful
        idle = store.capabilities.expires_unasked_after
        if idle is not None and idle <= self.unasked_grace:
            pytest.skip(f"this store admits it forgets after {idle}s")
        await store.put("a", b"x")
        await asyncio.sleep(self.unasked_grace)
        assert await store.get("a") == b"x", (
            "a store that drops untouched entries must declare expires_unasked_after")

    # ── touch: a lease, renewed without a rewrite ─────────────────────────

    async def test_touch_extends_a_lease_and_keeps_the_token(self, store):
        if not store.capabilities.honors_ttl:
            pytest.skip("no ttl")
        # Survival tests give the put-then-touch real slack: a slow disk (CI,
        # with fsync) once took longer than a 0.1 s lease between the two.
        token = await store.put("a", b"x", ttl=0.6)
        entry = await store.touch("a", 60)
        assert entry.token == token, "renewing a lease must not break the holder's next CAS"
        assert entry.expires_at is not None and entry.expires_at > time.time() + 30
        await asyncio.sleep(0.8)
        assert await store.read("a") == (b"x", token)
        if store.capabilities.compare_and_set:
            await store.put("a", b"y", if_match=token)

    async def test_touch_can_shorten_and_can_remove_an_expiry(self, store):
        if not store.capabilities.honors_ttl:
            pytest.skip("no ttl")
        await store.put("a", b"x")
        await store.touch("a", 0.05)
        await asyncio.sleep(0.2)
        with pytest.raises(DocumentNotFound):
            await store.get("a")
        await store.put("b", b"x", ttl=0.6)
        assert (await store.touch("b", None)).expires_at is None
        await asyncio.sleep(0.8)
        assert await store.get("b") == b"x"

    async def test_touch_needs_a_live_document(self, store):
        if not store.capabilities.honors_ttl:
            pytest.skip("no ttl")
        with pytest.raises(DocumentNotFound):
            await store.touch("never", 60)
        await store.put("brief", b"x", ttl=0.05)
        await asyncio.sleep(0.2)
        with pytest.raises(DocumentNotFound):                  # an expired lease is not renewed
            await store.touch("brief", 60)

    async def test_touch_is_conditional(self, store):
        caps = store.capabilities
        if not (caps.honors_ttl and caps.compare_and_set):
            pytest.skip("no ttl or no compare_and_set")
        old = await store.put("a", b"one")
        await store.put("a", b"two")
        with pytest.raises(StaleWrite):
            await store.touch("a", 60, if_match=old)
        assert (await store.head("a")).expires_at is None
        with pytest.raises(ValueError):
            await store.touch("a", 60, if_match=ABSENT)        # type: ignore[arg-type]

    async def test_touch_without_the_capability_raises(self, store):
        if store.capabilities.honors_ttl:
            pytest.skip("store has ttl")
        await store.put("a", b"x")
        with pytest.raises(CapabilityError):
            await store.touch("a", 60)

    # ── size ──────────────────────────────────────────────────────────────

    async def test_nothing_is_silently_truncated(self, store):         # truncates past 4 MiB
        limit = store.capabilities.max_value_bytes
        size = limit if limit is not None else self.unbounded_probe
        payload = (b"0123456789abcdef" * (size // 16 + 1))[:size]
        await store.put("big", payload)
        got = await store.get("big")
        assert len(got) == size and got == payload
        assert (await store.head("big")).size == size

    # ── listing under writes ──────────────────────────────────────────────

    async def test_a_listing_neither_repeats_nor_loses_a_settled_key(self, store):
        settled = [f"k{i:02}" for i in range(10, 40)]
        churned = [f"k{i:02}" for i in range(10)]
        for key in churned + settled:
            await store.put(key, b"x")

        async def rewrite():
            for _ in range(4):
                for key in churned:
                    await store.put(key, b"y")
                    await asyncio.sleep(0)

        async def check():
            writer = asyncio.create_task(rewrite())
            try:
                seen = [key async for key in store.keys()]
            finally:
                await writer
            assert len(seen) == len(set(seen)), (
                f"repeated: {sorted(k for k in seen if seen.count(k) > 1)}")
            assert set(settled) <= set(seen), (
                f"lost while listing: {sorted(set(settled) - set(seen))}")
        await self.settled(store, check)

    async def test_a_listing_may_be_abandoned(self, store):
        for i in range(20):
            await store.put(f"k{i:02}", b"x")
        async with aclosing(store.keys()) as it:
            async for _ in it:
                break                                          # a caller that wants one
        await store.put("after", b"x")                         # the store still works
        assert await store.get("after") == b"x"

    # ── validation on every entry point, not just put and get ─────────────

    async def test_every_entry_point_refuses_an_illegal_key(self, store):
        for bad in ["", "a//b", "alice/../bob", "with space", "nul\x00", "x" * 600,
                    "\ud800", "caf\u00e9"]:
            for call in (store.get, store.read, store.head, store.delete):
                with pytest.raises(InvalidKey):
                    await call(bad)
            with pytest.raises(InvalidKey):
                await store.put(bad, b"x")

    async def test_an_illegal_prefix_is_refused(self, store):          # raw ValueError from chr()
        for bad in ["\ud800", "\U0010ffff", "with space", "x" * 600, 3]:
            with pytest.raises(InvalidKey):
                await collect(store.keys(bad))                  # type: ignore[arg-type]
        for fine in ["", "ali", "alice/", "a.b~c@d+e-f_"]:
            await collect(store.keys(fine))

    async def test_a_stale_write_says_what_it_held(self, store):
        if not store.capabilities.compare_and_set:
            pytest.skip("no compare_and_set")
        token = await store.put("a", b"one")
        await store.put("a", b"two")
        with pytest.raises(StaleWrite) as caught:
            await store.put("a", b"three", if_match=token)
        assert caught.value.key == "a" and caught.value.expected == token

    async def test_if_match_is_a_token_not_an_entry(self, store):     # wrote unconditionally
        if not store.capabilities.compare_and_set:
            pytest.skip("no compare_and_set")
        await store.put("a", b"one")
        entry = await store.head("a")
        with pytest.raises(TypeError):
            await store.put("a", b"two", if_match=entry)       # type: ignore[arg-type]
        assert await store.get("a") == b"one"

    # ── lifetime ──────────────────────────────────────────────────────────

    async def test_aclose_is_idempotent(self, store):
        await store.aclose()
        await store.aclose()

    async def test_a_closed_store_says_so(self, store):                # raw ProgrammingError
        await store.put("a", b"x")
        await store.aclose()
        for call in (store.get, store.read, store.head, store.delete):
            with pytest.raises(StoreClosed):
                await call("a")
        with pytest.raises(StoreClosed):
            await store.put("a", b"y")
        with pytest.raises(StoreClosed):
            await collect(store.keys())

    # ── durability, checked where a dict cannot follow ────────────────────

    async def test_survives_a_new_interpreter(self, store, reopen_spec):  # a dict with a reopen
        if reopen_spec is None:
            assert store.capabilities.durable is False, (
                "a store claiming durability must provide a `reopen_spec` fixture")
            pytest.skip("no reopen_spec fixture")
        await store.put("a", b"one")
        await store.aclose()                                   # nothing held open for the child
        factory, args = reopen_spec
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(1) as pool:
            got = await asyncio.to_thread(pool.apply, _read_in_a_child, (factory, args, "a"))
        assert got == b"one", "durable means it outlives this process, not this object"


async def _assert_empty(store) -> None:
    assert await collect(store.keys()) == []
