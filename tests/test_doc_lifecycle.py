"""Tests for Doc lifecycle — creation, disposal, change events."""

import pytest

from atomdoc import Doc, AtomNode, ChangeEvent


class ItemNode(AtomNode, node_type="item_lc"):
    name: str = ""


@pytest.fixture
def doc():
    return Doc(root_type="item_lc", nodes=[ItemNode])


def test_doc_creation(doc):
    assert doc.root is not None
    assert doc.root.id == doc.id
    assert isinstance(doc.root, ItemNode)


def test_doc_custom_id():
    from ulid import ULID
    custom_id = str(ULID()).lower()
    doc = Doc(root_type="item_lc", nodes=[ItemNode], doc_id=custom_id)
    assert doc.id == custom_id


def test_create_node(doc):
    node = doc.create_node(ItemNode, name="test")
    assert node.name == "test"
    assert node.id != doc.root.id


def test_create_unregistered_node(doc):
    class OtherNode(AtomNode, node_type="other_lc"):
        x: int = 0

    with pytest.raises(ValueError, match="not registered"):
        doc.create_node(OtherNode)


def test_on_change_fires(doc):
    events: list[ChangeEvent] = []
    doc.on_change(lambda ev: events.append(ev))
    with doc.transaction():
        doc.root.name = "updated"
    assert len(events) == 1
    assert events[0].operations[1]  # state patch exists


def test_on_change_unsubscribe(doc):
    events: list[ChangeEvent] = []
    unsub = doc.on_change(lambda ev: events.append(ev))
    with doc.transaction():
        doc.root.name = "a"
    assert len(events) == 1
    unsub()
    with doc.transaction():
        doc.root.name = "b"
    assert len(events) == 1  # no new event


def test_dispose(doc):
    doc.dispose()
    with pytest.raises(RuntimeError):
        with doc.transaction():
            doc.root.name = "fail"


def test_get_node_by_id(doc):
    assert doc.get_node_by_id(doc.root.id) is doc.root
    assert doc.get_node_by_id("nonexistent") is None


def test_implicit_transaction(doc):
    doc.root.name = "implicit"
    assert doc.root.name == "implicit"
