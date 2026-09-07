"""WebSocket transport using the ``websockets`` library."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from ._transport import ClientConnection, Transport

try:
    from websockets.asyncio.server import ServerConnection, serve
except ImportError as exc:
    raise ImportError(
        "The websockets package is required for WebSocketTransport. "
        "Install it with: pip install atomdoc[server]"
    ) from exc


logger = logging.getLogger(__name__)


class WebSocketClient(ClientConnection):
    """A client connected via WebSocket."""

    def __init__(self, ws: ServerConnection) -> None:
        self._ws = ws
        self._client_id = str(uuid4())

    @property
    def client_id(self) -> str:
        return self._client_id

    async def send(self, message: dict[str, Any]) -> None:
        await self._ws.send(json.dumps(message))

    async def close(self) -> None:
        await self._ws.close()


class WebSocketTransport(Transport):
    """WebSocket server transport.

    Usage::

        transport = WebSocketTransport(host="localhost", port=8765)
        await session.bind(transport)
    """

    def __init__(self, host: str = "localhost", port: int = 8765) -> None:
        self._host = host
        self._port = port
        self._server: Any = None

    async def start(
        self,
        on_connect: Callable[[ClientConnection], Awaitable[None]],
        on_message: Callable[[ClientConnection, dict[str, Any]], Awaitable[None]],
        on_disconnect: Callable[[ClientConnection], Awaitable[None]],
    ) -> None:
        async def handler(ws: ServerConnection) -> None:
            client = WebSocketClient(ws)
            await on_connect(client)
            try:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        # A malformed frame is the client's problem, not a
                        # reason to drop the connection.
                        await client.send({
                            "type": "error",
                            "ref": None,
                            "code": "invalid_op",
                            "message": "Malformed JSON frame",
                        })
                        continue
                    try:
                        await on_message(client, msg)
                    except Exception:
                        # A session-side failure must be visible, and one
                        # request must not take the connection down.
                        logger.exception(
                            "Error handling message from %s", client.client_id
                        )
            except Exception:
                logger.debug("Connection %s ended", client.client_id, exc_info=True)
            finally:
                await on_disconnect(client)

        self._server = await serve(handler, self._host, self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
