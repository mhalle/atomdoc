"""StateDescriptor — intercepts attribute access on DocNode instances."""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter


class StateDescriptor:
    """Descriptor installed on DocNode subclasses for each declared field.

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
