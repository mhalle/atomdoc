"""Tests ported from docnode/readonly.test.ts — traversal and navigation."""

from atomdoc import Doc, AtomNode


class TextNode(AtomNode, node_type="text_tr"):
    value: str = ""


def make_doc():
    return Doc(root_type="text_tr", nodes=[TextNode])


def text(doc, *values):
    return [doc.create_node(TextNode, value=v) for v in values]


def setup_tree():
    """Create doc with root -> [1, 2(->2.1, 2.2), 3, 4]."""
    doc = make_doc()
    with doc.transaction():
        doc.root.append(*text(doc, "1", "2", "3", "4"))
        doc.root.children[0].append(*text(doc, "1.1", "1.2"))
        doc.root.children[2].append(*text(doc, "3.1"))
    return doc


class TestNavigationProperties:
    def test_parent(self):
        doc = setup_tree()
        n1 = doc.root.children[0]
        assert n1.parent is doc.root
        assert doc.root.parent is None

    def test_prev_sibling(self):
        doc = setup_tree()
        n1, n2 = doc.root.children[0], doc.root.children[1]
        assert n1.prev_sibling is None
        assert n2.prev_sibling is n1

    def test_next_sibling(self):
        doc = setup_tree()
        n1, n2 = doc.root.children[0], doc.root.children[1]
        n4 = doc.root.children[3]
        assert n1.next_sibling is n2
        assert n4.next_sibling is None

    def test_get_node_by_id(self):
        doc = setup_tree()
        n1 = doc.root.children[0]
        assert doc.get_node_by_id(n1.id) is n1
        assert doc.get_node_by_id("nonexistent") is None


class TestDescendants:
    def test_depth_first(self):
        doc = setup_tree()
        vals = [n.value for n in doc.root.descendants()]
        assert vals == ["1", "1.1", "1.2", "2", "3", "3.1", "4"]

    def test_excludes_self(self):
        doc = setup_tree()
        n1 = doc.root.children[0]
        vals = [n.value for n in n1.descendants()]
        assert vals == ["1.1", "1.2"]

    def test_empty_for_leaf(self):
        doc = setup_tree()
        n4 = doc.root.children[3]
        assert list(n4.descendants()) == []


class TestAncestors:
    def test_walk_to_root(self):
        doc = setup_tree()
        n1_1 = doc.root.children[0].children[0]  # "1.1"
        ancestors = list(n1_1.ancestors())
        assert len(ancestors) == 2
        assert ancestors[0].value == "1"
        assert ancestors[1] is doc.root

    def test_excludes_self(self):
        doc = setup_tree()
        n1 = doc.root.children[0]
        ancestors = list(n1.ancestors())
        assert n1 not in ancestors

    def test_root_has_no_ancestors(self):
        doc = setup_tree()
        assert list(doc.root.ancestors()) == []


class TestNextSiblings:
    def test_forward_from_first(self):
        doc = setup_tree()
        n1 = doc.root.children[0]
        vals = [n.value for n in n1.next_siblings()]
        assert vals == ["2", "3", "4"]

    def test_excludes_self(self):
        doc = setup_tree()
        n1 = doc.root.children[0]
        assert n1 not in list(n1.next_siblings())

    def test_empty_for_last(self):
        doc = setup_tree()
        n4 = doc.root.children[3]
        assert list(n4.next_siblings()) == []


class TestPrevSiblings:
    def test_backward_from_last(self):
        doc = setup_tree()
        n4 = doc.root.children[3]
        vals = [n.value for n in n4.prev_siblings()]
        assert vals == ["3", "2", "1"]

    def test_excludes_self(self):
        doc = setup_tree()
        n4 = doc.root.children[3]
        assert n4 not in list(n4.prev_siblings())

    def test_empty_for_first(self):
        doc = setup_tree()
        n1 = doc.root.children[0]
        assert list(n1.prev_siblings()) == []


class TestChildren:
    def test_iteration(self):
        doc = setup_tree()
        vals = [c.value for c in doc.root.children]
        assert vals == ["1", "2", "3", "4"]

    def test_len(self):
        doc = setup_tree()
        assert len(doc.root.children) == 4

    def test_indexing(self):
        doc = setup_tree()
        assert doc.root.children[0].value == "1"
        assert doc.root.children[-1].value == "4"

    def test_slicing(self):
        doc = setup_tree()
        mid = doc.root.children[1:3]
        assert [c.value for c in mid] == ["2", "3"]

    def test_contains(self):
        doc = setup_tree()
        n1 = doc.root.children[0]
        assert n1 in doc.root.children
        n1_1 = n1.children[0]
        assert n1_1 not in doc.root.children  # only direct children

    def test_bool_empty(self):
        doc = make_doc()
        assert not doc.root.children

    def test_bool_non_empty(self):
        doc = setup_tree()
        assert doc.root.children


class TestRange:
    def test_iterate_range(self):
        doc = setup_tree()
        n1, n3 = doc.root.children[0], doc.root.children[2]
        vals = [n.value for n in n1.to(n3)]
        assert vals == ["1", "2", "3"]

    def test_single_node_range(self):
        doc = setup_tree()
        n2 = doc.root.children[1]
        vals = [n.value for n in n2.to(n2)]
        assert vals == ["2"]

    def test_invalid_range_raises(self):
        doc = setup_tree()
        n1 = doc.root.children[0]
        n4 = doc.root.children[3]
        # n4.to(n1) — n1 is not a later sibling of n4
        with __import__("pytest").raises(ValueError, match="not a later sibling"):
            list(n4.to(n1))

    def test_cross_parent_range_raises(self):
        """Range across different parents should raise."""
        doc = make_doc()
        with doc.transaction():
            p1 = doc.create_node(TextNode, value="p1")
            p2 = doc.create_node(TextNode, value="p2")
            doc.root.append(p1, p2)
            p1.append(*text(doc, "c1"))
            p2.append(*text(doc, "c2"))

        c1 = p1.children[0]
        c2 = p2.children[0]
        with __import__("pytest").raises(ValueError, match="not a later sibling"):
            list(c1.to(c2))


class TestIsinstance:
    def test_isinstance_works(self):
        doc = setup_tree()
        for child in doc.root.children:
            assert isinstance(child, TextNode)
            assert isinstance(child, AtomNode)

    def test_isinstance_root(self):
        doc = setup_tree()
        assert isinstance(doc.root, TextNode)
