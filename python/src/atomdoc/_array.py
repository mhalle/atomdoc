"""Array[T] — marker type for named child slots."""

from __future__ import annotations

import types

from typing import ClassVar, Union, Any, Generic, TypeVar, get_args, get_origin

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


def is_classvar_annotation(annotation: object) -> bool:
    """Whether an annotation is ``ClassVar[...]`` (a class member, not a
    field), also when it is still a string under postponed evaluation."""
    if isinstance(annotation, str):
        return annotation.startswith(("ClassVar", "typing.ClassVar"))
    return annotation is ClassVar or get_origin(annotation) is ClassVar


def is_array_annotation(annotation: object) -> bool:
    """Whether an annotation declares a slot: ``Array[T]`` or bare ``Array``."""
    return annotation is Array or get_origin(annotation) is Array


def get_array_element_type(annotation: object) -> Any:
    """The T of ``Array[T]`` (a class or a union of classes); None for a
    bare ``Array`` (any node) or for something that is not an Array."""
    origin = get_origin(annotation)
    if origin is Array:
        args = get_args(annotation)
        return args[0] if args else None
    return None


def slot_member_types(allowed: Any) -> tuple[Any, ...]:
    """The classes a slot's ``allowed_type`` names: one for ``Array[T]``,
    several for ``Array[A | B]``, none for a bare ``Array``."""
    if allowed is None:
        return ()
    origin = get_origin(allowed)
    if origin is Union or origin is types.UnionType:
        return tuple(a for a in get_args(allowed) if a is not type(None))
    return (allowed,)
