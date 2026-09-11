"""Partial replication: a client's scope, the view it holds, and the
projection of commits onto it.

A scoped client holds a *view* of the document: the subtrees under its
anchors (each an ``{id, depth?}``), the ancestors of those anchors as
stubs, the children just past a depth bound as stubs, and the targets
of references held by its full nodes as stubs. A stub is a node the
client knows by identity and type only.

Every commit is projected per view, synchronously at commit time, into
the ordered operations the client applies with the same code path as a
whole-document patch. Beyond the three structural operations, a
projected patch may carry:

- ``[3, id]``: the node becomes a stub: its state is dropped. Children
  that leave the view with it get their own operations.
- ``[4, id]``: the node and its subtree leave the view (it still
  exists). A node the client no longer has is ignored.
- ``[5, id, type]``: the node is now a detached stub (a reference target
  outside every held chain): created if unknown, otherwise taken out of
  the tree with its subtree and demoted.
- ``[6, id]``: the node, a stub in the client's tree, is filled with the
  state that follows: it entered the view in place.

A stub pair is ``[id, type, None]``. An insert may name a node the
client holds *detached*: it is placed there and, if the pair is full,
filled. An insert never names a node the client has in its tree.

Operations are emitted in an order that keeps every operation's
preconditions true on the client: deletes, then arrivals top-down
(entries, fills, and placements), then moves, then exits, demotions,
and detachments, then state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

from ._range import _descendants
from ._ref import ref_ids
from ._types import ChangeEvent, JsonDoc, Operations

if TYPE_CHECKING:
    from ._doc import Doc
    from ._node import AtomNode

Kind = Literal["full", "stub"]
Anchors = dict[str, int | None]

OP_STUB = 3
OP_EXIT = 4
OP_DETACHED = 5
OP_FILL = 6


class OutOfScope(Exception):
    """A request touches a node the client does not hold in full."""


def parse_anchors(raw: Any) -> Anchors:
    """Validate a ``scope`` request's anchors: a list of ``{id, depth?}``.
    Overlapping anchors keep the more generous depth."""
    if not isinstance(raw, list):
        raise ValueError("'anchors' must be a list of {id, depth?}")
    anchors: Anchors = {}
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ValueError("each anchor must be {id: str, depth?: int}")
        depth = entry.get("depth")
        if depth is not None and (
            isinstance(depth, bool) or not isinstance(depth, int) or depth < 0
        ):
            raise ValueError("an anchor's 'depth' must be a non-negative integer")
        node_id = entry["id"]
        if node_id in anchors:
            current = anchors[node_id]
            depth = None if current is None or depth is None else max(current, depth)
        anchors[node_id] = depth
    return anchors


def _stub_pair(node: AtomNode) -> list[Any]:
    return [node.id, node._node_type, None]


def _full_pair(node: AtomNode) -> list[Any]:
    return [node.id, node._node_type]


def _depth_of(node: AtomNode) -> int:
    depth = 0
    current = node._parent
    while current is not None:
        depth += 1
        current = current._parent
    return depth


def _order_key(node: AtomNode) -> tuple[int, list[tuple[int, int]]]:
    """(depth, document-order path): parents before children, siblings
    in slot order, for a deterministic patch."""
    path: list[tuple[int, int]] = []
    current: AtomNode | None = node
    while current is not None and current._parent is not None:
        parent = current._parent
        slot_index = parent._slot_order.index(current._slot_name) if current._slot_name else 0
        index = 0
        sibling = current._prev_sibling
        while sibling is not None:
            index += 1
            sibling = sibling._prev_sibling
        path.append((slot_index, index))
        current = parent
    path.reverse()
    return (len(path), path)


class _Overlay:
    """A set read through additions and removals recorded on top of a
    base set that is never copied: the simulation of the client's state
    while a patch is emitted touches a few nodes of a large view."""

    __slots__ = ("_base", "_added", "_removed")

    def __init__(self, base: set[str] | dict[str, Any]) -> None:
        self._base = base
        self._added: set[str] = set()
        self._removed: set[str] = set()

    def __contains__(self, node_id: object) -> bool:
        if node_id in self._added:
            return True
        if node_id in self._removed:
            return False
        return node_id in self._base

    def add(self, node_id: str) -> None:
        self._removed.discard(node_id)
        if node_id not in self._base:
            self._added.add(node_id)

    def discard(self, node_id: str) -> None:
        self._added.discard(node_id)
        if node_id in self._base:
            self._removed.add(node_id)


class ClientView:
    """What one scoped client holds, and how commits reach it."""

    def __init__(self, doc: Doc, anchors: Anchors) -> None:
        self._doc = doc
        self.anchors: Anchors = dict(anchors)
        # Node ID -> kind, for every node the client holds.
        self.held: dict[str, Kind] = {}
        # The held nodes that are stubs: few, and re-checked on every
        # commit.
        self.stubs: set[str] = set()
        # Held nodes the client has in its tree; the rest are detached
        # stubs (reference targets whose parent it does not hold).
        self.placed: set[str] = set()

    # --- Classification ---

    def _anchor_ancestors(self) -> set[str]:
        result: set[str] = set()
        for anchor_id in self.anchors:
            node = self._doc.get_node_by_id(anchor_id)
            if node is None:
                continue
            for ancestor in self._doc.ancestors(node):
                result.add(ancestor.id)
        return result

    def _is_full(self, node: AtomNode) -> bool:
        distance = 0
        current: AtomNode | None = node
        while current is not None:
            if current.id in self.anchors:
                depth = self.anchors[current.id]
                if depth is None or distance <= depth:
                    return True
            current = current._parent
            distance += 1
        return False

    def _classify(self, node: AtomNode, ancestor_ids: set[str]) -> Kind | None:
        boundary = False
        distance = 0
        current: AtomNode | None = node
        while current is not None:
            if current.id in self.anchors:
                depth = self.anchors[current.id]
                if depth is None or distance <= depth:
                    return "full"
                if distance == depth + 1:
                    boundary = True
            current = current._parent
            distance += 1
        if boundary or node.id in ancestor_ids or node is self._doc.root:
            return "stub"
        for referrer in self._doc.referrers(node):
            if self._is_full(referrer):
                return "stub"
        return None

    def resolved_anchors(self) -> list[dict[str, Any]]:
        """The anchors that name a node in the document."""
        result = []
        for anchor_id, depth in self.anchors.items():
            if self._doc.get_node_by_id(anchor_id) is None:
                continue
            entry: dict[str, Any] = {"id": anchor_id}
            if depth is not None:
                entry["depth"] = depth
            result.append(entry)
        return result

    def _targets_of(self, node: AtomNode) -> list[str]:
        result: list[str] = []
        for name in type(node)._ref_defs:
            result.extend(ref_ids(node, name))
        return result

    # --- Whole view ---

    def _compute_all(self) -> dict[str, Kind]:
        """Classify every node the current anchors reach: O(view)."""
        doc = self._doc
        held: dict[str, Kind] = {}
        node: AtomNode | None
        for anchor_id, depth in self.anchors.items():
            anchor = doc.get_node_by_id(anchor_id)
            if anchor is None:
                continue
            stack: list[tuple[AtomNode, int]] = [(anchor, 0)]
            while stack:
                node, distance = stack.pop()
                if depth is not None and distance > depth:
                    held.setdefault(node.id, "stub")  # a child at the boundary
                    continue
                held[node.id] = "full"
                for slot_name in node._slot_order:
                    child: AtomNode | None = node._slot_first.get(slot_name)
                    while child is not None:
                        stack.append((child, distance + 1))
                        child = child._next_sibling
        for ancestor_id in self._anchor_ancestors():
            held.setdefault(ancestor_id, "stub")
        held.setdefault(doc.root.id, "stub")
        for node_id, kind in list(held.items()):
            if kind != "full":
                continue
            node = doc.get_node_by_id(node_id)
            if node is None:
                continue
            for target_id in self._targets_of(node):
                if target_id not in held and doc.get_node_by_id(target_id) is not None:
                    held[target_id] = "stub"
        return held

    def reset(self) -> None:
        """Recompute the view from scratch (a snapshot follows)."""
        self.held = self._compute_all()
        self.stubs = {node_id for node_id, kind in self.held.items() if kind == "stub"}
        self._recompute_placed()

    def hold_everything(self) -> None:
        """The view of a client that received the whole document."""
        self.held = {node_id: "full" for node_id in self._doc._node_map}
        self.stubs = set()
        self.placed = set(self.held)

    def _recompute_placed(self, ids: Any = None) -> None:
        """A held node is in the client's tree when every ancestor up to
        the root is held; the rest are detached stubs. With ``ids``, only
        those nodes are reconsidered: a node's placement changes only
        with its own kind, its parent, or an ancestor's kind, and every
        such node is a candidate of the commit or scope change that did
        it, so the rest of the view keeps its placement."""
        doc = self._doc
        known: dict[str, bool] = {}
        if ids is None:
            ids = list(self.held)
            self.placed = set()
        for node_id in ids:
            node = doc.get_node_by_id(node_id)
            if node is None or node_id not in self.held:
                self.placed.discard(node_id)
                continue
            chain: list[str] = []
            current: AtomNode | None = node
            result = True
            while current is not None:
                if current.id in known:
                    result = known[current.id]
                    break
                if current.id not in self.held:
                    result = False
                    break
                chain.append(current.id)
                current = current._parent
            for chain_id in chain:
                known[chain_id] = result
            if result:
                self.placed.add(node_id)
            else:
                self.placed.discard(node_id)

    def _has_held_child(self, node: AtomNode) -> bool:
        for name in node._slot_order:
            child: AtomNode | None = node._slot_first.get(name)
            while child is not None:
                if child.id in self.held:
                    return True
                child = child._next_sibling
        return False

    def snapshot(self) -> tuple[JsonDoc, list[list[str]]]:
        """The held tree in wire form (stubs as ``[id, type, None]`` with
        only their held children) and the detached stubs as
        ``[id, type]`` pairs."""
        doc = self._doc
        held = self.held
        node: AtomNode | None

        def entry_for(node: AtomNode) -> JsonDoc:
            if held.get(node.id) == "full":
                entry: JsonDoc = [node.id, node._node_type, node._state_to_json_plain()]
                if node._slot_order:
                    entry.append({name: [] for name in node._slot_order})
                return entry
            entry = [node.id, node._node_type, None]
            if self._has_held_child(node):
                entry.append({name: [] for name in node._slot_order})
            return entry

        root_entry = entry_for(doc.root)
        stack: list[tuple[AtomNode, list[JsonDoc] | None, JsonDoc]] = [(doc.root, None, root_entry)]
        while stack:
            node, into, entry = stack.pop()
            if into is not None:
                into.append(entry)
            if len(entry) < 4:
                continue
            slots = entry[3]
            pending: list[tuple[AtomNode, list[JsonDoc] | None, JsonDoc]] = []
            for name in node._slot_order:
                child: AtomNode | None = node._slot_first.get(name)
                while child is not None:
                    if child.id in held:
                        pending.append((child, slots[name], entry_for(child)))
                    child = child._next_sibling
            stack.extend(reversed(pending))
        detached: list[list[str]] = []
        for node_id in held:
            if node_id in self.placed:
                continue
            node = doc.get_node_by_id(node_id)
            if node is not None:
                detached.append([node_id, node._node_type])
        return root_entry, detached

    # --- Checks on client requests ---

    def _require_full(self, node_id: Any, what: str) -> None:
        if not isinstance(node_id, str) or self.held.get(node_id) != "full":
            raise OutOfScope(f"{what} '{node_id}' is not held in full by this client")

    def _require_known(self, node_id: Any, what: str) -> None:
        if not isinstance(node_id, bool) and node_id in (0, None, ""):
            return
        if not isinstance(node_id, str) or node_id not in self.held:
            raise OutOfScope(f"{what} '{node_id}' is not held by this client")

    def _parent_id(self, raw: Any) -> Any:
        return self._doc.root.id if raw in (0, None, "") else raw

    def _require_range(self, start_id: Any, end_id: Any) -> None:
        """Every node of a sibling range must be held in full, not only
        its ends: the nodes between them go with it."""
        self._require_full(start_id, "Node")
        if end_id in (0, None):
            return
        self._require_full(end_id, "Node")
        node = self._doc.get_node_by_id(start_id)
        while node is not None and node.id != end_id:
            node = node._next_sibling
            if node is None:
                break
            self._require_full(node.id, "Node")

    def _require_sibling(self, parent_id: Any, slot: Any, sibling_id: Any) -> None:
        """A neighbor must be held (a stub will do) and lie in the slot
        the request names: the document places the node beside it,
        wherever it is."""
        if isinstance(sibling_id, bool) or sibling_id in (0, None, ""):
            return
        self._require_known(sibling_id, "Sibling")
        sibling = self._doc.get_node_by_id(sibling_id)
        parent = self._doc.get_node_by_id(parent_id)
        if sibling is None or parent is None:
            return  # the document decides
        if sibling._parent is not parent or sibling._slot_name != slot:
            raise OutOfScope(f"Sibling '{sibling_id}' is not in slot '{slot}' of '{parent_id}'")

    def check_operations(self, ops: Operations) -> None:
        """Refuse an ``op`` request that touches anything the client does
        not hold in full: it may not insert under, delete, move, or write
        a node it holds as a stub or not at all, nor insert a node that
        exists. Neighbors need only be held (a stub may be the neighbor a
        node lands beside); the nodes a request inserts count as full for
        the state it carries with them."""
        created: set[str] = set()
        doc = self._doc
        for raw in ops[0]:
            op = cast(tuple[Any, ...], raw)
            if op[0] == 0:
                parent_id = self._parent_id(op[2])
                self._require_full(parent_id, "Parent")
                for pair in op[1]:
                    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                        raise OutOfScope("A client inserts nodes as [id, type] pairs only")
                    node_id = pair[0]
                    # An id that exists, or that a deleted node still
                    # holds (an undo may bring it back), is not free.
                    if (
                        node_id in self.held
                        or doc.get_node_by_id(node_id) is not None
                        or node_id in doc._graveyard
                    ):
                        raise OutOfScope(f"Node '{node_id}' already exists")
                    created.add(node_id)
                self._require_sibling(parent_id, op[3], op[4])
                self._require_sibling(parent_id, op[3], op[5])
            elif op[0] == 1:
                self._require_range(op[1], op[2])
            elif op[0] == 2:
                self._require_range(op[1], op[2])
                parent_id = self._parent_id(op[3])
                self._require_full(parent_id, "Parent")
                self._require_sibling(parent_id, op[4], op[5])
                self._require_sibling(parent_id, op[4], op[6])
        for node_id in ops[1]:
            if node_id not in created:
                self._require_full(node_id, "Node")

    def check_create(self, parent_id: Any, slot: Any, target_id: Any) -> None:
        parent_id = self._parent_id(parent_id)
        self._require_full(parent_id, "Parent")
        if target_id:
            self._require_sibling(parent_id, slot, target_id)

    def redact(self, message: str) -> str:
        """Replace, in an error message, the id of any node this client
        does not hold (a deleted one still in the graveyard included): it
        must not learn what lies outside its view."""
        for node_id in (*self._doc._node_map, *self._doc._graveyard):
            if node_id not in self.held and node_id in message:
                message = message.replace(node_id, "(a node outside your scope)")
        return message

    # --- Projection ---

    def project(self, event: ChangeEvent) -> dict[str, Any] | None:
        """The operations this client applies for a commit, or None when
        the commit did not touch its view. Updates the view."""
        doc = self._doc
        diff = event.diff
        node: AtomNode | None
        deleted: dict[str, AtomNode] = {}
        for node_id, node in diff.deleted.items():
            deleted[node_id] = node
            for desc in _descendants(node):
                deleted[desc.id] = desc
        candidates: set[str] = set()
        for node_id in set(diff.inserted) | set(diff.moved):
            node = doc.get_node_by_id(node_id)
            if node is None:
                continue
            for member in (node, *_descendants(node)):
                candidates.add(member.id)
                if member.id in self.anchors:
                    for ancestor in doc.ancestors(member):
                        candidates.add(ancestor.id)
        candidates |= set(diff.updated)
        # Reference targets gain or lose their referent status with their
        # referrers: the current targets of every touched node, and the
        # former targets of updated and deleted ones.
        for node_id in list(candidates):
            node = doc.get_node_by_id(node_id)
            if node is not None:
                candidates.update(self._targets_of(node))
        for node in deleted.values():
            candidates.update(self._targets_of(node))
        for node_id, patch in event.inverse_operations[1].items():
            node = doc.get_node_by_id(node_id)
            if node is None:
                continue
            for name, value in patch.items():
                if name not in type(node)._ref_defs:
                    continue
                if isinstance(value, list):
                    candidates.update(v for v in value if isinstance(v, str))
                elif isinstance(value, str):
                    candidates.add(value)
        # Stubs are few and cheap to re-check; an anchor that moved
        # changes which ancestors are stubs, and a stub whose ancestor
        # left the view is detached.
        candidates.update(self.stubs)
        candidates -= deleted.keys()
        if not candidates and not (deleted.keys() & self.held.keys()):
            return None

        ancestor_ids = self._anchor_ancestors()
        new_kinds: dict[str, Kind | None] = {}
        for node_id in candidates:
            node = doc.get_node_by_id(node_id)
            new_kinds[node_id] = None if node is None else self._classify(node, ancestor_ids)
        return self._emit(new_kinds, deleted, set(diff.moved), event.operations[1])

    def change(self, anchors: Anchors) -> dict[str, Any]:
        """Replace the anchors; the operations that take the client from
        its current view to the new one (a delta, never a re-snapshot)."""
        self.anchors = dict(anchors)
        new_kinds: dict[str, Kind | None] = dict.fromkeys(self.held)
        new_kinds.update(self._compute_all())
        return self._emit(new_kinds, {}, set(), {}) or {"ordered": [], "state": {}}

    def _emit(
        self,
        new_kinds: dict[str, Kind | None],
        deleted: dict[str, AtomNode],
        moved: set[str],
        state_patch: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        doc = self._doc
        old = self.held
        node: AtomNode | None
        ordered: list[Any] = []
        # A simulation of what the client has as each operation lands:
        # ``present`` is every node it holds, ``placed`` those in its
        # tree (the rest are detached stubs). Overlays: a patch touches
        # a few nodes of a view that may hold thousands.
        present = _Overlay(old)
        placed = _Overlay(self.placed)
        emitted: set[str] = set()
        entered_state: dict[str, dict[str, Any]] = {}

        def kind_after(node_id: str) -> Kind | None:
            if node_id in new_kinds:
                return new_kinds[node_id]
            return old.get(node_id)

        placed_after_cache: dict[str, bool] = {}

        def placed_after(node: AtomNode) -> bool:
            """Whether the node is in the client's tree once this patch
            has landed: it and every ancestor are held."""
            chain: list[str] = []
            current: AtomNode | None = node
            result = True
            while current is not None:
                if current.id in placed_after_cache:
                    result = placed_after_cache[current.id]
                    break
                if kind_after(current.id) is None:
                    result = False
                    break
                chain.append(current.id)
                current = current._parent
            for chain_id in chain:
                placed_after_cache[chain_id] = result
            return result

        def parent_ref(node: AtomNode) -> Any:
            parent = node._parent
            assert parent is not None
            return 0 if parent is doc.root else parent.id

        # Nodes the client keeps in its tree that this commit moved: in
        # their old place until step 3, so not neighbors before that.
        pending_moves: set[str] = set()
        movers: list[AtomNode] = []
        for node_id in moved:
            if node_id not in placed:
                continue
            node = doc.get_node_by_id(node_id)
            if node is None or kind_after(node_id) is None or not placed_after(node):
                continue
            pending_moves.add(node_id)
            movers.append(node)

        def in_place(node: AtomNode) -> bool:
            return node.id in placed and node.id not in pending_moves

        def neighbors(node: AtomNode) -> tuple[Any, Any]:
            prev = node._prev_sibling
            while prev is not None and not in_place(prev):
                prev = prev._prev_sibling
            nxt = node._next_sibling
            while nxt is not None and not in_place(nxt):
                nxt = nxt._next_sibling
            return (prev.id if prev else 0, nxt.id if nxt else 0)

        def place_children(node: AtomNode) -> None:
            """Insert the held children of a node that just entered in
            full, slot by slot, in runs between the children the client
            already has in place; then descend into the new full ones."""
            for slot_name in node._slot_order:
                run: list[list[Any]] = []
                run_nodes: list[AtomNode] = []
                prev_placed: AtomNode | None = None
                descend: list[AtomNode] = []

                def flush(nxt: AtomNode | None) -> None:
                    if not run:
                        return
                    ordered.append([
                        0,
                        list(run),
                        0 if node is doc.root else node.id,
                        slot_name,
                        prev_placed.id if prev_placed is not None else 0,
                        nxt.id if nxt is not None else 0,
                    ])
                    for member in run_nodes:
                        present.add(member.id)
                        placed.add(member.id)
                        emitted.add(member.id)
                    run.clear()
                    run_nodes.clear()

                child: AtomNode | None = node._slot_first.get(slot_name)
                while child is not None:
                    kind = kind_after(child.id)
                    if kind is None or child.id in pending_moves:
                        # Not held, or moved here in step 3.
                        child = child._next_sibling
                        continue
                    if child.id in placed:
                        flush(child)
                        prev_placed = child
                    else:
                        if kind == "full":
                            run.append(_full_pair(child))
                            entered_state[child.id] = child._state_to_json_plain()
                            descend.append(child)
                        else:
                            run.append(_stub_pair(child))
                        run_nodes.append(child)
                    child = child._next_sibling
                flush(None)
                for child in descend:
                    place_children(child)

        # 1. Real deletes of held nodes: one per top-most node in the
        # client's tree (the subtree goes with it), one per detached stub.
        deleted_held = [node_id for node_id in deleted if node_id in present]
        for node_id in deleted_held:
            covered = False
            if node_id in placed:
                ancestor = deleted[node_id]._parent
                while ancestor is not None:
                    if ancestor.id in deleted and ancestor.id in placed:
                        covered = True
                        break
                    ancestor = ancestor._parent
            if not covered:
                ordered.append([1, node_id, 0])
        for node_id in deleted_held:
            present.discard(node_id)
            placed.discard(node_id)

        # 2. Arrivals, parents before children: nodes that enter in full
        # (filled in place, or inserted with their subtree), stubs that
        # take a place in the tree, and detached stubs that appear.
        arriving: list[AtomNode] = []
        for node_id, kind in new_kinds.items():
            if kind is None:
                continue
            node = doc.get_node_by_id(node_id)
            if node is None:
                continue
            was = old.get(node_id)
            if was is None or (was == "stub" and kind == "full") or (
                node_id not in placed and placed_after(node)
            ):
                arriving.append(node)
        arriving.sort(key=_order_key)
        for node in arriving:
            if node.id in emitted:
                continue
            kind = kind_after(node.id)
            if not placed_after(node):
                # A detached stub (its chain is not held).
                if node.id not in present:
                    ordered.append([OP_DETACHED, node.id, node._node_type])
                    present.add(node.id)
                    emitted.add(node.id)
                continue
            if node.id in placed:
                # In the tree already: a stub filling in place.
                if kind == "full" and old.get(node.id) != "full":
                    ordered.append([OP_FILL, node.id])
                    entered_state[node.id] = node._state_to_json_plain()
                    emitted.add(node.id)
                    place_children(node)
                continue
            parent = node._parent
            if parent is None or parent.id not in placed:
                continue  # its parent arrives first and places it from there
            prev, nxt = neighbors(node)
            pair = _full_pair(node) if kind == "full" else _stub_pair(node)
            ordered.append([0, [pair], parent_ref(node), node._slot_name, prev, nxt])
            present.add(node.id)
            placed.add(node.id)
            emitted.add(node.id)
            if kind == "full":
                entered_state[node.id] = node._state_to_json_plain()
                place_children(node)

        # 3. Moves of nodes the client keeps in its tree.
        movers.sort(key=_order_key)
        for node in movers:
            prev, nxt = neighbors(node)
            pending_moves.discard(node.id)
            ordered.append([2, node.id, 0, parent_ref(node), node._slot_name, prev, nxt])
            emitted.add(node.id)

        # 4. Nodes that leave the view, become stubs, or come out of the
        # tree as detached stubs, top-most first. Exits and detachments
        # are idempotent on the client, so a node under one that already
        # left may be named again.
        leaving: list[tuple[Any, str]] = []
        for node_id, kind in new_kinds.items():
            was = old.get(node_id)
            if was is None or node_id in deleted:
                continue
            node = doc.get_node_by_id(node_id)
            if node is None:
                continue
            if (
                kind is None
                or (was == "full" and kind == "stub")
                or (node_id in placed and not placed_after(node))
            ):
                leaving.append((_order_key(node), node_id))
        leaving.sort()
        # Subtrees an exit or detachment took off the client. A node
        # under one that leaves too needs no operation of its own; one
        # that stays held comes back detached.
        dropped: set[str] = set()
        for _, node_id in leaving:
            node = doc.get_node_by_id(node_id)
            assert node is not None
            kind = kind_after(node_id)
            # Only a node in the client's tree went with a dropped
            # ancestor; a detached stub is not under anything there.
            under_dropped = False
            ancestor = node._parent if node_id in placed else None
            while ancestor is not None:
                if ancestor.id in dropped:
                    under_dropped = True
                    break
                ancestor = ancestor._parent
            if kind is None:
                if not under_dropped:
                    ordered.append([OP_EXIT, node_id])
                present.discard(node_id)
                placed.discard(node_id)
                dropped.add(node_id)
            elif node_id in placed and not placed_after(node):
                ordered.append([OP_DETACHED, node_id, node._node_type])
                placed.discard(node_id)
                dropped.add(node_id)
            elif node_id in placed:
                ordered.append([OP_STUB, node_id])

        # 5. State: whole for nodes that entered, the commit's patch for
        # nodes the client already held in full.
        state: dict[str, dict[str, Any]] = {
            node_id: patch for node_id, patch in entered_state.items() if patch
        }
        for node_id, patch in state_patch.items():
            if node_id in entered_state:
                continue
            if old.get(node_id) == "full" and kind_after(node_id) == "full":
                state[node_id] = patch

        # Commit the new view.
        for node_id, kind in new_kinds.items():
            if kind is None:
                self.held.pop(node_id, None)
                self.stubs.discard(node_id)
            else:
                self.held[node_id] = kind
                if kind == "stub":
                    self.stubs.add(node_id)
                else:
                    self.stubs.discard(node_id)
        for node_id in deleted:
            self.held.pop(node_id, None)
            self.stubs.discard(node_id)
        self._recompute_placed([*new_kinds, *deleted])

        if not ordered and not state:
            return None
        return {"ordered": ordered, "state": state}
