"""Tests for the named-slot Array[T] system."""

import pytest
from pydantic import BaseModel

from atomdoc import Doc, DocNode, Array, node, UndoManager


# --- Schema definitions using @node decorator ---

class Color(BaseModel, frozen=True):
    r: int = 0
    g: int = 0
    b: int = 0


@node
class Annotation:
    label: str = ""
    color: Color = Color()


@node
class Note:
    text: str = ""


@node
class Slide:
    title: str = ""
    annotations: Array[Annotation] = []
    notes: Array[Note] = []


@node
class SimpleList:
    items: Array[Annotation] = []


def make_doc():
    return Doc(root_type="Slide", nodes=[Slide, Annotation, Note])


# --- @node decorator ---

class TestNodeDecorator:
    def test_creates_docnode_subclass(self):
        assert issubclass(Annotation, DocNode)
        assert Annotation._node_type == "Annotation"

    def test_preserves_class_name(self):
        assert Annotation.__name__ == "Annotation"

    def test_custom_type_name(self):
        @node("custom")
        class Foo:
            x: int = 0

        assert Foo._node_type == "custom"

    def test_state_fields(self):
        assert "label" in Annotation._field_tiers
        assert "color" in Annotation._field_tiers

    def test_slot_fields(self):
        assert "annotations" in Slide._slot_defs
        assert "notes" in Slide._slot_defs
        assert "title" not in Slide._slot_defs

    def test_slots_not_in_state(self):
        assert "annotations" not in Slide._field_tiers
        assert "notes" not in Slide._field_tiers


# --- Slot basics ---

class TestSlotBasics:
    def test_empty_slots(self):
        doc = make_doc()
        assert len(doc.root.annotations) == 0
        assert len(doc.root.notes) == 0
        assert not doc.root.annotations
        assert not doc.root.notes

    def test_append_to_slot(self):
        doc = make_doc()
        with doc.transaction():
            ann = doc.create_node(Annotation, label="a")
            doc.root.annotations.append(ann)
        assert len(doc.root.annotations) == 1
        assert doc.root.annotations[0].label == "a"

    def test_multiple_append(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.annotations.append(
                doc.create_node(Annotation, label="a"),
                doc.create_node(Annotation, label="b"),
                doc.create_node(Annotation, label="c"),
            )
        assert [a.label for a in doc.root.annotations] == ["a", "b", "c"]

    def test_prepend(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.annotations.append(doc.create_node(Annotation, label="b"))
            doc.root.annotations.prepend(doc.create_node(Annotation, label="a"))
        assert [a.label for a in doc.root.annotations] == ["a", "b"]

    def test_insert_at_index(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.annotations.append(
                doc.create_node(Annotation, label="a"),
                doc.create_node(Annotation, label="c"),
            )
            doc.root.annotations.insert(1, doc.create_node(Annotation, label="b"))
        assert [a.label for a in doc.root.annotations] == ["a", "b", "c"]

    def test_independent_slots(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.annotations.append(doc.create_node(Annotation, label="ann"))
            doc.root.notes.append(doc.create_node(Note, text="note"))
        assert len(doc.root.annotations) == 1
        assert len(doc.root.notes) == 1
        assert doc.root.annotations[0].label == "ann"
        assert doc.root.notes[0].text == "note"

    def test_cannot_assign_to_slot(self):
        doc = make_doc()
        with pytest.raises(AttributeError, match="Cannot assign"):
            doc.root.annotations = []  # type: ignore


# --- Slot indexing ---

class TestSlotIndexing:
    def test_positive_index(self):
        doc = make_doc()
        with doc.transaction():
            for i in range(4):
                doc.root.annotations.append(doc.create_node(Annotation, label=str(i)))
        assert doc.root.annotations[0].label == "0"
        assert doc.root.annotations[3].label == "3"

    def test_negative_index(self):
        doc = make_doc()
        with doc.transaction():
            for i in range(3):
                doc.root.annotations.append(doc.create_node(Annotation, label=str(i)))
        assert doc.root.annotations[-1].label == "2"

    def test_slice(self):
        doc = make_doc()
        with doc.transaction():
            for i in range(4):
                doc.root.annotations.append(doc.create_node(Annotation, label=str(i)))
        sliced = doc.root.annotations[1:3]
        assert [a.label for a in sliced] == ["1", "2"]

    def test_out_of_range(self):
        doc = make_doc()
        with pytest.raises(IndexError):
            doc.root.annotations[0]


# --- Navigation with slots ---

class TestSlotNavigation:
    def test_parent_is_direct(self):
        """doc.parent(ann) is the slide, not a container node."""
        doc = make_doc()
        with doc.transaction():
            ann = doc.create_node(Annotation, label="x")
            doc.root.annotations.append(ann)
        assert doc.parent(ann) is doc.root

    def test_slot_name_tracked(self):
        doc = make_doc()
        with doc.transaction():
            ann = doc.create_node(Annotation, label="x")
            doc.root.annotations.append(ann)
            note = doc.create_node(Note, text="y")
            doc.root.notes.append(note)
        assert ann._slot_name == "annotations"
        assert note._slot_name == "notes"

    def test_siblings_within_slot(self):
        doc = make_doc()
        with doc.transaction():
            a = doc.create_node(Annotation, label="a")
            b = doc.create_node(Annotation, label="b")
            doc.root.annotations.append(a, b)
        assert doc.next_sibling(a) is b
        assert doc.prev_sibling(b) is a

    def test_siblings_dont_cross_slots(self):
        doc = make_doc()
        with doc.transaction():
            ann = doc.create_node(Annotation, label="ann")
            note = doc.create_node(Note, text="note")
            doc.root.annotations.append(ann)
            doc.root.notes.append(note)
        assert doc.next_sibling(ann) is None
        assert doc.prev_sibling(note) is None

    def test_descendants_traverses_all_slots(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.annotations.append(doc.create_node(Annotation, label="ann"))
            doc.root.notes.append(doc.create_node(Note, text="note"))
        descs = list(doc.descendants(doc.root))
        assert len(descs) == 2

    def test_ancestors(self):
        doc = make_doc()
        with doc.transaction():
            ann = doc.create_node(Annotation, label="x")
            doc.root.annotations.append(ann)
        assert list(doc.ancestors(ann)) == [doc.root]


# --- Mutation ---

class TestSlotMutation:
    def test_delete_from_slot(self):
        doc = make_doc()
        with doc.transaction():
            a = doc.create_node(Annotation, label="a")
            b = doc.create_node(Annotation, label="b")
            doc.root.annotations.append(a, b)
        with doc.transaction():
            a.delete()
        assert [x.label for x in doc.root.annotations] == ["b"]

    def test_clear_slot(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.annotations.append(
                doc.create_node(Annotation, label="a"),
                doc.create_node(Annotation, label="b"),
            )
        with doc.transaction():
            doc.root.annotations.clear()
        assert len(doc.root.annotations) == 0

    def test_insert_before(self):
        doc = make_doc()
        with doc.transaction():
            a = doc.create_node(Annotation, label="a")
            c = doc.create_node(Annotation, label="c")
            doc.root.annotations.append(a, c)
        with doc.transaction():
            b = doc.create_node(Annotation, label="b")
            c.insert_before(b)
        assert [x.label for x in doc.root.annotations] == ["a", "b", "c"]

    def test_insert_after(self):
        doc = make_doc()
        with doc.transaction():
            a = doc.create_node(Annotation, label="a")
            c = doc.create_node(Annotation, label="c")
            doc.root.annotations.append(a, c)
        with doc.transaction():
            b = doc.create_node(Annotation, label="b")
            a.insert_after(b)
        assert [x.label for x in doc.root.annotations] == ["a", "b", "c"]

    def test_replace(self):
        doc = make_doc()
        with doc.transaction():
            a = doc.create_node(Annotation, label="a")
            doc.root.annotations.append(a)
        with doc.transaction():
            b = doc.create_node(Annotation, label="b")
            a.replace(b)
        assert [x.label for x in doc.root.annotations] == ["b"]

    def test_range_delete(self):
        doc = make_doc()
        with doc.transaction():
            a = doc.create_node(Annotation, label="a")
            b = doc.create_node(Annotation, label="b")
            c = doc.create_node(Annotation, label="c")
            doc.root.annotations.append(a, b, c)
        with doc.transaction():
            a.to(b).delete()
        assert [x.label for x in doc.root.annotations] == ["c"]


# --- Serialization ---

class TestSlotSerialization:
    def test_round_trip(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.title = "Hello"
            doc.root.annotations.append(
                doc.create_node(Annotation, label="ann1", color=Color(r=255)),
            )
            doc.root.notes.append(doc.create_node(Note, text="note1"))

        wire = doc.dump()
        doc2 = Doc.restore(wire, root_type=Slide)

        assert doc2.root.title == "Hello"
        assert len(doc2.root.annotations) == 1
        assert doc2.root.annotations[0].label == "ann1"
        assert doc2.root.annotations[0].color.r == 255
        assert len(doc2.root.notes) == 1
        assert doc2.root.notes[0].text == "note1"

    def test_preserves_ids(self):
        doc = make_doc()
        with doc.transaction():
            ann = doc.create_node(Annotation, label="x")
            doc.root.annotations.append(ann)
        ann_id = ann.id
        wire = doc.dump()
        doc2 = Doc.restore(wire, root_type=Slide)
        assert doc2.root.annotations[0].id == ann_id

    def test_isinstance_after_deserialize(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.annotations.append(doc.create_node(Annotation, label="x"))
        wire = doc.dump()
        doc2 = Doc.restore(wire, root_type=Slide)
        assert isinstance(doc2.root, Slide)
        assert isinstance(doc2.root.annotations[0], Annotation)

    def test_to_json_is_clean(self):
        doc = make_doc()
        data = doc.to_json()
        # Clean JSON — dict with slots as keys, no IDs
        assert isinstance(data, dict)
        assert "annotations" in data
        assert "notes" in data
        assert data["annotations"] == []
        assert data["notes"] == []

    def test_to_json_with_data(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.annotations.append(doc.create_node(Annotation, label="x"))
        data = doc.to_json()
        assert isinstance(data, dict)
        assert len(data["annotations"]) == 1
        assert data["annotations"][0]["label"] == "x"
        # No IDs in clean JSON
        assert "id" not in data
        assert "id" not in data["annotations"][0]

    def test_to_json_subtree(self):
        doc = make_doc()
        with doc.transaction():
            doc.root.annotations.append(
                doc.create_node(Annotation, label="x", color=Color(r=100))
            )
        data = doc.to_json(doc.root.annotations[0])
        assert data == {"label": "x", "color": {"r": 100, "g": 0, "b": 0}}


# --- Undo ---

class TestSlotUndo:
    def test_undo_append(self):
        doc = make_doc()
        undo = UndoManager(doc)
        with doc.transaction():
            doc.root.annotations.append(doc.create_node(Annotation, label="x"))
        assert len(doc.root.annotations) == 1
        undo.undo()
        assert len(doc.root.annotations) == 0

    def test_undo_delete(self):
        doc = make_doc()
        with doc.transaction():
            ann = doc.create_node(Annotation, label="x")
            doc.root.annotations.append(ann)
        undo = UndoManager(doc)
        with doc.transaction():
            ann.delete()
        assert len(doc.root.annotations) == 0
        undo.undo()
        assert len(doc.root.annotations) == 1

    def test_undo_state_change(self):
        doc = make_doc()
        undo = UndoManager(doc)
        with doc.transaction():
            doc.root.title = "Hello"
        undo.undo()
        assert doc.root.title == ""

    def test_redo(self):
        doc = make_doc()
        undo = UndoManager(doc)
        with doc.transaction():
            doc.root.annotations.append(doc.create_node(Annotation, label="x"))
        undo.undo()
        assert len(doc.root.annotations) == 0
        undo.redo()
        assert len(doc.root.annotations) == 1


# --- Change events ---

class TestSlotChangeEvents:
    def test_insert_fires_event(self):
        doc = make_doc()
        events = []
        doc.on_change(lambda ev: events.append(ev))
        with doc.transaction():
            doc.root.annotations.append(doc.create_node(Annotation, label="x"))
        assert len(events) == 1
        assert events[0].diff.inserted

    def test_slot_name_in_operations(self):
        doc = make_doc()
        events = []
        doc.on_change(lambda ev: events.append(ev))
        with doc.transaction():
            doc.root.annotations.append(doc.create_node(Annotation, label="x"))
        insert_ops = [op for op in events[0].operations[0] if op[0] == 0]
        assert len(insert_ops) >= 1
        # Slot name should be in the operation
        assert insert_ops[0][3] == "annotations"


# --- Nested slots ---

class TestNestedSlots:
    def test_nested_slot_nodes(self):
        @node
        class Container:
            items: Array[Annotation] = []

        @node
        class Root:
            containers: Array[Container] = []

        doc = Doc(root_type="Root", nodes=[Root, Container, Annotation])
        with doc.transaction():
            c = doc.create_node(Container)
            doc.root.containers.append(c)
            ann = doc.create_node(Annotation, label="nested")
            c.items.append(ann)

        assert len(doc.root.containers) == 1
        assert len(doc.root.containers[0].items) == 1
        assert doc.root.containers[0].items[0].label == "nested"
        assert doc.parent(ann) is c
        assert doc.parent(c) is doc.root

    def test_nested_descendants(self):
        @node
        class Inner:
            items: Array[Annotation] = []

        @node
        class Outer:
            inners: Array[Inner] = []

        doc = Doc(root_type="Outer", nodes=[Outer, Inner, Annotation])
        with doc.transaction():
            inner = doc.create_node(Inner)
            doc.root.inners.append(inner)
            inner.items.append(doc.create_node(Annotation, label="deep"))

        descs = list(doc.descendants(doc.root))
        assert len(descs) == 2  # inner + annotation

    def test_nested_serialization(self):
        @node
        class Inner:
            items: Array[Annotation] = []

        @node
        class Outer:
            inners: Array[Inner] = []

        doc = Doc(root_type="Outer", nodes=[Outer, Inner, Annotation])
        with doc.transaction():
            inner = doc.create_node(Inner)
            doc.root.inners.append(inner)
            inner.items.append(doc.create_node(Annotation, label="deep"))

        wire = doc.dump()
        doc2 = Doc.restore(wire, root_type=Outer)
        assert doc2.root.inners[0].items[0].label == "deep"
