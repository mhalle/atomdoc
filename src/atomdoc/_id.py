"""Node ID generation — port of idGenerator.ts.

Two layers:

- :class:`NodeIdGenerator` describes how *document* (root) IDs are made and
  validated. The default is a lowercase ULID.
- :func:`node_id_factory` produces compact, monotonically increasing IDs for
  non-root nodes, derived from the root ID's creation timestamp. It is only
  used when the generator can extract a timestamp from the root ID.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ulid import ULID

if TYPE_CHECKING:
    from ._doc import Doc

# lowercase ulid
ULID_REGEX = re.compile(r"^[0-7][0-9a-hjkmnp-tv-z]{25}$")

# RFC 4648 §5 alphabet reordered so each digit sorts in ASCII order.
# Encoded values are variable-length and unpadded, so multi-digit strings do
# NOT sort numerically ("z" < "0-" but 63 < 64). Node IDs are opaque.
BASE64_ALPHABET = "-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"
_ALPH_LEN = 64
_FIRST_CHAR = BASE64_ALPHABET[0]

# O(1) char -> index map
_IDX: dict[str, int] = {ch: i for i, ch in enumerate(BASE64_ALPHABET)}


@dataclass(frozen=True)
class NodeIdGenerator:
    """Custom ID generation strategy for a document.

    ``generate`` produces a new ID and ``validate`` checks one. If
    ``extract_time`` is provided, non-root nodes use the optimized compact
    ID scheme based on elapsed time since the root's creation; if it is
    omitted, ``generate`` is used for every node and every ID is validated
    when a document is restored.
    """

    generate: Callable[[], str]
    validate: Callable[[str], bool]
    extract_time: Callable[[str], int] | None = None


def _ulid_generate() -> str:
    return str(ULID()).lower()


def _ulid_validate(node_id: str) -> bool:
    return ULID_REGEX.match(node_id) is not None


def _ulid_extract_time(node_id: str) -> int:
    """Milliseconds since the epoch encoded in a ULID."""
    return ULID.from_str(node_id.upper()).milliseconds


def default_node_id_generator() -> NodeIdGenerator:
    """Lowercase ULID document IDs with compact Lamport-style node IDs."""
    return NodeIdGenerator(
        generate=_ulid_generate,
        validate=_ulid_validate,
        extract_time=_ulid_extract_time,
    )


def number_to_base64(num: int) -> str:
    """Convert a non-negative integer to base64 string."""
    if num == 0:
        return _FIRST_CHAR
    result: list[str] = []
    while num > 0:
        result.append(BASE64_ALPHABET[num % 64])
        num //= 64
    result.reverse()
    return "".join(result)


def random_base64(length: int) -> str:
    """Generate a random base64 string of the given length."""
    raw = os.urandom(length)
    return "".join(BASE64_ALPHABET[b % 64] for b in raw)


def increment_base64(s: str) -> str:
    """Increment a base64 string by one."""
    chars = list(s)
    for i in range(len(chars) - 1, -1, -1):
        idx = _IDX[chars[i]]
        if idx != _ALPH_LEN - 1:
            chars[i] = BASE64_ALPHABET[idx + 1]
            for j in range(i + 1, len(chars)):
                chars[j] = _FIRST_CHAR
            return "".join(chars)
    # All digits maxed — prepend next digit
    return BASE64_ALPHABET[1] + _FIRST_CHAR * len(s)


def session_prefix(node_id: str) -> str | None:
    """Session part of a compact node ID (``{session}.{clock}``), else None."""
    if "." not in node_id:
        return None
    return node_id.rsplit(".", 1)[0]


def mint_session_id(created_at_ms: int, existing: Collection[str] = ()) -> str:
    """Mint a session ID that is not in ``existing``.

    The session is ``{ms since root creation}{5 random chars}``. Two
    sessions minted in the same millisecond collide only if they draw the
    same 30-bit suffix (1 in ~1.07 billion); when ``existing`` holds the sessions already present
    in a document (see ``Doc.restore``), a collision is detected outright
    and a fresh suffix is drawn. If the suffix keeps colliding the
    millisecond component is bumped, so this always terminates.
    """
    ms_passed = max(0, int(time.time() * 1000) - created_at_ms)
    taken = set(existing)
    while True:
        for _ in range(16):
            session_id = number_to_base64(ms_passed) + random_base64(5)
            if session_id not in taken:
                return session_id
        ms_passed += 1


class CompactIdFactory:
    """Mints ``{session_id}.{clock}`` IDs for one document session.

    The session is fixed for the factory's lifetime; the clock increments
    per node, so IDs are unique and monotonically increasing within it.
    """

    __slots__ = ("session_id", "_clock")

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._clock = _FIRST_CHAR

    def __call__(self) -> str:
        node_id = f"{self.session_id}.{self._clock}"
        self._clock = increment_base64(self._clock)
        return node_id


def node_id_factory(
    doc: Doc,
    extract_time: Callable[[str], int] | None = None,
    existing_sessions: Collection[str] = (),
) -> CompactIdFactory:
    """Create a node ID generator for the given document.

    Returns a callable that produces monotonically increasing IDs in the
    format ``{session_id}.{clock}``. ``extract_time`` recovers the creation
    timestamp (ms) from the root ID; it defaults to ULID decoding.
    ``existing_sessions`` are session IDs already present in the document;
    the new session is guaranteed not to be one of them.
    """
    root_id = doc.root.id
    extract = extract_time or _ulid_extract_time
    try:
        created_at_ms = extract(root_id)
    except Exception as exc:
        raise ValueError(
            f"Failed to extract time from root id '{root_id}': {exc}"
        ) from exc

    return CompactIdFactory(mint_session_id(created_at_ms, existing_sessions))
