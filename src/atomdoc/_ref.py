"""``Ref[T]`` — references between nodes of one document.

A ``Ref[T]`` field holds the ID of another node in the same document.
Reading the field resolves the ID to the node; assigning accepts a node or
an ID. ``list[Ref[T]]`` holds several. Either form may be ``| None``.

A reference is *association*, not ownership. ``Array[T]`` owns its
children and controls their lifetime; a ``Ref[T]`` never does. The
document keeps a reverse index and checks referential integrity when a
transaction commits: every reference must resolve to a node of the
declared type, and a node that is still referenced cannot be deleted
(delete policy ``restrict``). Moving a node is not a delete, so
reparenting never trips the check.
"""

from __future__ import annotations

import types
from typing import Any, ForwardRef, Generic, TypeVar, Union, get_args, get_origin

from pydantic import TypeAdapter

T = TypeVar("T")


class RefIntegrityError(ValueError):
    """A reference does not resolve, has the wrong target type, or a
    referenced node would be deleted."""


class Ref(Generic[T]):
    """Type marker for a field that references another node in the same
    document.

    Never instantiated. The stored and transmitted value is the target's
    node ID (a string); Pydantic sees the field as ``str``.
    """

    __slots__ = ()

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        from pydantic_core import core_schema

        return core_schema.str_schema()


class RefDef:
    """Declaration of one reference field on a node class."""

    __slots__ = ("name", "target", "many", "optional", "policy")

    def __init__(
        self,
        name: str,
        target: type | str | None,
        many: bool,
        optional: bool,
        policy: str = "restrict",
    ) -> None:
        self.name = name
        self.target = target
        self.many = many
        self.optional = optional
        self.policy = policy

    @property
    def target_name(self) -> str | None:
        """Node type name of the declared target, or None for any node."""
        target = self.target
        if target is None:
            return None
        if isinstance(target, str):
            return target
        return getattr(target, "_node_type", getattr(target, "__name__", None))

    def accepts(self, node_cls: type, node_types: dict[str, type] | None = None) -> bool:
        """Whether a node of ``node_cls`` may be the target of this field."""
        target = self.target
        if target is None:
            return True
        if isinstance(target, type):
            if hasattr(target, "_node_type"):
                return issubclass(node_cls, target)
            # The source class of a @node (a self-reference inside a
            # BaseModel resolves to it): match by name.
            target = target.__name__
        if node_types is not None and target in node_types:
            return issubclass(node_cls, node_types[target])
        return any(getattr(c, "_node_type", None) == target for c in node_cls.__mro__)


def _target_of(arg: Any) -> type | str | None:
    if isinstance(arg, ForwardRef):
        return arg.__forward_arg__
    if isinstance(arg, TypeVar):
        return None
    return arg


def parse_ref_annotation(annotation: Any) -> tuple[type | str | None, bool, bool] | None:
    """Recognize a reference annotation.

    Returns ``(target, many, optional)`` for ``Ref[T]``, ``Ref[T] | None``,
    ``list[Ref[T]]`` and ``list[Ref[T]] | None``; ``None`` for anything else.
    """
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        all_args = get_args(annotation)
        args = [a for a in all_args if a is not type(None)]
        if len(args) != 1 or len(args) == len(all_args):
            return None
        inner = parse_ref_annotation(args[0])
        if inner is None:
            return None
        return inner[0], inner[1], True
    if origin is list:
        args = get_args(annotation)
        if len(args) != 1:
            return None
        inner = parse_ref_annotation(args[0])
        if inner is None or inner[1] or inner[2]:
            return None
        return inner[0], True, False
    if origin is Ref:
        args = get_args(annotation)
        return (_target_of(args[0]) if args else None), False, False
    if annotation is Ref:
        return None, False, False
    return None


def _is_node(value: Any) -> bool:
    return hasattr(value, "_node_type") and hasattr(value, "_doc_ref")


class RefAdapter:
    """Validates values written to a reference field.

    Nodes are coerced to their IDs; IDs pass through. The result is
    validated as ``str`` / ``list[str]`` (optionally ``None``), so the
    stored state is always plain JSON.
    """

    __slots__ = ("rdef", "_adapter")

    def __init__(self, rdef: RefDef) -> None:
        self.rdef = rdef
        base: Any = list[str] if rdef.many else str
        if rdef.optional:
            base = base | None
        self._adapter = TypeAdapter(base)

    def validate_python(self, value: Any, *, doc: Any = None) -> Any:
        return self._adapter.validate_python(self._coerce(value, doc))

    def json_schema(self) -> dict[str, Any]:
        return self._adapter.json_schema()

    def _coerce(self, value: Any, doc: Any) -> Any:
        if value is None:
            return None
        if self.rdef.many and isinstance(value, (list, tuple)):
            return [self._coerce_one(v, doc) for v in value]
        return self._coerce_one(value, doc)

    def _coerce_one(self, value: Any, doc: Any) -> Any:
        if not _is_node(value):
            return value
        if value._doc_ref is None or not value.id:
            raise ValueError(
                f"Cannot reference {value!r}: create it with doc.create_node() first"
            )
        if doc is not None and value._doc_ref is not doc:
            raise ValueError(
                f"Cannot reference {value!r}: it belongs to another document. "
                f"References resolve within one document only."
            )
        node_types = getattr(doc, "_node_types", None)
        if not self.rdef.accepts(type(value), node_types):
            raise TypeError(
                f"Field '{self.rdef.name}' references {self.rdef.target_name}, "
                f"got {type(value).__name__}"
            )
        return value.id


def ref_ids(node: Any, name: str) -> list[str]:
    """IDs stored in reference field ``name`` of ``node`` (empty if unset)."""
    raw = node._state.get(name)
    if raw is None:
        return []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, str)]
    return [raw] if isinstance(raw, str) else []
