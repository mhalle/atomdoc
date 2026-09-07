"""NodeRange — result of node.to(later_sibling)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._node import AtomNode


class NodeRange:
    """A contiguous range of siblings from ``start`` to ``end`` (inclusive)."""

    __slots__ = ("_start", "_end")

    def __init__(self, start: AtomNode, end: AtomNode) -> None:
        self._start = start
        self._end = end

    def __iter__(self) -> Iterator[AtomNode]:
        current: AtomNode | None = self._start
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

            # Validate before recording anything, so a bad range inside an
            # enclosing transaction leaves it untouched.
            doc._check_live(self._start)
            doc._check_live(self._end)
            nodes = list(_iter_range(self._start, self._end))

            ops.on_delete_range(doc, self._start, self._end)
            for node in nodes:
                for desc in _descendants_inclusive(node):
                    doc._node_map.pop(desc.id, None)
                    doc._graveyard[desc.id] = desc
                    doc._refs_remove(desc)
            _detach_range(self._start, self._end)

        with_transaction(doc, _do)

    def move(
        self,
        target: AtomNode,
        slot_name: str | None = None,
        position: str = "append",
    ) -> None:
        """Move all nodes in the range relative to ``target``.

        With ``append``/``prepend``, ``target`` is the new parent and
        ``slot_name`` names the slot. With ``before``/``after``, ``target``
        is a sibling and the range lands next to it in the sibling's slot.
        """
        doc = self._start._doc_ref
        if doc is None:
            raise RuntimeError("Node is not attached to a document")
        from ._transaction import with_transaction

        def _do() -> None:
            from . import _operations as ops

            if position not in ("append", "prepend", "before", "after"):
                raise ValueError(f"Invalid position: {position}")

            if position in ("before", "after"):
                new_parent = target._parent
                slot = target._slot_name
                if new_parent is None or slot is None:
                    raise ValueError("Cannot move before or after the root")
            else:
                if slot_name is None:
                    raise ValueError("slot_name is required for 'append' and 'prepend'")
                new_parent = target
                slot = slot_name

            # Validate slot exists on the new parent
            if slot not in new_parent._slot_defs:
                raise ValueError(
                    f"Slot '{slot}' does not exist on {type(new_parent).__name__}"
                )
            # A move keeps the node alive in the document, so the new
            # parent must be in the document: moving into a detached node
            # would orphan the range while it stays in the node map.
            if new_parent.id not in doc._node_map:
                raise ValueError(
                    f"Cannot move into {new_parent!r}: it is not in the document"
                )
            doc._check_live(self._start)
            doc._check_live(self._end)
            doc._check_live(new_parent)
            if position in ("before", "after"):
                doc._check_live(target)

            nodes_in_range = set(_iter_range(self._start, self._end))
            from ._doc import _check_allowed

            allowed = new_parent._slot_defs[slot].allowed_type
            for node in nodes_in_range:
                _check_allowed(allowed, node, slot, new_parent)
            if new_parent in nodes_in_range:
                raise ValueError("Target is in the range")
            anc = new_parent._parent
            while anc is not None:
                if anc in nodes_in_range:
                    raise ValueError("Target is descendant of the range")
                anc = anc._parent

            new_prev: AtomNode | None = None
            new_next: AtomNode | None = None

            if position == "append":
                if new_parent._slot_last.get(slot) is self._end:
                    return
                new_prev = new_parent._slot_last.get(slot)
            elif position == "prepend":
                if new_parent._slot_first.get(slot) is self._start:
                    return
                new_next = new_parent._slot_first.get(slot)
            elif position == "before":
                if target in nodes_in_range:
                    raise ValueError("Target is in the range")
                if target._prev_sibling is self._end:
                    return
                new_prev = target._prev_sibling
                new_next = target
            else:  # after
                if target in nodes_in_range:
                    raise ValueError("Target is in the range")
                if target._next_sibling is self._start:
                    return
                new_prev = target
                new_next = target._next_sibling

            ops.on_move_range(
                doc, self._start, self._end, new_parent, slot, new_prev, new_next
            )

            _detach_range(self._start, self._end)

            # Attach at new position
            self._start._prev_sibling = new_prev
            if new_prev is not None:
                new_prev._next_sibling = self._start
            else:
                new_parent._slot_first[slot] = self._start

            self._end._next_sibling = new_next
            if new_next is not None:
                new_next._prev_sibling = self._end
            else:
                new_parent._slot_last[slot] = self._end

            for node in _iter_range(self._start, self._end):
                node._parent = new_parent
                node._slot_name = slot

        with_transaction(doc, _do)


def _iter_range(start: AtomNode, end: AtomNode) -> Iterator[AtomNode]:
    """Iterate siblings from start to end (inclusive)."""
    current: AtomNode | None = start
    while current is not None:
        yield current
        if current is end:
            return
        current = current._next_sibling
    raise ValueError(
        f"Node '{end.id}' is not a later sibling of '{start.id}'"
    )


def _descendants_inclusive(node: AtomNode) -> Iterator[AtomNode]:
    """Depth-first traversal of node and all its descendants."""
    yield node
    for slot_name in node._slot_order:
        child = node._slot_first.get(slot_name)
        while child is not None:
            yield from _descendants_inclusive(child)
            child = child._next_sibling


def _descendants(node: AtomNode) -> Iterator[AtomNode]:
    """Depth-first traversal of descendants (excludes node itself)."""
    for slot_name in node._slot_order:
        child = node._slot_first.get(slot_name)
        while child is not None:
            yield child
            yield from _descendants(child)
            child = child._next_sibling


def _detach_range(start: AtomNode, end: AtomNode) -> None:
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
