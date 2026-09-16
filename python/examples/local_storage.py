"""A document that lives on your disk.

A to-do list kept in a local store, so every run of this script picks up
where the last one left off:

    uv run python examples/local_storage.py add "buy milk"
    uv run python examples/local_storage.py add "water plants"
    uv run python examples/local_storage.py done 1
    uv run python examples/local_storage.py show
    uv run python examples/local_storage.py ls

Add `--sqlite` to keep documents in one SQLite file instead of a directory,
and `--dir PATH` to choose where. `demo` runs a walkthrough of what a
long-running app needs on top: saving as you edit, two writers racing for
one document, and a scratch document that expires unless it is renewed.

The store never sees a Doc. It holds bytes, and what this file writes is a
small JSON envelope around `doc.dump()` — so the format is yours to version,
compress, or encrypt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from atomdoc import (ABSENT, Array, Doc, DocumentNotFound, DocumentStore, FileStore,
                     SqliteStore, StaleWrite, node)

# ── the schema ────────────────────────────────────────────────────────────────


@node
class Item:
    text: str = ""
    done: bool = False


@node
class TodoList:
    title: str = "To do"
    items: Array[Item] = []


# ── documents in and out of a store ───────────────────────────────────────────

FORMAT = "atomdoc-todo"
VERSION = 1


def encode(doc: Doc) -> bytes:
    return json.dumps({"format": FORMAT, "version": VERSION, "doc": doc.dump()}).encode()


def decode(data: bytes) -> Doc:
    envelope = json.loads(data)
    if envelope.get("format") != FORMAT or envelope.get("version") != VERSION:
        raise ValueError(f"not a version-{VERSION} {FORMAT} document")
    return Doc.restore(envelope["doc"], root_type=TodoList)


async def open_or_create(store: DocumentStore, key: str) -> tuple[Doc, str]:
    """The document and the token naming the version it was read from."""
    try:
        data, token = await store.read(key)
        return decode(data), token
    except DocumentNotFound:
        doc = Doc(TodoList())
        # ABSENT: create it only if nobody else did in the meantime.
        return doc, await store.put(key, encode(doc), if_match=ABSENT)


async def save(store: DocumentStore, key: str, doc: Doc, token: str) -> str:
    """Write only if the stored version is still the one we read. Another
    process that saved in between makes this raise StaleWrite instead of
    silently overwriting its work."""
    return await store.put(key, encode(doc), if_match=token)


def open_store(args: argparse.Namespace) -> DocumentStore:
    base = Path(args.dir)
    return SqliteStore(base / "documents.db") if args.sqlite else FileStore(base / "documents")


# ── the command line ──────────────────────────────────────────────────────────


async def command(args: argparse.Namespace) -> None:
    key = f"lists/{args.list}"
    async with open_store(args) as store:
        if args.command == "ls":
            entries = sorted([e async for e in store.list("lists/")], key=lambda e: e.key)
            for entry in entries:
                print(f"{entry.key:24} {entry.size:6} bytes")
            return

        doc, token = await open_or_create(store, key)
        if args.command == "add":
            with doc.transaction():
                doc.root.items.append(doc.create_node(Item, text=" ".join(args.text)))
        elif args.command == "done":
            if not 1 <= args.number <= len(doc.root.items):
                raise SystemExit(f"no item {args.number}: the list has {len(doc.root.items)}")
            with doc.transaction():
                doc.root.items[args.number - 1].done = True
        elif args.command == "clear-done":
            with doc.transaction():
                for item in [i for i in doc.root.items if i.done]:
                    item.delete()

        if args.command != "show":
            try:
                await save(store, key, doc, token)
            except StaleWrite:
                raise SystemExit("someone else changed this list since it was read; "
                                 "run the command again") from None

        print(f"{doc.root.title}  ({key})")
        for n, item in enumerate(doc.root.items, 1):
            print(f"  {n}. [{'x' if item.done else ' '}] {item.text}")
        if not len(doc.root.items):
            print("  (empty)")


# ── the walkthrough ───────────────────────────────────────────────────────────


class Autosaver:
    """Save a document shortly after it changes, however many edits arrive.

    `on_change` fires after every transaction; writing on each would put a
    save behind every keystroke. Instead the first change schedules one save
    a moment later, and the edits in between ride along with it.
    """

    def __init__(self, store: DocumentStore, key: str, doc: Doc, token: str,
                 delay: float = 0.2) -> None:
        self.store, self.key, self.doc, self.token, self.delay = store, key, doc, token, delay
        self.saves = 0
        self._pending: asyncio.Task[None] | None = None
        self._unsubscribe = doc.on_change(lambda _event: self._schedule())

    def _schedule(self) -> None:
        if self._pending is None:
            self._pending = asyncio.get_running_loop().create_task(self._save_soon())

    async def _save_soon(self) -> None:
        await asyncio.sleep(self.delay)
        self._pending = None
        self.token = await save(self.store, self.key, self.doc, self.token)
        self.saves += 1

    async def close(self) -> None:
        self._unsubscribe()
        if self._pending is not None:
            await self._pending


async def demo(store: DocumentStore) -> None:
    print(f"store: {store!r}\n")

    # 1. Many edits, one write.
    doc, token = await open_or_create(store, "lists/groceries")
    saver = Autosaver(store, "lists/groceries", doc, token)
    for text in ["milk", "eggs", "bread", "coffee"]:
        with doc.transaction():
            doc.root.items.append(doc.create_node(Item, text=text))
    await saver.close()
    print(f"1. four edits, {saver.saves} save; on disk now: "
          f"{[i.text for i in decode(await store.get('lists/groceries')).root.items]}")

    # 2. Two writers read the same version; the second to save is told so.
    phone, phone_token = await open_or_create(store, "lists/groceries")
    laptop, laptop_token = await open_or_create(store, "lists/groceries")
    with phone.transaction():
        phone.root.items[0].done = True
    await save(store, "lists/groceries", phone, phone_token)
    with laptop.transaction():
        laptop.root.title = "Groceries"
    try:
        await save(store, "lists/groceries", laptop, laptop_token)
        outcome = "saved over the phone's edit (this should not happen)"
    except StaleWrite:
        # Re-read, re-apply the edit to the current version, save again.
        laptop, laptop_token = await open_or_create(store, "lists/groceries")
        with laptop.transaction():
            laptop.root.title = "Groceries"
        await save(store, "lists/groceries", laptop, laptop_token)
        outcome = "refused, then re-applied to the phone's version"
    final = decode(await store.get("lists/groceries")).root
    print(f"2. laptop's stale save {outcome}: title {final.title!r}, "
          f"milk done (the phone's edit): {final.items[0].done}")

    # 3. A scratch document that lives only while someone renews it.
    if store.capabilities.honors_ttl:
        scratch = Doc(TodoList(title="scratch"))
        # Renew at a third of the lease, so one slow renewal does not lose it.
        await store.put("scratch/session-1", encode(scratch), ttl=0.9)
        for _ in range(4):                                    # a client still connected
            await asyncio.sleep(0.3)
            await store.touch("scratch/session-1", 0.9)
        alive = [k async for k in store.keys("scratch/")]
        await asyncio.sleep(1.2)                              # and then it left
        gone = [k async for k in store.keys("scratch/")]
        print(f"3. a 0.9s lease renewed for 1.2s: {alive}; unrenewed: {gone}")

    print("\nin the store:")
    async for entry in store.list():
        print(f"   {entry.key:22} {entry.size:5} bytes  token {entry.token}")


async def run_demo(args: argparse.Namespace) -> None:
    if args.dir is not None:
        async with open_store(args) as store:
            await demo(store)
        return
    with tempfile.TemporaryDirectory() as scratch:                 # leave nothing behind
        args.dir = scratch
        async with open_store(args) as store:
            await demo(store)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", help="where documents are kept (default: ~/.atomdoc-todo)")
    parser.add_argument("--sqlite", action="store_true", help="one SQLite file, not a directory")
    parser.add_argument("--list", default="default", help="which list (default: default)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    sub.add_parser("ls", help="every list in the store")
    sub.add_parser("add").add_argument("text", nargs="+")
    sub.add_parser("done").add_argument("number", type=int)
    sub.add_parser("clear-done")
    sub.add_parser("demo", help="autosave, a conflict, and an expiring document "
                                "(in a temporary directory unless --dir is given)")
    args = parser.parse_args(argv)
    if args.command == "demo":
        asyncio.run(run_demo(args))
    else:
        args.dir = args.dir or str(Path.home() / ".atomdoc-todo")
        asyncio.run(command(args))


if __name__ == "__main__":
    main()
