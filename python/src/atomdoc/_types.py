"""Type aliases, Diff, ChangeEvent, TransactionFlags, Operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ._node import AtomNode

# Lifecycle stages
LifeCycleStage = Literal[
    "init", "idle", "update", "normalize", "normalize2", "change", "disposed"
]

# Position for insertion / move
Position = Literal["append", "prepend", "before", "after"]

# Operation types — compact tuples for serialization
# Values use str for IDs, int(0) as null marker
# Insert: [0, [(id, type), ...], parent_id|0, slot_name, prev_id|0, next_id|0]
InsertOp = tuple[int, list[tuple[str, str]], str | int, str, str | int, str | int]
# Delete: [1, start_id, end_id|0]
DeleteOp = tuple[int, str, str | int]
# Move: [2, start_id, end_id|0, parent_id|0, slot_name, prev_id|0, next_id|0]
MoveOp = tuple[int, str, str | int, str | int, str, str | int, str | int]

OrderedOperation = InsertOp | DeleteOp | MoveOp

# StatePatch: {node_id: {field: json_value}}
# Values are native JSON (strings, numbers, booleans, arrays, objects, null).
# Opaque/bytes fields are base64 strings; receivers decode based on schema tier.
StatePatch = dict[str, dict[str, Any]]

# Operations: (ordered_ops, state_patch)
Operations = tuple[list[OrderedOperation], StatePatch]


class ListenerError(Exception):
    """One or more change listeners raised after a transaction committed.

    Change listeners are observers: they run after validation, once the
    commit is final, so a failing listener cannot undo what other
    listeners (a session broadcast, a UI store) have already seen. The
    document keeps the change; this error reports the failures to the
    caller afterwards. ``errors`` holds every exception raised, in
    listener order; the first is also the ``__cause__``.
    """

    def __init__(self, errors: list[BaseException]) -> None:
        self.errors = errors
        first = errors[0]
        more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
        super().__init__(
            f"{len(errors)} change listener(s) failed after commit: "
            f"{type(first).__name__}: {first}{more}"
        )
        self.__cause__ = first


@dataclass(frozen=True)
class TransactionFlags:
    """Per-transaction flags, delivered to change listeners.

    ``skip_undo`` marks a transaction that must not enter undo history —
    typically one that applies operations received from a remote peer.
    """

    skip_undo: bool = False


class Diff:
    """Summary of changes during a transaction."""

    __slots__ = ("inserted", "deleted", "moved", "updated")

    def __init__(self) -> None:
        self.inserted: set[str] = set()
        self.deleted: dict[str, AtomNode] = {}
        self.moved: set[str] = set()
        self.updated: set[str] = set()


class ChangeEvent:
    """Emitted after a transaction commits."""

    __slots__ = ("operations", "inverse_operations", "diff", "flags")

    def __init__(
        self,
        operations: Operations,
        inverse_operations: Operations,
        diff: Diff,
        flags: TransactionFlags | None = None,
    ) -> None:
        self.operations = operations
        self.inverse_operations = inverse_operations
        self.diff = diff
        self.flags = flags if flags is not None else TransactionFlags()


# JSON document format (new — dict-based children):
# [doc_id, root_type, {state}, {"slot1": [...], "slot2": [...]}]
# Each child: [node_id, node_type, {state}] or [node_id, node_type, {state}, {slots}]
JsonAtomNode = list[Any]
JsonDoc = list[Any]
