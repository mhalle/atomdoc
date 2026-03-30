"""Tests for the Session manager with a mock transport."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from pydantic import BaseModel
from atomdoc import Array, Doc, node
from atomdoc._protocol import MSG_CREATE, MSG_ERROR, MSG_OP, MSG_PATCH, MSG_SCHEMA, MSG_SNAPSHOT, MSG_UNDO, MSG_REDO, operations_to_wire
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

    import json
    await transport.send_message(client, {
        "type": MSG_OP,
        "ref": "op1",
        "operations": {
            "ordered": [],
            "state": {node_id: {"label": json.dumps("new")}},
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

    # Source client's patch has source_client set
    patch = next(m for m in client1.messages if m["type"] == MSG_PATCH)
    assert patch["source_client"] == client1.client_id
