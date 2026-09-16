"""Durable storage for documents, as bytes under a key.

A live document is a :class:`~atomdoc.Doc` held by the process that owns it;
this is where it rests between lives. The store knows nothing about documents:
it moves opaque bytes, so what is written can be a snapshot envelope, a
compressed one, or an encrypted one, and a stored document stays readable by
something that never imported these models.

Backends differ in what they can promise — an object store has no per-key
expiry, a distributed dictionary drops entries on its own schedule, an S3
ETag names a *value* rather than a *write* — so each declares
:class:`StoreCapabilities`, the conformance suite tests exactly what is
claimed, and anything unclaimed that a caller asks for raises
:class:`CapabilityError` rather than being quietly ignored.
"""

from __future__ import annotations

import asyncio
import re
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

__all__ = [
    "ABSENT",
    "CapabilityError",
    "DocumentNotFound",
    "DocumentStore",
    "Entry",
    "InvalidKey",
    "MAX_KEY_BYTES",
    "MemoryStore",
    "StaleWrite",
    "StoreBase",
    "StoreCapabilities",
    "StoreClosed",
    "StoreError",
    "StoreUnavailable",
    "ValueTooLarge",
    "check_key",
    "check_prefix",
]


class StoreError(Exception):
    """Every failure a *store* reports is one of these, so a caller can catch
    the family and never see a backend's own `OSError` or `sqlite3.Error`.

    A call that is malformed — bytes that are not bytes, a negative `ttl`, an
    `if_match` that is not a token — is the caller's bug rather than the
    store's failure, and raises the plain `TypeError` or `ValueError` that any
    other Python function would. `InvalidKey` sits in both families because a
    key usually arrives from outside and is usually handled with the rest of
    the store's errors.
    """


class DocumentNotFound(StoreError, KeyError):
    """No document under that key. Also a `KeyError`, so `except KeyError`
    around a bare `get` behaves as a reader expects."""

    def __init__(self, key: str):
        super().__init__(key)
        self.key = key

    def __str__(self) -> str:
        return f"no document at {self.key!r}"


class StaleWrite(StoreError):
    """A conditional write lost: the stored document moved on since the token
    the caller held. Read again, merge, retry — the store will not decide which
    version wins. `actual` is best effort; a backend that would need a second
    request to find out leaves it None."""

    def __init__(self, key: str, expected: object, actual: str | None = None):
        super().__init__(key, expected, actual)
        self.key, self.expected, self.actual = key, expected, actual

    def __str__(self) -> str:
        held = "absent" if self.expected is ABSENT else repr(self.expected)
        return f"{self.key!r} has moved on (held {held}, found {self.actual!r})"


class CapabilityError(StoreError, NotImplementedError):
    """The store was asked for something it does not claim in `capabilities`.
    Raised rather than ignored: a caller running a compare-and-set loop against
    a store that silently drops `if_match` gets last-writer-wins and no
    signal."""


class InvalidKey(StoreError, ValueError):
    """The key is not one a store will accept. Validation belongs to the store
    because the untrusted half of a key — a document id from a tool argument —
    arrives through the caller."""


class ValueTooLarge(StoreError):
    """Larger than `capabilities.max_value_bytes`."""


class StoreUnavailable(StoreError):
    """The backend could not be reached and the caller may reasonably try again
    later. Distinguished from the rest so a save loop can keep the document
    alive rather than treating a network blip as a lost document."""


class StoreClosed(StoreError):
    """Used after `aclose()`. A bug in the caller's lifetime handling, reported
    as a store error rather than as whichever exception the backend's own
    closed handle happens to raise."""


class _Absent:
    """The sentinel for "only if this key does not exist". Not `""`, which is a
    legal (if unlikely) token."""

    _instance: _Absent | None = None

    def __new__(cls) -> _Absent:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "ABSENT"


ABSENT: Final = _Absent()

MAX_KEY_BYTES: Final = 512
_SEGMENT: Final = re.compile(r"[A-Za-z0-9._~@+-]{1,128}")
_PREFIX: Final = re.compile(r"[A-Za-z0-9._~@+/-]*")


def check_key(key: str) -> str:
    """Return `key` if a store may accept it, else raise `InvalidKey`.

    A key is one or more segments joined by `/`, each 1-128 characters of
    `A-Z a-z 0-9 . _ ~ @ + -`, no segment `.` or `..`, at most
    `MAX_KEY_BYTES` of UTF-8 in total. Keys are **case sensitive**: a backend
    on a case-folding filesystem must encode rather than collapse, or one
    principal reads another's documents.

    `/` has no meaning to a store — it neither implies a hierarchy nor bounds
    a prefix match — but restricting keys to path-shaped strings is what lets
    a filesystem or an object store hold them without escaping games.
    """
    if not isinstance(key, str):
        raise InvalidKey(f"a key is a str, not {type(key).__name__}")
    try:
        size = len(key.encode("utf-8"))
    except UnicodeEncodeError:                  # a lone surrogate: not text at all
        raise InvalidKey(f"{key!r} is not encodable text") from None
    if not key or size > MAX_KEY_BYTES:
        raise InvalidKey(f"a key is 1-{MAX_KEY_BYTES} bytes of UTF-8, got {size}")
    for segment in key.split("/"):
        if segment in (".", "..") or not _SEGMENT.fullmatch(segment):
            raise InvalidKey(f"{key!r}: {segment!r} is not a usable key segment")
    return key


def check_prefix(prefix: str) -> str:
    """Return `prefix` if a store may list under it, else raise `InvalidKey`.

    A prefix is not a key: `""` lists everything and `"ali"` is a legal prefix
    of `alice/1` that is no key at all. It must still be text a store can put
    in a path or a range bound, so it is held to the key alphabet and length
    and nothing more.
    """
    if not isinstance(prefix, str):
        raise InvalidKey(f"a prefix is a str, not {type(prefix).__name__}")
    try:
        size = len(prefix.encode("utf-8"))
    except UnicodeEncodeError:
        raise InvalidKey(f"{prefix!r} is not encodable text") from None
    if size > MAX_KEY_BYTES or not _PREFIX.fullmatch(prefix):
        raise InvalidKey(f"{prefix!r} is not a usable key prefix")
    return prefix


@dataclass(frozen=True)
class Entry:
    """What a store knows about a document without reading it."""

    key: str
    token: str
    size: int
    modified: float | None = None       # epoch seconds, None if the backend has none
    expires_at: float | None = None     # epoch seconds, None if it does not expire


@dataclass(frozen=True)
class StoreCapabilities:
    """What a backend promises. Each claim is asserted by the conformance
    suite and what is not claimed is skipped, so a backend is honest by being
    tested rather than by saying so. Claims that cannot hang together are
    refused here, at construction.

    atomic_put       a reader never sees a partially written document
    durable          a returned `put` survives the process — checked by reading
                     it back in a *fresh interpreter*, which is the only place
                     a dictionary cannot follow
    honors_ttl       `ttl` expires an entry, and an expired entry is gone from
                     `get` and from `keys` (not merely swept eventually)
    compare_and_set  `put(if_match=token)` refuses a stale write
    create_if_absent `put(if_match=ABSENT)` refuses to overwrite
    token_is_write_unique
                     a token names one *write*: re-storing identical bytes
                     yields a new token, and a token from before a delete never
                     matches again. A content-derived token (an S3 ETag, a git
                     blob id) does not qualify — it permits ABA, where a stale
                     token wins a compare-and-set because the bytes came back.
                     Without this, `compare_and_set` protects against nothing,
                     so claiming the one requires the other.
    strong_list      `keys` reflects every `put` that has already returned. A
                     backend that declares this False has its listings given
                     time to catch up rather than being failed for lagging.
    max_value_bytes  the largest document, or None for no practical limit —
                     in which case the suite pushes a multi-megabyte document
                     through and reads it back, so "no limit" is a claim that
                     can fail rather than a blank cheque
    expires_unasked_after
                     seconds after which this store drops an untouched entry
                     whatever `ttl` said (Modal Dict: 604800). A caller that
                     needs a document to persist must refuse such a store.
    """

    atomic_put: bool = True
    durable: bool = True
    honors_ttl: bool = False
    compare_and_set: bool = False
    create_if_absent: bool = False
    token_is_write_unique: bool = False
    strong_list: bool = True
    max_value_bytes: int | None = None
    expires_unasked_after: float | None = None

    def __post_init__(self) -> None:
        if self.compare_and_set and not self.token_is_write_unique:
            raise ValueError(
                "compare_and_set over value-derived tokens permits ABA, so it "
                "protects nothing: claim token_is_write_unique as well, or drop "
                "compare_and_set")
        if self.max_value_bytes is not None and self.max_value_bytes <= 0:
            raise ValueError("max_value_bytes is a positive size, or None for no limit")
        if self.expires_unasked_after is not None and self.expires_unasked_after <= 0:
            raise ValueError("expires_unasked_after is a positive number of seconds, "
                             "or None if the store keeps what it is given")


@runtime_checkable
class DocumentStore(Protocol):
    """Bytes under an opaque key.

    Keys are composed by the caller — `f"{principal}/{doc_id}"` scopes by
    owner and `keys(prefix)` lists one owner's documents — which is what keeps
    authorization out of the store. The store still validates what it is
    given: see `check_key`.

    Rules a backend must follow, each of which two implementers would
    otherwise decide differently:

    * A **token** names one write of one key. See `token_is_write_unique`.
      Tokens are non-empty strings, compared only by equality, and need only
      be meaningful within the lifetime of one store object.
    * `b""` is **a value**, not an absence: a key holding it exists, `get`
      returns it, and `keys` lists it.
    * `put` **copies** what it is given; mutating a `bytearray` afterwards
      changes nothing, and `get` returns `bytes`.
    * `prefix` is matched as a **string** prefix, not a path component:
      `keys("ali")` includes `alice/1`.
    * A `put` **without** `ttl` clears any expiry the key had; `touch` changes
      the expiry and nothing else, **keeping the token**, because a lease
      renewal must not invalidate the token the renewing holder will use for
      its next compare-and-set.
    * `expires_at` and `ttl` are on the **caller's** clock: a store that
      resolves them against its own shortens or lengthens every lease by its
      skew.
    * `keys` and `list` may reflect writes made while they run, but a key that
      is present unchanged for the whole traversal is yielded exactly once.
    * An argument the store does not claim raises `CapabilityError`: an
      `if_match` without `compare_and_set`, a `ttl` without `honors_ttl`.
    * A cancelled `put` may or may not have landed; read again to find out.

    Every method is async because the interesting backends are network calls;
    a synchronous backend does its work in `asyncio.to_thread` rather than
    blocking the loop that is serving connections.
    """

    @property
    def capabilities(self) -> StoreCapabilities: ...

    async def get(self, key: str) -> bytes: ...

    async def read(self, key: str) -> tuple[bytes, str]:
        """The bytes and the token naming this version."""
        ...

    async def head(self, key: str) -> Entry:
        """What is known about the document without reading it — size, token,
        expiry. A reaper and a schema migration both want this; without it
        they download every document to look at a header."""
        ...

    async def put(self, key: str, data: bytes, *, if_match: str | _Absent | None = None,
                  ttl: float | None = None) -> str:
        """Store `data`; return the new token.

        `if_match=<token>` stores only if that token is still current, and
        `if_match=ABSENT` only if the key does not exist; either raises
        `StaleWrite` when the condition fails. `ttl` expires the entry after
        that many seconds.
        """
        ...

    async def touch(self, key: str, ttl: float | None, *,
                    if_match: str | None = None) -> Entry:
        """Set the expiry to `ttl` seconds from now — or `None` to remove it —
        without rewriting the document, and return what the entry now is.

        This is the operation a lease is made of: an ephemeral document lives
        while its connections do, which means extending its expiry on every
        heartbeat. Without it that costs a full read and a full write, and
        races every concurrent save. The **token does not change**, so a holder
        may renew a lease and still compare-and-set afterwards.
        """
        ...

    async def delete(self, key: str, *, if_match: str | _Absent | None = None) -> bool:
        """Remove the document; return whether there was one. Deleting what is
        absent is not an error — a caller deletes to reach a state. `if_match`
        closes the race between a reaper and a save that is still in flight."""
        ...

    def keys(self, prefix: str = "") -> AsyncIterator[str]:
        """Every key under `prefix`, in no guaranteed order. Not a coroutine:
        calling this returns an async iterator. A caller that stops early
        should close it (`contextlib.aclosing`)."""
        ...

    def list(self, prefix: str = "") -> AsyncIterator[Entry]:
        """`keys`, with what the backend already knows about each entry."""
        ...

    async def aclose(self) -> None:
        """Release whatever the backend holds — a connection, a pool, a file
        handle. Idempotent."""
        ...


class StoreBase(ABC):
    """What every backend would otherwise write twice — and the checks none of
    them may skip.

    The public methods are final in practice: each validates the call against
    the key rules and this store's own claims, then hands the work to the
    `_read` / `_head` / `_put` / `_touch` / `_delete` / `_list` a backend
    implements. A backend therefore cannot forget to validate, which is what
    made the validation advisory when it was a helper backends were expected
    to call.
    """

    capabilities: StoreCapabilities = StoreCapabilities()

    _VALIDATED: Final = frozenset({"get", "read", "head", "put", "touch", "delete",
                                   "keys", "list", "aclose"})

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        overridden = sorted(cls._VALIDATED & set(vars(cls)))
        if overridden:
            raise TypeError(
                f"{cls.__name__} overrides {', '.join(overridden)}; implement "
                f"{', '.join('_' + name for name in overridden if name not in ('get', 'keys'))} "
                f"instead, so the call is validated before it reaches the backend. "
                f"A backend that must own the public methods implements the "
                f"DocumentStore protocol directly and passes the conformance suite.")

    # ── what a backend implements ─────────────────────────────────────────

    @abstractmethod
    async def _read(self, key: str) -> tuple[bytes, str]: ...

    @abstractmethod
    async def _head(self, key: str) -> Entry: ...

    @abstractmethod
    async def _put(self, key: str, data: bytes, if_match: str | _Absent | None,
                   ttl: float | None) -> str: ...

    @abstractmethod
    async def _delete(self, key: str, if_match: str | _Absent | None) -> bool: ...

    @abstractmethod
    def _list(self, prefix: str) -> AsyncIterator[Entry]: ...

    async def _touch(self, key: str, ttl: float | None, if_match: str | None) -> Entry:
        raise CapabilityError(f"{type(self).__name__} cannot change an expiry in place")

    async def _aclose(self) -> None:
        """Release what the backend holds. Called once, however often `aclose` is."""
        return None

    # ── what a caller sees ────────────────────────────────────────────────

    async def get(self, key: str) -> bytes:
        return (await self.read(key))[0]

    async def read(self, key: str) -> tuple[bytes, str]:
        self._check(key)
        return await self._read(key)

    async def head(self, key: str) -> Entry:
        self._check(key)
        return await self._head(key)

    async def put(self, key: str, data: bytes, *, if_match: str | _Absent | None = None,
                  ttl: float | None = None) -> str:
        payload = self._check(key, data, if_match=if_match, ttl=ttl)
        return await self._put(key, payload, if_match, ttl)

    async def touch(self, key: str, ttl: float | None, *,
                    if_match: str | None = None) -> Entry:
        if if_match is ABSENT:
            raise ValueError("touch extends the life of a document that exists; "
                             "if_match=ABSENT asks for one that does not")
        self._check(key, if_match=if_match, ttl=ttl)
        if not self.capabilities.honors_ttl:
            raise CapabilityError(f"{type(self).__name__} does not expire entries")
        return await self._touch(key, ttl, if_match)

    async def delete(self, key: str, *, if_match: str | _Absent | None = None) -> bool:
        self._check(key, if_match=if_match)
        return await self._delete(key, if_match)

    async def keys(self, prefix: str = "") -> AsyncIterator[str]:
        async for entry in self.list(prefix):
            yield entry.key

    async def list(self, prefix: str = "") -> AsyncIterator[Entry]:
        self._check_open()
        check_prefix(prefix)
        async for entry in self._list(prefix):
            yield entry

    _closed: bool = False

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ── the checks ────────────────────────────────────────────────────────

    def _check_open(self) -> None:
        if self._closed:
            raise StoreClosed(f"{type(self).__name__} has been closed")

    def _check(self, key: str, data: bytes | None = None, *,
               if_match: str | _Absent | None = None, ttl: float | None = None) -> bytes:
        """Validate one call against the key rules and this store's claims."""
        self._check_open()
        check_key(key)
        caps = self.capabilities
        if if_match is not None and if_match is not ABSENT and not isinstance(if_match, str):
            raise TypeError(f"if_match is a token (str), ABSENT, or None, not "
                            f"{type(if_match).__name__} — a token comes from read() or put(), "
                            f"so pass entry.token rather than the Entry")
        if if_match is ABSENT and not caps.create_if_absent:
            raise CapabilityError(f"{type(self).__name__} cannot store only if absent")
        if isinstance(if_match, str) and not caps.compare_and_set:
            raise CapabilityError(f"{type(self).__name__} has no compare-and-set")
        if ttl is not None and not caps.honors_ttl:
            raise CapabilityError(f"{type(self).__name__} does not expire entries")
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl is a positive number of seconds")
        if data is None:
            return b""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("a document is stored as bytes")
        if caps.max_value_bytes is not None and len(data) > caps.max_value_bytes:
            raise ValueTooLarge(f"{len(data)} bytes exceeds this store's "
                                f"{caps.max_value_bytes}")
        # `bytes` is already immutable, and copying one here would memcpy the
        # whole document on the event loop. Only a buffer the caller can still
        # write through has to be taken away from them.
        return data if type(data) is bytes else bytes(data)


class MemoryStore(StoreBase):
    """The reference implementation: a dict with the semantics written down.

    What the conformance suite runs against, what an ephemeral document uses
    when nothing should outlive the process, and the yardstick a new backend
    is compared with. Not durable, and says so.
    """

    capabilities = StoreCapabilities(atomic_put=True, durable=False, honors_ttl=True,
                                     compare_and_set=True, create_if_absent=True,
                                     token_is_write_unique=True, strong_list=True)

    def __init__(self) -> None:
        self._items: dict[str, tuple[bytes, str, float, float | None]] = {}
        self._lock = asyncio.Lock()
        self._writes = 0

    def __repr__(self) -> str:
        return f"MemoryStore({len(self._items)} documents)"

    def _live(self, key: str) -> tuple[bytes, str, float, float | None] | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        if entry[3] is not None and entry[3] <= time.time():
            del self._items[key]                               # expired: gone when looked at
            return None
        return entry

    def _entry(self, key: str) -> Entry:
        data, token, written, expires = self._items[key]
        return Entry(key=key, token=token, size=len(data), modified=written,
                     expires_at=expires)

    async def _read(self, key: str) -> tuple[bytes, str]:
        async with self._lock:
            entry = self._live(key)
            if entry is None:
                raise DocumentNotFound(key)
            return entry[0], entry[1]

    async def _head(self, key: str) -> Entry:
        async with self._lock:
            if self._live(key) is None:
                raise DocumentNotFound(key)
            return self._entry(key)

    async def _put(self, key: str, data: bytes, if_match: str | _Absent | None,
                   ttl: float | None) -> str:
        async with self._lock:                                 # no await inside: all or nothing
            entry = self._live(key)
            if if_match is ABSENT and entry is not None:
                raise StaleWrite(key, ABSENT, entry[1])
            if isinstance(if_match, str):
                held = entry[1] if entry else None
                if held != if_match:
                    raise StaleWrite(key, if_match, held)
            self._writes += 1
            token = f"{self._writes}"                          # a write, not a value
            self._items[key] = (data, token, time.time(),
                                None if ttl is None else time.time() + ttl)
            return token

    async def _touch(self, key: str, ttl: float | None, if_match: str | None) -> Entry:
        async with self._lock:
            entry = self._live(key)
            if entry is None:
                raise DocumentNotFound(key)
            if if_match is not None and entry[1] != if_match:
                raise StaleWrite(key, if_match, entry[1])
            data, token, written, _ = entry                    # the token stays: it is a lease
            self._items[key] = (data, token, written,
                                None if ttl is None else time.time() + ttl)
            return self._entry(key)

    async def _delete(self, key: str, if_match: str | _Absent | None) -> bool:
        async with self._lock:
            entry = self._live(key)
            if if_match is ABSENT and entry is not None:
                raise StaleWrite(key, ABSENT, entry[1])
            if isinstance(if_match, str) and (entry[1] if entry else None) != if_match:
                raise StaleWrite(key, if_match, entry[1] if entry else None)
            return self._items.pop(key, None) is not None

    async def _list(self, prefix: str) -> AsyncIterator[Entry]:
        async with self._lock:                                 # _live evicts: copy the keys
            names = [k for k in list(self._items) if k.startswith(prefix) and self._live(k)]
            entries = [self._entry(k) for k in names]
        for entry in entries:
            yield entry
