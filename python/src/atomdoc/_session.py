"""Session manager: connects a Doc to clients via a transport."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal

from ._doc import Doc
from ._protocol import (
    MSG_CREATE,
    MSG_ERROR,
    MSG_OP,
    MSG_PATCH,
    MSG_REDO,
    MSG_SCHEMA,
    MSG_SCOPE,
    MSG_SCOPE_ACK,
    MSG_SNAPSHOT,
    MSG_UNDO,
    operations_from_wire,
    operations_to_wire,
)
from ._scope import ClientView, OutOfScope, parse_anchors
from ._transport import ClientConnection, Transport
from ._types import ChangeEvent, JsonDoc, ListenerError, Operations
from ._undo import UndoManager

logger = logging.getLogger(__name__)

UndoPolicy = Literal["per-client", "global", "none"]


def _normalize(value: Any) -> Any:
    """JSON-normalize an operations payload for comparison (tuples vs
    lists, ints vs floats)."""
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError, RecursionError):
        return value


class _Rejected(Exception):
    """A well-formed request the document refused to apply.

    ``resync`` says whether the requester must be sent a fresh snapshot
    (an ``op`` or ``create``: a thick client may hold a field write it
    applied locally) or kept its document (an undo step, which it never
    applied itself).
    """

    def __init__(
        self, cause: BaseException, *, resync: bool = True, code: str = "rejected"
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.resync = resync
        self.code = code


class _Unsupported(Exception):
    """A request kind this session does not serve."""


class _Request:
    """The client request being handled, for labeling its commits."""

    __slots__ = ("client_id", "ref", "ops")

    def __init__(self, client_id: str, ref: Any, ops: Any = None) -> None:
        self.client_id = client_id
        self.ref = ref
        # JSON-normalized operations of an ``op`` request, so a commit
        # that equals them can be labeled as the sender's echo.
        self.ops = ops


class Session:
    """Manages a Doc and its connected clients.

    The session is the single authority for a document.  Clients connect
    via a :class:`Transport`, receive the schema and a snapshot, then
    send operations which the session applies and broadcasts.

    ``undo`` sets what a client's ``undo``/``redo`` request means:

    - ``"per-client"`` (default): a client reverts only commits it
      requested itself. A step that no longer applies because someone
      else edited in between is rejected and kept. This is what a thick
      client does locally, so thin and thick clients agree.
    - ``"global"``: any client reverts the document's last commit,
      whoever made it — right for one user looking at the document
      through several views, wrong for several users. When the document's
      own undo manager is enabled it is the one used, so host and clients
      share a single history; otherwise the session makes one.
    - ``"none"``: the requests are refused (error code ``unsupported``).

    Passing ``undo_manager`` selects the global policy with that manager.
    Under ``"per-client"`` and ``"none"`` the host's own
    ``doc.undo_manager`` is untouched.
    """

    def __init__(
        self,
        doc: Doc,
        undo_manager: UndoManager | None = None,
        *,
        undo: UndoPolicy = "per-client",
        undo_steps: int = 100,
    ) -> None:
        self._doc = doc
        if undo_manager is not None:
            undo = "global"
        if undo not in ("per-client", "global", "none"):
            raise ValueError(f"Unknown undo policy: {undo!r}")
        self._undo_policy: UndoPolicy = undo
        self._undo_steps = undo_steps
        self._undo: UndoManager | None = None
        if undo == "global":
            if undo_manager is not None:
                self._undo = undo_manager
            elif doc.undo_manager.is_enabled:
                self._undo = doc.undo_manager
            else:
                self._undo = UndoManager(doc, undo_steps)
        # Per-client undo managers, created when a client starts its
        # handshake and disposed when it leaves.
        self._client_undo: dict[str, UndoManager] = {}
        self._clients: dict[str, ClientConnection] = {}
        # Clients whose handshake (schema + snapshot) is in flight. A
        # commit that lands meanwhile is newer than their snapshot, so it
        # is held here and delivered right after the snapshot.
        self._connecting: dict[str, tuple[ClientConnection, list[dict[str, Any]]]] = {}
        # Scoped clients (partial replication): what each one holds, and
        # how commits are projected onto it. A partial client is
        # ``_awaiting_scope`` between the schema and its first ``scope``.
        self._views: dict[str, ClientView] = {}
        self._awaiting_scope: set[str] = set()
        # One scope request at a time per client: a second one waits for
        # the first's snapshot or delta to be sent.
        self._scope_locks: dict[str, asyncio.Lock] = {}
        self._version: int = 0
        self._transport: Transport | None = None

        # Pending broadcasts set synchronously by the on_change callback
        # and consumed asynchronously. One request can commit more than
        # once (a multi-step undo), hence a list. Each entry records the
        # clients connected (or connecting) at commit time: a client that
        # connects later gets the change in its snapshot instead.
        self._pending_broadcasts: list[tuple[dict[str, Any], list[str]]] = []
        self._flush_lock = asyncio.Lock()
        self._flush_tasks: set[asyncio.Task[None]] = set()
        self._request: _Request | None = None

        # Cache the schema so we don't rebuild it on every connect.
        self._cached_schema: dict[str, Any] | None = None

        self._doc.on_change(self._on_doc_change)

    @property
    def doc(self) -> Doc:
        return self._doc

    def snapshot(self) -> JsonDoc:
        """The document as a client would receive it: ``doc.dump()`` at
        the current version. For checking a client against the server."""
        return self._doc.dump()

    async def settled(self) -> None:
        """Wait until every commit so far has been sent to every client:
        the host's own broadcasts included."""
        for task in list(self._flush_tasks):
            if not task.done():
                await task
        await self._flush_broadcast()

    @property
    def version(self) -> int:
        return self._version

    @property
    def clients(self) -> dict[str, ClientConnection]:
        return dict(self._clients)

    @property
    def undo_policy(self) -> UndoPolicy:
        return self._undo_policy

    def _undo_for(self, client_id: str) -> UndoManager | None:
        """The undo manager a client's ``undo``/``redo`` request acts on."""
        if self._undo_policy == "global":
            return self._undo
        if self._undo_policy == "per-client":
            return self._client_undo.get(client_id)
        return None

    def _make_client_undo(self, client_id: str) -> None:
        if self._undo_policy != "per-client" or client_id in self._client_undo:
            return

        def mine(event: ChangeEvent, cid: str = client_id) -> bool:
            request = self._request
            return request is not None and request.client_id == cid

        self._client_undo[client_id] = UndoManager(
            self._doc, self._undo_steps, accept=mine
        )

    def _drop_client_undo(self, client_id: str) -> None:
        manager = self._client_undo.pop(client_id, None)
        if manager is not None:
            manager.dispose()

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
        self._connecting.clear()
        self._views.clear()
        self._awaiting_scope.clear()
        for client_id in list(self._client_undo):
            self._drop_client_undo(client_id)
        for task in list(self._flush_tasks):
            task.cancel()
        self._flush_tasks.clear()
        self._pending_broadcasts.clear()

    # --- Doc change listener (synchronous) ---

    def _on_doc_change(self, event: ChangeEvent) -> None:
        """Called synchronously by Doc after a transaction commits."""
        self._version += 1
        wire = operations_to_wire(event.operations)
        request = self._request
        source: str | None = None
        ref: Any = None
        if request is not None:
            ref = request.ref
            # ``source_client`` names the sender only when the commit
            # carries an ``op`` request's operations verbatim. A commit
            # that differs in any way (a normalizer ran, a value was
            # coerced, an insert was anchored differently), or one
            # produced by create/undo/redo, is not marked; thick clients
            # match their requests by ``ref`` and treat this as advisory.
            if request.ops is not None and _normalize(wire) == request.ops:
                source = request.client_id
        recipients = [
            cid
            for cid in (*self._clients, *self._connecting)
            if cid not in self._views and cid not in self._awaiting_scope
        ]
        self._pending_broadcasts.append((
            {
                "type": MSG_PATCH,
                "version": self._version,
                "operations": wire,
                "source_client": source,
                "ref": ref,
            },
            recipients,
        ))
        # Scoped clients get the commit projected onto what they hold,
        # computed now against the tree as committed. A client whose
        # view the commit did not touch hears nothing, unless the commit
        # answers its own request.
        for cid, view in self._views.items():
            if cid not in self._clients and cid not in self._connecting:
                continue
            projected = view.project(event)
            mine = request is not None and request.client_id == cid
            if projected is None:
                if not mine:
                    continue
                projected = {"ordered": [], "state": {}}
            self._pending_broadcasts.append((
                {
                    "type": MSG_PATCH,
                    "version": self._version,
                    "operations": projected,
                    "source_client": None,
                    "ref": ref if mine else None,
                },
                [cid],
            ))
        if request is None:
            # Not inside a request handler (the host edited the document
            # directly): nothing will flush this later, so do it now.
            self._schedule_flush()

    def _schedule_flush(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop: the broadcast waits for the next request or
            # connect, which flush before they send anything else.
            return
        task = loop.create_task(self._flush_broadcast())
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)

    # --- Transport callbacks ---

    async def _handle_connect(self, client: ClientConnection) -> None:
        # Anything committed but not yet sent predates this client's
        # snapshot; deliver it to the others first so the order of
        # versions each client sees stays monotonic.
        await self._flush_broadcast()

        if self._cached_schema is None:
            self._cached_schema = self._doc.atomdoc_schema()

        if client.wants_partial:
            # Partial replication: the schema now, the snapshot once the
            # client has said what it holds (its first ``scope``).
            self._connecting[client.client_id] = (client, [])
            self._awaiting_scope.add(client.client_id)
            self._make_client_undo(client.client_id)
            try:
                await client.send({"type": MSG_SCHEMA, "schema": self._cached_schema})
            except BaseException:
                self._forget(client.client_id)
                raise
            return

        # Register as connecting and take the snapshot in the same
        # synchronous step: every commit from here on is newer than the
        # snapshot and is buffered for delivery after it.
        buffer: list[dict[str, Any]] = []
        self._connecting[client.client_id] = (client, buffer)
        self._make_client_undo(client.client_id)
        snapshot = self._snapshot_message(client, with_client_id=True)
        try:
            await client.send({"type": MSG_SCHEMA, "schema": self._cached_schema})
            await client.send(snapshot)
        except BaseException:
            self._connecting.pop(client.client_id, None)
            self._drop_client_undo(client.client_id)
            raise
        # Promote and drain under the flush lock: a concurrent flush must
        # not slip a newer patch between two buffered ones.
        async with self._flush_lock:
            if self._connecting.pop(client.client_id, None) is None:
                return  # disconnected during the handshake
            self._clients[client.client_id] = client
            for message in buffer:
                await self._safe_send(client, message)

    def _snapshot_message(
        self, client: ClientConnection, *, with_client_id: bool = False
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "type": MSG_SNAPSHOT,
            "doc_id": self._doc.id,
            "version": self._version,
        }
        view = self._views.get(client.client_id)
        if view is None:
            message["data"] = self._doc.dump()
        else:
            data, stubs = view.snapshot()
            message["data"] = data
            message["partial"] = True
            message["stubs"] = stubs
            message["anchors"] = view.resolved_anchors()
        if with_client_id:
            message["client_id"] = client.client_id
        return message

    async def _send_snapshot(
        self, client: ClientConnection, *, with_client_id: bool = False
    ) -> None:
        view = self._views.get(client.client_id)
        if view is not None:
            # A scoped resync re-sends the client's scope, never the
            # whole document.
            view.reset()
        await client.send(self._snapshot_message(client, with_client_id=with_client_id))

    def _forget(self, client_id: str) -> None:
        self._clients.pop(client_id, None)
        self._connecting.pop(client_id, None)
        self._views.pop(client_id, None)
        self._awaiting_scope.discard(client_id)
        self._scope_locks.pop(client_id, None)
        self._drop_client_undo(client_id)

    def _knows(self, client_id: str) -> bool:
        return client_id in self._clients or client_id in self._connecting

    async def _handle_scope(self, client: ClientConnection, msg: dict[str, Any]) -> None:
        """A client sets (or replaces) its scope. The first ``scope`` of
        a partial connection completes its handshake with a partial
        snapshot; any later one is answered with a ``scope_ack`` carrying
        the delta from the old view to the new."""
        cid = client.client_id
        ref = msg.get("ref")
        if not self._knows(cid):
            return  # a client that already left
        error: str | None = None
        anchors: Any = None
        if "types" in msg:
            error = "Type filters are not supported: a scope is a set of anchors"
        else:
            try:
                anchors = parse_anchors(msg.get("anchors"))
            except ValueError as exc:
                error = str(exc)
        if error is not None:
            await client.send({
                "type": MSG_ERROR, "ref": ref, "code": "invalid_op", "message": error,
            })
            return
        lock = self._scope_locks.setdefault(cid, asyncio.Lock())
        async with lock:
            if not self._knows(cid):
                return
            if cid in self._connecting and cid not in self._awaiting_scope:
                # A whole-document client still receiving its snapshot.
                await client.send({
                    "type": MSG_ERROR,
                    "ref": ref,
                    "code": "invalid_op",
                    "message": "A scope cannot be set before the snapshot has been received",
                })
                return
            # Everything committed so far goes out first: the snapshot or
            # delta below is taken at the current version.
            await self._flush_broadcast()
            if not self._knows(cid):
                return
            view: ClientView | None
            if cid in self._awaiting_scope:
                entry = self._connecting[cid]
                view = ClientView(self._doc, anchors)
                view.reset()
                self._views[cid] = view
                self._awaiting_scope.discard(cid)
                snapshot = self._snapshot_message(client, with_client_id=True)
                snapshot["ref"] = ref
                try:
                    await client.send(snapshot)
                except BaseException:
                    self._forget(cid)
                    raise
                async with self._flush_lock:
                    if self._connecting.pop(cid, None) is None:
                        return
                    self._clients[cid] = client
                    for message in entry[1]:
                        await self._safe_send(client, message)
                return
            view = self._views.get(cid)
            if view is None:
                # A whole-document client narrowing its view: it holds
                # everything, and the delta is what leaves.
                view = ClientView(self._doc, {})
                view.hold_everything()
                self._views[cid] = view
            operations = view.change(anchors)
            # Queued like a patch: a commit projected after this delta
            # is queued after it, and reaches the client after it.
            self._pending_broadcasts.append((
                {
                    "type": MSG_SCOPE_ACK,
                    "ref": ref,
                    "version": self._version,
                    "operations": operations,
                    "source_client": None,
                    "anchors": view.resolved_anchors(),
                },
                [cid],
            ))
            await self._flush_broadcast()

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

        if msg_type == MSG_SCOPE:
            await self._handle_scope(client, msg)
            return
        if client.client_id in self._awaiting_scope:
            await client.send({
                "type": MSG_ERROR,
                "ref": ref,
                "code": "no_scope",
                "message": "A partial connection must send its scope before anything else",
            })
            return

        # The request context lives only for the synchronous dispatch:
        # every commit made while it is set is attributed to this request.
        # It is cleared before any await, so a host-side commit landing
        # while a reply is in flight is never mislabeled.
        request = _Request(client.client_id, ref)
        self._request = request
        error: dict[str, Any] | None = None
        rejected: _Rejected | None = None
        queued_before = len(self._pending_broadcasts)
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
                error = {"code": "unknown_type", "message": f"Unknown message type: {msg_type}"}
        except _Rejected as exc:
            rejected = exc
        except ListenerError:
            # The request was applied and committed; a host-side change
            # listener failed afterwards. That is the host's bug, not the
            # client's: log it and deliver the patch like any other.
            logger.exception(
                "Change listener failed after applying %s from %s",
                msg_type, client.client_id,
            )
        except _Unsupported as unsupported:
            error = {"code": "unsupported", "message": str(unsupported)}
        except Exception as exc:
            # A malformed request is the client's problem: log it as
            # such, not as a server failure.
            logger.info("Invalid %s from %s: %s", msg_type, client.client_id, exc)
            error = {"code": "invalid_op", "message": str(exc)}
        finally:
            self._request = None

        if rejected is not None:
            # A valid message that is invalid against the current document
            # (referential integrity, validation, a node that is gone). The
            # document rolled it back and nothing was broadcast. A thick
            # client may hold a field write it applied locally, so send it
            # the truth: a fresh snapshot replaces its local document. Whatever was
            # committed before this request goes out first, so the
            # snapshot is the newest thing the client receives.
            logger.info(
                "Rejected %s from %s: %s", msg_type, client.client_id, rejected
            )
            await self._flush_broadcast()
            message = str(rejected)
            view = self._views.get(client.client_id)
            if view is not None:
                message = view.redact(message)
            await client.send({
                "type": MSG_ERROR,
                "ref": ref,
                "code": rejected.code,
                "message": message,
            })
            if rejected.resync:
                await self._send_snapshot(client)
            return
        if error is not None:
            await client.send({"type": MSG_ERROR, "ref": ref, **error})
            return
        if ref is not None and len(self._pending_broadcasts) == queued_before:
            # The request committed nothing and nothing answered it yet
            # (an undo with nothing left to revert, say). A request that
            # carries a ref is always answered, so the client can retire
            # it: an empty patch at the current version.
            self._pending_broadcasts.append((self._empty_answer(ref), [client.client_id]))

        # Broadcast the patch to ALL clients (including the source).
        # Thin clients need the echo to update their store.
        # Thick clients match their own requests by ``ref`` (see PROTOCOL.md).
        await self._flush_broadcast()

    async def _handle_disconnect(self, client: ClientConnection) -> None:
        self._forget(client.client_id)

    # --- Message handlers ---

    def _apply_op(self, client: ClientConnection, msg: dict[str, Any]) -> None:
        raw = msg.get("operations")
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("ordered", []), list)
            or not isinstance(raw.get("state", {}), dict)
        ):
            raise ValueError("'operations' must be {'ordered': [...], 'state': {...}}")
        ops = operations_from_wire(raw)
        self._check_well_formed(ops)
        self._check_scope(client, lambda view: view.check_operations(ops))
        # Compare against the canonical form so a minimal or reordered
        # frame still matches its own echo. The root spelled by its ID is
        # the root spelled as 0.
        request = self._request
        assert request is not None
        request.ops = _normalize(operations_to_wire(self._root_as_zero(ops)))
        queued_before = len(self._pending_broadcasts)
        try:
            # Strict: a missing target is a failure, and any failure is
            # rolled back and raised here as a rejection. The server must
            # never silently drop what a client is waiting to see confirmed.
            self._doc.apply_operations(ops, strict=True)
        except ListenerError:
            raise  # applied and committed; an observer failed afterwards
        except Exception as exc:
            raise _Rejected(exc) from exc
        committed = len(self._pending_broadcasts) > queued_before
        if not committed:
            # The request changed nothing here (a move to where the node
            # already is, a write of the value already held) and so
            # produced no echo. Answer it anyway, alone, with a patch at
            # the current version carrying nothing to apply beyond the
            # stored values of the fields it wrote, so the requester can
            # retire it. Queued like any broadcast, it is delivered in
            # order: before any later commit.
            self._pending_broadcasts.append((
                {
                    "type": MSG_PATCH,
                    "version": self._version,
                    "operations": {"ordered": [], "state": self._stored_values(ops)},
                    "source_client": None,
                    "ref": request.ref,
                },
                [client.client_id],
            ))

    def _check_scope(self, client: ClientConnection, check: Any) -> None:
        """A scoped client may only touch what it holds in full; the
        rejection is ``out_of_scope`` and, like any rejection, resyncs
        the client (with its scope)."""
        view = self._views.get(client.client_id)
        if view is None:
            return
        try:
            check(view)
        except OutOfScope as exc:
            raise _Rejected(exc, code="out_of_scope") from exc

    def _empty_answer(self, ref: Any) -> dict[str, Any]:
        return {
            "type": MSG_PATCH,
            "version": self._version,
            "operations": {"ordered": [], "state": {}},
            "source_client": None,
            "ref": ref,
        }

    def _check_well_formed(self, ops: Operations) -> None:
        """Refuse a malformed frame before touching the document: an
        unknown operation code, or a field the node's type does not have.
        These are ``invalid_op`` errors (no resync), not rejections."""
        doc = self._doc
        for op in ops[0]:
            if op[0] not in (0, 1, 2):
                raise ValueError(f"Unknown operation code: {op[0]!r}")
        for node_id, patch in ops[1].items():
            node = doc.get_node_by_id(node_id)
            if node is None:
                continue  # a node that is gone is a rejection, decided below
            for key in patch:
                if key not in node._field_adapters:
                    raise ValueError(f"{type(node).__name__} has no field {key!r}")

    def _root_as_zero(self, ops: Operations) -> Operations:
        root_id = self._doc.root.id
        ordered: list[Any] = []
        for op in ops[0]:
            op = list(op)
            if op[0] == 0 and op[2] == root_id:
                op[2] = 0
            elif op[0] == 2 and op[3] == root_id:
                op[3] = 0
            ordered.append(op)
        return (ordered, ops[1])  # type: ignore[return-value]

    def _stored_values(self, ops: Operations) -> dict[str, dict[str, Any]]:
        """The values the document holds for the fields ``ops`` write."""
        doc = self._doc
        state: dict[str, dict[str, Any]] = {}
        for node_id, patch in ops[1].items():
            node = doc.get_node_by_id(node_id)
            if node is None:
                continue
            state[node_id] = {
                key: node._state_key_to_json(key)
                for key in patch
                if key in node._field_adapters
            }
        return state

    def _handle_create(self, client: ClientConnection, msg: dict[str, Any]) -> None:
        node_type = msg.get("node_type")
        state = msg.get("state", {})
        parent_id = msg.get("parent_id")
        slot = msg.get("slot")
        if not isinstance(node_type, str):
            raise ValueError("'node_type' must be a string")
        if not isinstance(slot, str):
            raise ValueError("'slot' must be a string")
        position = msg.get("position", "append")
        target_id = msg.get("target_id")

        node_cls = self._doc._node_types.get(node_type)
        if node_cls is None:
            raise ValueError(f"Unknown node type: {node_type!r}")
        if not isinstance(state, dict):
            raise ValueError("'state' must be an object")
        if position not in ("append", "prepend", "before", "after"):
            raise ValueError(f"Unknown position: {position!r}")
        if position in ("before", "after") and not target_id:
            raise ValueError(f"position {position!r} needs a 'target_id'")
        self._check_scope(client, lambda view: view.check_create(parent_id, slot, target_id))

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
                    if target is None:
                        raise ValueError(f"Target node not found: {target_id!r}")

                self._doc._insert_into_slot(
                    parent, slot, position, [new_node], target=target
                )
        except ListenerError:
            raise
        except Exception as exc:
            raise _Rejected(exc) from exc

    def _handle_undo(self, client: ClientConnection, msg: dict[str, Any]) -> None:
        self._step_history(client, msg, "undo")

    def _handle_redo(self, client: ClientConnection, msg: dict[str, Any]) -> None:
        self._step_history(client, msg, "redo")

    def _step_history(
        self, client: ClientConnection, msg: dict[str, Any], direction: str
    ) -> None:
        manager = self._undo_for(client.client_id)
        if manager is None:
            raise _Unsupported(f"{direction} is disabled on this session")
        if self._undo_policy == "global" and client.client_id in self._views:
            # A global step may revert commits touching nodes this
            # client never held; its own history is per-client.
            raise _Unsupported(f"{direction} is global on this session: not for a scoped client")
        steps = msg.get("steps", 1)
        if not isinstance(steps, int) or steps < 1:
            raise ValueError("'steps' must be a positive integer")
        step = manager.undo if direction == "undo" else manager.redo
        listener_errors: list[BaseException] = []
        for _ in range(steps):
            if not (manager.can_undo if direction == "undo" else manager.can_redo):
                break
            try:
                step()
            except ListenerError as exc:
                # The step applied; only a host listener failed. Keep
                # stepping and report afterwards.
                listener_errors.extend(exc.errors)
            except Exception as exc:
                # The step no longer applies (someone else edited what it
                # would revert). It is kept for a retry; the client applied
                # nothing itself, so no resync.
                raise _Rejected(exc, resync=False) from exc
        if listener_errors:
            raise ListenerError(listener_errors)

    # --- Broadcasting ---

    async def _flush_broadcast(self, exclude: str | None = None) -> None:
        """Send every pending broadcast produced by the Doc changes, in order.

        Serialized: two flushes never interleave, so every client sees
        versions in order.
        """
        async with self._flush_lock:
            while self._pending_broadcasts:
                broadcast, recipients = self._pending_broadcasts[0]
                await self._broadcast(broadcast, exclude=exclude, only=recipients)
                # Popped only once sent: a cancellation mid-send leaves it
                # queued rather than opening a version gap.
                self._pending_broadcasts.pop(0)

    async def _broadcast(
        self,
        message: dict[str, Any],
        exclude: str | None = None,
        only: list[str] | None = None,
    ) -> None:
        """Send a message to connected clients (``only`` those, if given),
        optionally excluding one. A client still in its handshake gets
        the message queued behind its snapshot."""
        tasks = []
        for cid, client in self._clients.items():
            if cid == exclude or (only is not None and cid not in only):
                continue
            tasks.append(self._safe_send(client, message))
        for cid, (_, buffer) in self._connecting.items():
            if cid == exclude or (only is not None and cid not in only):
                continue
            buffer.append(message)
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
            self._forget(client.client_id)
