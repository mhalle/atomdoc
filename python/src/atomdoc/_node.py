"""AtomNode base class — plain Python objects with Pydantic-powered schemas."""

from __future__ import annotations

import copy

import re
import sys
from collections.abc import Callable
from typing import Any, ClassVar, get_type_hints

from pydantic_core import to_jsonable_python
from pydantic import BaseModel, ConfigDict, TypeAdapter, create_model
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from ._array import Array, get_array_element_type, is_array_annotation
from ._children import ChildrenView
from ._descriptors import _MISSING, RefDescriptor, StateDescriptor
from ._range import NodeRange
from ._ref import Ref, RefAdapter, RefDef, parse_ref_annotation
from ._tier import Tier, classify_field

# A string annotation that (probably) declares an ``Array[...]`` slot or a
# ``Ref[...]`` field.
_MARKER_LIKE = re.compile(r"\b(?:Array|Ref)\s*\[")


def _resolve_annotations(
    cls: type, annotations: dict[str, Any], owners: dict[str, type]
) -> dict[str, Any]:
    """Resolve string annotations left by ``from __future__ import annotations``.

    ``typing.get_type_hints`` is tried first.  Passing only ``localns`` makes
    it evaluate each class in the MRO against that class's *own* module
    globals, so mixin bases from other modules and AtomNode's internals all
    resolve.  ``localns`` adds the atomdoc names a field may reference
    without importing them under the same name, plus the class itself so
    self-referential slots (``children: Array[Tree]``) resolve before the
    module-level name is bound.

    ``get_type_hints`` raises as a whole when any one name is unresolvable,
    so on failure each field is evaluated on its own (against the globals of
    the class that declared it) and the rest still resolve.  A field that is
    still a string and looks like ``Array[...]`` or ``Ref[...]`` raises
    TypeError: left alone it would silently become an ordinary state field
    (a plain Python list, or a bare string) and the document would never
    record its children or track the reference.
    """
    localns: dict[str, Any] = {
        "Array": Array, "Ref": Ref, "AtomNode": AtomNode, cls.__name__: cls,
    }
    # Inherited slot annotations are re-resolved on every subclass, so a
    # base node class must be findable by name even when it is function-local.
    for base in cls.__mro__[1:]:
        if base is AtomNode or base is object:
            continue
        localns.setdefault(base.__name__, base)
    resolved = dict(annotations)

    try:
        hints = get_type_hints(cls, localns=localns, include_extras=True)
    except Exception:
        hints = {}
    for name in annotations:
        if name in hints:
            resolved[name] = hints[name]

    for name, ann in resolved.items():
        if not isinstance(ann, str):
            continue
        owner = owners[name]
        module = sys.modules.get(owner.__module__, None)
        globalns = module.__dict__ if module is not None else {}
        try:
            resolved[name] = eval(ann, globalns, localns)  # noqa: S307
        except Exception as exc:
            if _MARKER_LIKE.search(ann):
                raise TypeError(
                    f"{cls.__qualname__}.{name}: cannot resolve the "
                    f"annotation {ann!r} ({type(exc).__name__}: {exc}). "
                    f"Array[...] and Ref[...] target types must be resolvable from the "
                    f"globals of module {owner.__module__!r} (plus atomdoc names "
                    f"and the node class itself); a class defined inside a "
                    f"function cannot be found from a string annotation. Define "
                    f"the element type at module level, or drop "
                    f"'from __future__ import annotations' in that module."
                ) from exc
            # Non-slot fields keep the string; Pydantic gets a chance to
            # resolve it when the schema model is built.
    return resolved


class SlotDef:
    """Definition of a named child slot on a AtomNode class."""

    __slots__ = ("name", "allowed_type")

    def __init__(self, name: str, allowed_type: type | None) -> None:
        self.name = name
        self.allowed_type = allowed_type


_IMMUTABLE_LEAVES = (str, int, float, bool, bytes, type(None))


def _copy_container(value: Any) -> Any:
    """Copy of a default value that shares nothing mutable with it.

    Cheaper than ``copy.deepcopy`` for the JSON-like defaults fields hold
    (a 4x4 matrix as a list of floats, say): immutable leaves are shared,
    containers are rebuilt, and anything else (a model, whose own list
    fields are mutable even when the model is frozen) is deep-copied.
    """
    if isinstance(value, _IMMUTABLE_LEAVES):
        return value
    if isinstance(value, list):
        return [_copy_container(v) for v in value]
    if isinstance(value, dict):
        return {k: _copy_container(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return tuple(_copy_container(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return type(value)(_copy_container(v) for v in value)
    if isinstance(value, BaseModel):
        return value.model_copy(deep=True)
    return copy.deepcopy(value)


def _value_to_json(value: Any) -> Any:
    """A state value as native JSON.

    Models (frozen values, handles) dump in JSON mode; ``bytes`` (the
    opaque tier) are base64; a container is walked so a ``list[Color]`` or
    ``dict[str, Color]`` serializes its models too; JSON scalars pass
    through.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, bytes):
        import base64

        return base64.b64encode(value).decode()
    if isinstance(value, (list, tuple, dict, set, frozenset)):
        return to_jsonable_python(value)
    return value


class SlotDescriptor:
    """Property descriptor that returns a ChildrenView for a named slot.

    The view is created once per node and slot and cached on the node, so
    the view's index cursor (see ``ChildrenView.__getitem__``) survives
    repeated ``node.slot[i]`` accesses.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        views = obj.__dict__.get("_slot_views")
        if views is None:
            views = {}
            object.__setattr__(obj, "_slot_views", views)
        view = views.get(self.name)
        if view is None:
            view = views[self.name] = ChildrenView(obj, self.name)
        return view

    def __set__(self, obj: Any, value: Any) -> None:
        raise AttributeError(f"Cannot assign to slot '{self.name}' directly; use .append(), .insert(), etc.")


class AtomNode:
    """Base class for all document nodes.

    Subclass with ``class MyNode(AtomNode, node_type="my_type"):`` to define
    a node type. Fields are declared as class-level annotations with defaults.
    Array[T] fields become named child slots.
    """

    # --- ClassVars populated by __init_subclass__ ---
    _node_type: ClassVar[str]
    _schema_model: ClassVar[type[BaseModel] | None]
    _validator_model: ClassVar[type[BaseModel] | None]  # source BaseModel with validators
    _constraint_model: ClassVar[type[BaseModel] | None] = None  # schema model, for Field constraints
    _constrained_fields: ClassVar[set[str]] = set()
    _field_defaults: ClassVar[dict[str, Any]]
    _field_tiers: ClassVar[dict[str, Tier]]
    _field_annotations: ClassVar[dict[str, Any]] = {}
    _field_infos: ClassVar[dict[str, FieldInfo]] = {}
    _field_factories: ClassVar[dict[str, Callable[[], Any]]] = {}
    _ref_defs: ClassVar[dict[str, RefDef]] = {}
    _field_adapters: ClassVar[dict[str, TypeAdapter[Any]]]
    _slot_defs: ClassVar[dict[str, SlotDef]]
    _slot_order: ClassVar[list[str]]
    _is_abstract: ClassVar[bool]

    # --- Instance attributes (set via object.__setattr__ in __init__) ---
    id: str
    _state: dict[str, Any]
    _doc_ref: Any  # Doc | None
    _parent: AtomNode | None
    _slot_name: str | None  # which slot of parent this node belongs to
    _prev_sibling: AtomNode | None
    _next_sibling: AtomNode | None
    # Per-slot first/last pointers: {slot_name: AtomNode | None}
    _slot_first: dict[str, AtomNode | None]
    _slot_last: dict[str, AtomNode | None]

    def __init_subclass__(cls, node_type: str | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        if node_type is None:
            cls._is_abstract = True
            return

        cls._is_abstract = False
        cls._node_type = node_type
        if not hasattr(cls, "_validator_model"):
            cls._validator_model = None

        # Walk MRO to collect annotations + defaults, remembering which
        # class declared each field so its string annotations can be
        # resolved against that class's module.
        annotations: dict[str, Any] = {}
        defaults: dict[str, Any] = {}
        owners: dict[str, type] = {}

        for base in reversed(cls.__mro__):
            if base is AtomNode or base is object:
                continue
            base_annotations = getattr(base, "__annotations__", {})
            for name, ann in base_annotations.items():
                if name.startswith("_"):
                    continue
                annotations[name] = ann
                owners[name] = base
                if hasattr(base, name):
                    val = getattr(base, name)
                    if isinstance(val, (StateDescriptor, RefDescriptor, SlotDescriptor)):
                        # A node base has already replaced its declaration
                        # with a descriptor; recover what it was built from
                        # so the field inherits its default, factory and
                        # constraints rather than the descriptor object.
                        info = getattr(base, "_field_infos", {}).get(name)
                        if info is not None:
                            defaults[name] = info
                        else:
                            inherited = getattr(base, "_field_defaults", {}).get(name, _MISSING)
                            if inherited is not _MISSING:
                                defaults[name] = inherited
                    else:
                        defaults[name] = val

        # Resolve string annotations (needed for ``from __future__ import
        # annotations``).  Raises TypeError for an unresolvable Array slot.
        annotations = _resolve_annotations(cls, annotations, owners)

        # Separate Array fields (slots) from state fields
        state_annotations: dict[str, Any] = {}
        state_defaults: dict[str, Any] = {}
        field_infos: dict[str, FieldInfo] = {}
        field_factories: dict[str, Callable[[], Any]] = {}
        slot_defs: dict[str, SlotDef] = {}
        slot_order: list[str] = []

        for name, ann in annotations.items():
            if is_array_annotation(ann):
                elem_type = get_array_element_type(ann)  # None: any node
                slot_defs[name] = SlotDef(name, elem_type)
                slot_order.append(name)
                # Install slot descriptor
                setattr(cls, name, SlotDescriptor(name))
            else:
                state_annotations[name] = ann
                if name in defaults:
                    default = defaults[name]
                    if isinstance(default, FieldInfo):
                        # ``x: float = Field(ge=0, default=1.0)`` on a plain
                        # class: keep the constraints, unwrap the default.
                        field_infos[name] = default
                        if default.default is not PydanticUndefined:
                            state_defaults[name] = default.default
                        elif default.default_factory is not None:
                            # The factory runs once per node (see
                            # ``_fresh_default``); this value is the
                            # representative default for export and
                            # "is it still the default" comparisons.
                            field_factories[name] = default.default_factory  # type: ignore[assignment]
                            state_defaults[name] = default.default_factory()  # type: ignore[call-arg]
                    else:
                        state_defaults[name] = default

        cls._slot_defs = slot_defs
        cls._slot_order = slot_order
        cls._field_annotations = dict(state_annotations)
        cls._field_infos = field_infos
        cls._field_factories = field_factories
        cls._ref_defs = {}

        # Build Pydantic schema model from state fields only
        if not state_annotations:
            cls._field_defaults = {}
            cls._field_tiers = {}
            cls._field_adapters = {}
            cls._schema_model = create_model(f"{cls.__name__}Schema") if state_annotations else None  # type: ignore[call-overload]
            return

        model_fields: dict[str, Any] = {}
        for name, ann in state_annotations.items():
            if name in field_infos:
                model_fields[name] = (ann, field_infos[name])
            elif name in state_defaults:
                model_fields[name] = (ann, state_defaults[name])
            else:
                model_fields[name] = (ann, ...)

        cls._schema_model = create_model(  # type: ignore[call-overload]
            f"{cls.__name__}Schema",
            # Commit validation feeds the model by field name even when a
            # field declares an alias.
            __config__=ConfigDict(populate_by_name=True),
            **model_fields,
        )

        # Classify fields and create descriptors
        cls._field_defaults = {}
        cls._field_tiers = {}
        cls._field_adapters = {}

        for name, ann in state_annotations.items():
            default = state_defaults.get(name, _MISSING)
            cls._field_defaults[name] = default

            ref_spec = parse_ref_annotation(ann)
            if ref_spec is not None:
                target, many, optional = ref_spec
                rdef = RefDef(name, target, many, optional)
                cls._ref_defs[name] = rdef
                cls._field_tiers[name] = "ref"
                ref_adapter = RefAdapter(rdef)
                cls._field_adapters[name] = ref_adapter  # type: ignore[assignment]
                setattr(cls, name, RefDescriptor(name, ref_adapter))
                continue

            tier = classify_field(ann)
            cls._field_tiers[name] = tier
            adapter = TypeAdapter(ann)
            cls._field_adapters[name] = adapter

            desc = StateDescriptor(name, ann, default)
            setattr(cls, name, desc)

        # Constraints declared with ``Field(...)`` (own or inherited) are
        # enforced at commit through the schema model, unless the class's
        # validator model already covers those fields (a BaseModel source
        # validates its own constraints).
        constrained = {name for name, fi in field_infos.items() if fi.metadata}
        cls._constraint_model = cls._schema_model if constrained else None
        cls._constrained_fields = constrained

    @classmethod
    def _fresh_default(cls, name: str) -> Any:
        """A default value for ``name`` that is safe to hand to one node.

        ``default_factory`` runs per node; a mutable literal default (list,
        dict, set) is copied so nodes never share one object. Returns the
        ``_MISSING`` sentinel when the field has no default.
        """
        factory = cls._field_factories.get(name)
        if factory is not None:
            return factory()
        default = cls._field_defaults.get(name, _MISSING)
        if default is _MISSING or isinstance(default, _IMMUTABLE_LEAVES):
            return default
        return _copy_container(default)

    @classmethod
    def _apply_defaults(cls, state: dict[str, Any]) -> None:
        """Fill every unset field of ``state`` that has a default."""
        for name, default in cls._field_defaults.items():
            if default is not _MISSING and name not in state:
                state[name] = cls._fresh_default(name)

    def __init__(self, _id: str | None = None, _doc: Any = None, **kwargs: Any) -> None:
        if _id is not None:
            # Internal construction — called by Doc.create_node / deserialization
            self._init_internal(_id, _doc)
        else:
            # User construction — Annotation(label="x", color=Color(...))
            # Creates a snapshot that Doc will later convert to a live node
            self._init_snapshot(kwargs)

    def _init_internal(self, _id: str, _doc: Any) -> None:
        # A revived node must not keep slot views (and their cursors)
        # from its earlier life.
        self.__dict__.pop("_slot_views", None)
        object.__setattr__(self, "_state", {})
        object.__setattr__(self, "id", _id)
        object.__setattr__(self, "_doc_ref", _doc)
        object.__setattr__(self, "_parent", None)
        object.__setattr__(self, "_slot_name", None)
        object.__setattr__(self, "_prev_sibling", None)
        object.__setattr__(self, "_next_sibling", None)
        object.__setattr__(self, "_snapshot", None)
        slot_first: dict[str, AtomNode | None] = {}
        slot_last: dict[str, AtomNode | None] = {}
        for name in self._slot_order:
            slot_first[name] = None
            slot_last[name] = None
        object.__setattr__(self, "_slot_first", slot_first)
        object.__setattr__(self, "_slot_last", slot_last)

    def _init_snapshot(self, kwargs: dict[str, Any]) -> None:
        """User-facing construction: store field values and slot children."""
        object.__setattr__(self, "id", "")
        object.__setattr__(self, "_doc_ref", None)
        object.__setattr__(self, "_parent", None)
        object.__setattr__(self, "_slot_name", None)
        object.__setattr__(self, "_prev_sibling", None)
        object.__setattr__(self, "_next_sibling", None)
        object.__setattr__(self, "_slot_first", {})
        object.__setattr__(self, "_slot_last", {})

        state: dict[str, Any] = {}
        slots: dict[str, list[AtomNode]] = {}

        # Apply defaults for state fields
        self._apply_defaults(state)

        # Process kwargs
        for name, value in kwargs.items():
            if name in self._slot_defs:
                # It's a slot — value should be a list of node snapshots
                if not isinstance(value, (list, tuple)):
                    raise TypeError(f"Slot '{name}' expects a list, got {type(value).__name__}")
                slots[name] = list(value)
            elif name in self._field_adapters:
                state[name] = self._field_adapters[name].validate_python(value)
            else:
                raise TypeError(f"{type(self).__name__} has no field {name!r}")

        object.__setattr__(self, "_state", state)
        object.__setattr__(self, "_snapshot", slots)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id!r}>"

    def ref_id(self, name: str) -> str | list[str] | None:
        """The stored ID (or IDs) of reference field ``name``, unresolved."""
        if name not in self._ref_defs:
            raise AttributeError(f"'{name}' is not a reference field")
        value = self._state.get(name)
        return list(value) if isinstance(value, list) else value

    # --- Range ---

    def to(self, later_sibling: AtomNode) -> NodeRange:
        """Create a range from this node to ``later_sibling`` (inclusive)."""
        return NodeRange(self, later_sibling)

    # --- Mutation methods ---

    def delete(self) -> None:
        """Delete this node and all its descendants."""
        self.to(self).delete()

    def move(
        self,
        target: AtomNode,
        slot_name: str | None = None,
        position: str = "append",
    ) -> None:
        """Move this node to a slot on ``target`` (append/prepend) or next to
        a sibling ``target`` (before/after)."""
        self.to(self).move(target, slot_name, position)

    def insert_after(self, *nodes: AtomNode) -> None:
        """Insert nodes after this node in the same slot."""
        doc = self._doc_ref
        if doc is None:
            raise RuntimeError("Node is not attached to a document")
        parent = self._parent
        slot = self._slot_name
        if parent is None or slot is None:
            raise RuntimeError("Node has no parent slot")
        doc._insert_into_slot(parent, slot, "after", list(nodes), target=self)

    def insert_before(self, *nodes: AtomNode) -> None:
        """Insert nodes before this node in the same slot."""
        doc = self._doc_ref
        if doc is None:
            raise RuntimeError("Node is not attached to a document")
        parent = self._parent
        slot = self._slot_name
        if parent is None or slot is None:
            raise RuntimeError("Node has no parent slot")
        doc._insert_into_slot(parent, slot, "before", list(nodes), target=self)

    def replace(self, *nodes: AtomNode) -> None:
        """Replace this node with the given nodes."""
        prev = self._prev_sibling
        next_sib = self._next_sibling
        parent = self._parent
        slot = self._slot_name
        self.to(self).delete()
        if prev is not None:
            prev.insert_after(*nodes)
        elif next_sib is not None:
            next_sib.insert_before(*nodes)
        elif parent is not None and slot is not None:
            doc = parent._doc_ref
            if doc is not None:
                doc._insert_into_slot(parent, slot, "append", list(nodes))

    # --- State serialization helpers ---

    def _state_to_json(self) -> dict[str, Any]:
        """Serialize non-default state fields to native JSON values.

        Values are JSON-compatible (strings, numbers, booleans, arrays,
        objects, or null). Opaque/bytes fields are base64-encoded strings;
        receivers decode based on the field's schema tier.
        """
        return self._state_to_json_plain(include_defaults=False)

    def _state_key_to_json(self, key: str) -> Any:
        """Serialize a single state key to a native JSON value.

        Falls back to the field's default if unset, or ``None`` if no
        default is defined.
        """
        if key not in self._state:
            default = self._field_defaults.get(key, _MISSING)
            if default is _MISSING:
                return None
            value = default
        else:
            value = self._state[key]

        return _value_to_json(value)

    def _parse_state_key(self, key: str, json_val: Any) -> Any:
        """Parse a native JSON state value into its Python type."""
        return self._parse_json_value(key, json_val)

    # --- Plain JSON serialization (for document format) ---

    def _state_to_json_plain(self, include_defaults: bool = False) -> dict[str, Any]:
        """Serialize state fields to plain JSON values (no double-stringify).

        If ``include_defaults`` is False (default), fields matching their
        default value are omitted.
        """
        result: dict[str, Any] = {}
        for key, value in self._state.items():
            if not include_defaults:
                default = self._field_defaults.get(key, _MISSING)
                if default is not _MISSING and value == default:
                    continue
            result[key] = _value_to_json(value)
        return result

    def _parse_json_value(self, key: str, json_val: Any) -> Any:
        """Parse a plain JSON value back to its Python type (for deserialization)."""
        if json_val is None and self._field_defaults.get(key) is None:
            # ``null`` is "unset" for a field without a default, or whose
            # default is None (that is how an unset field serializes; a
            # required field's inverse op carries it and must round-trip).
            # For any other field ``null`` is a value and is validated
            # like one below.
            return None
        tier = self._field_tiers.get(key)
        if tier == "opaque" and isinstance(json_val, str):
            import base64
            return base64.b64decode(json_val)
        adapter = self._field_adapters.get(key)
        if adapter is not None:
            return adapter.validate_python(json_val)
        return json_val
