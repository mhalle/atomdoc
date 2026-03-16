"""Type aliases, Diff, ChangeEvent, Operations."""

from __future__ import annotations

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

# StatePatch: {node_id: {field: json_string}}
StatePatch = dict[str, dict[str, str]]

# Operations: (ordered_ops, state_patch)
Operations = tuple[list[OrderedOperation], StatePatch]


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

    __slots__ = ("operations", "inverse_operations", "diff")

    def __init__(
        self,
        operations: Operations,
        inverse_operations: Operations,
        diff: Diff,
    ) -> None:
        self.operations = operations
        self.inverse_operations = inverse_operations
        self.diff = diff


# JSON document format (new — dict-based children):
# [doc_id, root_type, {state}, {"slot1": [...], "slot2": [...]}]
# Each child: [node_id, node_type, {state}] or [node_id, node_type, {state}, {slots}]
JsonAtomNode = list[Any]
JsonDoc = list[Any]
