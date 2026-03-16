"""Lamport-timestamp node ID generation — port of idGenerator.ts."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Callable

from ulid import ULID

if TYPE_CHECKING:
    from ._doc import Doc

# RFC 4648 §5 alphabet, lexicographically sorted
BASE64_ALPHABET = "-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"
_ALPH_LEN = 64
_FIRST_CHAR = BASE64_ALPHABET[0]

# O(1) char -> index map
_IDX: dict[str, int] = {ch: i for i, ch in enumerate(BASE64_ALPHABET)}


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


def node_id_factory(doc: "Doc") -> Callable[[], str]:
    """Create a node ID generator for the given document.

    Returns a callable that produces monotonically increasing IDs
    in the format ``{session_id}.{clock}``.
    """
    root_id = doc.root.id
    ulid_obj = ULID.from_str(root_id.upper())
    # python-ulid v3: .milliseconds is int ms, .timestamp is float seconds
    created_at_ms = ulid_obj.milliseconds

    import time as _time
    ms_passed = max(0, int(_time.time() * 1000) - created_at_ms)
    ms_base64 = number_to_base64(ms_passed)
    random_part = random_base64(3)
    session_id = ms_base64 + random_part

    clock = _FIRST_CHAR

    def generate() -> str:
        nonlocal clock
        node_id = f"{session_id}.{clock}"
        clock = increment_base64(clock)
        return node_id

    return generate
