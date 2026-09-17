"""StateDescriptor — intercepts attribute access on AtomNode instances."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, TypeAdapter

# Sentinel for "no default"
_MISSING = object()


def _holds_model(value: Any) -> bool:
    if isinstance(value, BaseModel):
        return True
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_holds_model(item) for item in value)
    if isinstance(value, dict):
        return any(_holds_model(item) for item in value.values())
    return False


def _revalidated(value: Any) -> Any:
    """``value`` with every model instance in it validated again, inside out,
    each by its own class. Its own class, not the field's annotation: two
    members of a union may share a shape (a weak and a strong handle), and
    validating plain data would hand back whichever the union tries first."""
    if isinstance(value, BaseModel):
        cls = type(value)
        data: dict[str, Any] = {}
        for name, field in cls.model_fields.items():
            if name in value.__dict__:
                data[field.alias or name] = _revalidated(value.__dict__[name])
        for name, extra in (value.model_extra or {}).items():
            data[name] = _revalidated(extra)
        return cls.model_validate(data)
    if isinstance(value, list):
        return [_revalidated(item) for item in value]
    if isinstance(value, (tuple, set, frozenset)):
        return type(value)(_revalidated(item) for item in value)
    if isinstance(value, dict):
        return {key: _revalidated(item) for key, item in value.items()}
    return value


class ValueAdapter:
    """A field's TypeAdapter that does not trust a model instance it is handed.

    Pydantic accepts an instance of the right class without looking inside
    it, and ``model_copy(update=...)`` and ``model_construct(...)`` both make
    instances nobody validated: a ``Color`` with ``r='x'``. Every model
    instance in a value — the value itself, or inside a list, tuple, set, or
    dict, or inside another model — is validated again by its own class
    before the field's type checks the whole, so what enters a document has
    been validated however it was built. Values without one take the
    ordinary path.
    """

    __slots__ = ("_adapter",)

    def __init__(self, annotation: Any) -> None:
        self._adapter: TypeAdapter[Any] = TypeAdapter(annotation)

    def validate_python(self, value: Any, **kwargs: Any) -> Any:
        if _holds_model(value):
            value = _revalidated(value)
        return self._adapter.validate_python(value, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)


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
        self.adapter = ValueAdapter(annotation)

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        value = obj._state.get(self.name, self.default)
        # A required field that has not been set reads as None; the
        # sentinel never leaks to user code.
        return None if value is _MISSING else value

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
