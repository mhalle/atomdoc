"""UndoManager — stack-based undo/redo."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._doc import Doc
    from ._types import Operations


class UndoManager:
    """Stack-based undo/redo manager for a Doc."""

    def __init__(self, doc: Doc, max_steps: int = 100) -> None:
        self._doc = doc
        self._max_steps = max_steps
        self._undo_stack: list[Operations] = []
        self._redo_stack: list[Operations] = []
        self._tx_type: str = "update"  # "undo" | "redo" | "update"

        doc.on_change(self._on_change)

    def _on_change(self, event: object) -> None:
        from ._types import ChangeEvent

        assert isinstance(event, ChangeEvent)
        if self._tx_type == "update":
            if len(self._undo_stack) < self._max_steps:
                self._undo_stack.append(event.inverse_operations)
            self._redo_stack.clear()
        elif self._tx_type == "undo":
            self._redo_stack.append(event.inverse_operations)
            self._tx_type = "update"
        elif self._tx_type == "redo":
            self._undo_stack.append(event.inverse_operations)
            self._tx_type = "update"

    def undo(self) -> None:
        """Undo the last transaction."""
        self._doc.force_commit()
        self._tx_type = "undo"
        if not self._undo_stack:
            self._tx_type = "update"
            return
        ops = self._undo_stack.pop()
        self._doc.apply_operations(ops)
        self._doc.force_commit()

    def redo(self) -> None:
        """Redo the last undone transaction."""
        self._doc.force_commit()
        self._tx_type = "redo"
        if not self._redo_stack:
            self._tx_type = "update"
            return
        ops = self._redo_stack.pop()
        self._doc.apply_operations(ops)
        self._doc.force_commit()

    @property
    def can_undo(self) -> bool:
        return len(self._undo_stack) > 0

    @property
    def can_redo(self) -> bool:
        return len(self._redo_stack) > 0
