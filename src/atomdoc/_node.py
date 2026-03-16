"""AtomNode base class — plain Python objects with Pydantic-powered schemas."""

from __future__ import annotations

import sys
from typing import Any, ClassVar, get_type_hints

from pydantic import BaseModel, TypeAdapter, create_model

from ._array import get_array_element_type
from ._children import ChildrenView
from ._descriptors import StateDescriptor
from ._range import NodeRange
from ._tier import Tier, classify_field

# Sentinel for "no default"
_MISSING = object()


class SlotDef:
    """Definition of a named child slot on a AtomNode class."""

    __slots__ = ("name", "allowed_type")

    def __init__(self, name: str, allowed_type: type | None) -> None:
        self.name = name
        self.allowed_type = allowed_type


class SlotDescriptor:
    """Property descriptor that returns a ChildrenView for a named slot."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        return ChildrenView(obj, self.name)

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
    _field_defaults: ClassVar[dict[str, Any]]
    _field_tiers: ClassVar[dict[str, Tier]]
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

        # Walk MRO to collect annotations + defaults
        annotations: dict[str, Any] = {}
        defaults: dict[str, Any] = {}

        for base in reversed(cls.__mro__):
            if base is AtomNode or base is object:
                continue
            base_annotations = getattr(base, "__annotations__", {})
            for name, ann in base_annotations.items():
                if name.startswith("_"):
                    continue
                annotations[name] = ann
                if hasattr(base, name):
                    val = getattr(base, name)
                    if not isinstance(val, (StateDescriptor, SlotDescriptor)):
                        defaults[name] = val

        # Resolve string annotations
        module = sys.modules.get(cls.__module__, None)
        globalns = getattr(module, "__dict__", {}) if module else {}
        try:
            resolved = get_type_hints(cls, globalns=globalns, include_extras=True)
        except Exception:
            resolved = {}

        for name in list(annotations):
            if name in resolved:
                annotations[name] = resolved[name]

        # Separate Array fields (slots) from state fields
        state_annotations: dict[str, Any] = {}
        state_defaults: dict[str, Any] = {}
        slot_defs: dict[str, SlotDef] = {}
        slot_order: list[str] = []

        for name, ann in annotations.items():
            elem_type = get_array_element_type(ann)
            if elem_type is not None:
                slot_defs[name] = SlotDef(name, elem_type)
                slot_order.append(name)
                # Install slot descriptor
                setattr(cls, name, SlotDescriptor(name))
            else:
                state_annotations[name] = ann
                if name in defaults:
                    state_defaults[name] = defaults[name]

        cls._slot_defs = slot_defs
        cls._slot_order = slot_order

        # Build Pydantic schema model from state fields only
        if not state_annotations:
            cls._field_defaults = {}
            cls._field_tiers = {}
            cls._field_adapters = {}
            cls._schema_model = create_model(f"{cls.__name__}Schema") if state_annotations else None  # type: ignore[call-overload]
            return

        model_fields: dict[str, Any] = {}
        for name, ann in state_annotations.items():
            if name in state_defaults:
                model_fields[name] = (ann, state_defaults[name])
            else:
                model_fields[name] = (ann, ...)

        cls._schema_model = create_model(  # type: ignore[call-overload]
            f"{cls.__name__}Schema",
            **model_fields,
        )

        # Classify fields and create descriptors
        cls._field_defaults = {}
        cls._field_tiers = {}
        cls._field_adapters = {}

        for name, ann in state_annotations.items():
            default = state_defaults.get(name, _MISSING)
            tier = classify_field(ann)
            cls._field_tiers[name] = tier
            adapter = TypeAdapter(ann)
            cls._field_adapters[name] = adapter

            if default is not _MISSING:
                cls._field_defaults[name] = default
            else:
                cls._field_defaults[name] = _MISSING

            desc = StateDescriptor(name, ann, default if default is not _MISSING else _MISSING)
            setattr(cls, name, desc)

    def __init__(self, _id: str | None = None, _doc: Any = None, **kwargs: Any) -> None:
        if _id is not None:
            # Internal construction — called by Doc.create_node / deserialization
            self._init_internal(_id, _doc)
        else:
            # User construction — Annotation(label="x", color=Color(...))
            # Creates a snapshot that Doc will later convert to a live node
            self._init_snapshot(kwargs)

    def _init_internal(self, _id: str, _doc: Any) -> None:
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
        for name, default in self._field_defaults.items():
            if default is not _MISSING:
                state[name] = default

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
                state[name] = value

        object.__setattr__(self, "_state", state)
        object.__setattr__(self, "_snapshot", slots)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id!r}>"

    # --- Range ---

    def to(self, later_sibling: AtomNode) -> NodeRange:
        """Create a range from this node to ``later_sibling`` (inclusive)."""
        return NodeRange(self, later_sibling)

    # --- Mutation methods ---

    def delete(self) -> None:
        """Delete this node and all its descendants."""
        self.to(self).delete()

    def move(self, target: AtomNode, slot_name: str, position: str = "append") -> None:
        """Move this node to a slot on target."""
        self.to(self).move(target, slot_name, position)  # type: ignore[arg-type]

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

    def _state_to_json(self) -> dict[str, str]:
        """Serialize non-default state fields to {field: json_string}."""
        import json
        from pydantic import BaseModel as _BM

        result: dict[str, str] = {}
        for key, value in self._state.items():
            default = self._field_defaults.get(key, _MISSING)
            if default is not _MISSING and value == default:
                continue
            if isinstance(value, _BM):
                result[key] = json.dumps(value.model_dump(mode="json"))
            elif isinstance(value, bytes):
                import base64
                result[key] = json.dumps(base64.b64encode(value).decode())
            else:
                result[key] = json.dumps(value)
        return result

    def _stringify_state_key(self, key: str) -> str:
        """Serialize a single state key to a JSON string."""
        import json
        from pydantic import BaseModel as _BM

        if key not in self._state:
            default = self._field_defaults.get(key, _MISSING)
            if default is _MISSING:
                return json.dumps(None)
            value = default
        else:
            value = self._state[key]

        if isinstance(value, _BM):
            return json.dumps(value.model_dump(mode="json"))
        elif isinstance(value, bytes):
            import base64
            return json.dumps(base64.b64encode(value).decode())
        else:
            return json.dumps(value)

    def _parse_state_key(self, key: str, stringified: str) -> Any:
        """Parse a stringified state value (used internally by op tracking)."""
        import json

        json_val = json.loads(stringified)
        tier = self._field_tiers.get(key)
        if tier == "opaque" and isinstance(json_val, str):
            import base64
            return base64.b64decode(json_val)
        adapter = self._field_adapters.get(key)
        if adapter is not None:
            return adapter.validate_python(json_val)
        return json_val

    # --- Plain JSON serialization (for document format) ---

    def _state_to_json_plain(self) -> dict[str, Any]:
        """Serialize non-default state fields to plain JSON values (no double-stringify)."""
        from pydantic import BaseModel as _BM

        result: dict[str, Any] = {}
        for key, value in self._state.items():
            default = self._field_defaults.get(key, _MISSING)
            if default is not _MISSING and value == default:
                continue
            if isinstance(value, _BM):
                result[key] = value.model_dump(mode="json")
            elif isinstance(value, bytes):
                import base64
                result[key] = base64.b64encode(value).decode()
            else:
                result[key] = value
        return result

    def _parse_json_value(self, key: str, json_val: Any) -> Any:
        """Parse a plain JSON value back to its Python type (for deserialization)."""
        tier = self._field_tiers.get(key)
        if tier == "opaque" and isinstance(json_val, str):
            import base64
            return base64.b64decode(json_val)
        adapter = self._field_adapters.get(key)
        if adapter is not None:
            return adapter.validate_python(json_val)
        return json_val
