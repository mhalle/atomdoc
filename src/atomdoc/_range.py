"""NodeRange — result of node.to(later_sibling)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._node import DocNode


class NodeRange:
    """A contiguous range of siblings from ``start`` to ``end`` (inclusive)."""

    __slots__ = ("_start", "_end")

    def __init__(self, start: DocNode, end: DocNode) -> None:
        self._start = start
        self._end = end

    def __iter__(self) -> Iterator[DocNode]:
        current: DocNode | None = self._start
        while current is not None:
            yield current
            if current is self._end:
                break
            current = current._next_sibling
        else:
            raise ValueError(
                f"Node '{self._end.id}' is not a later sibling of '{self._start.id}'"
            )

    def delete(self) -> None:
        """Delete all nodes in the range and their descendants."""
        doc = self._start._doc_ref
        if doc is None:
            raise RuntimeError("Node is not attached to a document")
        from ._transaction import with_transaction

        def _do() -> None:
            if self._start._doc_ref is not None and self._start is self._start._doc_ref.root:
                raise RuntimeError("Root node cannot be deleted")
            from . import _operations as ops

            ops.on_delete_range(doc, self._start, self._end)
            for node in _iter_range(self._start, self._end):
                for desc in _descendants_inclusive(node):
                    doc._node_map.pop(desc.id, None)
            _detach_range(self._start, self._end)

        with_transaction(doc, _do)

    def move(self, target: DocNode, slot_name: str, position: str = "append") -> None:
        """Move all nodes in the range to a slot on target."""
        doc = self._start._doc_ref
        if doc is None:
            raise RuntimeError("Node is not attached to a document")
        from ._transaction import with_transaction

        def _do() -> None:
            from . import _operations as ops

            # Validate slot exists on target
            if slot_name not in target._slot_defs:
                raise ValueError(f"Slot '{slot_name}' does not exist on {type(target).__name__}")

            nodes_in_range = set(_iter_range(self._start, self._end))
            if target in nodes_in_range:
                raise ValueError("Target is in the range")
            anc = target._parent
            while anc is not None:
                if anc in nodes_in_range:
                    raise ValueError("Target is descendant of the range")
                anc = anc._parent

            new_prev: DocNode | None = None
            new_next: DocNode | None = None

            if position == "append":
                if target._slot_last.get(slot_name) is self._end:
                    return
                new_prev = target._slot_last.get(slot_name)
            elif position == "prepend":
                if target._slot_first.get(slot_name) is self._start:
                    return
                new_next = target._slot_first.get(slot_name)
            elif position == "before":
                raise ValueError("Use 'append' or 'prepend' for slot moves, or use insert_before on a node")
            elif position == "after":
                raise ValueError("Use 'append' or 'prepend' for slot moves, or use insert_after on a node")
            else:
                raise ValueError(f"Invalid position: {position}")

            ops.on_move_range(
                doc, self._start, self._end, target, slot_name, new_prev, new_next
            )

            _detach_range(self._start, self._end)

            # Attach at new position
            self._start._prev_sibling = new_prev
            if new_prev is not None:
                new_prev._next_sibling = self._start
            else:
                target._slot_first[slot_name] = self._start

            self._end._next_sibling = new_next
            if new_next is not None:
                new_next._prev_sibling = self._end
            else:
                target._slot_last[slot_name] = self._end

            for node in _iter_range(self._start, self._end):
                node._parent = target
                node._slot_name = slot_name

        with_transaction(doc, _do)


def _iter_range(start: DocNode, end: DocNode) -> Iterator[DocNode]:
    """Iterate siblings from start to end (inclusive)."""
    current: DocNode | None = start
    while current is not None:
        yield current
        if current is end:
            return
        current = current._next_sibling
    raise ValueError(
        f"Node '{end.id}' is not a later sibling of '{start.id}'"
    )


def _descendants_inclusive(node: DocNode) -> Iterator[DocNode]:
    """Depth-first traversal of node and all its descendants."""
    yield node
    for slot_name in node._slot_order:
        child = node._slot_first.get(slot_name)
        while child is not None:
            yield from _descendants_inclusive(child)
            child = child._next_sibling


def _descendants(node: DocNode) -> Iterator[DocNode]:
    """Depth-first traversal of descendants (excludes node itself)."""
    for slot_name in node._slot_order:
        child = node._slot_first.get(slot_name)
        while child is not None:
            yield child
            yield from _descendants(child)
            child = child._next_sibling


def _detach_range(start: DocNode, end: DocNode) -> None:
    """Unlink a range of siblings from the tree."""
    old_prev = start._prev_sibling
    old_next = end._next_sibling
    parent = start._parent
    slot_name = start._slot_name

    if old_prev is not None:
        old_prev._next_sibling = old_next
    elif parent is not None and slot_name is not None:
        parent._slot_first[slot_name] = old_next

    if old_next is not None:
        old_next._prev_sibling = old_prev
    elif parent is not None and slot_name is not None:
        parent._slot_last[slot_name] = old_prev
