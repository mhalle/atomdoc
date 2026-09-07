"""Array[T] — marker type for named child slots."""

from __future__ import annotations

from typing import Any, Generic, TypeVar, get_args, get_origin

T = TypeVar("T")


class Array(list[T], Generic[T]):
    """Type marker for a named child slot on a AtomNode.

    Never instantiated at runtime. ``__init_subclass__`` inspects the
    annotation and wires up a per-slot linked list + ChildrenView property.

    Implements ``__get_pydantic_core_schema__`` so it can be used as a
    field on a Pydantic BaseModel — Pydantic treats it as ``list[T]``.
    """

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: Any
    ) -> Any:
        from pydantic_core import core_schema

        args = get_args(source_type)
        if args:
            try:
                item_schema = handler.generate_schema(args[0])
                return core_schema.list_schema(item_schema)
            except Exception:
                return core_schema.list_schema()
        return core_schema.list_schema()


def is_array_type(annotation: object) -> bool:
    """Check if an annotation is ``Array[T]``."""
    origin = get_origin(annotation)
    return origin is Array or origin is list and _is_array_subclass(annotation)


def _is_array_subclass(annotation: object) -> bool:
    origin = get_origin(annotation)
    if origin is None:
        return isinstance(annotation, type) and issubclass(annotation, Array)
    return origin is Array


def get_array_element_type(annotation: object) -> type | None:
    """Extract T from ``Array[T]``, or None if not an Array type."""
    origin = get_origin(annotation)
    if origin is Array:
        args = get_args(annotation)
        return args[0] if args else None
    return None
