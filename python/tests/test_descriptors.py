"""Tests for StateDescriptor."""

import pytest
from pydantic import BaseModel, ValidationError

from atomdoc import Doc, AtomNode


class Color(BaseModel, frozen=True):
    r: int = 0
    g: int = 0
    b: int = 0


class DescNode(AtomNode, node_type="test_desc"):
    name: str = ""
    count: int = 0
    color: Color = Color()


@pytest.fixture
def doc():
    return Doc(root_type="test_desc", nodes=[DescNode])


def test_get_default(doc):
    assert doc.root.name == ""
    assert doc.root.count == 0


def test_set_and_get(doc):
    with doc.transaction():
        doc.root.name = "hello"
    assert doc.root.name == "hello"
    assert type(doc.root.name) is str


def test_set_validates(doc):
    with pytest.raises(ValidationError):
        with doc.transaction():
            doc.root.count = "not a number"  # type: ignore


def test_atomic_field(doc):
    with doc.transaction():
        doc.root.color = Color(r=255, g=128, b=0)
    assert doc.root.color.r == 255
    assert isinstance(doc.root.color, Color)


def test_descriptor_on_class_returns_descriptor():
    from atomdoc._descriptors import StateDescriptor
    assert isinstance(DescNode.__dict__["name"], StateDescriptor)
