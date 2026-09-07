"""Wire protocol: message types and operation serialization."""

from __future__ import annotations

from typing import Any

from ._types import Operations

# Message type constants
MSG_SCHEMA = "schema"
MSG_SNAPSHOT = "snapshot"
MSG_PATCH = "patch"
MSG_ERROR = "error"
MSG_OP = "op"
MSG_CREATE = "create"
MSG_UNDO = "undo"
MSG_REDO = "redo"


def operations_to_wire(ops: Operations) -> dict[str, Any]:
    """Convert an Operations tuple to a JSON-serializable dict.

    Operations = (list[OrderedOperation], StatePatch)
    Wire format: {"ordered": [...], "state": {...}}
    """
    return {
        "ordered": [list(op) for op in ops[0]],
        "state": ops[1],
    }


def operations_from_wire(data: dict[str, Any]) -> Operations:
    """Convert a wire-format dict back to an Operations tuple.

    Ordered operations are converted to tuples. The inner node-pair
    lists (e.g. [["id", "type"], ...]) are left as lists since
    on_apply_operations iterates them the same way.
    """
    ordered = [tuple(op) for op in data.get("ordered", [])]
    state = data.get("state", {})
    return (ordered, state)
