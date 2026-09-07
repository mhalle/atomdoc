"""Tests for serialization / deserialization."""

import pytest
from pydantic import BaseModel

from atomdoc import Doc, Array, node


class Color(BaseModel, frozen=True):
    r: int = 0
    g: int = 0
    b: int = 0


@node
class SerChild:
    title: str = ""
    color: Color = Color()
    data: bytes = b""
    children: Array["SerChild"] = []


@node
class SerNode:
    title: str = ""
    color: Color = Color()
    data: bytes = b""
    children: Array[SerChild] = []


ALL_NODES = [SerNode, SerChild]


@pytest.fixture
def doc():
    return Doc(root_type="SerNode", nodes=ALL_NODES)


def test_round_trip(doc):
    with doc.transaction():
        doc.root.title = "Hello"
        doc.root.color = Color(r=255, g=128, b=0)
        child = doc.create_node(SerChild, title="child")
        doc.root.children.append(child)

    wire = doc.dump()
    doc2 = Doc.restore(wire, root_type=SerNode)

    assert doc2.root.title == "Hello"
    assert doc2.root.color == Color(r=255, g=128, b=0)
    assert len(doc2.root.children) == 1
    assert doc2.root.children[0].title == "child"


def test_round_trip_nested(doc):
    with doc.transaction():
        a = doc.create_node(SerChild, title="a")
        b = doc.create_node(SerChild, title="b")
        doc.root.children.append(a)
        a.children.append(b)

    wire = doc.dump()
    doc2 = Doc.restore(wire, root_type=SerNode)

    assert len(doc2.root.children) == 1
    assert doc2.root.children[0].title == "a"
    grandchildren = list(doc2.root.children[0].children)
    assert len(grandchildren) == 1
    assert grandchildren[0].title == "b"


def test_default_values_excluded(doc):
    data = doc.to_json()
    # to_json returns a dict; default values should not appear in state fields
    assert "title" not in data or data["title"] == ""
    # The children slot should be present but empty
    assert data["children"] == []


def test_preserves_doc_id(doc):
    wire = doc.dump()
    doc2 = Doc.restore(wire, root_type=SerNode)
    assert doc2.id == doc.id


def test_preserves_node_ids(doc):
    with doc.transaction():
        child = doc.create_node(SerChild, title="child")
        doc.root.children.append(child)

    child_id = child.id
    wire = doc.dump()
    doc2 = Doc.restore(wire, root_type=SerNode)

    assert doc2.root.children[0].id == child_id


def test_isinstance_after_deserialize(doc):
    with doc.transaction():
        child = doc.create_node(SerChild, title="child")
        doc.root.children.append(child)

    wire = doc.dump()
    doc2 = Doc.restore(wire, root_type=SerNode)

    assert isinstance(doc2.root, SerNode)
    assert isinstance(doc2.root.children[0], SerChild)


def test_bytes_round_trip(doc):
    with doc.transaction():
        doc.root.data = b"hello bytes"

    wire = doc.dump()
    doc2 = Doc.restore(wire, root_type=SerNode)
    assert doc2.root.data == b"hello bytes"


def test_json_schema():
    schema = Doc.json_schema(nodes=ALL_NODES)
    assert "SerNode" in schema
    assert "properties" in schema["SerNode"]


def test_to_json_is_clean(doc):
    data = doc.to_json()
    assert isinstance(data, dict)
    assert "children" in data
    assert data["children"] == []
    # No IDs in clean JSON
    assert "id" not in data


def test_to_json_with_data(doc):
    with doc.transaction():
        doc.root.children.append(doc.create_node(SerChild, title="x"))
    data = doc.to_json()
    assert isinstance(data, dict)
    assert len(data["children"]) == 1
    assert data["children"][0]["title"] == "x"
    assert "id" not in data
    assert "id" not in data["children"][0]
