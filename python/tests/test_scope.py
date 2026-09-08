"""Partial replication: scoped views, partial snapshots, projected
commits, scope changes, and out-of-scope rejections."""

import asyncio
from typing import Any

import pytest

from atomdoc import Array, Doc, Ref, node
from atomdoc._protocol import (
    MSG_CREATE,
    MSG_ERROR,
    MSG_OP,
    MSG_PATCH,
    MSG_SCHEMA,
    MSG_SCOPE,
    MSG_SCOPE_ACK,
    MSG_SNAPSHOT,
    MSG_UNDO,
)
from atomdoc._scope import parse_anchors
from atomdoc._session import Session
from atomdoc._transport import ClientConnection, Transport


# --- Schema: Page > Section > Item > Note; a Section may reference an Item ---


@node
class Note:
    text: str = ""


@node
class Item:
    label: str = ""
    notes: Array[Note] = []


@node
class Section:
    heading: str = ""
    related: Ref[Item] | None = None
    items: Array[Item] = []


@node
class Page:
    title: str = ""
    sections: Array[Section] = []


# --- Mock transport with partial clients ---


class MockClient(ClientConnection):
    def __init__(self, cid: str, *, partial: bool = False) -> None:
        self._client_id = cid
        self._partial = partial
        self.messages: list[dict[str, Any]] = []

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def wants_partial(self) -> bool:
        return self._partial

    async def send(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    async def close(self) -> None:
        pass


class MockTransport(Transport):
    async def start(self, on_connect, on_message, on_disconnect) -> None:
        self._on_connect = on_connect
        self._on_message = on_message
        self._on_disconnect = on_disconnect

    async def stop(self) -> None:
        pass

    async def connect(self, client: MockClient) -> None:
        await self._on_connect(client)

    async def send(self, client: MockClient, msg: dict[str, Any]) -> None:
        await self._on_message(client, msg)

    async def disconnect(self, client: MockClient) -> None:
        await self._on_disconnect(client)


def build_doc() -> tuple[Doc, dict[str, Any]]:
    """root > [s1 > [i1 > [n1], i2], s2 > [i3 > [n3]]]; s1.related -> i3."""
    doc = Doc(root_type=Page)
    ids: dict[str, Any] = {}
    with doc.transaction():
        doc.root.title = "Page"
        s1 = doc.create_node(Section, heading="One")
        s2 = doc.create_node(Section, heading="Two")
        doc.root.sections.append(s1)
        doc.root.sections.append(s2)
        i1 = doc.create_node(Item, label="A")
        i2 = doc.create_node(Item, label="B")
        s1.items.append(i1)
        s1.items.append(i2)
        n1 = doc.create_node(Note, text="note 1")
        i1.notes.append(n1)
        i3 = doc.create_node(Item, label="C")
        s2.items.append(i3)
        n3 = doc.create_node(Note, text="note 3")
        i3.notes.append(n3)
        s1.related = i3
    ids.update(s1=s1, s2=s2, i1=i1, i2=i2, i3=i3, n1=n1, n3=n3)
    return doc, ids


async def scoped_session(anchors: list[dict[str, Any]]):
    """A session with a whole-document client ``full`` and a scoped
    client ``part`` holding ``anchors`` (named by the keys of
    ``build_doc``, or ``"root"``); both message logs cleared."""
    doc, ids = build_doc()
    ids["root"] = doc.root
    anchors = [{**a, "id": ids[a["id"]].id} for a in anchors]
    session = Session(doc)
    transport = MockTransport()
    await session.bind(transport)
    full = MockClient("full")
    part = MockClient("part", partial=True)
    await transport.connect(full)
    await transport.connect(part)
    await transport.send(part, {"type": MSG_SCOPE, "ref": "scope-0", "anchors": anchors})
    full.messages.clear()
    part.messages.clear()
    return session, transport, full, part, ids


def ops_of(client: MockClient) -> list[dict[str, Any]]:
    return [m["operations"] for m in client.messages if m["type"] == MSG_PATCH]


def types_of(client: MockClient) -> list[str]:
    return [m["type"] for m in client.messages]


# --- Anchors ---


def test_parse_anchors_merges_overlaps_deeper_wins():
    assert parse_anchors([{"id": "a"}, {"id": "b", "depth": 1}, {"id": "b", "depth": 3}]) == {
        "a": None,
        "b": 3,
    }
    assert parse_anchors([{"id": "b", "depth": 1}, {"id": "b"}]) == {"b": None}
    with pytest.raises(ValueError):
        parse_anchors({"id": "a"})
    with pytest.raises(ValueError):
        parse_anchors([{"id": "a", "depth": -1}])
    with pytest.raises(ValueError):
        parse_anchors([{"id": "a", "depth": True}])
    with pytest.raises(ValueError):
        parse_anchors([{"depth": 1}])


# --- Partial snapshots ---


def test_dump_scope_whole_subtree_with_ancestor_and_referent_stubs():
    doc, ids = build_doc()
    data, stubs = doc.dump_scope([{"id": ids["s1"].id}])
    assert data == [
        doc.root.id,
        "Page",
        None,
        {
            "sections": [
                [
                    ids["s1"].id,
                    "Section",
                    {"heading": "One", "related": ids["i3"].id},
                    {
                        "items": [
                            [
                                ids["i1"].id,
                                "Item",
                                {"label": "A"},
                                {"notes": [[ids["n1"].id, "Note", {"text": "note 1"}]]},
                            ],
                            [ids["i2"].id, "Item", {"label": "B"}, {"notes": []}],
                        ]
                    },
                ],
            ],
        },
    ]
    # i3 is referenced by s1 but its parent s2 is not held: detached.
    assert stubs == [[ids["i3"].id, "Item"]]


def test_dump_scope_depth_zero_holds_children_as_stubs():
    doc, ids = build_doc()
    data, stubs = doc.dump_scope([{"id": doc.root.id, "depth": 0}])
    assert data == [
        doc.root.id,
        "Page",
        {"title": "Page"},
        {"sections": [[ids["s1"].id, "Section", None], [ids["s2"].id, "Section", None]]},
    ]
    assert stubs == []


def test_dump_scope_depth_one_and_referent_in_tree():
    doc, ids = build_doc()
    data, stubs = doc.dump_scope([{"id": ids["s1"].id, "depth": 0}, {"id": ids["s2"].id, "depth": 1}])
    sections = data[3]["sections"]
    assert sections[0][:3] == [ids["s1"].id, "Section", {"heading": "One", "related": ids["i3"].id}]
    assert sections[0][3] == {"items": [[ids["i1"].id, "Item", None], [ids["i2"].id, "Item", None]]}
    # s2 at depth 1: i3 full (also the referent), its note a stub.
    assert sections[1][3] == {
        "items": [[ids["i3"].id, "Item", {"label": "C"}, {"notes": [[ids["n3"].id, "Note", None]]}]]
    }
    assert stubs == []


def test_dump_scope_unknown_anchor_is_a_bare_root_stub():
    doc, ids = build_doc()
    data, stubs = doc.dump_scope([{"id": "nope"}])
    assert data == [doc.root.id, "Page", None]
    assert stubs == []


def test_dump_scope_root_anchor_is_the_whole_document():
    doc, ids = build_doc()
    data, stubs = doc.dump_scope([{"id": doc.root.id}])
    assert data == doc.dump()
    assert stubs == []


# --- Handshake ---


@pytest.mark.asyncio
async def test_partial_handshake_waits_for_scope():
    doc, ids = build_doc()
    session = Session(doc)
    transport = MockTransport()
    await session.bind(transport)
    part = MockClient("part", partial=True)
    await transport.connect(part)
    assert types_of(part) == [MSG_SCHEMA]
    # Anything but a scope is refused until the scope arrives.
    await transport.send(part, {"type": MSG_OP, "ref": "r1", "operations": {"ordered": [], "state": {}}})
    assert types_of(part) == [MSG_SCHEMA, MSG_ERROR]
    assert part.messages[1]["code"] == "no_scope"
    assert part.messages[1]["ref"] == "r1"
    await transport.send(part, {"type": MSG_SCOPE, "ref": "s1", "anchors": [{"id": ids["s1"].id}]})
    snap = part.messages[2]
    assert snap["type"] == MSG_SNAPSHOT
    assert snap["partial"] is True
    assert snap["client_id"] == "part"
    assert snap["ref"] == "s1"
    assert snap["anchors"] == [{"id": ids["s1"].id}]
    assert snap["stubs"] == [[ids["i3"].id, "Item"]]
    assert snap["data"][2] is None  # the root is a stub
    assert "part" in session.clients


@pytest.mark.asyncio
async def test_invalid_scope_is_invalid_op():
    doc, ids = build_doc()
    session = Session(doc)
    transport = MockTransport()
    await session.bind(transport)
    part = MockClient("part", partial=True)
    await transport.connect(part)
    await transport.send(part, {"type": MSG_SCOPE, "ref": "s1", "anchors": "all"})
    assert part.messages[-1]["code"] == "invalid_op"
    assert "part" not in session.clients


@pytest.mark.asyncio
async def test_commit_during_handshake_is_delivered_after_the_partial_snapshot():
    doc, ids = build_doc()
    session = Session(doc)
    transport = MockTransport()
    await session.bind(transport)
    part = MockClient("part", partial=True)
    await transport.connect(part)
    # A host commit before the scope: the snapshot will include it.
    with doc.transaction():
        ids["i1"].label = "A2"
    await transport.send(part, {"type": MSG_SCOPE, "ref": "s", "anchors": [{"id": ids["s1"].id}]})
    assert types_of(part) == [MSG_SCHEMA, MSG_SNAPSHOT]
    items = part.messages[1]["data"][3]["sections"][0][3]["items"]
    assert items[0][2] == {"label": "A2"}


# --- Projection of other clients' commits ---


@pytest.mark.asyncio
async def test_state_write_reaches_only_holders():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    await transport.send(full, {
        "type": MSG_OP, "ref": "f1",
        "operations": {"ordered": [], "state": {ids["i1"].id: {"label": "AA"}, ids["i3"].id: {"label": "CC"}}},
    })
    assert ops_of(full) == [{"ordered": [], "state": {ids["i1"].id: {"label": "AA"}, ids["i3"].id: {"label": "CC"}}}]
    # i3 is a stub for the scoped client: its write is not delivered.
    assert ops_of(part) == [{"ordered": [], "state": {ids["i1"].id: {"label": "AA"}}}]
    assert part.messages[0]["ref"] is None
    assert part.messages[0]["version"] == session.version


@pytest.mark.asyncio
async def test_write_outside_the_view_is_silent():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    await transport.send(full, {
        "type": MSG_OP, "ref": "f1",
        "operations": {"ordered": [], "state": {ids["n3"].id: {"text": "x"}}},
    })
    assert len(ops_of(full)) == 1
    assert part.messages == []


@pytest.mark.asyncio
async def test_create_under_held_parent_is_an_insert_with_state():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    await transport.send(full, {
        "type": MSG_CREATE, "ref": "c1", "node_type": "Item", "state": {"label": "D"},
        "parent_id": ids["s1"].id, "slot": "items",
    })
    new_id = session.doc.root.sections[0].items[2].id
    assert ops_of(part) == [{
        "ordered": [[0, [[new_id, "Item"]], ids["s1"].id, "items", ids["i2"].id, 0]],
        "state": {new_id: {"label": "D"}},
    }]
    # Under an unheld parent: nothing.
    await transport.send(full, {
        "type": MSG_CREATE, "ref": "c2", "node_type": "Item", "state": {"label": "E"},
        "parent_id": ids["s2"].id, "slot": "items",
    })
    assert len(part.messages) == 1


@pytest.mark.asyncio
async def test_insert_beside_unheld_siblings_is_anchored_to_held_ones():
    # The scoped client holds s1 at depth 0: i1, i2 are stubs, and a new
    # item between them is placed relative to them.
    session, transport, full, part, ids = await scoped_session([{"id": "s1", "depth": 0}])
    await transport.send(full, {
        "type": MSG_CREATE, "ref": "c1", "node_type": "Item", "state": {"label": "D"},
        "parent_id": ids["s1"].id, "slot": "items", "position": "after", "target_id": ids["i1"].id,
    })
    new_id = session.doc.root.sections[0].items[1].id
    # A child past the depth bound arrives as a stub, between its stub siblings.
    assert ops_of(part) == [{
        "ordered": [[0, [[new_id, "Item", None]], ids["s1"].id, "items", ids["i1"].id, ids["i2"].id]],
        "state": {},
    }]


@pytest.mark.asyncio
async def test_root_slot_insert_projects_prev_next_to_held_siblings():
    # Holding s2 only: root is a stub whose held child is s2. A new
    # section appended after s2 is out of scope (not under an anchor) and
    # so is not delivered; a section inserted before s1 is not either.
    session, transport, full, part, ids = await scoped_session([{"id": "s2"}])
    await transport.send(full, {
        "type": MSG_CREATE, "ref": "c1", "node_type": "Section", "state": {"heading": "Three"},
        "slot": "sections",
    })
    assert part.messages == []


@pytest.mark.asyncio
async def test_delete_of_held_node_and_of_unheld_node():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    # A note under s2: unheld, silent.
    await transport.send(full, {
        "type": MSG_OP, "ref": "f1", "operations": {"ordered": [[1, ids["n3"].id, 0]], "state": {}},
    })
    assert part.messages == []
    # i2, held: the delete is delivered as is.
    await transport.send(full, {
        "type": MSG_OP, "ref": "f2", "operations": {"ordered": [[1, ids["i2"].id, 0]], "state": {}},
    })
    assert ops_of(part) == [{"ordered": [[1, ids["i2"].id, 0]], "state": {}}]


@pytest.mark.asyncio
async def test_delete_of_an_unheld_ancestor_drops_held_descendants():
    # The scoped client holds i1 (anchor) under stub ancestors s1 and root.
    session, transport, full, part, ids = await scoped_session([{"id": "i1"}])
    await transport.send(full, {
        "type": MSG_OP, "ref": "f1", "operations": {"ordered": [[1, ids["s1"].id, 0]], "state": {}},
    })
    # s1 is an ancestor stub the client holds: the delete names it.
    assert ops_of(part) == [{"ordered": [[1, ids["s1"].id, 0]], "state": {}}]


@pytest.mark.asyncio
async def test_move_out_of_scope_is_an_exit_and_back_in_is_an_enter():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    # i2 moves under s2: it leaves the view.
    await transport.send(full, {
        "type": MSG_OP, "ref": "f1",
        "operations": {"ordered": [[2, ids["i2"].id, 0, ids["s2"].id, "items", 0, 0]], "state": {}},
    })
    assert ops_of(part) == [{"ordered": [[4, ids["i2"].id]], "state": {}}]
    part.messages.clear()
    # Its note gains a note meanwhile: unseen.
    await transport.send(full, {
        "type": MSG_CREATE, "ref": "c1", "node_type": "Note", "state": {"text": "n2"},
        "parent_id": ids["i2"].id, "slot": "notes",
    })
    assert part.messages == []
    n2 = ids["i2"].notes[0].id
    # Back under s1, before i1: it enters with its state and its subtree.
    await transport.send(full, {
        "type": MSG_OP, "ref": "f2",
        "operations": {"ordered": [[2, ids["i2"].id, 0, ids["s1"].id, "items", 0, ids["i1"].id]], "state": {}},
    })
    assert ops_of(part) == [{
        "ordered": [
            [0, [[ids["i2"].id, "Item"]], ids["s1"].id, "items", 0, ids["i1"].id],
            [0, [[n2, "Note"]], ids["i2"].id, "notes", 0, 0],
        ],
        "state": {ids["i2"].id: {"label": "B"}, n2: {"text": "n2"}},
    }]


@pytest.mark.asyncio
async def test_move_within_the_view_is_a_move_with_projected_neighbors():
    # Depth 0 on s1: i1, i2 are stubs; the client sees the move between them.
    session, transport, full, part, ids = await scoped_session([{"id": "s1", "depth": 0}])
    await transport.send(full, {
        "type": MSG_OP, "ref": "f1",
        "operations": {"ordered": [[2, ids["i2"].id, 0, ids["s1"].id, "items", 0, ids["i1"].id]], "state": {}},
    })
    assert ops_of(part) == [{
        "ordered": [[2, ids["i2"].id, 0, ids["s1"].id, "items", 0, ids["i1"].id]],
        "state": {},
    }]


@pytest.mark.asyncio
async def test_referenced_node_leaving_scope_becomes_a_detached_stub():
    # Hold s2 in full: i3 is full, and s2 references nothing. Make s2
    # reference i3, then move i3 out under s1 (unheld): i3 must stay
    # known as a detached stub.
    session, transport, full, part, ids = await scoped_session([{"id": "s2"}])
    await transport.send(full, {
        "type": MSG_OP, "ref": "f0",
        "operations": {"ordered": [], "state": {ids["s2"].id: {"related": ids["i3"].id}}},
    })
    assert ops_of(part) == [{"ordered": [], "state": {ids["s2"].id: {"related": ids["i3"].id}}}]
    part.messages.clear()
    await transport.send(full, {
        "type": MSG_OP, "ref": "f1",
        "operations": {"ordered": [[2, ids["i3"].id, 0, ids["s1"].id, "items", 0, 0]], "state": {}},
    })
    assert ops_of(part) == [{"ordered": [[5, ids["i3"].id, "Item"]], "state": {}}]
    part.messages.clear()
    # Clearing the reference drops the stub.
    await transport.send(full, {
        "type": MSG_OP, "ref": "f2",
        "operations": {"ordered": [], "state": {ids["s2"].id: {"related": None}}},
    })
    assert ops_of(part) == [{"ordered": [[4, ids["i3"].id]], "state": {ids["s2"].id: {"related": None}}}]


@pytest.mark.asyncio
async def test_new_reference_target_appears_as_a_detached_stub():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    # s1.related: i3 -> n3? No: Ref[Item]. Point it at i3's sibling-to-be.
    await transport.send(full, {
        "type": MSG_CREATE, "ref": "c1", "node_type": "Item", "state": {"label": "E"},
        "parent_id": ids["s2"].id, "slot": "items",
    })
    assert part.messages == []
    new_id = ids["s2"].items[1].id
    await transport.send(full, {
        "type": MSG_OP, "ref": "f1",
        "operations": {"ordered": [], "state": {ids["s1"].id: {"related": new_id}}},
    })
    assert ops_of(part) == [{
        "ordered": [[5, new_id, "Item"], [4, ids["i3"].id]],
        "state": {ids["s1"].id: {"related": new_id}},
    }]


# --- The scoped client's own requests ---


@pytest.mark.asyncio
async def test_scoped_client_op_is_echoed_by_ref():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    await transport.send(part, {
        "type": MSG_OP, "ref": "part:1",
        "operations": {"ordered": [], "state": {ids["i1"].id: {"label": "mine"}}},
    })
    assert types_of(part) == [MSG_PATCH]
    assert part.messages[0]["ref"] == "part:1"
    assert part.messages[0]["operations"] == {"ordered": [], "state": {ids["i1"].id: {"label": "mine"}}}
    assert ops_of(full) == [{"ordered": [], "state": {ids["i1"].id: {"label": "mine"}}}]


@pytest.mark.asyncio
async def test_scoped_client_noop_is_answered():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    await transport.send(part, {
        "type": MSG_OP, "ref": "part:1",
        "operations": {"ordered": [], "state": {ids["i1"].id: {"label": "A"}}},
    })
    assert types_of(part) == [MSG_PATCH]
    assert part.messages[0]["ref"] == "part:1"
    assert part.messages[0]["operations"]["ordered"] == []


@pytest.mark.asyncio
async def test_scoped_client_create_and_delete():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    await transport.send(part, {
        "type": MSG_CREATE, "ref": "part:1", "node_type": "Item", "state": {"label": "D"},
        "parent_id": ids["s1"].id, "slot": "items", "position": "prepend",
    })
    new_id = ids["s1"].items[0].id
    assert ops_of(part) == [{
        "ordered": [[0, [[new_id, "Item"]], ids["s1"].id, "items", 0, ids["i1"].id]],
        "state": {new_id: {"label": "D"}},
    }]
    assert part.messages[0]["ref"] == "part:1"
    part.messages.clear()
    await transport.send(part, {
        "type": MSG_OP, "ref": "part:2", "operations": {"ordered": [[1, new_id, 0]], "state": {}},
    })
    assert ops_of(part) == [{"ordered": [[1, new_id, 0]], "state": {}}]


@pytest.mark.asyncio
async def test_writes_to_stubs_are_out_of_scope_with_a_partial_resync():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    version = session.version
    cases = [
        {"ordered": [], "state": {ids["i3"].id: {"label": "x"}}},  # referent stub
        {"ordered": [], "state": {session.doc.root.id: {"title": "x"}}},  # ancestor stub
        {"ordered": [], "state": {ids["n3"].id: {"text": "x"}}},  # unknown
        {"ordered": [[1, ids["i3"].id, 0]], "state": {}},
        {"ordered": [[2, ids["i1"].id, 0, ids["s2"].id, "items", 0, 0]], "state": {}},
        {"ordered": [[2, ids["i3"].id, 0, ids["s1"].id, "items", 0, 0]], "state": {}},
        {"ordered": [[0, [["new1", "Item"]], 0, "sections", 0, 0]], "state": {}},
        {"ordered": [[0, [["new2", "Item", None]], ids["s1"].id, "items", 0, 0]], "state": {}},
        {"ordered": [[0, [["new3", "Item"]], ids["s1"].id, "items", ids["n3"].id, 0]], "state": {}},
    ]
    for i, operations in enumerate(cases):
        part.messages.clear()
        await transport.send(part, {"type": MSG_OP, "ref": f"part:{i}", "operations": operations})
        assert types_of(part) == [MSG_ERROR, MSG_SNAPSHOT], operations
        assert part.messages[0]["code"] == "out_of_scope"
        assert part.messages[0]["ref"] == f"part:{i}"
        assert part.messages[1]["partial"] is True
        assert "client_id" not in part.messages[1]
    assert session.version == version
    assert full.messages == []
    # A create under a stub, too.
    part.messages.clear()
    await transport.send(part, {
        "type": MSG_CREATE, "ref": "part:c", "node_type": "Section", "state": {}, "slot": "sections",
    })
    assert [m["code"] for m in part.messages if m["type"] == MSG_ERROR] == ["out_of_scope"]


@pytest.mark.asyncio
async def test_scoped_client_may_place_beside_a_stub():
    session, transport, full, part, ids = await scoped_session([{"id": "s1", "depth": 0}])
    await transport.send(part, {
        "type": MSG_OP, "ref": "part:1",
        "operations": {"ordered": [[0, [["new1", "Item"]], ids["s1"].id, "items", ids["i1"].id, 0]], "state": {}},
    })
    assert types_of(part) == [MSG_PATCH]
    assert [i.id for i in ids["s1"].items] == [ids["i1"].id, "new1", ids["i2"].id]
    # The new node is past the depth bound: the client gets it as a stub.
    assert ops_of(part) == [{
        "ordered": [[0, [["new1", "Item", None]], ids["s1"].id, "items", ids["i1"].id, ids["i2"].id]],
        "state": {},
    }]


@pytest.mark.asyncio
async def test_scoped_client_undo_is_projected():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    await transport.send(part, {
        "type": MSG_OP, "ref": "part:1",
        "operations": {"ordered": [[1, ids["i2"].id, 0]], "state": {}},
    })
    part.messages.clear()
    await transport.send(part, {"type": MSG_UNDO, "ref": "part:2"})
    assert types_of(part) == [MSG_PATCH]
    assert part.messages[0]["ref"] == "part:2"
    assert part.messages[0]["operations"] == {
        "ordered": [[0, [[ids["i2"].id, "Item"]], ids["s1"].id, "items", ids["i1"].id, 0]],
        "state": {ids["i2"].id: {"label": "B"}},
    }


# --- Scope changes ---


@pytest.mark.asyncio
async def test_deepening_upgrades_stubs_in_place():
    session, transport, full, part, ids = await scoped_session([{"id": "s1", "depth": 0}])
    await transport.send(part, {
        "type": MSG_SCOPE, "ref": "part:s", "anchors": [{"id": ids["s1"].id, "depth": 1}],
    })
    assert types_of(part) == [MSG_SCOPE_ACK]
    ack = part.messages[0]
    assert ack["ref"] == "part:s"
    assert ack["anchors"] == [{"id": ids["s1"].id, "depth": 1}]
    # The boundary stubs fill in place; the next level arrives as stubs.
    assert ack["operations"] == {
        "ordered": [
            [6, ids["i1"].id],
            [0, [[ids["n1"].id, "Note", None]], ids["i1"].id, "notes", 0, 0],
            [6, ids["i2"].id],
        ],
        "state": {ids["i1"].id: {"label": "A"}, ids["i2"].id: {"label": "B"}},
    }


@pytest.mark.asyncio
async def test_adding_an_anchor_enters_its_subtree_and_removing_exits_it():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    await transport.send(part, {
        "type": MSG_SCOPE, "ref": "part:s", "anchors": [{"id": ids["s1"].id}, {"id": ids["s2"].id}],
    })
    ack = part.messages[0]
    # s2 enters after s1 with its subtree; i3, held detached, is placed
    # and filled by the same insert.
    assert ack["operations"] == {
        "ordered": [
            [0, [[ids["s2"].id, "Section"]], 0, "sections", ids["s1"].id, 0],
            [0, [[ids["i3"].id, "Item"]], ids["s2"].id, "items", 0, 0],
            [0, [[ids["n3"].id, "Note"]], ids["i3"].id, "notes", 0, 0],
        ],
        "state": {
            ids["s2"].id: {"heading": "Two"},
            ids["i3"].id: {"label": "C"},
            ids["n3"].id: {"text": "note 3"},
        },
    }
    part.messages.clear()
    await transport.send(part, {"type": MSG_SCOPE, "ref": "part:t", "anchors": [{"id": ids["s2"].id}]})
    # s1 leaves; i3 stays (held in full under s2).
    assert part.messages[0]["operations"] == {"ordered": [[4, ids["s1"].id]], "state": {}}
    part.messages.clear()
    # And the scoped client can now edit under s2 but not s1.
    await transport.send(part, {
        "type": MSG_OP, "ref": "part:1",
        "operations": {"ordered": [], "state": {ids["i1"].id: {"label": "x"}}},
    })
    assert part.messages[0]["code"] == "out_of_scope"


@pytest.mark.asyncio
async def test_narrowing_to_depth_demotes_and_keeps_referents():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    await transport.send(part, {
        "type": MSG_SCOPE, "ref": "part:s", "anchors": [{"id": ids["s1"].id, "depth": 0}],
    })
    # i1 and i2 become stubs and i1's note leaves; i3 stays a detached stub.
    assert part.messages[0]["operations"] == {
        "ordered": [[3, ids["i1"].id], [3, ids["i2"].id], [4, ids["n1"].id]],
        "state": {},
    }


@pytest.mark.asyncio
async def test_whole_document_client_can_narrow():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    await transport.send(full, {"type": MSG_SCOPE, "ref": "full:s", "anchors": [{"id": ids["s2"].id}]})
    assert types_of(full) == [MSG_SCOPE_ACK]
    ops = full.messages[0]["operations"]
    # s1 leaves; the root becomes a stub (an ancestor of the anchor).
    assert ops == {"ordered": [[3, session.doc.root.id], [4, ids["s1"].id]], "state": {}}
    full.messages.clear()
    await transport.send(part, {
        "type": MSG_OP, "ref": "part:1",
        "operations": {"ordered": [], "state": {ids["i1"].id: {"label": "x"}}},
    })
    assert full.messages == []


@pytest.mark.asyncio
async def test_scope_change_with_no_difference_is_an_empty_ack():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    await transport.send(part, {"type": MSG_SCOPE, "ref": "part:s", "anchors": [{"id": ids["s1"].id}]})
    assert part.messages[0]["operations"] == {"ordered": [], "state": {}}
    assert part.messages[0]["version"] == session.version


@pytest.mark.asyncio
async def test_anchor_deleted_then_restored_by_undo():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    await transport.send(full, {
        "type": MSG_OP, "ref": "f1", "operations": {"ordered": [[1, ids["s1"].id, 0]], "state": {}},
    })
    # With s1 gone, nothing held references i3: the detached stub leaves.
    assert ops_of(part) == [{"ordered": [[1, ids["s1"].id, 0], [4, ids["i3"].id]], "state": {}}]
    part.messages.clear()
    await transport.send(full, {"type": MSG_UNDO, "ref": "f2"})
    # The anchor comes back into the view with its whole subtree.
    ops = ops_of(part)[0]
    assert ops["ordered"][0] == [0, [[ids["s1"].id, "Section"]], 0, "sections", 0, 0]
    assert ops["ordered"][1] == [0, [[ids["i1"].id, "Item"], [ids["i2"].id, "Item"]], ids["s1"].id, "items", 0, 0]
    assert ops["ordered"][2] == [0, [[ids["n1"].id, "Note"]], ids["i1"].id, "notes", 0, 0]
    assert ops["state"][ids["s1"].id] == {"heading": "One", "related": ids["i3"].id}
    # i3 is referenced again: a detached stub.
    assert [5, ids["i3"].id, "Item"] in ops["ordered"]


@pytest.mark.asyncio
async def test_disconnect_forgets_the_view():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    await transport.disconnect(part)
    assert "part" not in session.clients
    await transport.send(full, {
        "type": MSG_OP, "ref": "f1",
        "operations": {"ordered": [], "state": {ids["i1"].id: {"label": "x"}}},
    })
    assert part.messages == []


# --- Handshake edge cases ---


@pytest.mark.asyncio
async def test_scope_from_a_departed_client_leaves_no_view():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    await transport.disconnect(part)
    await transport.send(part, {"type": MSG_SCOPE, "ref": "late", "anchors": [{"id": ids["s1"].id}]})
    assert part.messages == []
    assert "part" not in session._views


@pytest.mark.asyncio
async def test_scope_during_a_whole_document_handshake_is_refused():
    doc, ids = build_doc()
    session = Session(doc)
    transport = MockTransport()
    await session.bind(transport)

    class Slow(MockClient):
        async def send(self, message):
            await asyncio.sleep(0)
            self.messages.append(message)

    client = Slow("slow")
    connect = asyncio.ensure_future(transport.connect(client))
    await asyncio.sleep(0)  # the schema is in flight; the snapshot is not sent yet
    await transport.send(client, {"type": MSG_SCOPE, "ref": "early", "anchors": [{"id": ids["s1"].id}]})
    await connect
    kinds = types_of(client)
    assert kinds.count(MSG_SNAPSHOT) == 1
    error = next(m for m in client.messages if m["type"] == MSG_ERROR)
    assert error["code"] == "invalid_op"
    assert kinds.index(MSG_ERROR) < kinds.index(MSG_SNAPSHOT) or client.messages[-1]["type"] == MSG_ERROR
    assert "slow" not in session._views


@pytest.mark.asyncio
async def test_two_scopes_in_flight_are_answered_in_order():
    doc, ids = build_doc()
    session = Session(doc)
    transport = MockTransport()
    await session.bind(transport)

    class Slow(MockClient):
        async def send(self, message):
            await asyncio.sleep(0)
            self.messages.append(message)

    part = Slow("part", partial=True)
    await transport.connect(part)
    await asyncio.gather(
        transport.send(part, {"type": MSG_SCOPE, "ref": "s0", "anchors": [{"id": ids["s1"].id}]}),
        transport.send(part, {"type": MSG_SCOPE, "ref": "s1", "anchors": [{"id": ids["s2"].id}]}),
    )
    assert types_of(part) == [MSG_SCHEMA, MSG_SNAPSHOT, MSG_SCOPE_ACK]
    assert part.messages[1]["ref"] == "s0"
    assert part.messages[2]["ref"] == "s1"
    assert part.messages[2]["operations"]["ordered"][0][0] == 0  # s2 enters


@pytest.mark.asyncio
async def test_type_filters_are_refused():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    await transport.send(part, {
        "type": MSG_SCOPE, "ref": "t", "anchors": [{"id": ids["s1"].id}], "types": ["Item"],
    })
    assert part.messages[0]["code"] == "invalid_op"


@pytest.mark.asyncio
async def test_dead_scoped_client_is_forgotten():
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])

    async def fail(message):
        raise ConnectionError("gone")

    part.send = fail  # type: ignore[method-assign]
    await transport.send(full, {
        "type": MSG_OP, "ref": "f1",
        "operations": {"ordered": [], "state": {ids["i1"].id: {"label": "x"}}},
    })
    assert "part" not in session.clients
    assert "part" not in session._views


def test_dump_scope_rejects_a_bare_anchor():
    doc, ids = build_doc()
    with pytest.raises(ValueError):
        doc.dump_scope({"id": ids["s1"].id})


@pytest.mark.asyncio
async def test_scoped_client_op_insert_carries_state_for_its_new_node():
    # What the thick client sends for createNode: the insert and the
    # node's state in one request.
    session, transport, full, part, ids = await scoped_session([{"id": "s1"}])
    await transport.send(part, {
        "type": MSG_OP, "ref": "part:1",
        "operations": {
            "ordered": [[0, [["new1", "Item"]], ids["s1"].id, "items", ids["i2"].id, 0]],
            "state": {"new1": {"label": "D"}},
        },
    })
    assert types_of(part) == [MSG_PATCH]
    assert part.messages[0]["operations"] == {
        "ordered": [[0, [["new1", "Item"]], ids["s1"].id, "items", ids["i2"].id, 0]],
        "state": {"new1": {"label": "D"}},
    }
    # But not for a node that exists, held or not.
    for existing in (ids["i3"].id, ids["i1"].id):
        part.messages.clear()
        await transport.send(part, {
            "type": MSG_OP, "ref": "part:2",
            "operations": {"ordered": [[0, [[existing, "Item"]], ids["s1"].id, "items", 0, 0]], "state": {}},
        })
        assert part.messages[0]["code"] == "out_of_scope"


# --- WebSocket handshake flag ---


def test_websocket_client_reads_partial_from_the_url():
    from types import SimpleNamespace

    from atomdoc._ws_transport import WebSocketClient

    def ws(path: str | None):
        return SimpleNamespace(request=SimpleNamespace(path=path))

    assert WebSocketClient(ws("/")).wants_partial is False
    assert WebSocketClient(ws("/?partial=1")).wants_partial is True
    assert WebSocketClient(ws("/doc?x=1&partial=true")).wants_partial is True
    assert WebSocketClient(ws("/?partial=0")).wants_partial is False
    assert WebSocketClient(ws(None)).wants_partial is False
    assert WebSocketClient(SimpleNamespace()).wants_partial is False
