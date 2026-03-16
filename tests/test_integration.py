"""Integration tests — full workflows."""

from pydantic import BaseModel

from atomdoc import Doc, DocNode, UndoManager, ChangeEvent


class Color(BaseModel, frozen=True):
    r: int = 0
    g: int = 0
    b: int = 0


class PageNode(DocNode, node_type="page_int"):
    title: str = ""


class AnnotationNode(DocNode, node_type="annotation_int"):
    label: str = ""
    color: Color = Color()


def test_full_workflow():
    """Create, mutate, undo, serialize, deserialize, verify."""
    doc = Doc(root_type="page_int", nodes=[PageNode, AnnotationNode])
    undo = UndoManager(doc)

    # Create and mutate
    with doc.transaction():
        doc.root.title = "Hello"
        ann = doc.create_node(AnnotationNode)
        doc.root.append(ann)
        ann.color = Color(r=255, g=0, b=0)

    assert doc.root.title == "Hello"
    assert isinstance(doc.root.children[0], AnnotationNode)
    assert doc.root.children[0].color.r == 255
    assert type(doc.root.title) is str

    # Serialize
    data = doc.to_json()

    # Deserialize
    doc2 = Doc.from_json(data, nodes=[PageNode, AnnotationNode])
    assert doc2.root.title == "Hello"
    assert isinstance(doc2.root.children[0], AnnotationNode)
    assert doc2.root.children[0].color.r == 255

    # Undo
    undo.undo()
    assert doc.root.title == ""
    assert len(doc.root.children) == 0

    # Redo
    undo.redo()
    assert doc.root.title == "Hello"
    assert len(doc.root.children) == 1


def test_multiple_transactions_with_events():
    doc = Doc(root_type="page_int", nodes=[PageNode, AnnotationNode])
    events: list[ChangeEvent] = []
    doc.on_change(lambda ev: events.append(ev))

    with doc.transaction():
        doc.root.title = "First"

    with doc.transaction():
        ann = doc.create_node(AnnotationNode, label="ann1")
        doc.root.append(ann)

    with doc.transaction():
        ann.label = "updated"

    assert len(events) == 3
    assert doc.root.title == "First"
    assert doc.root.children[0].label == "updated"


def test_tree_manipulation_workflow():
    doc = Doc(root_type="page_int", nodes=[PageNode, AnnotationNode])

    with doc.transaction():
        a = doc.create_node(AnnotationNode, label="a")
        b = doc.create_node(AnnotationNode, label="b")
        c = doc.create_node(AnnotationNode, label="c")
        doc.root.append(a, b, c)

    # Move b before a
    with doc.transaction():
        b.move(a, "before")

    assert [ch.label for ch in doc.root.children] == ["b", "a", "c"]

    # Delete range a-c
    with doc.transaction():
        a.to(c).delete()

    assert [ch.label for ch in doc.root.children] == ["b"]

    # Serialize and restore
    data = doc.to_json()
    doc2 = Doc.from_json(data, nodes=[PageNode, AnnotationNode])
    assert [ch.label for ch in doc2.root.children] == ["b"]


def test_isinstance_type_narrowing():
    doc = Doc(root_type="page_int", nodes=[PageNode, AnnotationNode])

    with doc.transaction():
        ann = doc.create_node(AnnotationNode, label="test", color=Color(r=100))
        doc.root.append(ann)

    for child in doc.root.children:
        if isinstance(child, AnnotationNode):
            assert child.color.r == 100
            assert child.label == "test"


def test_nested_children():
    doc = Doc(root_type="page_int", nodes=[PageNode, AnnotationNode])

    with doc.transaction():
        parent = doc.create_node(AnnotationNode, label="parent")
        child1 = doc.create_node(AnnotationNode, label="child1")
        child2 = doc.create_node(AnnotationNode, label="child2")
        doc.root.append(parent)
        parent.append(child1, child2)

    assert len(doc.root.children) == 1
    assert len(doc.root.children[0].children) == 2
    assert doc.root.children[0].children[0].label == "child1"
    assert doc.root.children[0].children[1].label == "child2"

    # Verify traversal
    descs = list(doc.root.descendants())
    assert len(descs) == 3  # parent, child1, child2


def test_change_event_diff():
    doc = Doc(root_type="page_int", nodes=[PageNode, AnnotationNode])
    events: list[ChangeEvent] = []
    doc.on_change(lambda ev: events.append(ev))

    with doc.transaction():
        ann = doc.create_node(AnnotationNode)
        doc.root.append(ann)
        ann.label = "new"

    diff = events[0].diff
    assert ann.id in diff.inserted
    assert doc.root.id not in diff.inserted
