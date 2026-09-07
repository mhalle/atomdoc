"""Regression tests for the second adversarial review (document core)."""

import pytest
from pydantic import BaseModel

from atomdoc import Array, Doc, Handle, UndoManagerConfig, node


@node
class Item:
    count: int = 0
    name: str = "d"
    kids: Array["Item"] = []


@node
class Other:
    x: int = 0


@node
class Root:
    tags: list[str] = []
    items: Array[Item] = []


def make_doc():
    doc = Doc(Root, undo_manager=UndoManagerConfig(max_steps=10))
    with doc.transaction():
        for i in range(3):
            doc.root.items.append(doc.create_node(Item, count=i))
    return doc


# --- handles survive undo and rollback ---


def test_handle_survives_undo_of_delete():
    doc = make_doc()
    it = doc.root.items[1]
    it.delete()
    doc.undo_manager.undo()
    assert doc.get_node_by_id(it.id) is it
    it.count = 99
    assert doc.get_node_by_id(it.id).count == 99
    assert [n[2].get("count", 0) for n in doc.dump()[3]["items"]][1] == 99


def test_handle_survives_transaction_rollback():
    doc = make_doc()
    it = doc.root.items[1]
    with pytest.raises(RuntimeError):
        with doc.transaction():
            it.delete()
            raise RuntimeError("boom")
    assert doc.get_node_by_id(it.id) is it
    assert it._parent is doc.root


def test_delete_through_stale_handle_after_undo_keeps_tree_consistent():
    doc = make_doc()
    a, b, c = list(doc.root.items)
    b.delete()
    doc.undo_manager.undo()
    a.delete()
    b.delete()
    assert [n.id for n in doc.root.items] == [c.id]
    for entry in doc.dump()[3]["items"]:
        assert doc.get_node_by_id(entry[0]) is not None


def test_stale_handle_from_another_object_is_rejected():
    doc = make_doc()
    it = doc.root.items[1]
    other = Doc(Root)
    with pytest.raises(RuntimeError, match="not in the document"):
        other._check_live(it)


# --- nested transaction failure ---


def test_failing_nested_apply_aborts_enclosing_transaction():
    doc = make_doc()
    a = doc.root.items[0]
    events = []
    doc.on_change(events.append)
    steps = len(doc.undo_manager._undo_stack)
    with pytest.raises(RuntimeError):
        with doc.transaction():
            a.count = 11
            doc.apply_operations(([(0, [(a.id, "Item")], 0, "items", 0, 0)], {}))
            a.count = 22
    assert a.count == 0
    assert doc._lifecycle_stage == "idle"
    assert events == []
    assert len(doc.undo_manager._undo_stack) == steps


def test_caught_nested_failure_leaves_outer_transaction_intact():
    doc = make_doc()
    a, b, c = list(doc.root.items)
    with doc.transaction():
        a.count = 11
        with pytest.raises(ValueError):
            b.to(a).delete()  # backwards range
        a.count = 22
    assert a.count == 22
    assert [n.id for n in doc.root.items] == [a.id, b.id, c.id]
    assert doc.undo_manager.can_undo
    doc.undo_manager.undo()
    assert a.count == 0


def test_one_operation_set_is_atomic():
    doc = make_doc()
    x, y, _ = list(doc.root.items)
    before = doc.dump()
    events = []
    doc.on_change(events.append)
    ops = (
        [
            (0, [("aaa", "Item")], 0, "items", 0, 0),
            (1, y.id, x.id),
            (0, [("bbb", "Item")], 0, "items", 0, 0),
        ],
        {"aaa": {"count": 7}, "bbb": {"count": 8}},
    )
    # Strict: the bad delete fails the whole entry, nothing is applied.
    with pytest.raises(ValueError):
        doc.apply_operations(ops, strict=True)
    assert doc.dump() == before
    assert events == []
    # Lenient: the bad delete is skipped, the rest applies in one commit.
    assert doc.apply_operations(ops) == []
    assert [n[0] for n in doc.dump()[3]["items"]][-2:] == ["aaa", "bbb"]
    assert len(events) == 1


def test_empty_journal_with_skip_undo_does_not_commit_callers_transaction():
    doc = make_doc()
    a = doc.root.items[0]
    with doc.transaction():
        a.count = 5
        doc.apply_operations([], skip_undo=True)
        assert doc._lifecycle_stage == "update"
    assert a.count == 5


# --- root defaults ---


def test_root_gets_fresh_mutable_defaults():
    d1 = Doc(Root)
    d1.root.tags.append("polluted")
    assert Doc(Root).root.tags == []
    assert Root._field_defaults["tags"] == []
    assert d1.dump()[2]["tags"] == ["polluted"]


# --- strict null and unknown keys / opcodes ---


def test_null_for_typed_field_is_rejected():
    doc = make_doc()
    it = doc.root.items[0]
    with pytest.raises(Exception):
        doc.apply_operations(([], {it.id: {"name": None}}), strict=True)
    assert it.name == "d"


def test_unknown_state_key_is_rejected():
    doc = make_doc()
    it = doc.root.items[0]
    with pytest.raises(ValueError, match="no field"):
        doc.apply_operations(([], {it.id: {"__evil__": 1}}), raise_on_error=True)
    assert "__evil__" not in it._state
    with pytest.raises(TypeError, match="no field"):
        doc.create_node(Item, nope=3)
    with pytest.raises(TypeError, match="no field"):
        Item(nope=3)


def test_unknown_opcode_is_rejected():
    doc = make_doc()
    with pytest.raises(ValueError, match="Unknown operation code"):
        doc.apply_operations(([(99, "x", 0)], {}), raise_on_error=True)


# --- slot allowed types ---


def test_slot_allowed_type_enforced():
    @node
    class Mixed:
        items: Array[Item] = []
        others: Array[Other] = []

    doc = Doc(Mixed)
    o = doc.create_node(Other)
    with pytest.raises(TypeError, match="accepts Item"):
        doc.root.items.append(o)
    doc.root.others.append(o)
    with pytest.raises(TypeError, match="accepts Item"):
        o.move(doc.root, "items")
    assert [n.id for n in doc.root.others] == [o.id]
    with pytest.raises(TypeError):
        doc.apply_operations(
            ([(0, [("zzz", "Other")], 0, "items", 0, 0)], {}), raise_on_error=True
        )


# --- discovery ---


def test_duplicate_node_type_in_discovery_raises():
    @node("shape")
    class Circle:
        r: float = 1.0

    @node("shape")
    class Square:
        s: float = 1.0

    @node
    class Canvas:
        circles: Array[Circle] = []
        squares: Array[Square] = []

    with pytest.raises(ValueError, match="Duplicate node type 'shape'"):
        Doc(Canvas)


# --- change event isolation and undo bookkeeping ---


def test_failed_listener_leaves_no_phantom_undo_entry_and_event_intact():
    doc = make_doc()
    recorded = []
    doc.on_change(recorded.append)
    doc.on_change(lambda e: 1 / 0)
    before = doc.dump()
    steps = len(doc.undo_manager._undo_stack)
    with pytest.raises(ZeroDivisionError):
        with doc.transaction():
            doc.root.items.append(doc.create_node(Item, count=5))
    assert doc.dump() == before
    assert len(doc.undo_manager._undo_stack) == steps
    event = recorded[-1]
    assert len(event.operations[0]) == 1 and event.operations[0][0][0] == 0
    assert len(event.diff.inserted) == 1


def test_failed_listener_during_undo_keeps_redo_stack_clean():
    doc = make_doc()
    a = doc.root.items[0]
    a.count = 1
    fail = {"on": False}

    def listener(e):
        if fail["on"]:
            raise RuntimeError("listener")

    doc.on_change(listener)
    fail["on"] = True
    with pytest.raises(RuntimeError):
        doc.undo_manager.undo()
    assert a.count == 1
    assert doc.undo_manager.can_undo
    assert not doc.undo_manager.can_redo


# --- nested handles ---


class Thumb(Handle):
    pass


class Voxels(Handle):
    strength = "strong"


class Material(BaseModel, frozen=True):
    texture: Thumb | None = None
    voxels: Voxels | None = None


@node
class Vol:
    voxels: Voxels | None = None
    material: Material | None = None
    extra: list[Voxels] = []


@node
class VolRoot:
    vols: Array[Vol] = []


def test_nested_handles_are_visible():
    doc = Doc(VolRoot)
    v = doc.create_node(
        Vol,
        voxels=Voxels(uri="file://direct"),
        material=Material(voxels=Voxels(uri="file://nested")),
        extra=[Voxels(uri="file://listed")],
    )
    doc.root.vols.append(v)
    uris = sorted(h.uri for _, _, h in doc.handles(strength="strong"))
    assert uris == ["file://direct", "file://listed", "file://nested"]
    schema = doc.atomdoc_schema()
    assert set(schema["value_types"]) >= {"Material", "Voxels", "Thumb"}
    assert "voxels" in schema["node_types"]["Vol"]["handles"]
