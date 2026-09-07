"""UndoManager — stack-based undo/redo with merge interval and history export."""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypedDict

from ._operations import merge_operations
from ._types import ChangeEvent, Operations

if TYPE_CHECKING:
    from ._doc import Doc


@dataclass(frozen=True)
class UndoManagerConfig:
    """Configuration for a document's built-in undo manager.

    ``max_steps`` is the number of undo steps to keep; ``0`` (the default)
    disables undo entirely. ``merge_interval`` is the window, in seconds,
    within which consecutive transactions collapse into a single undo step;
    ``0`` disables merging.
    """

    max_steps: int = 0
    merge_interval: float = 0.0


@dataclass
class UndoStackItem:
    operations: Operations
    meta: dict[str, Any] = field(default_factory=dict)


class UndoHistoryItem(TypedDict):
    operations: Operations
    meta: dict[str, Any]


class UndoHistory(TypedDict, total=False):
    """Serializable undo/redo state, see :meth:`UndoManager.export_history`."""

    doc_id: str
    doc_type: str
    undo_stack: list[UndoHistoryItem]
    redo_stack: list[UndoHistoryItem]
    last_update: float | None


class UndoManager:
    """Stack-based undo/redo manager for a Doc.

    Every document owns one (``doc.undo_manager``), configured through
    ``Doc(undo_manager=UndoManagerConfig(...))`` and disabled by default. A
    standalone manager can still be constructed for a document; it defaults
    to 100 steps for backwards compatibility.
    """

    def __init__(
        self,
        doc: Doc,
        max_steps: int = 100,
        merge_interval: float = 0.0,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._doc = doc
        self._max_steps = max_steps
        self._merge_interval = merge_interval
        self._clock = clock
        self._undo_stack: list[UndoStackItem] = []
        self._redo_stack: list[UndoStackItem] = []
        self._tx_type: str = "update"  # "undo" | "redo" | "update"
        self._last_update: float | None = None
        # How the last change event altered the stacks, so a commit that
        # fails after this manager saw it can be taken back exactly.
        self._last_change: tuple[str, Any] | None = None
        self._unsubscribe: Callable[[], None] | None = None

        if self.is_enabled:
            self._unsubscribe = doc.on_change(self._on_change)

    @property
    def is_enabled(self) -> bool:
        return self._max_steps > 0

    @property
    def max_steps(self) -> int:
        return self._max_steps

    @property
    def merge_interval(self) -> float:
        return self._merge_interval

    def _on_change(self, event: ChangeEvent) -> None:
        self._last_change = None
        if event.flags.skip_undo:
            return
        item = UndoStackItem(operations=event.inverse_operations)
        if self._tx_type == "update":
            now = self._clock()
            last = self._undo_stack[-1] if self._undo_stack else None
            saved = (
                list(self._undo_stack),
                [(it, it.operations) for it in self._undo_stack],
                list(self._redo_stack),
                self._last_update,
            )
            if (
                last is not None
                and self._last_update is not None
                and now - self._last_update < self._merge_interval
            ):
                # Newest inverse first: undoing replays it before the older one.
                last.operations = merge_operations(item.operations, last.operations)
            else:
                if len(self._undo_stack) >= self._max_steps:
                    del self._undo_stack[0]
                self._undo_stack.append(item)
            self._redo_stack.clear()
            self._last_update = now
            self._last_change = ("update", saved)
        elif self._tx_type == "undo":
            self._redo_stack.append(item)
            self._tx_type = "update"
            self._last_change = ("redo_push", item)
        elif self._tx_type == "redo":
            self._undo_stack.append(item)
            self._tx_type = "update"
            self._last_change = ("undo_push", item)

    def _discard_last_change(self) -> None:
        """Take back what the last change event did to the stacks.

        Called by the document when a commit is rolled back *after* its
        change listeners ran (a later listener failed). Without this the
        stack would hold an entry for a change that never happened.
        """
        change = self._last_change
        self._last_change = None
        if change is None:
            return
        kind, payload = change
        if kind == "update":
            undo_items, undo_ops, redo_items, last_update = payload
            self._undo_stack = undo_items
            for it, ops in undo_ops:
                it.operations = ops
            self._redo_stack = redo_items
            self._last_update = last_update
        elif kind == "redo_push":
            if self._redo_stack and self._redo_stack[-1] is payload:
                self._redo_stack.pop()
        elif kind == "undo_push":
            if self._undo_stack and self._undo_stack[-1] is payload:
                self._undo_stack.pop()

    def undo(self) -> None:
        """Undo the last transaction."""
        self._doc.force_commit()
        if not self._undo_stack:
            return
        item = self._undo_stack.pop()
        self._tx_type = "undo"
        self._last_update = None
        try:
            self._doc.apply_operations(item.operations, raise_on_error=True)
        except Exception:
            # The step could not be applied (for example it would delete a
            # node that is now referenced). Keep it so the user can retry
            # after fixing the cause, instead of silently losing it.
            self._undo_stack.append(item)
            raise
        finally:
            self._tx_type = "update"

    def redo(self) -> None:
        """Redo the last undone transaction."""
        self._doc.force_commit()
        if not self._redo_stack:
            return
        item = self._redo_stack.pop()
        self._tx_type = "redo"
        self._last_update = None
        try:
            self._doc.apply_operations(item.operations, raise_on_error=True)
        except Exception:
            self._redo_stack.append(item)
            raise
        finally:
            self._tx_type = "update"

    @property
    def can_undo(self) -> bool:
        return len(self._undo_stack) > 0

    @property
    def can_redo(self) -> bool:
        return len(self._redo_stack) > 0

    def clear(self) -> None:
        """Drop all undo and redo history."""
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._last_update = None

    def dispose(self) -> None:
        """Stop listening to the document."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    # --- History transfer ---

    def export_history(self) -> UndoHistory:
        """Export undo and redo state for transfer to a matching document.

        Pending edits are committed first so they are part of the history.
        """
        self._doc.force_commit()
        history: UndoHistory = {
            "doc_id": self._doc.root.id,
            "doc_type": self._doc.root._node_type,
            "undo_stack": [_export_item(i) for i in self._undo_stack],
            "redo_stack": [_export_item(i) for i in self._redo_stack],
        }
        if self._last_update is not None:
            history["last_update"] = self._last_update
        return history

    def import_history(self, history: Any) -> None:
        """Replace this manager's history with a previously exported one.

        The document ID and type must match, because operations reference
        node IDs. Stacks are truncated to ``max_steps``.
        """
        validated = _validate_history(history)
        if (
            validated["doc_id"] != self._doc.root.id
            or validated["doc_type"] != self._doc.root._node_type
        ):
            raise ValueError("Undo history belongs to a different document")
        self._undo_stack = _import_stack(validated["undo_stack"], self._max_steps)
        self._redo_stack = _import_stack(validated["redo_stack"], self._max_steps)
        self._last_update = validated.get("last_update")
        self._tx_type = "update"


# --- helpers ---


def _clone_operations(operations: Operations) -> Operations:
    ordered = [tuple(op) for op in copy.deepcopy(list(operations[0]))]
    state = {nid: dict(patch) for nid, patch in operations[1].items()}
    return (ordered, state)  # type: ignore[return-value]


def _export_item(item: UndoStackItem) -> UndoHistoryItem:
    return {"operations": _clone_operations(item.operations), "meta": dict(item.meta)}


def _import_stack(items: list[UndoHistoryItem], max_steps: int) -> list[UndoStackItem]:
    retained = [] if max_steps == 0 else items[-max_steps:]
    return [
        UndoStackItem(operations=_clone_operations(i["operations"]), meta=dict(i["meta"]))
        for i in retained
    ]


def _is_ref(value: Any) -> bool:
    return value == 0 or isinstance(value, str)


def _is_ordered_op(op: Any) -> bool:
    if not isinstance(op, (list, tuple)) or not op:
        return False
    code = op[0]
    if code == 0:
        return (
            len(op) == 6
            and isinstance(op[1], (list, tuple))
            and all(
                isinstance(pair, (list, tuple))
                and len(pair) == 2
                and all(isinstance(p, str) for p in pair)
                for pair in op[1]
            )
            and _is_ref(op[2])
            and isinstance(op[3], str)
            and _is_ref(op[4])
            and _is_ref(op[5])
        )
    if code == 1:
        return len(op) == 3 and isinstance(op[1], str) and _is_ref(op[2])
    if code == 2:
        return (
            len(op) == 7
            and isinstance(op[1], str)
            and _is_ref(op[2])
            and _is_ref(op[3])
            and isinstance(op[4], str)
            and _is_ref(op[5])
            and _is_ref(op[6])
        )
    return False


def _is_operations(value: Any) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return False
    ordered, state = value
    return (
        isinstance(ordered, (list, tuple))
        and all(_is_ordered_op(op) for op in ordered)
        and isinstance(state, dict)
        and all(isinstance(k, str) and isinstance(v, dict) for k, v in state.items())
    )


def _is_item(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and _is_operations(value.get("operations"))
        and isinstance(value.get("meta"), dict)
    )


def _validate_history(value: Any) -> UndoHistory:
    ok = (
        isinstance(value, dict)
        and isinstance(value.get("doc_id"), str)
        and isinstance(value.get("doc_type"), str)
        and isinstance(value.get("undo_stack"), list)
        and all(_is_item(i) for i in value["undo_stack"])
        and isinstance(value.get("redo_stack"), list)
        and all(_is_item(i) for i in value["redo_stack"])
        and (
            value.get("last_update") is None
            or isinstance(value.get("last_update"), (int, float))
        )
    )
    if not ok:
        raise TypeError("Invalid undo history")
    return value  # type: ignore[no-any-return]
