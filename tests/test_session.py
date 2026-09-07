"""Tests for the Session manager with a mock transport."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from pydantic import BaseModel
from atomdoc import Array, Doc, Ref, node
from atomdoc._protocol import MSG_CREATE, MSG_ERROR, MSG_OP, MSG_PATCH, MSG_SCHEMA, MSG_SNAPSHOT, MSG_UNDO, MSG_REDO
from atomdoc._session import Session
from atomdoc._transport import ClientConnection, Transport


# --- Test schema ---


class Color(BaseModel, frozen=True):
    r: int = 0
    g: int = 0
    b: int = 0


@node
class Annotation:
    label: str = ""
    color: Color = Color()


@node
class Page:
    title: str = ""
    annotations: Array[Annotation] = []


# --- Mock transport ---


class MockClient(ClientConnection):
    def __init__(self, cid: str | None = None) -> None:
        self._client_id = cid or str(uuid4())
        self.messages: list[dict[str, Any]] = []
        self.closed = False

    @property
    def client_id(self) -> str:
        return self._client_id

    async def send(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    async def close(self) -> None:
        self.closed = True


class MockTransport(Transport):
    def __init__(self) -> None:
        self._on_connect = None
        self._on_message = None
        self._on_disconnect = None
        self.started = False
        self.stopped = False

    async def start(self, on_connect, on_message, on_disconnect) -> None:
        self._on_connect = on_connect
        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def connect_client(self, client: MockClient) -> None:
        await self._on_connect(client)

    async def send_message(self, client: MockClient, msg: dict[str, Any]) -> None:
        await self._on_message(client, msg)

    async def disconnect_client(self, client: MockClient) -> None:
        await self._on_disconnect(client)


# --- Helpers ---


@pytest.fixture
def doc():
    return Doc(root_type=Page)


@pytest.fixture
def session(doc):
    return Session(doc)


@pytest.fixture
def transport():
    return MockTransport()


async def setup_session(session, transport):
    await session.bind(transport)
    client = MockClient()
    await transport.connect_client(client)
    return client


# --- Tests ---


@pytest.mark.asyncio
async def test_bind_starts_transport(session, transport):
    await session.bind(transport)
    assert transport.started


@pytest.mark.asyncio
async def test_unbind_stops_transport(session, transport):
    await session.bind(transport)
    await session.unbind()
    assert transport.stopped


@pytest.mark.asyncio
async def test_connect_sends_schema_then_snapshot(session, transport):
    client = await setup_session(session, transport)
    assert len(client.messages) >= 2
    assert client.messages[0]["type"] == MSG_SCHEMA
    assert client.messages[1]["type"] == MSG_SNAPSHOT


@pytest.mark.asyncio
async def test_schema_contains_node_types(session, transport):
    client = await setup_session(session, transport)
    schema = client.messages[0]["schema"]
    assert "Page" in schema["node_types"]
    assert "Annotation" in schema["node_types"]


@pytest.mark.asyncio
async def test_snapshot_contains_doc_data(session, transport):
    client = await setup_session(session, transport)
    snapshot = client.messages[1]
    assert snapshot["doc_id"] == session.doc.id
    assert snapshot["version"] == 0
    assert isinstance(snapshot["data"], list)


@pytest.mark.asyncio
async def test_create_node(session, transport):
    client = await setup_session(session, transport)
    client.messages.clear()

    # Create a second client to receive the broadcast
    client2 = MockClient()
    await transport.connect_client(client2)
    client2.messages.clear()

    await transport.send_message(client, {
        "type": MSG_CREATE,
        "ref": "c1",
        "node_type": "Annotation",
        "state": {"label": "test"},
        "slot": "annotations",
    })

    # Client2 should receive a patch (client1 is excluded as source)
    assert len(client2.messages) == 1
    assert client2.messages[0]["type"] == MSG_PATCH
    assert client2.messages[0]["version"] == 1

    # Verify node was created in doc
    root = session.doc.root
    assert len(root.annotations) == 1
    assert root.annotations[0].label == "test"


@pytest.mark.asyncio
async def test_create_with_parent_id(session, transport):
    client = await setup_session(session, transport)

    # First create an annotation
    await transport.send_message(client, {
        "type": MSG_CREATE,
        "ref": "c1",
        "node_type": "Annotation",
        "state": {"label": "first"},
        "slot": "annotations",
    })

    assert len(session.doc.root.annotations) == 1


@pytest.mark.asyncio
async def test_op_applies_state_patch(session, transport):
    client = await setup_session(session, transport)

    # First create a node so we have something to patch
    with session.doc.transaction():
        ann = session.doc.create_node(Annotation, label="old")
        session.doc._insert_into_slot(session.doc.root, "annotations", "append", [ann])

    node_id = session.doc.root.annotations[0].id
    client.messages.clear()

    client2 = MockClient()
    await transport.connect_client(client2)
    client2.messages.clear()

    await transport.send_message(client, {
        "type": MSG_OP,
        "ref": "op1",
        "operations": {
            "ordered": [],
            "state": {node_id: {"label": "new"}},
        },
    })

    assert session.doc.root.annotations[0].label == "new"
    assert len(client2.messages) == 1
    assert client2.messages[0]["type"] == MSG_PATCH


@pytest.mark.asyncio
async def test_undo_redo(session, transport):
    client = await setup_session(session, transport)

    # Make a change
    await transport.send_message(client, {
        "type": MSG_CREATE,
        "ref": "c1",
        "node_type": "Annotation",
        "state": {"label": "to_undo"},
        "slot": "annotations",
    })
    assert len(session.doc.root.annotations) == 1

    # Undo
    await transport.send_message(client, {"type": MSG_UNDO, "ref": "u1"})
    assert len(session.doc.root.annotations) == 0

    # Redo
    await transport.send_message(client, {"type": MSG_REDO, "ref": "r1"})
    assert len(session.doc.root.annotations) == 1


@pytest.mark.asyncio
async def test_error_on_unknown_type(session, transport):
    client = await setup_session(session, transport)
    client.messages.clear()

    await transport.send_message(client, {"type": "bogus", "ref": "x"})
    assert len(client.messages) == 1
    assert client.messages[0]["type"] == MSG_ERROR
    assert client.messages[0]["code"] == "unknown_type"


@pytest.mark.asyncio
async def test_error_on_invalid_create(session, transport):
    client = await setup_session(session, transport)
    client.messages.clear()

    await transport.send_message(client, {
        "type": MSG_CREATE,
        "ref": "bad",
        "node_type": "NonExistent",
        "state": {},
        "slot": "annotations",
    })
    assert len(client.messages) == 1
    assert client.messages[0]["type"] == MSG_ERROR
    assert client.messages[0]["ref"] == "bad"


@pytest.mark.asyncio
async def test_version_increments(session, transport):
    client = await setup_session(session, transport)
    assert session.version == 0

    await transport.send_message(client, {
        "type": MSG_CREATE,
        "ref": "c1",
        "node_type": "Annotation",
        "state": {"label": "a"},
        "slot": "annotations",
    })
    assert session.version == 1

    await transport.send_message(client, {
        "type": MSG_CREATE,
        "ref": "c2",
        "node_type": "Annotation",
        "state": {"label": "b"},
        "slot": "annotations",
    })
    assert session.version == 2


@pytest.mark.asyncio
async def test_disconnect_removes_client(session, transport):
    client = await setup_session(session, transport)
    assert client.client_id in session.clients

    await transport.disconnect_client(client)
    assert client.client_id not in session.clients


@pytest.mark.asyncio
async def test_broadcast_includes_source(session, transport):
    client1 = await setup_session(session, transport)
    client1.messages.clear()

    client2 = MockClient()
    await transport.connect_client(client2)
    client2.messages.clear()

    await transport.send_message(client1, {
        "type": MSG_CREATE,
        "ref": "c1",
        "node_type": "Annotation",
        "state": {"label": "test"},
        "slot": "annotations",
    })

    # Both clients get the patch (thin clients need the echo;
    # thick clients skip self-echoes via source_client field)
    assert any(m["type"] == MSG_PATCH for m in client1.messages)
    assert any(m["type"] == MSG_PATCH for m in client2.messages)

    # A ``create`` is not the requester's echo: the server minted the
    # node, the client never applied it locally. The request ref is
    # carried so the client can match the reply.
    patch = next(m for m in client1.messages if m["type"] == MSG_PATCH)
    assert patch["source_client"] is None
    assert patch["ref"] == "c1"


# --- Rejection and resync ---


@node
class Transform:
    name: str = ""


@node
class Volume:
    transform: Ref[Transform] | None = None


@node
class Scene:
    transforms: Array[Transform] = []
    volumes: Array[Volume] = []


async def setup_scene_session():
    doc = Doc(root_type=Scene)
    with doc.transaction():
        t = doc.create_node(Transform, name="t")
        doc.root.transforms.append(t)
        v = doc.create_node(Volume, transform=t)
        doc.root.volumes.append(v)
    session = Session(doc)
    transport = MockTransport()
    await session.bind(transport)
    a = MockClient("a")
    b = MockClient("b")
    await transport.connect_client(a)
    await transport.connect_client(b)
    a.messages.clear()
    b.messages.clear()
    return session, transport, a, b, t, v


@pytest.mark.asyncio
async def test_rejected_op_gets_error_then_snapshot():
    session, transport, a, b, t, v = await setup_scene_session()
    version_before = session.version

    # Delete a transform that a volume still references.
    await transport.send_message(a, {
        "type": MSG_OP,
        "ref": "op-1",
        "operations": {"ordered": [[1, t.id, 0]], "state": {}},
    })

    assert [m["type"] for m in a.messages] == [MSG_ERROR, MSG_SNAPSHOT]
    err, snap = a.messages
    assert err["code"] == "rejected"
    assert err["ref"] == "op-1"
    assert "still referenced" in err["message"]
    assert snap["version"] == version_before
    assert snap["data"] == session.doc.dump()
    assert "client_id" not in snap
    # Nothing was applied or broadcast.
    assert session.doc.get_node_by_id(t.id) is not None
    assert session.version == version_before
    assert b.messages == []


@pytest.mark.asyncio
async def test_rejected_create_with_dangling_reference():
    session, transport, a, b, t, v = await setup_scene_session()
    await transport.send_message(a, {
        "type": MSG_CREATE,
        "ref": "c-1",
        "node_type": "Volume",
        "state": {"transform": "ghost"},
        "slot": "volumes",
    })
    assert [m["type"] for m in a.messages] == [MSG_ERROR, MSG_SNAPSHOT]
    assert a.messages[0]["code"] == "rejected"
    assert len(session.doc.root.volumes) == 1
    assert b.messages == []


@pytest.mark.asyncio
async def test_malformed_request_is_invalid_op_without_snapshot():
    session, transport, a, b, t, v = await setup_scene_session()
    await transport.send_message(a, {"type": MSG_OP, "ref": "x"})  # no operations
    assert [m["type"] for m in a.messages] == [MSG_ERROR]
    assert a.messages[0]["code"] == "invalid_op"


@pytest.mark.asyncio
async def test_valid_op_after_rejection_still_broadcasts():
    session, transport, a, b, t, v = await setup_scene_session()
    await transport.send_message(a, {
        "type": MSG_OP, "ref": "bad",
        "operations": {"ordered": [[1, t.id, 0]], "state": {}},
    })
    a.messages.clear()
    await transport.send_message(a, {
        "type": MSG_OP, "ref": "good",
        "operations": {"ordered": [], "state": {v.id: {"transform": None}}},
    })
    assert [m["type"] for m in a.messages] == [MSG_PATCH]
    assert [m["type"] for m in b.messages] == [MSG_PATCH]
    assert session.doc.get_node_by_id(v.id).transform is None


# --- Second adversarial review: session ---


class YieldingClient(MockClient):
    """A client whose send yields to the loop, like a real transport."""

    async def send(self, message: dict[str, Any]) -> None:
        await asyncio.sleep(0)
        self.messages.append(message)


class DeadClient(MockClient):
    async def send(self, message: dict[str, Any]) -> None:
        raise ConnectionResetError("gone")


def _patch_versions(client: MockClient) -> list[int]:
    return [m["version"] for m in client.messages if m["type"] == MSG_PATCH]


async def settle(session: Session) -> None:
    """Wait for the flushes a host-side commit scheduled."""
    while session._flush_tasks:
        await asyncio.gather(*session._flush_tasks)


@pytest.mark.asyncio
async def test_undo_and_redo_patches_are_not_labelled_as_echo():
    session, transport, a, b, t, v = await setup_scene_session()
    await transport.send_message(a, {
        "type": MSG_OP, "ref": "r1",
        "operations": {"ordered": [], "state": {t.id: {"name": "x"}}},
    })
    a.messages.clear()
    b.messages.clear()
    await transport.send_message(a, {"type": MSG_UNDO, "ref": "u1"})
    for client in (a, b):
        patch = next(m for m in client.messages if m["type"] == MSG_PATCH)
        assert patch["source_client"] is None
        assert patch["ref"] == "u1"
    a.messages.clear()
    await transport.send_message(a, {"type": MSG_REDO, "ref": "d1"})
    patch = next(m for m in a.messages if m["type"] == MSG_PATCH)
    assert patch["source_client"] is None
    assert patch["ref"] == "d1"


# --- undo policy ---


def _set_name(client_ref: str, node_id: str, name: str) -> dict[str, Any]:
    return {
        "type": MSG_OP, "ref": client_ref,
        "operations": {"ordered": [], "state": {node_id: {"name": name}}},
    }


@pytest.mark.asyncio
async def test_per_client_undo_reverts_only_own_commits():
    session, transport, a, b, t, v = await setup_scene_session()
    assert session.undo_policy == "per-client"
    node = session.doc.get_node_by_id(t.id)
    await transport.send_message(a, _set_name("a1", t.id, "from a"))
    await transport.send_message(b, _set_name("b1", t.id, "from b"))
    a.messages.clear()
    b.messages.clear()

    # b has nothing of a's to undo; a's undo reverts a's own commit only.
    await transport.send_message(a, {"type": MSG_UNDO, "ref": "ua"})
    assert node.name == "t"  # a's inverse restores the value a saw
    assert [m["type"] for m in a.messages] == [MSG_PATCH]
    assert [m["type"] for m in b.messages] == [MSG_PATCH]
    a.messages.clear()
    b.messages.clear()

    # b's undo reverts b's commit, which a's undo did not touch.
    await transport.send_message(b, {"type": MSG_UNDO, "ref": "ub"})
    assert [m["type"] for m in b.messages] == [MSG_PATCH]
    assert node.name == "from a"

    # Nothing left for b: a no-op, no patch, no error.
    b.messages.clear()
    await transport.send_message(b, {"type": MSG_UNDO, "ref": "ub2"})
    assert b.messages == []


@pytest.mark.asyncio
async def test_per_client_redo_survives_other_clients_edits():
    session, transport, a, b, t, v = await setup_scene_session()
    node = session.doc.get_node_by_id(t.id)
    await transport.send_message(a, _set_name("a1", t.id, "from a"))
    await transport.send_message(a, {"type": MSG_UNDO, "ref": "ua"})
    assert node.name == "t"
    # Another client edits a different node; a keeps its redo.
    await transport.send_message(b, _set_name("b1", v.id, "vol"))
    a.messages.clear()
    await transport.send_message(a, {"type": MSG_REDO, "ref": "ra"})
    assert node.name == "from a"
    assert [m["type"] for m in a.messages] == [MSG_PATCH]


@pytest.mark.asyncio
async def test_per_client_undo_conflict_is_rejected_without_resync_and_kept():
    session, transport, a, b, t, v = await setup_scene_session()
    # a creates a node, b references it, a's undo (delete) can't apply.
    await transport.send_message(a, {
        "type": MSG_CREATE, "ref": "c1", "node_type": "Transform",
        "state": {"name": "new"}, "slot": "transforms",
    })
    patch = next(m for m in a.messages if m["type"] == MSG_PATCH)
    new_id = patch["operations"]["ordered"][0][1][0][0]
    await transport.send_message(b, {
        "type": MSG_OP, "ref": "b1",
        "operations": {"ordered": [], "state": {v.id: {"transform": new_id}}},
    })
    a.messages.clear()
    b.messages.clear()
    await transport.send_message(a, {"type": MSG_UNDO, "ref": "ua"})
    assert [m["type"] for m in a.messages] == [MSG_ERROR]
    assert a.messages[0]["code"] == "rejected"
    assert a.messages[0]["ref"] == "ua"
    assert b.messages == []
    assert session.doc.get_node_by_id(new_id) is not None
    # Once the reference is gone the kept step applies.
    await transport.send_message(b, {
        "type": MSG_OP, "ref": "b2",
        "operations": {"ordered": [], "state": {v.id: {"transform": t.id}}},
    })
    a.messages.clear()
    await transport.send_message(a, {"type": MSG_UNDO, "ref": "ua2"})
    assert [m["type"] for m in a.messages] == [MSG_PATCH]
    assert session.doc.get_node_by_id(new_id) is None


@pytest.mark.asyncio
async def test_per_client_history_is_dropped_on_disconnect():
    session, transport, a, b, t, v = await setup_scene_session()
    await transport.send_message(a, _set_name("a1", t.id, "from a"))
    await transport.disconnect_client(a)
    assert "a" not in session._client_undo
    a2 = MockClient("a")
    await transport.connect_client(a2)
    a2.messages.clear()
    await transport.send_message(a2, {"type": MSG_UNDO, "ref": "u"})
    assert a2.messages == []
    assert session.doc.get_node_by_id(t.id).name == "from a"


@pytest.mark.asyncio
async def test_global_undo_policy_reverts_anyones_commit():
    doc = Doc(root_type=Scene)
    with doc.transaction():
        t = doc.create_node(Transform, name="t")
        doc.root.transforms.append(t)
    session = Session(doc, undo="global")
    transport = MockTransport()
    await session.bind(transport)
    a, b = MockClient("a"), MockClient("b")
    await transport.connect_client(a)
    await transport.connect_client(b)
    await transport.send_message(a, _set_name("a1", t.id, "from a"))
    await transport.send_message(b, {"type": MSG_UNDO, "ref": "ub"})
    assert doc.get_node_by_id(t.id).name == "t"
    # Host edits are on the global stack too.
    with doc.transaction():
        doc.get_node_by_id(t.id).name = "host"
    await transport.send_message(a, {"type": MSG_UNDO, "ref": "ua"})
    assert doc.get_node_by_id(t.id).name == "t"


@pytest.mark.asyncio
async def test_undo_none_policy_is_unsupported():
    doc = Doc(root_type=Scene)
    session = Session(doc, undo="none")
    transport = MockTransport()
    await session.bind(transport)
    a = MockClient("a")
    await transport.connect_client(a)
    a.messages.clear()
    await transport.send_message(a, {"type": MSG_UNDO, "ref": "u"})
    assert a.messages[0]["type"] == MSG_ERROR
    assert a.messages[0]["code"] == "unsupported"
    assert a.messages[0]["ref"] == "u"
    await transport.send_message(a, {"type": MSG_REDO, "ref": "r"})
    assert a.messages[1]["code"] == "unsupported"


@pytest.mark.asyncio
async def test_undo_steps_must_be_a_positive_integer():
    session, transport, a, b, t, v = await setup_scene_session()
    await transport.send_message(a, {"type": MSG_UNDO, "ref": "u", "steps": 0})
    assert a.messages[0]["code"] == "invalid_op"
    await transport.send_message(a, {"type": MSG_UNDO, "ref": "u", "steps": "3"})
    assert a.messages[1]["code"] == "invalid_op"


def test_unknown_undo_policy_rejected():
    with pytest.raises(ValueError, match="undo policy"):
        Session(Doc(root_type=Scene), undo="everyone")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_minimal_op_frame_is_still_its_own_echo():
    session, transport, a, b, t, v = await setup_scene_session()
    await transport.send_message(a, {
        "type": MSG_OP, "ref": "r1",
        "operations": {"state": {t.id: {"name": "x"}}},
    })
    patch = next(m for m in a.messages if m["type"] == MSG_PATCH)
    assert patch["source_client"] == "a"
    assert patch["ref"] == "r1"


@pytest.mark.asyncio
async def test_connecting_client_never_gets_a_patch_older_than_its_snapshot():
    session, transport, a, b, t, v = await setup_scene_session()
    newbie = YieldingClient("n")
    connect = asyncio.ensure_future(transport.connect_client(newbie))
    await asyncio.sleep(0)  # handshake is in flight, snapshot taken
    await transport.send_message(a, {
        "type": MSG_OP, "ref": "r1",
        "operations": {"ordered": [], "state": {t.id: {"name": "during"}}},
    })
    await connect
    snapshot = next(m for m in newbie.messages if m["type"] == MSG_SNAPSHOT)
    versions = _patch_versions(newbie)
    assert all(ver > snapshot["version"] for ver in versions)
    # The change is delivered exactly once, either in the snapshot or as a
    # patch after it.
    in_snapshot = any(
        entry[2].get("name") == "during" for entry in snapshot["data"][3]["transforms"]
    )
    assert in_snapshot != bool(versions)
    types = [m["type"] for m in newbie.messages]
    assert types.index(MSG_SNAPSHOT) < len(types) - len(versions)


@pytest.mark.asyncio
async def test_resync_snapshot_is_newer_than_everything_queued():
    session, transport, a, b, t, v = await setup_scene_session()
    # A host-side edit outside any request (no loop flush yet in this
    # synchronous block).
    with session.doc.transaction():
        session.doc.get_node_by_id(t.id).name = "server-side"
    await settle(session)
    assert _patch_versions(a) == [1]
    await transport.send_message(a, {
        "type": MSG_OP, "ref": "bad",
        "operations": {"ordered": [[1, "nope", 0]], "state": {}},
    })
    snapshot = next(m for m in a.messages if m["type"] == MSG_SNAPSHOT)
    assert snapshot["version"] == 1
    await transport.send_message(b, {
        "type": MSG_OP, "ref": "ok",
        "operations": {"ordered": [], "state": {t.id: {"name": "after"}}},
    })
    later = [ver for ver in _patch_versions(a) if ver > 1]
    assert later == [2]


@pytest.mark.asyncio
async def test_host_side_commit_is_broadcast_without_a_client_message():
    session, transport, a, b, t, v = await setup_scene_session()
    with session.doc.transaction():
        session.doc.get_node_by_id(t.id).name = "host"
    await settle(session)
    for client in (a, b):
        patch = next(m for m in client.messages if m["type"] == MSG_PATCH)
        assert patch["source_client"] is None
        assert patch["ref"] is None


@pytest.mark.asyncio
async def test_hostile_frame_does_not_poison_later_broadcasts():
    session, transport, a, b, t, v = await setup_scene_session()
    deep: Any = []
    for _ in range(20000):
        deep = [deep]
    await transport.send_message(a, {
        "type": MSG_OP, "ref": "deep",
        "operations": {"ordered": [], "state": {"nope": {"k": deep}}},
    })
    assert any(m["type"] == MSG_ERROR for m in a.messages)
    assert session._request is None
    a.messages.clear()
    with session.doc.transaction():
        session.doc.get_node_by_id(t.id).name = "host"
    await settle(session)
    patch = next(m for m in a.messages if m["type"] == MSG_PATCH)
    assert patch["source_client"] is None


@pytest.mark.asyncio
async def test_malformed_operations_are_rejected_not_dropped():
    session, transport, a, b, t, v = await setup_scene_session()
    for bad in (
        {"ordered": [[99, "whatever", 0]], "state": {}},
        {"ordered": "abc", "state": {}},
        "not a dict",
        {"ordered": [], "state": {t.id: {"__evil__": {"any": "json"}}}},
    ):
        a.messages.clear()
        await transport.send_message(a, {"type": MSG_OP, "ref": "x", "operations": bad})
        assert a.messages, bad
        assert a.messages[0]["type"] == MSG_ERROR
        assert a.messages[0]["ref"] == "x"
    assert "__evil__" not in session.doc.get_node_by_id(t.id)._state
    assert b.messages == []


@pytest.mark.asyncio
async def test_create_respects_slot_allowed_type():
    session, transport, a, b, t, v = await setup_scene_session()
    await transport.send_message(a, {
        "type": MSG_CREATE, "ref": "c", "node_type": "Transform", "slot": "volumes",
    })
    assert a.messages[0]["type"] == MSG_ERROR
    assert a.messages[0]["code"] == "rejected"
    assert [type(n).__name__ for n in session.doc.root.volumes] == ["Volume"]


@pytest.mark.asyncio
async def test_failed_handshake_leaves_no_client_behind():
    session, transport, a, b, t, v = await setup_scene_session()
    dead = DeadClient("dead")
    with pytest.raises(ConnectionResetError):
        await transport.connect_client(dead)
    assert "dead" not in session.clients
    assert "dead" not in session._connecting
    with session.doc.transaction():
        session.doc.get_node_by_id(t.id).name = "host"
    await settle(session)
    assert _patch_versions(a) == [1]
