"""Tests for UndoManager."""

import pytest

from atomdoc import Doc, Array, node, UndoManager


@node
class UndoChild:
    value: str = ""


@node
class UndoRoot:
    value: str = ""
    children: Array[UndoChild] = []


@pytest.fixture
def doc():
    return Doc(root_type="UndoRoot", nodes=[UndoRoot, UndoChild])


def test_undo_state_change(doc):
    undo = UndoManager(doc)

    with doc.transaction():
        doc.root.value = "hello"

    assert doc.root.value == "hello"
    assert undo.can_undo

    undo.undo()
    assert doc.root.value == ""
    assert not undo.can_undo


def test_redo(doc):
    undo = UndoManager(doc)

    with doc.transaction():
        doc.root.value = "hello"

    undo.undo()
    assert doc.root.value == ""
    assert undo.can_redo

    undo.redo()
    assert doc.root.value == "hello"
    assert not undo.can_redo


def test_undo_insert(doc):
    undo = UndoManager(doc)

    with doc.transaction():
        n = doc.create_node(UndoChild, value="child")
        doc.root.children.append(n)

    assert len(doc.root.children) == 1

    undo.undo()
    assert len(doc.root.children) == 0


def test_undo_delete(doc):
    with doc.transaction():
        n = doc.create_node(UndoChild, value="child")
        doc.root.children.append(n)

    undo = UndoManager(doc)

    with doc.transaction():
        n.delete()

    assert len(doc.root.children) == 0

    undo.undo()
    assert len(doc.root.children) == 1


def test_max_steps(doc):
    undo = UndoManager(doc, max_steps=3)

    for i in range(5):
        with doc.transaction():
            doc.root.value = f"v{i}"

    # Only 3 undos possible
    count = 0
    while undo.can_undo:
        undo.undo()
        count += 1
    assert count == 3


def test_redo_cleared_on_new_change(doc):
    undo = UndoManager(doc)

    with doc.transaction():
        doc.root.value = "a"
    with doc.transaction():
        doc.root.value = "b"

    undo.undo()
    assert undo.can_redo

    with doc.transaction():
        doc.root.value = "c"

    assert not undo.can_redo
