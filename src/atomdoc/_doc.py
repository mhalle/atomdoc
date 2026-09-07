"""Doc class and @node decorator."""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Annotated, Any, ForwardRef

from pydantic import BaseModel, TypeAdapter
from pydantic_core import PydanticUndefined, to_jsonable_python

from ._id import (
    CompactIdFactory,
    NodeIdGenerator,
    default_node_id_generator,
    node_id_factory,
    session_prefix,
)
from ._handle import Handle, is_handle_type
from ._node import AtomNode, _MISSING
from ._ref import RefIntegrityError, ref_ids
from ._tier import frozen_models_in
from ._types import (
    ChangeEvent,
    Diff,
    JsonDoc,
    LifeCycleStage,
    Operations,
    TransactionFlags,
)
from ._undo import UndoManager, UndoManagerConfig


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


def _is_resolved_annotation(field_info: Any) -> bool:
    """True if Pydantic resolved this field's annotation to a real type."""
    ann = field_info.annotation
    return (
        ann is not None
        and not isinstance(ann, (str, ForwardRef))
        and not field_info.metadata
    )


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
            elif isinstance(annotations[field_name], str) and _is_resolved_annotation(field_info):
                # Under ``from __future__ import annotations`` the raw
                # annotation is a string.  Pydantic has already resolved it
                # (it can see function-local names through the defining
                # frame, which __init_subclass__ cannot), so prefer its
                # result.  Skipped when the field carries constraint
                # metadata, because Pydantic strips ``Annotated[...]`` into
                # ``field_info.metadata`` and the per-field adapter would
                # lose those constraints.
                annotations[field_name] = field_info.annotation
            if field_name in defaults:
                continue
            if field_info.metadata or field_info.default_factory is not None:
                # Constraints (``Field(ge=...)``, ``Annotated[...]``) or a
                # ``default_factory``: hand the FieldInfo through so the
                # node keeps them.
                defaults[field_name] = field_info
            elif field_info.default is not PydanticUndefined:
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

    # A self-reference (``Ref["Volume"]`` inside ``class Volume(BaseModel)``)
    # resolves to the source class; point it at the node class instead.
    for ref_def in new_cls._ref_defs.values():
        if ref_def.target is source_cls:
            ref_def.target = new_cls

    return new_cls


# ---------------------------------------------------------------------------
# Extension
# ---------------------------------------------------------------------------

class Extension:
    """Bundle of node types and optional registration / normalization hooks.

    ``register`` runs during document construction (the ``init`` stage). It
    may call ``doc.on_normalize`` and may mutate the document; those
    mutations are committed, and normalizers run, before the constructor
    returns. ``normalize`` is a shorthand for registering one normalizer.
    """

    def __init__(
        self,
        nodes: list[type[AtomNode]] | None = None,
        normalize: Callable[[Diff], None] | None = None,
        register: Callable[[Doc], None] | None = None,
    ) -> None:
        self.nodes = nodes or []
        self.normalize = normalize
        self.register = register


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
        for ref_def in getattr(cls, "_ref_defs", {}).values():
            target = ref_def.target
            if isinstance(target, type) and hasattr(target, "_node_type") and target not in result.values():
                pending.append(target)  # type: ignore[arg-type]
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
        undo_manager: UndoManagerConfig | None = None,
        node_id_generator: NodeIdGenerator | None = None,
        _defer_init: bool = False,
    ) -> None:
        """Create a document.

        ``undo_manager`` configures the built-in undo manager (disabled by
        default). ``node_id_generator`` overrides how document and node IDs
        are generated and validated (default: lowercase ULIDs).
        """
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

        self._node_id_generator = node_id_generator or default_node_id_generator()
        if doc_id is not None:
            if not self._node_id_generator.validate(doc_id):
                raise ValueError(f"Invalid document id: {doc_id!r}")
            self._id = doc_id
        else:
            self._id = self._node_id_generator.generate()

        self._node_map: dict[str, AtomNode] = {}
        # Reverse reference index: target id -> {(referrer id, field): None}.
        # Derived state — never serialized, rebuilt on restore.
        self._ref_index: dict[str, dict[tuple[str, str], None]] = {}
        self._strict_mode = strict_mode
        self._lifecycle_stage: LifeCycleStage = "idle"
        self._operations: Operations = ([], {})
        self._inverse_operations: Operations = ([], {})
        self._transaction_flags = TransactionFlags()
        self._diff = Diff()
        self._change_listeners: list[Callable[[ChangeEvent], None]] = []
        self._normalize_listeners: list[Callable[[Diff], None]] = []
        self._undo_config = undo_manager or UndoManagerConfig()
        self._undo_manager: UndoManager | None = None

        # Create root node
        root_node_cls = self._node_types[root_type_str]
        self._root = root_node_cls(_id=self._id, _doc=self)
        self._node_map[self._id] = self._root

        extract_time = self._node_id_generator.extract_time
        self._id_gen: Callable[[], str] = (
            node_id_factory(self, extract_time)
            if extract_time is not None
            else self._node_id_generator.generate
        )

        # If a snapshot was provided, populate the tree from it
        if root_snapshot is not None:
            self._apply_snapshot(self._root, root_snapshot)

        self._lifecycle_stage = "init"
        for ext in all_extensions:
            if ext.normalize is not None:
                self._normalize_listeners.append(ext.normalize)
            if ext.register is not None:
                ext.register(self)
        self._lifecycle_stage = "idle"

        if not _defer_init:
            self._finish_init()

    def _finish_init(self) -> None:
        """Commit init-stage mutations, run normalizers, and attach undo.

        Normalizers run even when nothing changed so extensions can
        establish invariants (e.g. a default child) on a fresh document.
        Nothing done here enters undo history.
        """
        self._lifecycle_stage = "idle"
        self._rebuild_ref_index()
        self._check_dangling_refs()
        self._force_commit(ignore_empty_diff=True)
        self._undo_manager = UndoManager(
            self,
            max_steps=self._undo_config.max_steps,
            merge_interval=self._undo_config.merge_interval,
        )

    @property
    def undo_manager(self) -> UndoManager:
        """The document's built-in undo manager (see ``UndoManagerConfig``)."""
        if self._undo_manager is None:
            raise RuntimeError("Document initialization has not finished")
        return self._undo_manager

    @property
    def node_id_generator(self) -> NodeIdGenerator:
        return self._node_id_generator

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
                child_cls._apply_defaults(child._state)
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
        for name in node_cls._field_defaults:
            if name not in state:
                default = node_cls._fresh_default(name)
                if default is not _MISSING:
                    node._state[name] = default
        for name, value in state.items():
            adapter = node_cls._field_adapters.get(name)
            if name in node_cls._ref_defs:
                node._state[name] = adapter.validate_python(value, doc=self)  # type: ignore[union-attr]
            elif adapter is not None:
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
                if key in node._ref_defs:
                    self._refs_update(node, key, current, value)

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

            # Validate nodes (also against each other: the same node or ID
            # twice in one batch would link a node to itself)
            seen: set[str] = set()
            for top_node in nodes:
                for desc in _descendants_inclusive_iter(top_node):
                    if desc._doc_ref is not self:
                        raise RuntimeError("Node is from a different document")
                    if desc.id in self._node_map or desc.id in seen:
                        raise RuntimeError(
                            f"Node '{desc.id}' already exists in the document"
                        )
                    seen.add(desc.id)

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
                self._refs_add(desc)

    # --- Transaction API ---

    def transaction(self, *, skip_undo: bool = False) -> AbstractContextManager[None]:
        """Batch mutations into one change event.

        ``skip_undo`` marks the transaction so the undo manager ignores it.
        Such a transaction is always isolated: if one is already open it is
        committed first, so the caller's own edits stay undoable and only the
        flagged work is excluded. Edits made after the block exits start a
        new transaction.
        """
        from ._transaction import transaction_context

        flags = TransactionFlags(skip_undo=True) if skip_undo else None
        return transaction_context(self, flags)

    def force_commit(self) -> None:
        """Commit the current transaction synchronously, firing events."""
        self._force_commit()

    def _force_commit(self, ignore_empty_diff: bool = False) -> None:
        from . import _operations as ops

        if self._lifecycle_stage == "change":
            raise RuntimeError("Cannot trigger an update inside a change event")

        # Validation of inserted/updated nodes happens in
        # maybe_trigger_listeners, after normalizers have run, so nodes a
        # normalizer inserts or edits are validated too.
        self._lifecycle_stage = "idle"
        try:
            ops.maybe_trigger_listeners(self, ignore_empty_diff)
        except Exception:
            # Validation or a listener failed. Reopen the transaction and
            # leave the recorded operations in place so the caller
            # (with_transaction / transaction_context) can roll back.
            self._lifecycle_stage = "update"
            raise
        self._operations = ([], {})
        self._inverse_operations = ([], {})
        self._transaction_flags = TransactionFlags()
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

    # --- References ---

    def referrers(self, node: AtomNode, *, field: str | None = None) -> list[AtomNode]:
        """Nodes holding a reference to ``node``, optionally only via ``field``.

        Backed by the document's reverse index: O(1) per referrer.
        """
        result: list[AtomNode] = []
        seen: set[str] = set()
        for referrer_id, field_name in self._ref_index.get(node.id, {}):
            if field is not None and field_name != field:
                continue
            if referrer_id in seen:
                continue
            referrer = self._node_map.get(referrer_id)
            if referrer is not None:
                seen.add(referrer_id)
                result.append(referrer)
        return result

    # --- Handles ---

    def handles(
        self, *, strength: str | None = None
    ) -> list[tuple[AtomNode, str, Handle]]:
        """Every handle held by the document as ``(node, field, handle)``.

        With ``strength="strong"`` this is the document's hard dependency
        list: what must resolve for the document to be usable. Nothing is
        resolved or fetched; the handles are read off the tree.
        """
        result: list[tuple[AtomNode, str, Handle]] = []

        def walk(node: AtomNode, name: str, value: Any) -> None:
            if isinstance(value, Handle):
                if strength is None or value.strength == strength:
                    result.append((node, name, value))
            elif isinstance(value, (list, tuple, set)):
                for item in value:
                    walk(node, name, item)
            elif isinstance(value, dict):
                for item in value.values():
                    walk(node, name, item)

        for node in self._node_map.values():
            for name, value in node._state.items():
                walk(node, name, value)
        return result

    def _refs_add(self, node: AtomNode) -> None:
        for name in type(node)._ref_defs:
            for target_id in ref_ids(node, name):
                self._ref_index.setdefault(target_id, {})[(node.id, name)] = None

    def _refs_remove(self, node: AtomNode) -> None:
        for name in type(node)._ref_defs:
            for target_id in ref_ids(node, name):
                self._refs_discard(target_id, node.id, name)

    def _refs_discard(self, target_id: str, referrer_id: str, name: str) -> None:
        entries = self._ref_index.get(target_id)
        if entries is None:
            return
        entries.pop((referrer_id, name), None)
        if not entries:
            del self._ref_index[target_id]

    def _refs_update(self, node: AtomNode, name: str, old: Any, new: Any) -> None:
        """Re-index reference field ``name`` of an attached node."""
        old_ids = old if isinstance(old, list) else ([old] if isinstance(old, str) else [])
        new_ids = new if isinstance(new, list) else ([new] if isinstance(new, str) else [])
        for target_id in old_ids:
            if target_id not in new_ids:
                self._refs_discard(target_id, node.id, name)
        for target_id in new_ids:
            if isinstance(target_id, str):
                self._ref_index.setdefault(target_id, {})[(node.id, name)] = None

    def _rebuild_ref_index(self) -> None:
        self._ref_index.clear()
        for node in self._node_map.values():
            self._refs_add(node)

    def _check_dangling_refs(self) -> None:
        """Raise (strict) or warn on references that do not resolve.

        Runs when a document is built from a snapshot or restored from a
        dump: those paths write the tree directly and never go through the
        commit-time check.
        """
        dangling = self._dangling_refs()
        if not dangling:
            return
        referrer, name, target_id = dangling[0]
        message = (
            f"{len(dangling)} unresolved reference(s) in document, "
            f"e.g. {type(referrer).__name__}.{name} on node "
            f"'{referrer.id}' -> '{target_id}'"
        )
        if self._strict_mode:
            raise RefIntegrityError(message)
        warnings.warn(message, stacklevel=3)

    def _dangling_refs(self) -> list[tuple[AtomNode, str, str]]:
        """(referrer, field, target id) for every reference that does not resolve."""
        result: list[tuple[AtomNode, str, str]] = []
        for target_id, entries in self._ref_index.items():
            if target_id in self._node_map:
                continue
            for referrer_id, name in entries:
                referrer = self._node_map.get(referrer_id)
                if referrer is not None:
                    result.append((referrer, name, target_id))
        return result

    def _check_ref_integrity(self) -> None:
        """Commit-time referential integrity for this transaction.

        Every reference written by an inserted or updated node must resolve
        to a node of the declared type, and no deleted node may still be
        referenced by a live one (delete policy ``restrict``). Raises
        ``RefIntegrityError``; the transaction is then rolled back.
        """
        diff = self._diff
        for node_id in diff.inserted | diff.updated:
            node = self._node_map.get(node_id)
            if node is None:
                continue
            for name, rdef in type(node)._ref_defs.items():
                for target_id in ref_ids(node, name):
                    target = self._node_map.get(target_id)
                    if target is None:
                        raise RefIntegrityError(
                            f"{type(node).__name__}.{name} on node '{node_id}' "
                            f"references '{target_id}', which is not in the document"
                        )
                    if not rdef.accepts(type(target), self._node_types):
                        raise RefIntegrityError(
                            f"{type(node).__name__}.{name} on node '{node_id}' "
                            f"references {type(target).__name__} '{target_id}', "
                            f"expected {rdef.target_name}"
                        )
        for deleted_id in diff.deleted:
            for referrer_id, name in self._ref_index.get(deleted_id, {}):
                referrer = self._node_map.get(referrer_id)
                if referrer is not None:
                    raise RefIntegrityError(
                        f"Cannot delete node '{deleted_id}': still referenced by "
                        f"{type(referrer).__name__}.{name} on node '{referrer_id}'"
                    )

    def _reseed_id_factory(self) -> None:
        """Re-mint the ID session if it collides with one already in the tree.

        ``restore`` mints the session before the nodes are loaded. With every
        existing ID in hand this is a deterministic check, not a probability.
        """
        gen = self._id_gen
        if not isinstance(gen, CompactIdFactory):
            return
        existing = {p for p in map(session_prefix, self._node_map) if p is not None}
        if gen.session_id in existing:
            self._id_gen = node_id_factory(
                self, self._node_id_generator.extract_time, existing
            )

    def abort(self) -> None:
        from . import _operations as ops

        # Inverse ops are recorded in forward order; roll back in reverse.
        inverse: Operations = (
            list(reversed(self._inverse_operations[0])),
            dict(self._inverse_operations[1]),
        )
        try:
            ops.on_apply_operations(self, inverse)
        finally:
            # Whatever happens, the document must not stay in the update
            # stage: a wedged document can never commit or dump again.
            self._operations = ([], {})
            self._inverse_operations = ([], {})
            self._transaction_flags = TransactionFlags()
            self._diff = Diff()
            self._lifecycle_stage = "idle"
            self._rebuild_ref_index()

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
        skip_undo: bool = False,
        strict: bool = False,
        raise_on_error: bool | None = None,
    ) -> list[Operations]:
        """Apply operations. Returns any unapplied operations.

        ``operations`` can be a single Operations tuple or a list of them
        (a journal). ``limit`` controls how many entries to apply from a
        journal. Returns the remaining unapplied entries (empty list if all
        applied).

        ``skip_undo`` applies the operations in their own transaction(s)
        flagged so the undo manager ignores them — use it for operations
        received from a remote peer. Any open transaction is committed first.

        By default a failing entry is rolled back and silently skipped
        (best effort, for undo and journals). With ``raise_on_error`` the
        failure propagates after the rollback. With ``strict`` an operation
        whose target is missing is itself a failure, and ``raise_on_error``
        defaults to True — what an authoritative server wants.
        """
        if raise_on_error is None:
            raise_on_error = strict
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

        flags = TransactionFlags(skip_undo=True) if skip_undo else None

        for single_ops in to_apply:
            def _do(op: Operations = single_ops) -> None:
                if not op[0] and not op[1]:
                    return
                ops.on_apply_operations(self, op, strict=strict)
            with_transaction(
                self, _do, is_apply_operations=not raise_on_error, flags=flags
            )

        if skip_undo and self._lifecycle_stage == "update":
            self.force_commit()

        return remaining

    def dispose(self) -> None:
        if self._lifecycle_stage != "idle":
            raise RuntimeError(
                f"Cannot dispose during '{self._lifecycle_stage}' stage"
            )
        self._change_listeners.clear()
        self._normalize_listeners.clear()
        self._ref_index.clear()
        self._lifecycle_stage = "disposed"

    # --- Clean JSON (user-facing, no IDs) ---

    def to_json(
        self, node: AtomNode | None = None, *, include_defaults: bool = False,
    ) -> dict[str, Any]:
        """Return clean JSON for a node (default: root).

        Node IDs are omitted; the tree is nested data. The one exception is
        a ``Ref[T]`` field, whose value *is* a node ID and is emitted as
        such. For a format that round-trips, use ``dump()``.

        If ``include_defaults`` is True, fields with default values are
        included in the output.
        """
        if self._lifecycle_stage not in ("idle", "change"):
            raise RuntimeError("Cannot serialize during an active transaction")
        target = node if node is not None else self._root
        return _node_to_data(target, include_defaults=include_defaults)

    # --- Wire format (dump/restore, has IDs) ---

    def dump(
        self, node: AtomNode | None = None, *, include_defaults: bool = False
    ) -> JsonDoc:
        """Serialize to wire format (with IDs) for persistence and sync.

        With ``node``, serialize that subtree only — a fragment another
        document can take in with ``adopt()``. If ``include_defaults`` is
        True, fields with default values are included in the output.
        """
        if self._lifecycle_stage not in ("idle", "change"):
            raise RuntimeError("Cannot serialize during an active transaction")
        target = node if node is not None else self._root
        return _node_to_wire(target, include_defaults=include_defaults)

    # --- Composition ---

    def adopt(
        self,
        fragment: JsonDoc | list[JsonDoc],
        parent: AtomNode,
        slot_name: str,
        position: str = "append",
        target: AtomNode | None = None,
    ) -> list[AtomNode]:
        """Take in a subtree dumped from another document.

        ``fragment`` is one node entry from ``dump(node)`` or a list of
        them. The nodes keep their IDs, so references *within* the fragment
        stay intact and citations of the fragment's nodes made elsewhere
        remain valid. Only an ID that already exists in this document is
        re-minted, and references inside the fragment to it are rewritten.
        That is the whole cost of composition: no other state changes.

        References from the fragment to nodes outside it must resolve in
        this document once inserted; the commit-time integrity check
        enforces that. Returns the top-level adopted nodes, in order.
        """
        entries: list[JsonDoc]
        if fragment and isinstance(fragment[0], list):
            entries = list(fragment)  # type: ignore[arg-type]
        else:
            entries = [fragment]  # type: ignore[list-item]

        def collect(entry: JsonDoc, into: list[str]) -> None:
            into.append(entry[0])
            if len(entry) > 3 and entry[3]:
                for children in entry[3].values():
                    for child in children:
                        collect(child, into)

        # IDs already present in the document, plus those taken by earlier
        # entries of this call: the same fragment adopted twice yields two
        # distinct copies.
        taken: set[str] = set(self._node_map)
        nodes: list[AtomNode] = []
        for entry in entries:
            incoming: list[str] = []
            collect(entry, incoming)
            if len(set(incoming)) != len(incoming):
                dup = next(i for i in incoming if incoming.count(i) > 1)
                raise ValueError(f"Fragment contains node id {dup!r} more than once")
            remap = {i: self._id_gen() for i in incoming if i in taken}
            taken.update(remap.get(i, i) for i in incoming)
            nodes.append(self._build_fragment_node(entry, remap))

        self._insert_into_slot(parent, slot_name, position, nodes, target=target)
        # The fragment may carry this document's own session prefix (a copy
        # of an earlier dump); make sure new IDs cannot walk into it.
        self._reseed_id_factory()
        return nodes

    def _build_fragment_node(self, entry: JsonDoc, remap: dict[str, str]) -> AtomNode:
        """Detached node tree from a wire entry, applying an ID remap."""
        node_id = remap.get(entry[0], entry[0])
        state = entry[2] if len(entry) > 2 else {}
        node = self._create_node_from_json([node_id, entry[1], state])
        if remap:
            for name in type(node)._ref_defs:
                value = node._state.get(name)
                if isinstance(value, str):
                    node._state[name] = remap.get(value, value)
                elif isinstance(value, list):
                    node._state[name] = [remap.get(v, v) for v in value]
        if len(entry) > 3 and entry[3]:
            for slot_name, children in entry[3].items():
                if slot_name not in node._slot_first:
                    continue
                prev: AtomNode | None = None
                for child_entry in children:
                    child = self._build_fragment_node(child_entry, remap)
                    child._parent = node
                    child._slot_name = slot_name
                    child._prev_sibling = prev
                    if prev is not None:
                        prev._next_sibling = child
                    else:
                        node._slot_first[slot_name] = child
                    prev = child
                node._slot_last[slot_name] = prev
        return node

    @classmethod
    def restore(
        cls,
        data: JsonDoc,
        root_type: type[AtomNode] | None = None,
        nodes: list[type[AtomNode]] | None = None,
        extensions: list[Extension] | None = None,
        strict_mode: bool = True,
        undo_manager: UndoManagerConfig | None = None,
        node_id_generator: NodeIdGenerator | None = None,
    ) -> Doc:
        """Restore a document from wire format (dump output).

        Normalizers run once the tree is loaded, and the result is not
        recorded in undo history.
        """
        doc_id = data[0]
        root_type_str = data[1]

        effective_root: type[AtomNode] | str = root_type if root_type is not None else root_type_str

        doc = cls(
            root_type=effective_root,
            nodes=nodes,
            extensions=extensions,
            doc_id=doc_id,
            strict_mode=strict_mode,
            undo_manager=undo_manager,
            node_id_generator=node_id_generator,
            _defer_init=True,
        )

        root = doc._create_node_from_json(data)
        doc._node_map.pop(doc._root.id, None)
        doc._root = root
        doc._node_map[root.id] = root

        if len(data) > 3 and data[3]:
            _deserialize_slots(doc, root, data[3])

        doc._reseed_id_factory()

        # Normalizers see the restored tree; nothing here is undoable.
        # Unresolved references raise (strict) or warn in _finish_init.
        doc._finish_init()
        return doc

    def _create_node_from_json(self, json_node: JsonDoc) -> AtomNode:
        node_id = json_node[0]
        node_type = json_node[1]
        state_dict = json_node[2] if len(json_node) > 2 else {}

        # With a time-extracting generator, non-root IDs use the compact
        # scheme and only the root is validated (in the constructor).
        if self._node_id_generator.extract_time is None and not (
            isinstance(node_id, str) and self._node_id_generator.validate(node_id)
        ):
            raise ValueError(f"Invalid node id: {node_id!r}")

        node_cls = self._node_types.get(node_type)
        if node_cls is None:
            raise ValueError(f"Unknown node type: '{node_type}'")

        node = node_cls(_id=node_id, _doc=self)
        node_cls._apply_defaults(node._state)

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
        node_types: dict[str, Any] = {}
        value_types: dict[str, Any] = {}
        value_type_classes: dict[str, type] = {}

        for type_name, node_cls in self._node_types.items():
            entry: dict[str, Any] = {}

            # JSON Schema per field (avoids _schema_model rebuild issues).
            # ``Field(...)`` constraints ride along via Annotated metadata.
            properties: dict[str, Any] = {}
            for fname, adapter in node_cls._field_adapters.items():
                try:
                    info = node_cls._field_infos.get(fname)
                    if info is not None and info.metadata and fname not in node_cls._ref_defs:
                        ann = node_cls._field_annotations[fname]
                        prop = TypeAdapter(Annotated[(ann, *info.metadata)]).json_schema()
                    else:
                        prop = adapter.json_schema()
                except Exception:
                    prop = {}
                prop = _inline_defs(prop)
                fdefault = node_cls._field_defaults.get(fname, _MISSING)
                if fdefault is not _MISSING and "default" not in prop:
                    try:
                        prop["default"] = _json_safe(fdefault)
                    except Exception:
                        pass
                properties[fname] = prop
            entry["json_schema"] = {"type": "object", "properties": properties}

            # Field tiers
            entry["field_tiers"] = dict(node_cls._field_tiers)

            # References: target type, cardinality, delete policy
            entry["refs"] = {
                fname: {
                    "target_type": rdef.target_name,
                    "many": rdef.many,
                    "policy": rdef.policy,
                }
                for fname, rdef in node_cls._ref_defs.items()
            }

            # Handles: fields whose value type names something outside the
            # document, with the declared strength.
            handles: dict[str, Any] = {}
            for fname, ann in node_cls._field_annotations.items():
                handle_types = [m for m in frozen_models_in(ann) if is_handle_type(m)]
                if handle_types:
                    # A field that may hold several handle types is as strong
                    # as its strongest option.
                    chosen = next(
                        (h for h in handle_types if h.strength == "strong"),  # type: ignore[attr-defined]
                        handle_types[0],
                    )
                    handles[fname] = {
                        "value_type": chosen.__name__,
                        "strength": chosen.strength,  # type: ignore[attr-defined]
                    }
            entry["handles"] = handles

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
                defaults[fname] = _json_safe(fdefault)
            entry["field_defaults"] = defaults

            node_types[type_name] = entry

            # Discover frozen value types from the field annotations
            # (including members of unions and Optional).
            for fname, ann in node_cls._field_annotations.items():
                for vtype in frozen_models_in(ann):
                    seen_type = value_type_classes.get(vtype.__name__)
                    if seen_type is vtype:
                        continue
                    if seen_type is not None:
                        raise ValueError(
                            f"Two value types named {vtype.__name__!r} "
                            f"({seen_type.__module__} and {vtype.__module__}); "
                            f"the schema export keys value types by name"
                        )
                    value_type_classes[vtype.__name__] = vtype
                    ventry: dict[str, Any] = {
                        "json_schema": _inline_defs(vtype.model_json_schema()),
                        "frozen": True,
                    }
                    if is_handle_type(vtype):
                        ventry["handle"] = {"strength": vtype.strength}  # type: ignore[attr-defined]
                    value_types[vtype.__name__] = ventry

        return {
            "version": 1,
            "root_type": self._root_type,
            "node_types": node_types,
            "value_types": value_types,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inline_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local ``$ref``s into the schema so it is self-contained.

    Pydantic emits ``$defs`` for unions and nested models. Clients that
    read the export field by field should not have to chase references;
    a recursive definition (``JsonValue``) becomes ``{}`` (any value).
    """
    defs = schema.get("$defs")
    if not defs:
        return schema

    def resolve(obj: Any, stack: tuple[str, ...]) -> Any:
        if isinstance(obj, dict):
            ref = obj.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref[len("#/$defs/"):]
                if name in stack or name not in defs:
                    return {}
                resolved = resolve(defs[name], stack + (name,))
                extra = {k: v for k, v in obj.items() if k not in ("$ref", "$defs")}
                return {**resolved, **extra} if isinstance(resolved, dict) else resolved
            out = {k: resolve(v, stack) for k, v in obj.items() if k != "$defs"}
            disc = out.get("discriminator")
            if isinstance(disc, dict) and "mapping" in disc:
                # The mapping pointed into $defs, which no longer exist;
                # the variants are inlined in order under oneOf/anyOf.
                out["discriminator"] = {k: v for k, v in disc.items() if k != "mapping"}
            return out
        if isinstance(obj, list):
            return [resolve(v, stack) for v in obj]
        return obj

    return resolve(schema, ())


def _json_safe(value: Any) -> Any:
    """A JSON-compatible copy of a field default (models, bytes, datetimes,
    enums, decimals, sets, ...)."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, bytes):
        import base64

        return base64.b64encode(value).decode()
    return to_jsonable_python(value)


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
            if child_json[0] in doc._node_map:
                raise ValueError(f"Duplicate node id in document: {child_json[0]!r}")
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
