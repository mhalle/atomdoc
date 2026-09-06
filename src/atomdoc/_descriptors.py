"""StateDescriptor — intercepts attribute access on AtomNode instances."""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter


class StateDescriptor:
    """Descriptor installed on AtomNode subclasses for each declared field.

    - ``__get__`` reads from ``node._state`` (plain value).
    - ``__set__`` validates via TypeAdapter, then delegates to
      ``doc._set_node_state()`` for transaction + op tracking.
    """

    __slots__ = ("name", "default", "adapter")

    def __init__(self, name: str, annotation: Any, default: Any) -> None:
        self.name = name
        self.default = default
        self.adapter = TypeAdapter(annotation)

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        return obj._state.get(self.name, self.default)

    def __set__(self, obj: Any, value: Any) -> None:
        validated = self.adapter.validate_python(value)
        doc = obj._doc_ref
        if doc is None:
            # Node not yet attached to a doc — store directly
            obj._state[self.name] = validated
        else:
            doc._set_node_state(obj, self.name, validated)


class RefDescriptor:
    """Descriptor for a ``Ref[T]`` / ``list[Ref[T]]`` field.

    - ``__get__`` resolves the stored ID(s) to node(s) through the owning
      document. A dangling ID (possible only after a non-strict restore)
      reads as ``None``. On a detached snapshot the raw ID(s) are returned.
    - ``__set__`` accepts nodes or IDs, stores IDs, and routes through
      ``doc._set_node_state()`` so the write is tracked and indexed.
    """

    __slots__ = ("name", "adapter")

    def __init__(self, name: str, adapter: Any) -> None:
        self.name = name
        self.adapter = adapter

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        raw = obj._state.get(self.name)
        doc = obj._doc_ref
        if doc is None:
            return raw
        if self.adapter.rdef.many:
            if raw is None:
                return None if self.adapter.rdef.optional else []
            return [doc._node_map.get(i) for i in raw]
        return doc._node_map.get(raw) if raw is not None else None

    def __set__(self, obj: Any, value: Any) -> None:
        doc = obj._doc_ref
        validated = self.adapter.validate_python(value, doc=doc)
        if doc is None:
            obj._state[self.name] = validated
        else:
            doc._set_node_state(obj, self.name, validated)
