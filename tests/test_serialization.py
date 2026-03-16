"""Tests for serialization / deserialization."""

import pytest
from pydantic import BaseModel

from atomdoc import Doc, AtomNode


class Color(BaseModel, frozen=True):
    r: int = 0
    g: int = 0
    b: int = 0


class SerNode(AtomNode, node_type="ser_node"):
    title: str = ""
    color: Color = Color()
    data: bytes = b""


@pytest.fixture
def doc():
    return Doc(root_type="ser_node", nodes=[SerNode])


def test_round_trip(doc):
    with doc.transaction():
        doc.root.title = "Hello"
        doc.root.color = Color(r=255, g=128, b=0)
        child = doc.create_node(SerNode, title="child")
        doc.root.append(child)

    data = doc.to_json()
    doc2 = Doc.from_json(data, nodes=[SerNode])

    assert doc2.root.title == "Hello"
    assert doc2.root.color == Color(r=255, g=128, b=0)
    assert len(doc2.root.children) == 1
    assert doc2.root.children[0].title == "child"


def test_round_trip_nested(doc):
    with doc.transaction():
        a = doc.create_node(SerNode, title="a")
        b = doc.create_node(SerNode, title="b")
        doc.root.append(a)
        a.append(b)

    data = doc.to_json()
    doc2 = Doc.from_json(data, nodes=[SerNode])

    assert len(doc2.root.children) == 1
    assert doc2.root.children[0].title == "a"
    grandchildren = list(doc2.root.children[0].children)
    assert len(grandchildren) == 1
    assert grandchildren[0].title == "b"


def test_default_values_excluded(doc):
    data = doc.to_json()
    state = data[2]
    # Default values should not be in the serialized state
    assert "title" not in state
    assert "color" not in state


def test_preserves_doc_id(doc):
    data = doc.to_json()
    doc2 = Doc.from_json(data, nodes=[SerNode])
    assert doc2.id == doc.id


def test_preserves_node_ids(doc):
    with doc.transaction():
        child = doc.create_node(SerNode, title="child")
        doc.root.append(child)

    child_id = child.id
    data = doc.to_json()
    doc2 = Doc.from_json(data, nodes=[SerNode])

    assert doc2.root.children[0].id == child_id


def test_isinstance_after_deserialize(doc):
    with doc.transaction():
        child = doc.create_node(SerNode, title="child")
        doc.root.append(child)

    data = doc.to_json()
    doc2 = Doc.from_json(data, nodes=[SerNode])

    assert isinstance(doc2.root, SerNode)
    assert isinstance(doc2.root.children[0], SerNode)


def test_bytes_round_trip(doc):
    with doc.transaction():
        doc.root.data = b"hello bytes"

    data = doc.to_json()
    doc2 = Doc.from_json(data, nodes=[SerNode])
    assert doc2.root.data == b"hello bytes"


def test_json_schema():
    schema = Doc.json_schema(nodes=[SerNode])
    assert "ser_node" in schema
    assert "properties" in schema["ser_node"]
