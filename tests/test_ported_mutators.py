"""Tests ported from docnode/mutators.test.ts."""

import pytest

from atomdoc import Doc, AtomNode


class TextNode(AtomNode, node_type="text_m"):
    value: str = ""


def make_doc():
    return Doc(root_type="text_m", nodes=[TextNode])


def text(doc, *values):
    nodes = []
    for v in values:
        n = doc.create_node(TextNode, value=v)
        nodes.append(n)
    return nodes


def assert_doc(doc, expected):
    """Assert children labels match expected (flat, children of root only for simple cases)."""
    actual = [c.value for c in doc.root.children]
    assert actual == expected


def assert_tree(doc, expected):
    """Assert full tree structure with __ prefix for depth."""
    result = []
    def walk(node, depth=0):
        for child in node.children:
            prefix = "__" * depth
            result.append(f"{prefix}{child.value}")
            walk(child, depth + 1)
    walk(doc.root)
    assert result == expected


# --- base ---

class TestBase:
    def test_node_from_different_doc_should_throw(self):
        doc1 = make_doc()
        doc2 = make_doc()
        node1 = doc1.create_node(TextNode)
        with pytest.raises(RuntimeError, match="different document"):
            with doc2.transaction():
                doc2.root.append(node1)

    def test_duplicate_node_id_should_throw(self):
        doc = make_doc()
        node1 = doc.create_node(TextNode)
        with doc.transaction():
            doc.root.append(node1)
        with pytest.raises(RuntimeError, match="already exists"):
            with doc.transaction():
                doc.root.append(node1)


# --- append ---

class TestAppend:
    def test_append_single(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1"))
        assert_doc(doc, ["1"])

    def test_append_multiple(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3", "4"))
        assert_doc(doc, ["1", "2", "3", "4"])

    def test_append_to_child(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3", "4"))
            doc.root.children[1].append(*text(doc, "2.1", "2.2"))
        assert_tree(doc, ["1", "2", "__2.1", "__2.2", "3", "4"])

    def test_append_empty_does_nothing(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append()
        assert len(doc.root.children) == 0


# --- prepend ---

class TestPrepend:
    def test_prepend_single(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2"))
            doc.root.prepend(*text(doc, "0"))
        assert_doc(doc, ["0", "1", "2"])

    def test_prepend_multiple(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "3", "4"))
            doc.root.prepend(*text(doc, "1", "2"))
        assert_doc(doc, ["1", "2", "3", "4"])

    def test_prepend_to_empty(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.prepend(*text(doc, "1"))
        assert_doc(doc, ["1"])


# --- insertBefore ---

class TestInsertBefore:
    def test_before_middle(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "3"))
        with doc.transaction():
            doc.root.children[1].insert_before(*text(doc, "2"))
        assert_doc(doc, ["1", "2", "3"])

    def test_before_first(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "2", "3"))
        with doc.transaction():
            doc.root.children[0].insert_before(*text(doc, "1"))
        assert_doc(doc, ["1", "2", "3"])

    def test_before_multiple(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "4"))
        with doc.transaction():
            doc.root.children[1].insert_before(*text(doc, "2", "3"))
        assert_doc(doc, ["1", "2", "3", "4"])

    def test_insert_before_root_raises(self):
        doc = make_doc()
        with pytest.raises(RuntimeError, match="Root"):
            with doc.transaction():
                doc.root.insert_before(*text(doc, "x"))


# --- insertAfter ---

class TestInsertAfter:
    def test_after_middle(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "3"))
        with doc.transaction():
            doc.root.children[0].insert_after(*text(doc, "2"))
        assert_doc(doc, ["1", "2", "3"])

    def test_after_last(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2"))
        with doc.transaction():
            doc.root.children[-1].insert_after(*text(doc, "3"))
        assert_doc(doc, ["1", "2", "3"])

    def test_after_multiple(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "4"))
        with doc.transaction():
            doc.root.children[0].insert_after(*text(doc, "2", "3"))
        assert_doc(doc, ["1", "2", "3", "4"])

    def test_insert_after_root_raises(self):
        doc = make_doc()
        with pytest.raises(RuntimeError, match="Root"):
            with doc.transaction():
                doc.root.insert_after(*text(doc, "x"))


# --- delete ---

class TestDelete:
    def test_delete_first(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3", "4"))
        with doc.transaction():
            doc.root.children[0].delete()
        assert_doc(doc, ["2", "3", "4"])

    def test_delete_middle(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3", "4"))
        with doc.transaction():
            doc.root.children[1].delete()
        assert_doc(doc, ["1", "3", "4"])

    def test_delete_last(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3", "4"))
        with doc.transaction():
            doc.root.children[-1].delete()
        assert_doc(doc, ["1", "2", "3"])

    def test_delete_all(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3", "4"))
        with doc.transaction():
            doc.root.delete_children()
        assert len(doc.root.children) == 0

    def test_delete_root_raises(self):
        doc = make_doc()
        with pytest.raises(RuntimeError, match="Root"):
            doc.root.delete()

    def test_delete_with_descendants(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3"))
            doc.root.children[1].append(*text(doc, "2.1", "2.2"))
        with doc.transaction():
            doc.root.children[1].delete()
        assert_doc(doc, ["1", "3"])

    def test_delete_removes_from_node_map(self):
        doc = make_doc()
        with doc.transaction():
            n = doc.create_node(TextNode, value="x")
            doc.root.append(n)
        nid = n.id
        assert doc.get_node_by_id(nid) is n
        with doc.transaction():
            n.delete()
        assert doc.get_node_by_id(nid) is None


# --- deleteChildren ---

class TestDeleteChildren:
    def test_delete_children(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3"))
        with doc.transaction():
            doc.root.delete_children()
        assert len(doc.root.children) == 0

    def test_delete_children_empty_noop(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.delete_children()
        assert len(doc.root.children) == 0


# --- range delete ---

class TestRangeDelete:
    def test_delete_range_middle(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3", "4"))
        n2, n3 = doc.root.children[1], doc.root.children[2]
        with doc.transaction():
            n2.to(n3).delete()
        assert_doc(doc, ["1", "4"])

    def test_delete_range_all(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3", "4"))
        first = doc.root.children[0]
        last = doc.root.children[-1]
        with doc.transaction():
            first.to(last).delete()
        assert len(doc.root.children) == 0

    def test_delete_range_single(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3"))
        n2 = doc.root.children[1]
        with doc.transaction():
            n2.to(n2).delete()
        assert_doc(doc, ["1", "3"])


# --- move ---

class TestMove:
    def test_move_append(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3", "4"))
        n1 = doc.root.children[0]
        n4 = doc.root.children[3]
        with doc.transaction():
            n1.move(n4, "append")
        assert_doc(doc, ["2", "3", "4"])
        assert [c.value for c in n4.children] == ["1"]

    def test_move_prepend(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3"))
        n3 = doc.root.children[2]
        n1 = doc.root.children[0]
        with doc.transaction():
            n3.move(n1, "prepend")
        assert_doc(doc, ["1", "2"])
        assert [c.value for c in n1.children] == ["3"]

    def test_move_before(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3", "4"))
        n4 = doc.root.children[3]
        n2 = doc.root.children[1]
        with doc.transaction():
            n4.move(n2, "before")
        assert_doc(doc, ["1", "4", "2", "3"])

    def test_move_after(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3", "4"))
        n1 = doc.root.children[0]
        n3 = doc.root.children[2]
        with doc.transaction():
            n1.move(n3, "after")
        assert_doc(doc, ["2", "3", "1", "4"])

    def test_move_noop_already_in_position(self):
        """Moving a node to where it already is should be a no-op."""
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3"))
        n2 = doc.root.children[1]
        n1 = doc.root.children[0]
        with doc.transaction():
            n2.move(n1, "after")  # n2 is already after n1
        assert_doc(doc, ["1", "2", "3"])


# --- range move ---

class TestRangeMove:
    def test_move_range_append(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3", "4"))
        n1, n2, n4 = doc.root.children[0], doc.root.children[1], doc.root.children[3]
        with doc.transaction():
            n1.to(n2).move(n4, "append")
        assert_doc(doc, ["3", "4"])
        assert [c.value for c in n4.children] == ["1", "2"]

    def test_move_range_before(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3", "4"))
        n3, n4, n1 = doc.root.children[2], doc.root.children[3], doc.root.children[0]
        with doc.transaction():
            n3.to(n4).move(n1, "before")
        assert_doc(doc, ["3", "4", "1", "2"])

    def test_move_to_self_raises(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2"))
        n1 = doc.root.children[0]
        with pytest.raises(ValueError, match="Target is in the range"):
            with doc.transaction():
                n1.to(n1).move(n1, "append")

    def test_move_to_descendant_raises(self):
        doc = make_doc()
        with doc.transaction():
            parent = doc.create_node(TextNode, value="parent")
            child = doc.create_node(TextNode, value="child")
            doc.root.append(parent)
            parent.append(child)
        with pytest.raises(ValueError, match="descendant"):
            with doc.transaction():
                parent.move(child, "append")


# --- replace ---

class TestReplace:
    def test_replace_single(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3"))
        n2 = doc.root.children[1]
        with doc.transaction():
            replacement = doc.create_node(TextNode, value="NEW")
            n2.replace(replacement)
        assert_doc(doc, ["1", "NEW", "3"])

    def test_replace_first(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3"))
        n1 = doc.root.children[0]
        with doc.transaction():
            n1.replace(*text(doc, "NEW"))
        assert_doc(doc, ["NEW", "2", "3"])

    def test_replace_last(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.append(*text(doc, "1", "2", "3"))
        n3 = doc.root.children[2]
        with doc.transaction():
            n3.replace(*text(doc, "NEW"))
        assert_doc(doc, ["1", "2", "NEW"])


# --- mixed operations ---

class TestMixedOperations:
    def test_delete_and_reinsert_same_transaction(self):
        """A node deleted and reinserted in the same tx should appear as moved."""
        doc = make_doc()
        with doc.transaction():
            n1 = doc.create_node(TextNode, value="1")
            doc.root.append(n1)
            n1.delete()
            doc.root.append(n1)
        assert_doc(doc, ["1"])

    def test_insert_multiple_levels(self):
        doc = make_doc()
        with doc.transaction():
            p = doc.create_node(TextNode, value="parent")
            c1 = doc.create_node(TextNode, value="child1")
            c2 = doc.create_node(TextNode, value="child2")
            g1 = doc.create_node(TextNode, value="grandchild1")
            doc.root.append(p)
            p.append(c1, c2)
            c1.append(g1)
        assert_tree(doc, ["parent", "__child1", "____grandchild1", "__child2"])

    def test_delete_parent_removes_descendants_from_map(self):
        doc = make_doc()
        with doc.transaction():
            p = doc.create_node(TextNode, value="parent")
            c = doc.create_node(TextNode, value="child")
            doc.root.append(p)
            p.append(c)
        pid, cid = p.id, c.id
        with doc.transaction():
            p.delete()
        assert doc.get_node_by_id(pid) is None
        assert doc.get_node_by_id(cid) is None
