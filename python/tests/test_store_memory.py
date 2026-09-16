"""MemoryStore against the shared conformance suite."""

from __future__ import annotations

import asyncio

import pytest

from atomdoc import (CapabilityError, Entry, InvalidKey, MemoryStore, StoreBase,
                     StoreCapabilities, ValueTooLarge)

from store_conformance import StoreConformance


class TestMemoryStore(StoreConformance):
    @pytest.fixture
    def store(self):
        return MemoryStore()


def test_memory_store_is_honest_about_durability():
    assert MemoryStore().capabilities.durable is False


# ── StoreBase: what no backend can opt out of ────────────────────────────────


class _Careless(StoreBase):
    """A backend that checks nothing: every rule below must hold anyway."""

    capabilities = StoreCapabilities(durable=False, max_value_bytes=10)

    def __init__(self):
        self.items = {}

    async def _read(self, key):
        return self.items[key], "t"

    async def _head(self, key):
        return Entry(key=key, token="t", size=len(self.items[key]))

    async def _put(self, key, data, if_match, ttl):
        self.items[key] = data
        return "t"

    async def _delete(self, key, if_match):
        return self.items.pop(key, None) is not None

    async def _list(self, prefix):
        for key in list(self.items):
            if key.startswith(prefix):
                yield Entry(key=key, token="t", size=0)


def test_a_backend_cannot_skip_the_checks():
    async def run():
        store = _Careless()
        for call in (store.get, store.read, store.head, store.delete):
            with pytest.raises(InvalidKey):
                await call("../escape")
        with pytest.raises(InvalidKey):
            await store.put("../escape", b"x")
        with pytest.raises(CapabilityError):
            await store.put("a", b"x", if_match="t")
        with pytest.raises(CapabilityError):
            await store.put("a", b"x", ttl=5)
        with pytest.raises(CapabilityError):
            await store.touch("a", 5)
        with pytest.raises(ValueTooLarge):
            await store.put("a", b"x" * 11)
        with pytest.raises(InvalidKey):
            [k async for k in store.keys("bad prefix")]
        assert store.items == {}                                # nothing reached the backend
    asyncio.run(run())


def test_a_backend_cannot_replace_a_checked_method():
    with pytest.raises(TypeError, match="implement _put"):
        class Sneaky(_Careless):
            async def put(self, key, data, *, if_match=None, ttl=None):
                return await self._put(key, data, if_match, ttl)


def test_a_backend_must_implement_every_operation():
    class Partial(StoreBase):
        async def _read(self, key):
            return b"", "t"
    with pytest.raises(TypeError):
        Partial()


def test_claims_that_cannot_hang_together_are_refused():
    with pytest.raises(ValueError, match="ABA"):
        StoreCapabilities(compare_and_set=True)                 # an S3 ETag store, honestly
    with pytest.raises(ValueError):
        StoreCapabilities(max_value_bytes=0)
    with pytest.raises(ValueError):
        StoreCapabilities(expires_unasked_after=-1)
    StoreCapabilities(compare_and_set=True, token_is_write_unique=True)


def test_bytes_are_not_copied_on_the_loop_but_buffers_are():
    store = MemoryStore()
    payload = b"x" * 1000
    assert store._check("a", payload) is payload               # immutable: no memcpy
    buffer = bytearray(payload)
    taken = store._check("a", buffer)
    buffer[0] = 0
    assert taken == payload and type(taken) is bytes
