"""Tests for operation tracking."""

from atomdoc import Doc, Array, ChangeEvent, node


@node
class OpNode:
    value: str = ""
    children: Array["OpNode"] = []


def test_state_patch_recorded():
    doc = Doc(root_type="OpNode", nodes=[OpNode])
    events: list[ChangeEvent] = []
    doc.on_change(lambda ev: events.append(ev))

    with doc.transaction():
        doc.root.value = "hello"

    assert len(events) == 1
    patch = events[0].operations[1]
    assert doc.root.id in patch
    assert "value" in patch[doc.root.id]


def test_insert_op_recorded():
    doc = Doc(root_type="OpNode", nodes=[OpNode])
    events: list[ChangeEvent] = []
    doc.on_change(lambda ev: events.append(ev))

    with doc.transaction():
        n = doc.create_node(OpNode)
        doc.root.children.append(n)

    assert len(events) == 1
    ordered = events[0].operations[0]
    assert any(op[0] == 0 for op in ordered)  # insert op


def test_delete_op_recorded():
    doc = Doc(root_type="OpNode", nodes=[OpNode])
    with doc.transaction():
        n = doc.create_node(OpNode)
        doc.root.children.append(n)

    events: list[ChangeEvent] = []
    doc.on_change(lambda ev: events.append(ev))

    with doc.transaction():
        n.delete()

    assert len(events) == 1
    ordered = events[0].operations[0]
    assert any(op[0] == 1 for op in ordered)  # delete op


def test_diff_tracks_inserted():
    doc = Doc(root_type="OpNode", nodes=[OpNode])
    events: list[ChangeEvent] = []
    doc.on_change(lambda ev: events.append(ev))

    with doc.transaction():
        n = doc.create_node(OpNode)
        doc.root.children.append(n)

    assert n.id in events[0].diff.inserted


def test_diff_tracks_deleted():
    doc = Doc(root_type="OpNode", nodes=[OpNode])
    with doc.transaction():
        n = doc.create_node(OpNode)
        doc.root.children.append(n)

    events: list[ChangeEvent] = []
    doc.on_change(lambda ev: events.append(ev))

    nid = n.id
    with doc.transaction():
        n.delete()

    assert nid in events[0].diff.deleted


def test_diff_tracks_updated():
    doc = Doc(root_type="OpNode", nodes=[OpNode])
    events: list[ChangeEvent] = []
    doc.on_change(lambda ev: events.append(ev))

    with doc.transaction():
        doc.root.value = "changed"

    assert doc.root.id in events[0].diff.updated


def test_state_revert_removes_patch():
    doc = Doc(root_type="OpNode", nodes=[OpNode])
    events: list[ChangeEvent] = []
    doc.on_change(lambda ev: events.append(ev))

    with doc.transaction():
        doc.root.value = "temp"
        doc.root.value = ""  # revert

    # No change event if value reverted
    assert len(events) == 0
