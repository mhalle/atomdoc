"""Tests for tree operations — append, prepend, insert, delete, move."""

import pytest

from atomdoc import Doc, AtomNode


class RootNode(AtomNode, node_type="root_tree"):
    title: str = ""


class LeafNode(AtomNode, node_type="leaf_tree"):
    label: str = ""


@pytest.fixture
def doc():
    return Doc(root_type="root_tree", nodes=[RootNode, LeafNode])


def test_append(doc):
    with doc.transaction():
        a = doc.create_node(LeafNode, label="a")
        b = doc.create_node(LeafNode, label="b")
        doc.root.append(a)
        doc.root.append(b)
    assert [c.label for c in doc.root.children] == ["a", "b"]


def test_prepend(doc):
    with doc.transaction():
        a = doc.create_node(LeafNode, label="a")
        b = doc.create_node(LeafNode, label="b")
        doc.root.append(a)
        doc.root.prepend(b)
    assert [c.label for c in doc.root.children] == ["b", "a"]


def test_insert_before(doc):
    with doc.transaction():
        a = doc.create_node(LeafNode, label="a")
        c = doc.create_node(LeafNode, label="c")
        doc.root.append(a)
        doc.root.append(c)
        b = doc.create_node(LeafNode, label="b")
        c.insert_before(b)
    assert [c.label for c in doc.root.children] == ["a", "b", "c"]


def test_insert_after(doc):
    with doc.transaction():
        a = doc.create_node(LeafNode, label="a")
        c = doc.create_node(LeafNode, label="c")
        doc.root.append(a)
        doc.root.append(c)
        b = doc.create_node(LeafNode, label="b")
        a.insert_after(b)
    assert [c.label for c in doc.root.children] == ["a", "b", "c"]


def test_delete(doc):
    with doc.transaction():
        a = doc.create_node(LeafNode, label="a")
        b = doc.create_node(LeafNode, label="b")
        c = doc.create_node(LeafNode, label="c")
        doc.root.append(a, b, c)
    with doc.transaction():
        b.delete()
    assert [c.label for c in doc.root.children] == ["a", "c"]


def test_delete_root_raises(doc):
    with pytest.raises(RuntimeError, match="Root"):
        doc.root.delete()


def test_delete_children(doc):
    with doc.transaction():
        a = doc.create_node(LeafNode, label="a")
        b = doc.create_node(LeafNode, label="b")
        doc.root.append(a, b)
    with doc.transaction():
        doc.root.delete_children()
    assert len(doc.root.children) == 0


def test_move_append(doc):
    with doc.transaction():
        a = doc.create_node(LeafNode, label="a")
        b = doc.create_node(LeafNode, label="b")
        parent2 = doc.create_node(LeafNode, label="p2")
        doc.root.append(a, b, parent2)
    with doc.transaction():
        a.move(parent2, "append")
    assert [c.label for c in doc.root.children] == ["b", "p2"]
    assert [c.label for c in parent2.children] == ["a"]


def test_parent_navigation(doc):
    with doc.transaction():
        a = doc.create_node(LeafNode, label="a")
        doc.root.append(a)
    assert a.parent is doc.root
    assert doc.root.parent is None


def test_sibling_navigation(doc):
    with doc.transaction():
        a = doc.create_node(LeafNode, label="a")
        b = doc.create_node(LeafNode, label="b")
        c = doc.create_node(LeafNode, label="c")
        doc.root.append(a, b, c)
    assert a.next_sibling is b
    assert b.next_sibling is c
    assert c.next_sibling is None
    assert c.prev_sibling is b
    assert b.prev_sibling is a
    assert a.prev_sibling is None


def test_ancestors(doc):
    with doc.transaction():
        a = doc.create_node(LeafNode, label="a")
        doc.root.append(a)
        b = doc.create_node(LeafNode, label="b")
        a.append(b)
    ancestors = list(b.ancestors())
    assert ancestors == [a, doc.root]


def test_descendants(doc):
    with doc.transaction():
        a = doc.create_node(LeafNode, label="a")
        doc.root.append(a)
        b = doc.create_node(LeafNode, label="b")
        a.append(b)
    descs = list(doc.root.descendants())
    assert len(descs) == 2
    assert descs[0] is a
    assert descs[1] is b


def test_range_delete(doc):
    with doc.transaction():
        a = doc.create_node(LeafNode, label="a")
        b = doc.create_node(LeafNode, label="b")
        c = doc.create_node(LeafNode, label="c")
        d = doc.create_node(LeafNode, label="d")
        doc.root.append(a, b, c, d)
    with doc.transaction():
        b.to(c).delete()
    assert [ch.label for ch in doc.root.children] == ["a", "d"]


def test_range_move(doc):
    with doc.transaction():
        a = doc.create_node(LeafNode, label="a")
        b = doc.create_node(LeafNode, label="b")
        c = doc.create_node(LeafNode, label="c")
        target = doc.create_node(LeafNode, label="target")
        doc.root.append(a, b, c, target)
    with doc.transaction():
        a.to(b).move(target, "append")
    assert [ch.label for ch in doc.root.children] == ["c", "target"]
    assert [ch.label for ch in target.children] == ["a", "b"]


def test_multiple_append_in_one_call(doc):
    with doc.transaction():
        a = doc.create_node(LeafNode, label="a")
        b = doc.create_node(LeafNode, label="b")
        c = doc.create_node(LeafNode, label="c")
        doc.root.append(a, b, c)
    assert [ch.label for ch in doc.root.children] == ["a", "b", "c"]
