"""Test server for integration testing."""

import asyncio
import sys
import os

# Add atomdoc to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "atomdoc", "src"))

from pydantic import BaseModel
from atomdoc import Array, Doc, node
from atomdoc._session import Session
from atomdoc._ws_transport import WebSocketTransport


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


async def main():
    doc = Doc(Page(
        title="Hello World",
        annotations=[
            Annotation(label="First", color=Color(r=255)),
            Annotation(label="Second", color=Color(g=255)),
        ],
    ))

    session = Session(doc)
    port = int(os.environ.get("PORT", "9876"))
    transport = WebSocketTransport(host="localhost", port=port)
    await session.bind(transport)

    print("SERVER_READY", flush=True)

    # Run until killed
    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        pass
    finally:
        await session.unbind()


if __name__ == "__main__":
    asyncio.run(main())
