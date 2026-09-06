"""Tests for ID generation."""

from atomdoc._id import (
    BASE64_ALPHABET,
    increment_base64,
    number_to_base64,
    random_base64,
)


def test_number_to_base64_zero():
    assert number_to_base64(0) == BASE64_ALPHABET[0]


def test_number_to_base64_small():
    assert number_to_base64(1) == BASE64_ALPHABET[1]
    assert number_to_base64(63) == BASE64_ALPHABET[63]


def test_number_to_base64_large():
    result = number_to_base64(64)
    assert len(result) == 2
    assert result[0] == BASE64_ALPHABET[1]
    assert result[1] == BASE64_ALPHABET[0]


def test_random_base64_length():
    for n in (1, 3, 5, 10):
        result = random_base64(n)
        assert len(result) == n
        assert all(c in BASE64_ALPHABET for c in result)


def test_increment_simple():
    first = BASE64_ALPHABET[0]
    result = increment_base64(first)
    assert result == BASE64_ALPHABET[1]


def test_increment_carry():
    last = BASE64_ALPHABET[-1]
    result = increment_base64(last)
    assert len(result) == 2
    assert result[0] == BASE64_ALPHABET[1]
    assert result[1] == BASE64_ALPHABET[0]


def test_increment_middle():
    s = BASE64_ALPHABET[0] + BASE64_ALPHABET[-1]
    result = increment_base64(s)
    assert result == BASE64_ALPHABET[1] + BASE64_ALPHABET[0]


def test_node_id_factory_generates_unique_ids(doc):
    from atomdoc._id import node_id_factory
    gen = node_id_factory(doc)
    ids = set()
    for _ in range(100):
        nid = gen()
        assert nid not in ids
        ids.add(nid)
    assert len(ids) == 100


def test_node_id_format(doc):
    from atomdoc._id import node_id_factory
    gen = node_id_factory(doc)
    nid = gen()
    assert "." in nid
    session, clock = nid.split(".", 1)
    assert len(session) >= 2
    assert len(clock) >= 1


def test_mint_session_id_avoids_existing(monkeypatch):
    from atomdoc import _id

    monkeypatch.setattr(_id, "random_base64", lambda n: "AAA")
    monkeypatch.setattr(_id.time, "time", lambda: 1.0)
    fresh = _id.mint_session_id(0)
    assert fresh == number_to_base64(1000) + "AAA"
    # Same millisecond, same suffix: the collision is detected and resolved.
    again = _id.mint_session_id(0, existing={fresh})
    assert again != fresh
    assert again.endswith("AAA")


def test_restore_reseeds_colliding_session(monkeypatch):
    from atomdoc import _id, Array, Doc, node

    @node
    class Item:
        label: str = ""

    @node
    class Root:
        items: Array[Item] = []

    monkeypatch.setattr(_id, "random_base64", lambda n: "AAA")
    monkeypatch.setattr(_id.time, "time", lambda: 1_700_000_000.0)

    doc = Doc(Root)
    with doc.transaction():
        doc.root.items.append(doc.create_node(Item, label="first"))
    first_id = doc.root.items[0].id
    session = _id.session_prefix(first_id)

    # Restored under identical clock and randomness the new session would
    # collide with the one already in the tree.
    restored = Doc.restore(doc.dump(), Root)
    assert restored._id_gen.session_id != session
    with restored.transaction():
        restored.root.items.append(restored.create_node(Item, label="second"))
    assert [i.label for i in restored.root.items] == ["first", "second"]
    assert len(restored._node_map) == 3
    assert restored.get_node_by_id(first_id).label == "first"
