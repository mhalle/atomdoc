"""ChildrenView — Sequence interface over a named slot's linked list."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, overload

if TYPE_CHECKING:
    from ._node import AtomNode


class ChildrenView(Sequence["AtomNode"]):
    """Sequence view over a named slot's children.

    Supports len, indexing, slicing, iteration, truthiness, containment,
    and mutation (append, prepend, insert).
    """

    __slots__ = ("_node", "_slot_name")

    def __init__(self, node: AtomNode, slot_name: str) -> None:
        self._node = node
        self._slot_name = slot_name

    def __len__(self) -> int:
        count = 0
        current = self._node._slot_first.get(self._slot_name)
        while current is not None:
            count += 1
            current = current._next_sibling
        return count

    @overload
    def __getitem__(self, index: int) -> AtomNode: ...
    @overload
    def __getitem__(self, index: slice) -> list[AtomNode]: ...

    def __getitem__(self, index: int | slice) -> AtomNode | list[AtomNode]:
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            items = list(self)
            return items[index]
        current = self._node._slot_first.get(self._slot_name)
        i = 0
        while current is not None:
            if i == index:
                return current
            current = current._next_sibling
            i += 1
        raise IndexError(f"index {index} out of range for slot '{self._slot_name}'")

    def __iter__(self) -> Iterator[AtomNode]:
        current = self._node._slot_first.get(self._slot_name)
        while current is not None:
            yield current
            current = current._next_sibling

    def __bool__(self) -> bool:
        return self._node._slot_first.get(self._slot_name) is not None

    def __contains__(self, value: object) -> bool:
        for child in self:
            if child is value:
                return True
        return False

    def __repr__(self) -> str:
        items = list(self)
        return f"ChildrenView({self._slot_name!r}, {items!r})"

    # --- Mutation methods ---

    def append(self, *nodes: AtomNode) -> None:
        """Append nodes to the end of this slot."""
        doc = self._node._doc_ref
        if doc is None:
            raise RuntimeError("Node is not attached to a document")
        doc._insert_into_slot(self._node, self._slot_name, "append", list(nodes))

    def prepend(self, *nodes: AtomNode) -> None:
        """Prepend nodes to the beginning of this slot."""
        doc = self._node._doc_ref
        if doc is None:
            raise RuntimeError("Node is not attached to a document")
        doc._insert_into_slot(self._node, self._slot_name, "prepend", list(nodes))

    def insert(self, index: int, node: AtomNode) -> None:
        """Insert a node at the given index in this slot."""
        if index == 0:
            self.prepend(node)
            return
        try:
            target = self[index - 1]
            target.insert_after(node)
        except IndexError:
            self.append(node)

    def clear(self) -> None:
        """Delete all children in this slot."""
        first = self._node._slot_first.get(self._slot_name)
        if first is None:
            return
        last = self._node._slot_last.get(self._slot_name)
        assert last is not None
        first.to(last).delete()
