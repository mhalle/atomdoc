"""Doc class and @node decorator."""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterator
from typing import Any

from pydantic import BaseModel
from ulid import ULID

from ._id import node_id_factory
from ._node import AtomNode, _MISSING
from ._types import (
    ChangeEvent,
    Diff,
    JsonDoc,
    LifeCycleStage,
    Operations,
)


# ---------------------------------------------------------------------------
# @node decorator
# ---------------------------------------------------------------------------

def node(cls_or_name: type | str | None = None) -> Any:
    """Decorator to create a AtomNode subclass from any class with annotations.

    Usage::

        @node
        class Annotation:
            label: str = ""

        @node("custom_type")
        class Annotation:
            label: str = ""

    The class can be a plain class or a BaseModel — Array[T] fields are
    extracted as slots, everything else becomes state fields.
    """
    if isinstance(cls_or_name, str):
        def decorator(cls: type) -> type[AtomNode]:
            return _make_node_from_class(cls, cls_or_name)
        return decorator
    elif cls_or_name is None:
        def decorator(cls: type) -> type[AtomNode]:
            return _make_node_from_class(cls, cls.__name__)
        return decorator
    else:
        return _make_node_from_class(cls_or_name, cls_or_name.__name__)


def _make_node_from_class(source_cls: type, node_type_name: str) -> type[AtomNode]:
    """Create a AtomNode subclass from any annotated class."""

    # Extract annotations and defaults
    annotations: dict[str, Any] = {}
    defaults: dict[str, Any] = {}

    for name, ann in getattr(source_cls, "__annotations__", {}).items():
        if name.startswith("_"):
            continue
        annotations[name] = ann
        if hasattr(source_cls, name):
            val = getattr(source_cls, name)
            # Skip classmethod/staticmethod/property/descriptors
            if not callable(val) or isinstance(val, type):
                defaults[name] = val

    # If it's a BaseModel, also pull from model_fields for non-Array fields
    is_pydantic = isinstance(source_cls, type) and issubclass(source_cls, BaseModel)
    if is_pydantic:
        for field_name, field_info in source_cls.model_fields.items():  # type: ignore[attr-defined]
            if field_name not in annotations:
                annotations[field_name] = field_info.annotation
            if field_info.default is not None and field_name not in defaults:
                defaults[field_name] = field_info.default

    # Build namespace for the new class.
    # __module__ and __qualname__ must be set before type() because
    # __init_subclass__ uses __module__ to resolve string annotations
    # (needed when ``from __future__ import annotations`` is active).
    ns: dict[str, Any] = {
        "__annotations__": annotations,
        "__module__": source_cls.__module__,
        "__qualname__": source_cls.__qualname__,
    }
    for name, val in defaults.items():
        ns[name] = val

    # Create the AtomNode subclass
    new_cls = type(
        source_cls.__name__,
        (AtomNode,),
        ns,
        node_type=node_type_name,
    )

    # If the source is a BaseModel, store it as the validator model.
    # This preserves @field_validator, @model_validator, Field constraints, etc.
    # At commit time, updated nodes are validated against this model.
    if is_pydantic:
        new_cls._validator_model = source_cls  # type: ignore[attr-defined]

    return new_cls


# ---------------------------------------------------------------------------
# Extension
# ---------------------------------------------------------------------------

class Extension:
    """Bundle of node types and optional normalization hooks."""

    def __init__(
        self,
        nodes: list[type[AtomNode]] | None = None,
        normalize: Callable[[Diff], None] | None = None,
    ) -> None:
        self.nodes = nodes or []
        self.normalize = normalize


# ---------------------------------------------------------------------------
# Doc
# ---------------------------------------------------------------------------

def _discover_node_types(root_cls: type[AtomNode]) -> dict[str, type[AtomNode]]:
    """Walk slot declarations to discover all reachable node types from root."""
    result: dict[str, type[AtomNode]] = {}
    pending: list[type[AtomNode]] = [root_cls]
    while pending:
        cls = pending.pop()
        if not hasattr(cls, "_node_type"):
            continue
        if cls._node_type in result:
            continue
        result[cls._node_type] = cls
        for slot_def in getattr(cls, "_slot_defs", {}).values():
            if slot_def.allowed_type is not None and slot_def.allowed_type not in result.values():
                pending.append(slot_def.allowed_type)
    return result


class Doc:
    """The document container — a rooted tree of AtomNode instances."""

    def __init__(
        self,
        root_type: type[AtomNode] | AtomNode | str,
        nodes: list[type[AtomNode]] | None = None,
        extensions: list[Extension] | None = None,
        *,
        doc_id: str | None = None,
        strict_mode: bool = True,
    ) -> None:
        # Resolve root_type: accept an instance (snapshot), a class, or a string
        root_snapshot: AtomNode | None = None
        if isinstance(root_type, AtomNode):
            # Instance passed — extract class and snapshot data
            root_snapshot = root_type
            root_cls = type(root_snapshot)
            root_type_str = root_cls._node_type
        elif isinstance(root_type, type) and hasattr(root_type, "_node_type"):
            root_cls = root_type
            root_type_str = root_cls._node_type
        else:
            root_cls = None
            root_type_str = str(root_type)

        # Collect explicit node types
        all_nodes: list[type[AtomNode]] = list(nodes or [])
        all_extensions = extensions or []
        for ext in all_extensions:
            all_nodes.extend(ext.nodes)

        self._node_types: dict[str, type[AtomNode]] = {}

        # If root class provided, auto-discover reachable types from slots
        if root_cls is not None:
            discovered = _discover_node_types(root_cls)
            self._node_types.update(discovered)

        # Register explicitly provided node types (may extend discovered set)
        for node_cls in all_nodes:
            if hasattr(node_cls, "_node_type"):
                if node_cls._node_type in self._node_types:
                    existing = self._node_types[node_cls._node_type]
                    if existing is not node_cls:
                        raise ValueError(f"Duplicate node type: '{node_cls._node_type}'")
                else:
                    self._node_types[node_cls._node_type] = node_cls
                    for discovered_cls in _discover_node_types(node_cls).values():
                        if discovered_cls._node_type not in self._node_types:
                            self._node_types[discovered_cls._node_type] = discovered_cls

        self._root_type = root_type_str
        if root_type_str not in self._node_types:
            new_root_cls = _make_root_class(root_type_str)
            self._node_types[root_type_str] = new_root_cls

        if doc_id is not None:
            self._id = doc_id
        else:
            self._id = str(ULID()).lower()

        self._node_map: dict[str, AtomNode] = {}
        self._strict_mode = strict_mode
        self._lifecycle_stage: LifeCycleStage = "idle"
        self._operations: Operations = ([], {})
        self._inverse_operations: Operations = ([], {})
        self._diff = Diff()
        self._change_listeners: list[Callable[[ChangeEvent], None]] = []
        self._normalize_listeners: list[Callable[[Diff], None]] = []

        # Create root node
        root_node_cls = self._node_types[root_type_str]
        self._root = root_node_cls(_id=self._id, _doc=self)
        self._node_map[self._id] = self._root

        self._id_gen = node_id_factory(self)

        # If a snapshot was provided, populate the tree from it
        if root_snapshot is not None:
            self._apply_snapshot(self._root, root_snapshot)

        self._lifecycle_stage = "init"
        for ext in all_extensions:
            if ext.normalize is not None:
                self._normalize_listeners.append(ext.normalize)
        self._lifecycle_stage = "idle"

    def _apply_snapshot(self, live_node: AtomNode, snapshot: AtomNode) -> None:
        """Populate a live node tree from a snapshot (user-constructed node)."""
        # Copy state
        for key, value in snapshot._state.items():
            live_node._state[key] = value

        # Process slot children from snapshot
        snapshot_slots = getattr(snapshot, "_snapshot", None)
        if not snapshot_slots:
            return

        for slot_name, children in snapshot_slots.items():
            if slot_name not in live_node._slot_first:
                continue
            for child_snapshot in children:
                child_cls = type(child_snapshot)
                child_id = self._id_gen()
                child = child_cls(_id=child_id, _doc=self)
                # Copy state
                for key, value in child_snapshot._state.items():
                    child._state[key] = value
                # Apply defaults not in state
                for name, default in child_cls._field_defaults.items():
                    if default is not _MISSING and name not in child._state:
                        child._state[name] = default
                # Link into tree
                prev = live_node._slot_last.get(slot_name)
                child._parent = live_node
                child._slot_name = slot_name
                child._prev_sibling = prev
                if prev is not None:
                    prev._next_sibling = child
                else:
                    live_node._slot_first[slot_name] = child
                live_node._slot_last[slot_name] = child
                self._node_map[child.id] = child

                # Recurse into child's slots
                self._apply_snapshot(child, child_snapshot)

    @property
    def root(self) -> AtomNode:
        return self._root

    @property
    def id(self) -> str:
        return self._id

    def get_node_by_id(self, node_id: str) -> AtomNode | None:
        return self._node_map.get(node_id)

    # --- Tree navigation ---

    def parent(self, node: AtomNode) -> AtomNode | None:
        """Parent of this node, or None for root."""
        return node._parent

    def next_sibling(self, node: AtomNode) -> AtomNode | None:
        """Next sibling within the same slot."""
        return node._next_sibling

    def prev_sibling(self, node: AtomNode) -> AtomNode | None:
        """Previous sibling within the same slot."""
        return node._prev_sibling

    def ancestors(self, node: AtomNode) -> Iterator[AtomNode]:
        """Walk up from node to root (excludes node)."""
        current = node._parent
        while current is not None:
            yield current
            current = current._parent

    def descendants(self, node: AtomNode) -> Iterator[AtomNode]:
        """Depth-first traversal of all descendants across all slots (excludes node)."""
        for slot_name in node._slot_order:
            child = node._slot_first.get(slot_name)
            while child is not None:
                yield child
                yield from self.descendants(child)
                child = child._next_sibling

    def next_siblings(self, node: AtomNode) -> Iterator[AtomNode]:
        """Forward siblings after node (within same slot)."""
        current = node._next_sibling
        while current is not None:
            yield current
            current = current._next_sibling

    def prev_siblings(self, node: AtomNode) -> Iterator[AtomNode]:
        """Backward siblings before node (within same slot)."""
        current = node._prev_sibling
        while current is not None:
            yield current
            current = current._prev_sibling

    # --- Node creation ---

    def create_node(self, node_cls: type[AtomNode], **state: Any) -> AtomNode:
        if not hasattr(node_cls, "_node_type"):
            raise TypeError(f"{node_cls} is not a valid AtomNode subclass")
        if node_cls._node_type not in self._node_types:
            raise ValueError(
                f"Node type '{node_cls._node_type}' is not registered"
            )
        node_id = self._id_gen()
        node = node_cls(_id=node_id, _doc=self)
        for name, default in node_cls._field_defaults.items():
            if default is not _MISSING and name not in state:
                node._state[name] = default
        for name, value in state.items():
            adapter = node_cls._field_adapters.get(name)
            if adapter is not None:
                node._state[name] = adapter.validate_python(value)
            else:
                node._state[name] = value
        return node

    # --- Central write path ---

    def _set_node_state(self, node: AtomNode, key: str, value: Any) -> None:
        from . import _operations as ops
        from ._transaction import with_transaction

        def _do() -> None:
            current = node._state.get(key, node._field_defaults.get(key, _MISSING))
            if current is value or current == value:
                return
            is_attached = node.id in self._node_map
            if is_attached:
                ops.on_set_state_inverse(self, node, key)
            node._state[key] = value
            if is_attached:
                ops.on_set_state_forward(self, node, key)

        with_transaction(self, _do)

    # --- Slot-aware tree manipulation ---

    def _insert_into_slot(
        self,
        parent: AtomNode,
        slot_name: str,
        position: str,
        nodes: list[AtomNode],
        target: AtomNode | None = None,
    ) -> None:
        """Insert nodes into a specific slot of parent."""
        if not nodes:
            return
        from . import _operations as ops
        from ._transaction import with_transaction

        def _do() -> None:
            # Validate slot exists
            if slot_name not in parent._slot_first:
                raise ValueError(
                    f"Slot '{slot_name}' does not exist on {type(parent).__name__}"
                )

            # Validate nodes
            for top_node in nodes:
                for desc in _descendants_inclusive_iter(top_node):
                    if desc._doc_ref is not self:
                        raise RuntimeError("Node is from a different document")
                    if desc.id in self._node_map:
                        raise RuntimeError(
                            f"Node '{desc.id}' already exists in the document"
                        )

            # Handle position redirects
            if position == "prepend":
                first = parent._slot_first.get(slot_name)
                if first is not None:
                    self._insert_into_slot(parent, slot_name, "before", nodes, target=first)
                else:
                    self._insert_into_slot(parent, slot_name, "append", nodes)
                return

            if position == "after" and target is not None:
                nxt = target._next_sibling
                if nxt is not None:
                    self._insert_into_slot(parent, slot_name, "before", nodes, target=nxt)
                else:
                    self._insert_into_slot(parent, slot_name, "append", nodes)
                return

            # Record ops
            if position == "append":
                if parent.id in self._node_map:
                    ops.on_insert_range(self, parent, slot_name, "append", nodes)
            elif position == "before" and target is not None:
                ops.on_insert_range_before(self, target, slot_name, nodes)

            # Perform tree linking
            if position == "append":
                current = parent._slot_last.get(slot_name)
                for nd in nodes:
                    self._attach_node(nd, parent=parent, slot_name=slot_name, prev=current)
                    if current is not None:
                        current._next_sibling = nd
                    else:
                        parent._slot_first[slot_name] = nd
                    current = nd
                parent._slot_last[slot_name] = current

            elif position == "before" and target is not None:
                current_target = target
                for i in range(len(nodes) - 1, -1, -1):
                    nd = nodes[i]
                    prev_of_target = current_target._prev_sibling
                    self._attach_node(
                        nd, parent=parent, slot_name=slot_name,
                        prev=prev_of_target, next_=current_target,
                    )
                    if prev_of_target is not None:
                        prev_of_target._next_sibling = nd
                    current_target._prev_sibling = nd
                    current_target = nd
                if parent._slot_first.get(slot_name) is target:
                    parent._slot_first[slot_name] = nodes[0]

        with_transaction(self, _do)

    def _attach_node(
        self,
        node: AtomNode,
        parent: AtomNode,
        slot_name: str,
        prev: AtomNode | None = None,
        next_: AtomNode | None = None,
    ) -> None:
        node._parent = parent
        node._slot_name = slot_name
        node._prev_sibling = prev
        node._next_sibling = next_
        if parent.id in self._node_map:
            for desc in _descendants_inclusive_iter(node):
                self._node_map[desc.id] = desc

    # --- Transaction API ---

    def transaction(self) -> Generator[None, None, None]:
        from ._transaction import transaction_context
        return transaction_context(self)  # type: ignore[return-value]

    def force_commit(self) -> None:
        from . import _operations as ops

        if self._lifecycle_stage == "change":
            raise RuntimeError("Cannot trigger an update inside a change event")

        # Validate updated/inserted nodes against their validator model
        self._validate_changed_nodes()

        self._inverse_operations[0].reverse()
        self._lifecycle_stage = "idle"
        ops.maybe_trigger_listeners(self)

        self._operations = ([], {})
        self._inverse_operations = ([], {})
        self._diff = Diff()
        self._lifecycle_stage = "idle"

    def _validate_changed_nodes(self) -> None:
        """Run Pydantic model validation on nodes changed in this transaction."""
        from ._array import get_array_element_type

        for node_id in self._diff.updated | self._diff.inserted:
            node = self._node_map.get(node_id)
            if node is None:
                continue
            validator = getattr(type(node), "_validator_model", None)
            if validator is None:
                continue

            # Build a data dict for the validator model.
            # State fields get their current values; Array fields get empty lists.
            data: dict[str, Any] = {}
            for name, default in type(node)._field_defaults.items():
                if default is not _MISSING:
                    data[name] = node._state.get(name, default)
                elif name in node._state:
                    data[name] = node._state[name]

            # Fill Array fields with empty lists so the model doesn't complain
            for name in getattr(validator, "__annotations__", {}):
                ann = validator.__annotations__[name]
                if get_array_element_type(ann) is not None and name not in data:
                    data[name] = []

            validator.model_validate(data)

    def abort(self) -> None:
        from . import _operations as ops

        inverse: Operations = (
            list(self._inverse_operations[0]),
            dict(self._inverse_operations[1]),
        )
        ops.on_apply_operations(self, inverse)
        self._operations = ([], {})
        self._inverse_operations = ([], {})
        self._diff = Diff()
        self._lifecycle_stage = "idle"

    # --- Listeners ---

    def on_change(self, callback: Callable[[ChangeEvent], None]) -> Callable[[], None]:
        if self._lifecycle_stage not in ("idle", "init", "update"):
            raise RuntimeError(
                f"Cannot register a change listener during '{self._lifecycle_stage}' stage"
            )
        self._change_listeners.append(callback)

        def unsub() -> None:
            try:
                self._change_listeners.remove(callback)
            except ValueError:
                pass

        return unsub

    def on_normalize(self, callback: Callable[[Diff], None]) -> None:
        if self._lifecycle_stage != "init":
            raise RuntimeError(
                "on_normalize can only be called during extension registration"
            )
        self._normalize_listeners.append(callback)

    def apply_operations(
        self,
        operations: Operations | list[Operations],
        *,
        limit: int | None = None,
    ) -> list[Operations]:
        """Apply operations. Returns any unapplied operations.

        ``operations`` can be a single Operations tuple or a list of them
        (a journal). ``limit`` controls how many entries to apply from a
        journal. Returns the remaining unapplied entries (empty list if all
        applied).
        """
        from . import _operations as ops
        from ._transaction import with_transaction

        # Normalize to a journal (list of Operations)
        if isinstance(operations, tuple) and len(operations) == 2 and isinstance(operations[0], list):
            # Single Operations tuple
            journal: list[Operations] = [operations]  # type: ignore[list-item]
        else:
            journal = list(operations)  # type: ignore[arg-type]

        if limit is not None:
            to_apply = journal[:limit]
            remaining = journal[limit:]
        else:
            to_apply = journal
            remaining = []

        for single_ops in to_apply:
            def _do(op: Operations = single_ops) -> None:
                if not op[0] and not op[1]:
                    return
                ops.on_apply_operations(self, op)
            with_transaction(self, _do, is_apply_operations=True)

        return remaining

    def dispose(self) -> None:
        if self._lifecycle_stage != "idle":
            raise RuntimeError(
                f"Cannot dispose during '{self._lifecycle_stage}' stage"
            )
        self._change_listeners.clear()
        self._normalize_listeners.clear()
        self._lifecycle_stage = "disposed"

    # --- Clean JSON (user-facing, no IDs) ---

    def to_json(
        self, node: AtomNode | None = None, *, include_defaults: bool = False,
    ) -> dict[str, Any]:
        """Return clean JSON for a node (default: root). No internal IDs.

        If ``include_defaults`` is True, fields with default values are
        included in the output.
        """
        if self._lifecycle_stage not in ("idle", "change"):
            raise RuntimeError("Cannot serialize during an active transaction")
        target = node if node is not None else self._root
        return _node_to_data(target, include_defaults=include_defaults)

    # --- Wire format (dump/restore, has IDs) ---

    def dump(self, *, include_defaults: bool = False) -> JsonDoc:
        """Serialize to wire format (with IDs) for persistence and sync.

        If ``include_defaults`` is True, fields with default values are
        included in the output.
        """
        if self._lifecycle_stage not in ("idle", "change"):
            raise RuntimeError("Cannot serialize during an active transaction")
        return _node_to_wire(self._root, include_defaults=include_defaults)

    @classmethod
    def restore(
        cls,
        data: JsonDoc,
        root_type: type[AtomNode] | None = None,
        nodes: list[type[AtomNode]] | None = None,
        extensions: list[Extension] | None = None,
        strict_mode: bool = True,
    ) -> Doc:
        """Restore a document from wire format (dump output)."""
        doc_id = data[0]
        root_type_str = data[1]

        effective_root: type[AtomNode] | str = root_type if root_type is not None else root_type_str

        doc = cls(
            root_type=effective_root,
            nodes=nodes,
            extensions=extensions,
            doc_id=doc_id,
            strict_mode=strict_mode,
        )

        root = doc._create_node_from_json(data)
        doc._node_map.pop(doc._root.id, None)
        doc._root = root
        doc._node_map[root.id] = root

        if len(data) > 3 and data[3]:
            _deserialize_slots(doc, root, data[3])

        return doc

    def _create_node_from_json(self, json_node: JsonDoc) -> AtomNode:
        node_id = json_node[0]
        node_type = json_node[1]
        state_dict = json_node[2] if len(json_node) > 2 else {}

        node_cls = self._node_types.get(node_type)
        if node_cls is None:
            raise ValueError(f"Unknown node type: '{node_type}'")

        node = node_cls(_id=node_id, _doc=self)

        for name, default in node_cls._field_defaults.items():
            if default is not _MISSING:
                node._state[name] = default

        for key, json_val in state_dict.items():
            node._state[key] = node._parse_json_value(key, json_val)

        return node

    @staticmethod
    def json_schema(nodes: list[type[AtomNode]]) -> dict[str, Any]:
        schemas: dict[str, Any] = {}
        for node_cls in nodes:
            if hasattr(node_cls, "_schema_model") and node_cls._schema_model is not None:
                schemas[node_cls._node_type] = node_cls._schema_model.model_json_schema()
        return schemas

    def atomdoc_schema(self) -> dict[str, Any]:
        """Export JSON Schema with x-atomdoc extensions.

        Returns a schema document describing all node types and frozen
        value types, suitable for bootstrapping language-agnostic clients.
        """
        from ._tier import _is_frozen_model

        node_types: dict[str, Any] = {}
        value_types: dict[str, Any] = {}

        for type_name, node_cls in self._node_types.items():
            entry: dict[str, Any] = {}

            # JSON Schema from field adapters (avoids _schema_model rebuild issues)
            properties: dict[str, Any] = {}
            for fname, adapter in node_cls._field_adapters.items():
                try:
                    properties[fname] = adapter.json_schema()
                except Exception:
                    properties[fname] = {}
            entry["json_schema"] = {"type": "object", "properties": properties}

            # Field tiers
            entry["field_tiers"] = dict(node_cls._field_tiers)

            # Slots
            slots: dict[str, Any] = {}
            for slot_name, slot_def in node_cls._slot_defs.items():
                allowed = slot_def.allowed_type
                if allowed is None:
                    allowed_name = None
                elif isinstance(allowed, str):
                    allowed_name = allowed
                else:
                    allowed_name = allowed._node_type
                slots[slot_name] = {"allowed_type": allowed_name}
            entry["slots"] = slots

            # Field defaults (JSON-safe)
            defaults: dict[str, Any] = {}
            for fname, fdefault in node_cls._field_defaults.items():
                if fdefault is _MISSING:
                    continue
                if isinstance(fdefault, BaseModel):
                    defaults[fname] = fdefault.model_dump(mode="json")
                else:
                    defaults[fname] = fdefault
            entry["field_defaults"] = defaults

            node_types[type_name] = entry

            # Discover frozen value types from field tiers and adapters
            for fname, tier in node_cls._field_tiers.items():
                if tier == "atomic":
                    # Get the actual type from the adapter's core schema
                    adapter = node_cls._field_adapters.get(fname)
                    if adapter is None:
                        continue
                    # Walk defaults to find the type
                    default_val = node_cls._field_defaults.get(fname, _MISSING)
                    if default_val is not _MISSING and isinstance(default_val, BaseModel):
                        vtype = type(default_val)
                        if _is_frozen_model(vtype) and vtype.__name__ not in value_types:
                            value_types[vtype.__name__] = {
                                "json_schema": vtype.model_json_schema(),
                                "frozen": True,
                            }

        return {
            "version": 1,
            "root_type": self._root_type,
            "node_types": node_types,
            "value_types": value_types,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node_to_wire(node: AtomNode, include_defaults: bool = False) -> JsonDoc:
    """Serialize a node to wire format (with IDs)."""
    state = node._state_to_json_plain(include_defaults=include_defaults)
    result: JsonDoc = [node.id, node._node_type, state]

    if node._slot_order:
        slots_dict: dict[str, list[JsonDoc]] = {}
        for slot_name in node._slot_order:
            children: list[JsonDoc] = []
            child: AtomNode | None = node._slot_first.get(slot_name)
            while child is not None:
                children.append(_node_to_wire(child, include_defaults=include_defaults))
                child = child._next_sibling
            slots_dict[slot_name] = children
        result.append(slots_dict)

    return result


def _node_to_data(node: AtomNode, include_defaults: bool = False) -> dict[str, Any]:
    """Serialize a node to clean JSON (no IDs, just data)."""
    result: dict[str, Any] = {}

    # State fields
    for key, value in node._state.items():
        if not include_defaults:
            default = node._field_defaults.get(key, _MISSING)
            if default is not _MISSING and value == default:
                continue
        from pydantic import BaseModel as _BM
        if isinstance(value, _BM):
            result[key] = value.model_dump(mode="json")
        elif isinstance(value, bytes):
            import base64
            result[key] = base64.b64encode(value).decode()
        else:
            result[key] = value

    # Slots
    for slot_name in node._slot_order:
        children: list[dict[str, Any]] = []
        child: AtomNode | None = node._slot_first.get(slot_name)
        while child is not None:
            children.append(_node_to_data(child, include_defaults=include_defaults))
            child = child._next_sibling
        result[slot_name] = children

    return result


def _deserialize_slots(doc: Doc, parent: AtomNode, slots_data: dict[str, list[JsonDoc]]) -> None:
    """Recursively deserialize slot children."""
    for slot_name, children_data in slots_data.items():
        if slot_name not in parent._slot_first:
            continue  # skip unknown slots

        prev: AtomNode | None = None
        for child_json in children_data:
            child = doc._create_node_from_json(child_json)
            child._parent = parent
            child._slot_name = slot_name
            child._prev_sibling = prev
            if prev is not None:
                prev._next_sibling = child
            else:
                parent._slot_first[slot_name] = child
            doc._node_map[child.id] = child
            prev = child

        if prev is not None:
            parent._slot_last[slot_name] = prev

        # Recurse into children's slots
        for i, child_json in enumerate(children_data):
            if len(child_json) > 3 and child_json[3]:
                child_node = doc.get_node_by_id(child_json[0])
                if child_node is not None:
                    _deserialize_slots(doc, child_node, child_json[3])


def _descendants_inclusive_iter(node: AtomNode):  # type: ignore[no-untyped-def]
    """Yield node and all its descendants."""
    yield node
    for slot_name in node._slot_order:
        child = node._slot_first.get(slot_name)
        while child is not None:
            yield from _descendants_inclusive_iter(child)
            child = child._next_sibling


def _make_root_class(root_type: str) -> type[AtomNode]:
    ns: dict[str, Any] = {}
    cls = type(
        f"_Root_{root_type}",
        (AtomNode,),
        ns,
        node_type=root_type,
    )
    return cls
