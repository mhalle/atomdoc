"""Session manager: connects a Doc to clients via a transport."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ._doc import Doc
from ._protocol import (
    MSG_CREATE,
    MSG_ERROR,
    MSG_OP,
    MSG_PATCH,
    MSG_REDO,
    MSG_SCHEMA,
    MSG_SNAPSHOT,
    MSG_UNDO,
    operations_from_wire,
    operations_to_wire,
)
from ._transport import ClientConnection, Transport
from ._types import ChangeEvent
from ._undo import UndoManager

logger = logging.getLogger(__name__)


class Session:
    """Manages a Doc and its connected clients.

    The session is the single authority for a document.  Clients connect
    via a :class:`Transport`, receive the schema and a snapshot, then
    send operations which the session applies and broadcasts.
    """

    def __init__(
        self,
        doc: Doc,
        undo_manager: UndoManager | None = None,
    ) -> None:
        self._doc = doc
        self._undo = undo_manager or UndoManager(doc)
        self._clients: dict[str, ClientConnection] = {}
        self._version: int = 0
        self._transport: Transport | None = None

        # Pending broadcast data set synchronously by the on_change callback
        # and consumed by the async message handler.
        self._pending_broadcast: dict[str, Any] | None = None
        self._current_client_id: str | None = None

        # Cache the schema so we don't rebuild it on every connect.
        self._cached_schema: dict[str, Any] | None = None

        self._doc.on_change(self._on_doc_change)

    @property
    def doc(self) -> Doc:
        return self._doc

    @property
    def version(self) -> int:
        return self._version

    @property
    def clients(self) -> dict[str, ClientConnection]:
        return dict(self._clients)

    # --- Transport binding ---

    async def bind(self, transport: Transport) -> None:
        """Bind a transport and start accepting connections."""
        self._transport = transport
        await transport.start(
            self._handle_connect,
            self._handle_message,
            self._handle_disconnect,
        )

    async def unbind(self) -> None:
        """Stop the transport and disconnect all clients."""
        if self._transport is not None:
            await self._transport.stop()
            self._transport = None
        self._clients.clear()

    # --- Doc change listener (synchronous) ---

    def _on_doc_change(self, event: ChangeEvent) -> None:
        """Called synchronously by Doc after a transaction commits."""
        self._version += 1
        self._pending_broadcast = {
            "type": MSG_PATCH,
            "version": self._version,
            "operations": operations_to_wire(event.operations),
            "source_client": self._current_client_id,
        }

    # --- Transport callbacks ---

    async def _handle_connect(self, client: ClientConnection) -> None:
        self._clients[client.client_id] = client

        # Send schema (cached after first build)
        if self._cached_schema is None:
            self._cached_schema = self._doc.atomdoc_schema()
        await client.send({"type": MSG_SCHEMA, "schema": self._cached_schema})

        # Send snapshot (includes client_id so the client can identify self-echoes)
        await client.send({
            "type": MSG_SNAPSHOT,
            "doc_id": self._doc.id,
            "version": self._version,
            "data": self._doc.dump(),
            "client_id": client.client_id,
        })

    async def _handle_message(
        self, client: ClientConnection, msg: dict[str, Any]
    ) -> None:
        msg_type = msg.get("type")
        ref = msg.get("ref")

        try:
            if msg_type == MSG_OP:
                self._apply_op(client, msg)
            elif msg_type == MSG_CREATE:
                self._handle_create(client, msg)
            elif msg_type == MSG_UNDO:
                self._handle_undo(client, msg)
            elif msg_type == MSG_REDO:
                self._handle_redo(client, msg)
            else:
                await client.send({
                    "type": MSG_ERROR,
                    "ref": ref,
                    "code": "unknown_type",
                    "message": f"Unknown message type: {msg_type}",
                })
                return
        except Exception as exc:
            logger.exception("Error handling message from %s", client.client_id)
            await client.send({
                "type": MSG_ERROR,
                "ref": ref,
                "code": "invalid_op",
                "message": str(exc),
            })
            return

        # Broadcast the patch to ALL clients (including the source).
        # Thin clients need the echo to update their store.
        # Thick clients skip self-echoes via the source_client field.
        await self._flush_broadcast()

    async def _handle_disconnect(self, client: ClientConnection) -> None:
        self._clients.pop(client.client_id, None)

    # --- Message handlers ---

    def _apply_op(self, client: ClientConnection, msg: dict[str, Any]) -> None:
        ops = operations_from_wire(msg["operations"])
        self._current_client_id = client.client_id
        try:
            self._doc.apply_operations(ops)
        finally:
            self._current_client_id = None

    def _handle_create(self, client: ClientConnection, msg: dict[str, Any]) -> None:
        node_type = msg["node_type"]
        state = msg.get("state", {})
        parent_id = msg.get("parent_id")
        slot = msg["slot"]
        position = msg.get("position", "append")
        target_id = msg.get("target_id")

        node_cls = self._doc._node_types.get(node_type)
        if node_cls is None:
            raise ValueError(f"Unknown node type: {node_type!r}")

        self._current_client_id = client.client_id
        try:
            with self._doc.transaction():
                new_node = self._doc.create_node(node_cls, **state)
                parent = (
                    self._doc.get_node_by_id(parent_id)
                    if parent_id
                    else self._doc.root
                )
                if parent is None:
                    raise ValueError(f"Parent node not found: {parent_id!r}")

                target = None
                if target_id:
                    target = self._doc.get_node_by_id(target_id)

                self._doc._insert_into_slot(
                    parent, slot, position, [new_node], target=target
                )
        finally:
            self._current_client_id = None

    def _handle_undo(self, client: ClientConnection, msg: dict[str, Any]) -> None:
        steps = msg.get("steps", 1)
        self._current_client_id = client.client_id
        try:
            for _ in range(steps):
                if not self._undo.can_undo:
                    break
                self._undo.undo()
        finally:
            self._current_client_id = None

    def _handle_redo(self, client: ClientConnection, msg: dict[str, Any]) -> None:
        steps = msg.get("steps", 1)
        self._current_client_id = client.client_id
        try:
            for _ in range(steps):
                if not self._undo.can_redo:
                    break
                self._undo.redo()
        finally:
            self._current_client_id = None

    # --- Broadcasting ---

    async def _flush_broadcast(self, exclude: str | None = None) -> None:
        """Send pending broadcast if one was produced by the Doc change."""
        broadcast = self._pending_broadcast
        if broadcast is None:
            return
        self._pending_broadcast = None
        await self._broadcast(broadcast, exclude=exclude)

    async def _broadcast(
        self, message: dict[str, Any], exclude: str | None = None
    ) -> None:
        """Send a message to all connected clients, optionally excluding one."""
        tasks = []
        for cid, client in self._clients.items():
            if cid == exclude:
                continue
            tasks.append(self._safe_send(client, message))
        if tasks:
            await asyncio.gather(*tasks)

    async def _safe_send(
        self, client: ClientConnection, message: dict[str, Any]
    ) -> None:
        try:
            await client.send(message)
        except Exception:
            logger.warning(
                "Removing dead client %s", client.client_id
            )
            self._clients.pop(client.client_id, None)
