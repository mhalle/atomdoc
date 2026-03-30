"""Abstract transport interface for pluggable client connections."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any


class ClientConnection(ABC):
    """A single connected client."""

    @property
    @abstractmethod
    def client_id(self) -> str: ...

    @abstractmethod
    async def send(self, message: dict[str, Any]) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


class Transport(ABC):
    """Abstract transport that accepts connections and routes messages.

    The transport calls back into the session via the provided callbacks.
    It does not import or know about any session implementation.
    """

    @abstractmethod
    async def start(
        self,
        on_connect: Callable[[ClientConnection], Awaitable[None]],
        on_message: Callable[[ClientConnection, dict[str, Any]], Awaitable[None]],
        on_disconnect: Callable[[ClientConnection], Awaitable[None]],
    ) -> None:
        """Start listening for connections."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the transport and close all connections."""
        ...
