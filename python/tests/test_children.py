"""Tests for ChildrenView."""

import pytest

from atomdoc import Doc, Array, node


@node
class ChildCh:
    label: str = ""


@node
class ParentCh:
    title: str = ""
    children: Array[ChildCh] = []


@pytest.fixture
def doc():
    return Doc(root_type=ParentCh)


def test_empty_children(doc):
    assert len(doc.root.children) == 0
    assert not doc.root.children
    assert list(doc.root.children) == []


def test_children_len(doc):
    with doc.transaction():
        for i in range(3):
            c = doc.create_node(ChildCh, label=f"child{i}")
            doc.root.children.append(c)
    assert len(doc.root.children) == 3


def test_children_indexing(doc):
    with doc.transaction():
        for i in range(3):
            c = doc.create_node(ChildCh, label=f"child{i}")
            doc.root.children.append(c)
    assert doc.root.children[0].label == "child0"
    assert doc.root.children[1].label == "child1"
    assert doc.root.children[2].label == "child2"
    assert doc.root.children[-1].label == "child2"


def test_children_out_of_range(doc):
    with pytest.raises(IndexError):
        doc.root.children[0]


def test_children_slicing(doc):
    with doc.transaction():
        for i in range(4):
            c = doc.create_node(ChildCh, label=f"child{i}")
            doc.root.children.append(c)
    sliced = doc.root.children[1:3]
    assert len(sliced) == 2
    assert sliced[0].label == "child1"
    assert sliced[1].label == "child2"


def test_children_iteration(doc):
    with doc.transaction():
        for i in range(3):
            c = doc.create_node(ChildCh, label=f"child{i}")
            doc.root.children.append(c)
    labels = [c.label for c in doc.root.children]
    assert labels == ["child0", "child1", "child2"]


def test_children_truthiness(doc):
    assert not doc.root.children
    with doc.transaction():
        c = doc.create_node(ChildCh)
        doc.root.children.append(c)
    assert doc.root.children


def test_children_contains(doc):
    with doc.transaction():
        c = doc.create_node(ChildCh)
        doc.root.children.append(c)
    assert c in doc.root.children
    other = doc.create_node(ChildCh)
    assert other not in doc.root.children
