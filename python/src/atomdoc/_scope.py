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
- ``[4, id]``: the node and its subtree leave the view (it still exists).
- ``[5, id, type]``: a detached stub appears (a reference target outside
  every held chain).

An insert whose pair names a node the client already holds places it
there and, if the pair is full, fills it with the state that follows:
that is how a stub enters the view in place. A stub pair is
``[id, type, None]``. An insert naming the root fills it and places
nothing.

Operations are emitted in an order that keeps every operation's
preconditions true on the client: deletes, then appearances and
entries top-down, then moves, then exits and demotions, then state.
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


class ClientView:
    """What one scoped client holds, and how commits reach it."""

    def __init__(self, doc: Doc, anchors: Anchors) -> None:
        self._doc = doc
        self.anchors: Anchors = dict(anchors)
        # Node ID -> kind, for every node the client holds.
        self.held: dict[str, Kind] = {}
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
        self._recompute_placed()

    def hold_everything(self) -> None:
        """The view of a client that received the whole document."""
        self.held = {node_id: "full" for node_id in self._doc._node_map}
        self.placed = set(self.held)

    def _recompute_placed(self) -> None:
        doc = self._doc
        placed: set[str] = set()
        for node_id in self.held:
            node = doc.get_node_by_id(node_id)
            if node is None:
                continue
            if node is doc.root or (node._parent is not None and node._parent.id in self.held):
                placed.add(node_id)
        self.placed = placed

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
        if node_id in (0, None, ""):
            return
        if not isinstance(node_id, str) or node_id not in self.held:
            raise OutOfScope(f"{what} '{node_id}' is not held by this client")

    def _parent_id(self, raw: Any) -> Any:
        return self._doc.root.id if raw in (0, None, "") else raw

    def check_operations(self, ops: Operations) -> None:
        """Refuse an ``op`` request that touches anything the client does
        not hold in full: it may not insert under, delete, move, or write
        a node it holds as a stub or not at all. Neighbors need only be
        held (a stub may be the neighbor a node lands beside)."""
        for raw in ops[0]:
            op = cast(tuple[Any, ...], raw)
            if op[0] == 0:
                self._require_full(self._parent_id(op[2]), "Parent")
                for pair in op[1]:
                    if len(pair) != 2:
                        raise OutOfScope("A client may not insert a stub")
                self._require_known(op[4], "Sibling")
                self._require_known(op[5], "Sibling")
            elif op[0] == 1:
                self._require_full(op[1], "Node")
                if op[2] not in (0, None):
                    self._require_full(op[2], "Node")
            elif op[0] == 2:
                self._require_full(op[1], "Node")
                if op[2] not in (0, None):
                    self._require_full(op[2], "Node")
                self._require_full(self._parent_id(op[3]), "Parent")
                self._require_known(op[5], "Sibling")
                self._require_known(op[6], "Sibling")
        for node_id in ops[1]:
            self._require_full(node_id, "Node")

    def check_create(self, parent_id: Any, target_id: Any) -> None:
        self._require_full(self._parent_id(parent_id), "Parent")
        if target_id:
            self._require_known(target_id, "Sibling")

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
        # changes which ancestors are stubs.
        candidates.update(node_id for node_id, kind in self.held.items() if kind == "stub")
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
        # A simulation of what the client has as each operation lands.
        present: set[str] = set(old)
        placed: set[str] = set(self.placed)
        emitted: set[str] = set()
        entered_state: dict[str, dict[str, Any]] = {}
        # Nodes still waiting for their move are in their old place on
        # the client, so they cannot serve as neighbors until moved.
        pending_moves: set[str] = set()

        def kind_after(node_id: str) -> Kind | None:
            if node_id in new_kinds:
                return new_kinds[node_id]
            return old.get(node_id)

        def parent_ref(node: AtomNode) -> Any:
            parent = node._parent
            assert parent is not None
            return 0 if parent is doc.root else parent.id

        def neighbors(node: AtomNode) -> tuple[Any, Any]:
            prev = node._prev_sibling
            while prev is not None and not (prev.id in placed and prev.id not in pending_moves):
                prev = prev._prev_sibling
            nxt = node._next_sibling
            while nxt is not None and not (nxt.id in placed and nxt.id not in pending_moves):
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
                        pending_moves.discard(member.id)
                    run.clear()
                    run_nodes.clear()

                child: AtomNode | None = node._slot_first.get(slot_name)
                while child is not None:
                    kind = kind_after(child.id)
                    if kind is None:
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

        # 1. Real deletes of held nodes, top-most first: the client drops
        # the subtree with the node.
        deleted_held = [node_id for node_id in deleted if node_id in present]
        for node_id in deleted_held:
            ancestor = deleted[node_id]._parent
            covered = False
            while ancestor is not None:
                if ancestor.id in deleted and ancestor.id in present:
                    covered = True
                    break
                ancestor = ancestor._parent
            if not covered:
                ordered.append([1, node_id, 0])
        for node_id in deleted_held:
            present.discard(node_id)
            placed.discard(node_id)

        # 2. Nodes that appear, enter in full, or take a place in the
        # tree, parents before children.
        arriving: list[AtomNode] = []
        for node_id, kind in new_kinds.items():
            if kind is None:
                continue
            node = doc.get_node_by_id(node_id)
            if node is None:
                continue
            was = old.get(node_id)
            parent = node._parent
            parent_held = node is doc.root or (
                parent is not None and kind_after(parent.id) is not None
            )
            if (
                was is None
                or (was == "stub" and kind == "full")
                or (node_id not in placed and parent_held)
            ):
                arriving.append(node)
        arriving.sort(key=_order_key)
        for node in arriving:
            if node.id in emitted:
                continue
            kind = kind_after(node.id)
            if node is doc.root:
                # The root is always placed; gaining it in full (an
                # anchor on the root, set by a scope change) fills it.
                if kind == "full" and old.get(node.id) != "full":
                    ordered.append([0, [_full_pair(node)], 0, "", 0, 0])
                    entered_state[node.id] = node._state_to_json_plain()
                    emitted.add(node.id)
                    place_children(node)
                continue
            parent = node._parent
            if parent is None or parent.id not in present:
                # Its parent arrives first and places it from there, or
                # it is a detached stub.
                if kind == "stub" and node.id not in present:
                    ordered.append([OP_DETACHED, node.id, node._node_type])
                    present.add(node.id)
                    emitted.add(node.id)
                continue
            if kind == "stub" and node.id in placed:
                continue
            prev, nxt = neighbors(node)
            pair = _full_pair(node) if kind == "full" else _stub_pair(node)
            ordered.append([0, [pair], parent_ref(node), node._slot_name, prev, nxt])
            present.add(node.id)
            placed.add(node.id)
            emitted.add(node.id)
            pending_moves.discard(node.id)
            if kind == "full":
                entered_state[node.id] = node._state_to_json_plain()
                place_children(node)

        # 3. Moves of nodes the client keeps, into parents it has.
        movers: list[AtomNode] = []
        for node_id in moved:
            if node_id in emitted or node_id not in placed:
                continue
            node = doc.get_node_by_id(node_id)
            if node is None or kind_after(node_id) is None:
                continue
            parent = node._parent
            if parent is None or parent.id not in present:
                continue
            movers.append(node)
            pending_moves.add(node_id)
        movers.sort(key=_order_key)
        for node in movers:
            prev, nxt = neighbors(node)
            pending_moves.discard(node.id)
            ordered.append([2, node.id, 0, parent_ref(node), node._slot_name, prev, nxt])
            emitted.add(node.id)

        # 4. Nodes that leave the view or become stubs, top-most first.
        leaving: list[tuple[Any, str]] = []
        for node_id, kind in new_kinds.items():
            was = old.get(node_id)
            if was is None or node_id in deleted:
                continue
            node = doc.get_node_by_id(node_id)
            if node is None:
                continue
            parent = node._parent
            parent_present = node is doc.root or (parent is not None and parent.id in present)
            if (
                kind is None
                or (was == "full" and kind == "stub")
                or (node_id in placed and not parent_present)
            ):
                leaving.append((_order_key(node), node_id))
        leaving.sort()
        dropped: set[str] = set()
        for _, node_id in leaving:
            node = doc.get_node_by_id(node_id)
            assert node is not None
            kind = kind_after(node_id)
            under_dropped = False
            if node_id in placed:
                ancestor = node._parent
                while ancestor is not None:
                    if ancestor.id in dropped:
                        under_dropped = True
                        break
                    ancestor = ancestor._parent
            parent = node._parent
            parent_present = node is doc.root or (parent is not None and parent.id in present)
            if under_dropped:
                # Gone from the client with its ancestor; a node still
                # referenced comes back detached.
                placed.discard(node_id)
                present.discard(node_id)
                dropped.add(node_id)
                if kind == "stub":
                    ordered.append([OP_DETACHED, node_id, node._node_type])
                    present.add(node_id)
            elif kind is None:
                ordered.append([OP_EXIT, node_id])
                present.discard(node_id)
                placed.discard(node_id)
                dropped.add(node_id)
            elif parent_present and node_id in placed:
                # Its state goes; children that leave get their own ops.
                ordered.append([OP_STUB, node_id])
            elif node_id in placed:
                ordered.append([OP_EXIT, node_id])
                ordered.append([OP_DETACHED, node_id, node._node_type])
                placed.discard(node_id)
                dropped.add(node_id)

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
            else:
                self.held[node_id] = kind
        for node_id in deleted:
            self.held.pop(node_id, None)
        self._recompute_placed()

        if not ordered and not state:
            return None
        return {"ordered": ordered, "state": state}
