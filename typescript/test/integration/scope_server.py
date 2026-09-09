"""Test server for partial replication: Page > Section > Item > Note,
with a reference from Section to Item. Prints the node IDs it created
so a test can anchor on them."""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "python", "src"))

from atomdoc import Array, Doc, Ref, node  # noqa: E402
from atomdoc._session import Session  # noqa: E402
from atomdoc._ws_transport import WebSocketTransport  # noqa: E402


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


async def main() -> None:
    doc = Doc(root_type=Page)
    ids: dict[str, str] = {"root": doc.root.id}
    with doc.transaction():
        doc.root.title = "Page"
        for s in range(3):
            section = doc.create_node(Section, heading=f"Section {s}")
            doc.root.sections.append(section)
            ids[f"s{s}"] = section.id
            for i in range(3):
                item = doc.create_node(Item, label=f"Item {s}.{i}")
                section.items.append(item)
                ids[f"i{s}{i}"] = item.id
                note = doc.create_node(Note, text=f"Note {s}.{i}")
                item.notes.append(note)
                ids[f"n{s}{i}"] = note.id
        # Section 0 references an item of section 2.
        doc.root.sections[0].related = doc.root.sections[2].items[1]
        # Bulk: SIZE more sections of 3 items with a note each, so a
        # scoped client's working set can be measured against a large
        # document.
        for s in range(int(os.environ.get("SIZE", "0"))):
            section = doc.create_node(Section, heading=f"Bulk {s}")
            doc.root.sections.append(section)
            for i in range(3):
                item = doc.create_node(Item, label=f"Bulk {s}.{i}")
                section.items.append(item)
                item.notes.append(doc.create_node(Note, text=f"Bulk note {s}.{i}"))

    session = Session(doc)
    port = int(os.environ.get("PORT", "9880"))
    transport = WebSocketTransport(host="localhost", port=port)
    await session.bind(transport)
    print("IDS " + json.dumps(ids), flush=True)
    print("SERVER_READY", flush=True)
    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        pass
    finally:
        await session.unbind()


if __name__ == "__main__":
    asyncio.run(main())
