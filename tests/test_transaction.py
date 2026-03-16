"""Tests for transaction behavior."""

import pytest

from atomdoc import Doc, AtomNode, ChangeEvent


class TxNode(AtomNode, node_type="tx_node"):
    value: str = ""


@pytest.fixture
def doc():
    return Doc(root_type="tx_node", nodes=[TxNode])


def test_explicit_transaction(doc):
    events: list[ChangeEvent] = []
    doc.on_change(lambda ev: events.append(ev))

    with doc.transaction():
        doc.root.value = "a"
        doc.root.value = "b"

    # Only one event for the whole transaction
    assert len(events) == 1
    assert doc.root.value == "b"


def test_implicit_transaction(doc):
    events: list[ChangeEvent] = []
    doc.on_change(lambda ev: events.append(ev))

    doc.root.value = "implicit"

    assert doc.root.value == "implicit"
    assert len(events) == 1


def test_exception_rolls_back(doc):
    doc.root.value = "original"

    with pytest.raises(ValueError):
        with doc.transaction():
            doc.root.value = "changed"
            raise ValueError("oops")

    assert doc.root.value == "original"


def test_nested_joins_transaction(doc):
    events: list[ChangeEvent] = []
    doc.on_change(lambda ev: events.append(ev))

    with doc.transaction():
        doc.root.value = "outer"
        # Implicit transaction inside explicit should join
        n = doc.create_node(TxNode)
        doc.root.append(n)

    assert len(events) == 1


def test_cannot_mutate_in_change_handler(doc):
    def bad_handler(ev: ChangeEvent) -> None:
        doc.root.value = "sneaky"

    doc.on_change(bad_handler)

    with pytest.raises(RuntimeError):
        with doc.transaction():
            doc.root.value = "trigger"


def test_disposed_doc_rejects_transaction(doc):
    doc.dispose()
    with pytest.raises(RuntimeError):
        with doc.transaction():
            pass
