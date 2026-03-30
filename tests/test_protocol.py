"""Tests for wire protocol serialization."""

from atomdoc._protocol import operations_from_wire, operations_to_wire


def test_operations_round_trip_empty():
    ops = ([], {})
    wire = operations_to_wire(ops)
    assert wire == {"ordered": [], "state": {}}
    back = operations_from_wire(wire)
    assert back == ops


def test_operations_round_trip_insert():
    insert_op = (0, [("id1", "annotation")], "parent_id", "children", 0, 0)
    ops = ([insert_op], {})
    wire = operations_to_wire(ops)
    assert wire["ordered"] == [[0, [("id1", "annotation")], "parent_id", "children", 0, 0]]
    back = operations_from_wire(wire)
    assert back[0][0] == insert_op
    assert back[1] == {}


def test_operations_round_trip_delete():
    delete_op = (1, "start_id", "end_id")
    ops = ([delete_op], {})
    wire = operations_to_wire(ops)
    back = operations_from_wire(wire)
    assert back[0][0] == delete_op


def test_operations_round_trip_move():
    move_op = (2, "start_id", 0, "parent_id", "slot", 0, "next_id")
    ops = ([move_op], {})
    wire = operations_to_wire(ops)
    back = operations_from_wire(wire)
    assert back[0][0] == move_op


def test_operations_round_trip_state_patch():
    ops = ([], {"node1": {"title": '"hello"', "count": "42"}})
    wire = operations_to_wire(ops)
    assert wire["state"] == {"node1": {"title": '"hello"', "count": "42"}}
    back = operations_from_wire(wire)
    assert back[1] == ops[1]


def test_operations_round_trip_mixed():
    insert = (0, [("id1", "ann")], "p", "children", 0, 0)
    delete = (1, "x", 0)
    state = {"n1": {"label": '"test"'}}
    ops = ([insert, delete], state)
    wire = operations_to_wire(ops)
    back = operations_from_wire(wire)
    assert len(back[0]) == 2
    assert back[0][0] == insert
    assert back[0][1] == delete
    assert back[1] == state


def test_operations_from_wire_missing_fields():
    back = operations_from_wire({})
    assert back == ([], {})
