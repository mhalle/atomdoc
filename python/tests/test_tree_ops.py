"""Tests for tree operations — append, prepend, insert, delete, move."""

import pytest

from atomdoc import Doc, Array, node


@node
class LeafTree:
    label: str = ""
    children: Array["LeafTree"] = []


@node
class RootTree:
    title: str = ""
    children: Array[LeafTree] = []


@pytest.fixture
def doc():
    return Doc(root_type="RootTree", nodes=[RootTree, LeafTree])


def test_append(doc):
    with doc.transaction():
        a = doc.create_node(LeafTree, label="a")
        b = doc.create_node(LeafTree, label="b")
        doc.root.children.append(a)
        doc.root.children.append(b)
    assert [c.label for c in doc.root.children] == ["a", "b"]


def test_prepend(doc):
    with doc.transaction():
        a = doc.create_node(LeafTree, label="a")
        b = doc.create_node(LeafTree, label="b")
        doc.root.children.append(a)
        doc.root.children.prepend(b)
    assert [c.label for c in doc.root.children] == ["b", "a"]


def test_insert_before(doc):
    with doc.transaction():
        a = doc.create_node(LeafTree, label="a")
        c = doc.create_node(LeafTree, label="c")
        doc.root.children.append(a, c)
        b = doc.create_node(LeafTree, label="b")
        c.insert_before(b)
    assert [c.label for c in doc.root.children] == ["a", "b", "c"]


def test_insert_after(doc):
    with doc.transaction():
        a = doc.create_node(LeafTree, label="a")
        c = doc.create_node(LeafTree, label="c")
        doc.root.children.append(a, c)
        b = doc.create_node(LeafTree, label="b")
        a.insert_after(b)
    assert [c.label for c in doc.root.children] == ["a", "b", "c"]


def test_delete(doc):
    with doc.transaction():
        a = doc.create_node(LeafTree, label="a")
        b = doc.create_node(LeafTree, label="b")
        c = doc.create_node(LeafTree, label="c")
        doc.root.children.append(a, b, c)
    with doc.transaction():
        b.delete()
    assert [c.label for c in doc.root.children] == ["a", "c"]


def test_delete_root_raises(doc):
    with pytest.raises(RuntimeError, match="Root"):
        doc.root.delete()


def test_delete_children(doc):
    with doc.transaction():
        a = doc.create_node(LeafTree, label="a")
        b = doc.create_node(LeafTree, label="b")
        doc.root.children.append(a, b)
    with doc.transaction():
        doc.root.children.clear()
    assert len(doc.root.children) == 0


def test_move_append(doc):
    with doc.transaction():
        a = doc.create_node(LeafTree, label="a")
        b = doc.create_node(LeafTree, label="b")
        parent2 = doc.create_node(LeafTree, label="p2")
        doc.root.children.append(a, b, parent2)
    with doc.transaction():
        a.move(parent2, "children")
    assert [c.label for c in doc.root.children] == ["b", "p2"]
    assert [c.label for c in parent2.children] == ["a"]


def test_parent_navigation(doc):
    with doc.transaction():
        a = doc.create_node(LeafTree, label="a")
        doc.root.children.append(a)
    assert doc.parent(a) is doc.root
    assert doc.parent(doc.root) is None


def test_sibling_navigation(doc):
    with doc.transaction():
        a = doc.create_node(LeafTree, label="a")
        b = doc.create_node(LeafTree, label="b")
        c = doc.create_node(LeafTree, label="c")
        doc.root.children.append(a, b, c)
    assert doc.next_sibling(a) is b
    assert doc.next_sibling(b) is c
    assert doc.next_sibling(c) is None
    assert doc.prev_sibling(c) is b
    assert doc.prev_sibling(b) is a
    assert doc.prev_sibling(a) is None


def test_ancestors(doc):
    with doc.transaction():
        a = doc.create_node(LeafTree, label="a")
        doc.root.children.append(a)
        b = doc.create_node(LeafTree, label="b")
        a.children.append(b)
    ancestors = list(doc.ancestors(b))
    assert ancestors == [a, doc.root]


def test_descendants(doc):
    with doc.transaction():
        a = doc.create_node(LeafTree, label="a")
        doc.root.children.append(a)
        b = doc.create_node(LeafTree, label="b")
        a.children.append(b)
    descs = list(doc.descendants(doc.root))
    assert len(descs) == 2
    assert descs[0] is a
    assert descs[1] is b


def test_range_delete(doc):
    with doc.transaction():
        a = doc.create_node(LeafTree, label="a")
        b = doc.create_node(LeafTree, label="b")
        c = doc.create_node(LeafTree, label="c")
        d = doc.create_node(LeafTree, label="d")
        doc.root.children.append(a, b, c, d)
    with doc.transaction():
        b.to(c).delete()
    assert [ch.label for ch in doc.root.children] == ["a", "d"]


def test_range_move(doc):
    with doc.transaction():
        a = doc.create_node(LeafTree, label="a")
        b = doc.create_node(LeafTree, label="b")
        c = doc.create_node(LeafTree, label="c")
        target = doc.create_node(LeafTree, label="target")
        doc.root.children.append(a, b, c, target)
    with doc.transaction():
        a.to(b).move(target, "children")
    assert [ch.label for ch in doc.root.children] == ["c", "target"]
    assert [ch.label for ch in target.children] == ["a", "b"]


def test_multiple_append_in_one_call(doc):
    with doc.transaction():
        a = doc.create_node(LeafTree, label="a")
        b = doc.create_node(LeafTree, label="b")
        c = doc.create_node(LeafTree, label="c")
        doc.root.children.append(a, b, c)
    assert [ch.label for ch in doc.root.children] == ["a", "b", "c"]
