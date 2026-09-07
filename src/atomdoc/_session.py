"""Session manager: connects a Doc to clients via a transport."""

from __future__ import annotations

import asyncio
import json
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


def _normalize(value: Any) -> Any:
    """JSON-normalize an operations payload for comparison (tuples vs
    lists, ints vs floats)."""
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError):
        return value


class _Rejected(Exception):
    """A well-formed request the document refused to apply."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


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
        if undo_manager is not None:
            self._undo = undo_manager
        elif doc.undo_manager.is_enabled:
            self._undo = doc.undo_manager
        else:
            self._undo = UndoManager(doc)
        self._clients: dict[str, ClientConnection] = {}
        self._version: int = 0
        self._transport: Transport | None = None

        # Pending broadcasts set synchronously by the on_change callback
        # and consumed by the async message handler. One request can
        # commit more than once (a multi-step undo), hence a list. Each
        # entry records the clients connected at commit time: a client
        # that connects later gets the change in its snapshot instead.
        self._pending_broadcasts: list[tuple[dict[str, Any], list[str]]] = []
        self._current_client_id: str | None = None
        # The operations the current request sent, JSON-normalized, so a
        # commit that differs from them (a normalizer added or changed
        # something) is not tagged as the sender's own echo.
        self._current_request_ops: Any = None

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
        wire = operations_to_wire(event.operations)
        source = self._current_client_id
        if source is not None and self._current_request_ops is not None:
            if _normalize(wire) != self._current_request_ops:
                # The commit carries more than the client sent (a
                # normalizer ran). A thick client skips its own echoes, so
                # do not label this one as an echo.
                source = None
        self._pending_broadcasts.append((
            {
                "type": MSG_PATCH,
                "version": self._version,
                "operations": wire,
                "source_client": source,
            },
            list(self._clients),
        ))

    # --- Transport callbacks ---

    async def _handle_connect(self, client: ClientConnection) -> None:
        self._clients[client.client_id] = client

        # Send schema (cached after first build)
        if self._cached_schema is None:
            self._cached_schema = self._doc.atomdoc_schema()
        await client.send({"type": MSG_SCHEMA, "schema": self._cached_schema})

        # Send snapshot (includes client_id so the client can identify self-echoes)
        await self._send_snapshot(client, with_client_id=True)

    async def _send_snapshot(
        self, client: ClientConnection, *, with_client_id: bool = False
    ) -> None:
        message: dict[str, Any] = {
            "type": MSG_SNAPSHOT,
            "doc_id": self._doc.id,
            "version": self._version,
            "data": self._doc.dump(),
        }
        if with_client_id:
            message["client_id"] = client.client_id
        await client.send(message)

    async def _handle_message(
        self, client: ClientConnection, msg: dict[str, Any]
    ) -> None:
        if not isinstance(msg, dict):
            await client.send({
                "type": MSG_ERROR,
                "ref": None,
                "code": "invalid_op",
                "message": f"Expected a JSON object, got {type(msg).__name__}",
            })
            return
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
        except _Rejected as rejected:
            # A valid message that is invalid against the current document
            # (referential integrity, validation, a node that is gone). The
            # document rolled it back and nothing was broadcast. A thick
            # client applied it optimistically, so send it the truth: a
            # fresh snapshot replaces its local document.
            logger.info(
                "Rejected %s from %s: %s", msg_type, client.client_id, rejected
            )
            await client.send({
                "type": MSG_ERROR,
                "ref": ref,
                "code": "rejected",
                "message": str(rejected),
            })
            await self._send_snapshot(client)
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
        self._current_request_ops = _normalize(msg["operations"])
        try:
            # Strict: a missing target is a failure, and any failure is
            # rolled back and raised here as a rejection. The server must
            # never silently drop what a client applied optimistically.
            self._doc.apply_operations(ops, strict=True)
        except Exception as exc:
            raise _Rejected(exc) from exc
        finally:
            self._current_client_id = None
            self._current_request_ops = None

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
        except Exception as exc:
            raise _Rejected(exc) from exc
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
        """Send every pending broadcast produced by the Doc changes, in order."""
        broadcasts = self._pending_broadcasts
        if not broadcasts:
            return
        self._pending_broadcasts = []
        for broadcast, recipients in broadcasts:
            await self._broadcast(broadcast, exclude=exclude, only=recipients)

    async def _broadcast(
        self,
        message: dict[str, Any],
        exclude: str | None = None,
        only: list[str] | None = None,
    ) -> None:
        """Send a message to connected clients (``only`` those, if given),
        optionally excluding one."""
        tasks = []
        for cid, client in self._clients.items():
            if cid == exclude or (only is not None and cid not in only):
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
