"""Tests for UndoManager."""

import pytest

from atomdoc import Doc, DocNode, UndoManager


class UndoNode(DocNode, node_type="undo_node"):
    value: str = ""


@pytest.fixture
def doc():
    return Doc(root_type="undo_node", nodes=[UndoNode])


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
        n = doc.create_node(UndoNode, value="child")
        doc.root.append(n)

    assert len(doc.root.children) == 1

    undo.undo()
    assert len(doc.root.children) == 0


def test_undo_delete(doc):
    with doc.transaction():
        n = doc.create_node(UndoNode, value="child")
        doc.root.append(n)

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
