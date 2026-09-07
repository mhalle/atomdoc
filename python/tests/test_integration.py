"""Integration tests — full workflows."""

from pydantic import BaseModel

from atomdoc import Doc, Array, node, UndoManager, ChangeEvent


class Color(BaseModel, frozen=True):
    r: int = 0
    g: int = 0
    b: int = 0


@node
class AnnotationInt:
    label: str = ""
    color: Color = Color()


@node
class PageInt:
    title: str = ""
    annotations: Array[AnnotationInt] = []


def make_doc():
    return Doc(root_type="PageInt", nodes=[PageInt, AnnotationInt])


def test_full_workflow():
    """Create, mutate, undo, serialize, deserialize, verify."""
    doc = make_doc()
    undo = UndoManager(doc)

    # Create and mutate
    with doc.transaction():
        doc.root.title = "Hello"
        ann = doc.create_node(AnnotationInt)
        doc.root.annotations.append(ann)
        ann.color = Color(r=255, g=0, b=0)

    assert doc.root.title == "Hello"
    assert isinstance(doc.root.annotations[0], AnnotationInt)
    assert doc.root.annotations[0].color.r == 255
    assert type(doc.root.title) is str

    # Serialize
    wire = doc.dump()

    # Deserialize
    doc2 = Doc.restore(wire, root_type=PageInt)
    assert doc2.root.title == "Hello"
    assert isinstance(doc2.root.annotations[0], AnnotationInt)
    assert doc2.root.annotations[0].color.r == 255

    # Undo
    undo.undo()
    assert doc.root.title == ""
    assert len(doc.root.annotations) == 0

    # Redo
    undo.redo()
    assert doc.root.title == "Hello"
    assert len(doc.root.annotations) == 1


def test_multiple_transactions_with_events():
    doc = make_doc()
    events: list[ChangeEvent] = []
    doc.on_change(lambda ev: events.append(ev))

    with doc.transaction():
        doc.root.title = "First"

    with doc.transaction():
        ann = doc.create_node(AnnotationInt, label="ann1")
        doc.root.annotations.append(ann)

    with doc.transaction():
        ann.label = "updated"

    assert len(events) == 3
    assert doc.root.title == "First"
    assert doc.root.annotations[0].label == "updated"


def test_tree_manipulation_workflow():
    doc = make_doc()

    with doc.transaction():
        a = doc.create_node(AnnotationInt, label="a")
        b = doc.create_node(AnnotationInt, label="b")
        c = doc.create_node(AnnotationInt, label="c")
        doc.root.annotations.append(a, b, c)

    # Move b to the front (prepend) so it comes before a
    with doc.transaction():
        b.move(doc.root, "annotations", "prepend")

    assert [ch.label for ch in doc.root.annotations] == ["b", "a", "c"]

    # Delete range a-c
    with doc.transaction():
        a.to(c).delete()

    assert [ch.label for ch in doc.root.annotations] == ["b"]

    # Serialize and restore
    wire = doc.dump()
    doc2 = Doc.restore(wire, root_type=PageInt)
    assert [ch.label for ch in doc2.root.annotations] == ["b"]


def test_isinstance_type_narrowing():
    doc = make_doc()

    with doc.transaction():
        ann = doc.create_node(AnnotationInt, label="test", color=Color(r=100))
        doc.root.annotations.append(ann)

    for child in doc.root.annotations:
        if isinstance(child, AnnotationInt):
            assert child.color.r == 100
            assert child.label == "test"


def test_nested_children():
    @node
    class NestedAnnotation:
        label: str = ""
        children: Array[AnnotationInt] = []

    @node
    class NestedPage:
        title: str = ""
        items: Array[NestedAnnotation] = []

    doc = Doc(root_type="NestedPage", nodes=[NestedPage, NestedAnnotation, AnnotationInt])

    with doc.transaction():
        parent = doc.create_node(NestedAnnotation, label="parent")
        child1 = doc.create_node(AnnotationInt, label="child1")
        child2 = doc.create_node(AnnotationInt, label="child2")
        doc.root.items.append(parent)
        parent.children.append(child1, child2)

    assert len(doc.root.items) == 1
    assert len(doc.root.items[0].children) == 2
    assert doc.root.items[0].children[0].label == "child1"
    assert doc.root.items[0].children[1].label == "child2"

    # Verify traversal
    descs = list(doc.descendants(doc.root))
    assert len(descs) == 3  # parent, child1, child2


def test_change_event_diff():
    doc = make_doc()
    events: list[ChangeEvent] = []
    doc.on_change(lambda ev: events.append(ev))

    with doc.transaction():
        ann = doc.create_node(AnnotationInt)
        doc.root.annotations.append(ann)
        ann.label = "new"

    diff = events[0].diff
    assert ann.id in diff.inserted
    assert doc.root.id not in diff.inserted
