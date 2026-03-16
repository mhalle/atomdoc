"""Operation tracking with named slot support."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._range import _descendants

if TYPE_CHECKING:
    from ._doc import Doc
    from ._node import DocNode
    from ._types import Operations


def _is_obj_empty(d: dict[str, Any]) -> bool:
    return len(d) == 0


# --- State ops ---


def on_set_state_inverse(doc: Doc, node: DocNode, key: str) -> None:
    if doc._diff.inserted.__contains__(node.id):
        return
    inv_patch = doc._inverse_operations[1]
    if node.id in inv_patch and key in inv_patch[node.id]:
        return
    original = node._stringify_state_key(key)
    inv_patch.setdefault(node.id, {})[key] = original


def on_set_state_forward(doc: Doc, node: DocNode, key: str) -> None:
    state_patches = doc._operations[1]
    value_string = node._stringify_state_key(key)

    prev_value_string = doc._inverse_operations[1].get(node.id, {}).get(key)
    node_patch = state_patches.get(node.id)

    if prev_value_string == value_string and node_patch is not None:
        node_patch.pop(key, None)
        if _is_obj_empty(node_patch):
            del state_patches[node.id]
            doc._diff.updated.discard(node.id)
        inv_node = doc._inverse_operations[1].get(node.id)
        if inv_node is not None:
            inv_node.pop(key, None)
    else:
        state_patches.setdefault(node.id, {})[key] = value_string
        if node.id not in doc._diff.inserted:
            doc._diff.updated.add(node.id)


# --- Insert ops ---


def _copy_inserted_to_diff(doc: Doc, node: DocNode) -> None:
    diff = doc._diff
    was_deleted = node.id in diff.deleted
    if was_deleted:
        del diff.deleted[node.id]
        diff.moved.add(node.id)
        diff.updated.add(node.id)
    else:
        diff.inserted.add(node.id)

    json_state = node._state_to_json()
    if json_state:
        doc._operations[1][node.id] = json_state


def _get_slot_children_list(node: DocNode, slot_name: str) -> list[DocNode]:
    result: list[DocNode] = []
    child = node._slot_first.get(slot_name)
    while child is not None:
        result.append(child)
        child = child._next_sibling
    return result


def on_insert_range(
    doc: Doc,
    parent: DocNode,
    slot_name: str,
    position: str,
    nodes: list[DocNode],
) -> None:
    """Record insert operations with slot name."""
    new_prev: DocNode | None = None
    new_next: DocNode | None = None

    if position == "append":
        new_prev = parent._slot_last.get(slot_name)
    elif position == "before":
        # Target is the first node we're inserting before — passed via nodes context
        # For before/after, the target info comes from the calling context
        # In this case, parent is the actual parent, and we use the nodes' position
        pass
    else:
        return

    diff = doc._diff
    root = doc.root

    doc._operations[0].append((
        0,
        [(n.id, n._node_type) for n in nodes],
        0 if parent is root else parent.id,
        slot_name,
        new_prev.id if new_prev else 0,
        new_next.id if new_next else 0,
    ))

    if parent.id not in diff.inserted:
        doc._inverse_operations[0].append((
            1,
            nodes[0].id,
            nodes[-1].id if len(nodes) > 1 else 0,
        ))

    for top_node in nodes:
        _copy_inserted_to_diff(doc, top_node)
        for desc in _descendants(top_node):
            _copy_inserted_to_diff(doc, desc)
            desc_parent = desc._parent
            desc_slot = desc._slot_name
            if desc_parent is not None and desc_slot is not None and desc._prev_sibling is None:
                children = _get_slot_children_list(desc_parent, desc_slot)
                doc._operations[0].append((
                    0,
                    [(c.id, c._node_type) for c in children],
                    desc_parent.id,
                    desc_slot,
                    0,
                    0,
                ))
                if desc_parent.id not in diff.inserted:
                    last = desc_parent._slot_last.get(desc_slot)
                    doc._inverse_operations[0].append((
                        1,
                        desc.id,
                        last.id if last is not None and last is not desc else 0,
                    ))


def on_insert_range_before(
    doc: Doc,
    target: DocNode,
    slot_name: str,
    nodes: list[DocNode],
) -> None:
    """Record insert-before operations with slot name."""
    parent = target._parent
    assert parent is not None
    diff = doc._diff
    root = doc.root

    doc._operations[0].append((
        0,
        [(n.id, n._node_type) for n in nodes],
        0 if parent is root else parent.id,
        slot_name,
        target._prev_sibling.id if target._prev_sibling else 0,
        target.id,
    ))

    if parent.id not in diff.inserted:
        doc._inverse_operations[0].append((
            1,
            nodes[0].id,
            nodes[-1].id if len(nodes) > 1 else 0,
        ))

    for top_node in nodes:
        _copy_inserted_to_diff(doc, top_node)
        for desc in _descendants(top_node):
            _copy_inserted_to_diff(doc, desc)
            desc_parent = desc._parent
            desc_slot = desc._slot_name
            if desc_parent is not None and desc_slot is not None and desc._prev_sibling is None:
                children = _get_slot_children_list(desc_parent, desc_slot)
                doc._operations[0].append((
                    0,
                    [(c.id, c._node_type) for c in children],
                    desc_parent.id,
                    desc_slot,
                    0,
                    0,
                ))
                if desc_parent.id not in diff.inserted:
                    last = desc_parent._slot_last.get(desc_slot)
                    doc._inverse_operations[0].append((
                        1,
                        desc.id,
                        last.id if last is not None and last is not desc else 0,
                    ))


# --- Delete ops ---


def _copy_deleted_to_diff(doc: Doc, node: DocNode) -> None:
    diff = doc._diff
    was_inserted = node.id in diff.inserted
    if was_inserted:
        diff.inserted.discard(node.id)
        diff.moved.discard(node.id)
    else:
        inv_patch = doc._inverse_operations[1]
        current_state = node._state_to_json()
        prev_inv = inv_patch.get(node.id, {})
        merged = {**current_state, **prev_inv}
        inv_patch[node.id] = merged
        diff.deleted[node.id] = node


def on_delete_range(doc: Doc, start_node: DocNode, end_node: DocNode) -> None:
    from ._range import _detach_range, _iter_range, _descendants_inclusive

    operations = doc._operations[0]
    inverse_operations = doc._inverse_operations[0]
    temp_inverse: list[object] = []
    parent = start_node._parent
    slot_name = start_node._slot_name
    assert parent is not None
    assert slot_name is not None

    operations.append((
        1,
        start_node.id,
        end_node.id if end_node is not start_node else 0,
    ))

    json_nodes: list[tuple[str, str]] = []
    for node in _iter_range(start_node, end_node):
        json_nodes.append((node.id, node._node_type))
        _copy_deleted_to_diff(doc, node)

    should_add_inverse = parent.id not in doc._diff.inserted
    if should_add_inverse:
        temp_inverse.append((
            0,
            json_nodes,
            0 if parent is doc.root else parent.id,
            slot_name,
            start_node._prev_sibling.id if start_node._prev_sibling else 0,
            end_node._next_sibling.id if end_node._next_sibling else 0,
        ))

    _detach_range(start_node, end_node)

    for node in _iter_range(start_node, end_node):
        for desc in _descendants_inclusive(node):
            doc._operations[1].pop(desc.id, None)
            doc._diff.updated.discard(desc.id)
            # Check all slots for children
            for desc_slot in desc._slot_order:
                if desc._slot_first.get(desc_slot) is not None:
                    child_json: list[tuple[str, str]] = []
                    child: DocNode | None = desc._slot_first.get(desc_slot)
                    while child is not None:
                        _copy_deleted_to_diff(doc, child)
                        child_json.append((child.id, child._node_type))
                        child = child._next_sibling
                    if should_add_inverse:
                        temp_inverse.append((0, child_json, desc.id, desc_slot, 0, 0))

    temp_inverse.reverse()
    inverse_operations.extend(temp_inverse)  # type: ignore[arg-type]


# --- Move ops ---


def on_move_range(
    doc: Doc,
    start_node: DocNode,
    end_node: DocNode,
    new_parent: DocNode,
    new_slot: str,
    new_prev: DocNode | None,
    new_next: DocNode | None,
) -> None:
    from ._range import _iter_range

    end_id: str | int = 0 if end_node is start_node else end_node.id
    root = doc.root

    doc._operations[0].append((
        2,
        start_node.id,
        end_id,
        0 if new_parent is root else new_parent.id,
        new_slot,
        new_prev.id if new_prev else 0,
        new_next.id if new_next else 0,
    ))

    current_parent = start_node._parent
    current_slot = start_node._slot_name or ""
    assert current_parent is not None
    current_prev = start_node._prev_sibling
    current_next = end_node._next_sibling

    doc._inverse_operations[0].append((
        2,
        start_node.id,
        end_id,
        0 if current_parent is root else current_parent.id,
        current_slot,
        current_prev.id if current_prev else 0,
        current_next.id if current_next else 0,
    ))

    for node in _iter_range(start_node, end_node):
        if node.id not in doc._diff.inserted:
            doc._diff.moved.add(node.id)


# --- Apply remote operations ---


def on_apply_operations(doc: Doc, operations: Operations) -> None:
    from typing import Any as _Any
    ordered_op: _Any
    for ordered_op in operations[0]:
        op = ordered_op
        if op[0] == 0:
            # Insert: (0, nodes, parent_id, slot_name, prev_id, next_id)
            nodes = [
                doc._create_node_from_json([nid, ntype, {}])
                for nid, ntype in op[1]
            ]
            parent_id = op[2]
            slot_name = op[3]
            prev_id = op[4]
            next_id = op[5]

            parent = doc.get_node_by_id(str(parent_id)) if parent_id else doc.root
            if parent is None:
                continue

            if prev_id:
                prev = doc.get_node_by_id(str(prev_id))
                if prev:
                    doc._insert_into_slot(parent, slot_name, "after", nodes, target=prev)
                    continue
            if next_id:
                nxt = doc.get_node_by_id(str(next_id))
                if nxt:
                    doc._insert_into_slot(parent, slot_name, "before", nodes, target=nxt)
                    continue
            doc._insert_into_slot(parent, slot_name, "append", nodes)

        elif op[0] == 1:
            # Delete: (1, start_id, end_id)
            try:
                start = doc.get_node_by_id(op[1])
                end_id = op[2] or op[1]
                end = doc.get_node_by_id(str(end_id))
                if start and end:
                    start.to(end).delete()
            except Exception:
                pass

        elif op[0] == 2:
            # Move: (2, start_id, end_id, parent_id, slot_name, prev_id, next_id)
            start = doc.get_node_by_id(op[1])
            end_id = op[2] or op[1]
            end = doc.get_node_by_id(str(end_id))
            if not start or not end:
                continue
            try:
                parent_id = op[3]
                slot_name = op[4]
                parent = doc.get_node_by_id(str(parent_id)) if parent_id else doc.root
                if parent:
                    start.to(end).move(parent, slot_name, "append")
            except Exception:
                pass

    # Apply state patches
    to_apply = operations[1]
    current_patch = doc._operations[1]
    current_inv_patch = doc._inverse_operations[1]

    for node_id, patches in to_apply.items():
        node = doc.get_node_by_id(node_id)
        if node is None:
            continue
        current_patch[node_id] = {**current_patch.get(node_id, {}), **patches}
        if node_id not in doc._diff.inserted:
            doc._diff.updated.add(node_id)
        inserted_same_tx = node_id in doc._diff.inserted
        for key, str_val in patches.items():
            if not inserted_same_tx:
                if not current_inv_patch.get(node_id, {}).get(key):
                    original = node._stringify_state_key(key)
                    current_inv_patch.setdefault(node_id, {}).setdefault(key, original)
            node._state[key] = node._parse_state_key(key, str_val)


# --- Trigger listeners ---


def maybe_trigger_listeners(doc: Doc) -> None:
    def has_changes() -> bool:
        diff = doc._diff
        return bool(
            diff.inserted
            or diff.deleted
            or diff.moved
            or doc._operations[1]
        )

    if not has_changes():
        return

    doc._lifecycle_stage = "normalize"
    for listener in list(doc._normalize_listeners):
        listener(doc._diff)

    if doc._strict_mode:
        doc._lifecycle_stage = "normalize2"
        for listener in list(doc._normalize_listeners):
            listener(doc._diff)

    if not has_changes():
        return

    from ._types import ChangeEvent

    doc._lifecycle_stage = "change"
    event = ChangeEvent(
        operations=doc._operations,
        inverse_operations=doc._inverse_operations,
        diff=doc._diff,
    )
    for change_listener in list(doc._change_listeners):
        change_listener(event)
