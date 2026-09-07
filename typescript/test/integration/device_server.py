"""Convergence harness server: a fake device commits host-side changes.

The device writes into the session's document directly (no client
message), the way instrument callbacks do from a main event loop, while a
thick client edits over WebSocket. Environment:

  PORT         listen port
  SEED         PRNG seed (printed, for reproduction)
  COMMITS      number of device commits
  INTERVAL_MS  delay between device commits
  MODE         "disjoint": the device writes only device-owned fields
               (position) and inserts its own nodes;
               "overlap": it also writes the shared field the user edits
               and deletes nodes, so edits interfere.

The device starts once a client is connected, and marks the root with
``device_done`` after its last commit.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "atomdoc", "src"))

from atomdoc import Array, Doc, node  # noqa: E402
from atomdoc._session import Session  # noqa: E402
from atomdoc._ws_transport import WebSocketTransport  # noqa: E402


@node
class Device:
    position: list[float] = [0.0, 0.0, 0.0]  # device-owned
    label: str = ""  # user-owned
    shared: int = 0  # both, in overlap mode


@node
class Scene:
    device_done: bool = False
    device_commits: int = 0
    devices: Array[Device] = []


async def run_device(session: Session, rng: random.Random, commits: int, interval: float, mode: str) -> None:
    doc = session.doc
    while not session.clients:
        await asyncio.sleep(0.005)
    mine: list[str] = []
    for i in range(commits):
        await asyncio.sleep(interval)
        devices = list(doc.root.devices)
        roll = rng.random()
        with doc.transaction():
            if roll < 0.15:
                d = doc.create_node(Device, label=f"dev-{i}")
                doc.root.devices.append(d)
                mine.append(d.id)
            elif mode == "overlap" and roll < 0.25 and devices:
                victim = rng.choice(devices)
                victim.delete()
            elif devices:
                target = rng.choice(devices)
                target.position = [round(rng.uniform(-10, 10), 3) for _ in range(3)]
                if mode == "overlap" and rng.random() < 0.5:
                    target.shared = rng.randrange(1000)
            doc.root.device_commits = i + 1
    with doc.transaction():
        doc.root.device_done = True


async def main() -> None:
    seed = int(os.environ.get("SEED", "1"))
    commits = int(os.environ.get("COMMITS", "60"))
    interval = int(os.environ.get("INTERVAL_MS", "5")) / 1000
    mode = os.environ.get("MODE", "disjoint")
    rng = random.Random(seed)

    doc = Doc(Scene(devices=[Device(label=f"d{i}") for i in range(3)]))
    session = Session(doc)
    port = int(os.environ.get("PORT", "9878"))
    transport = WebSocketTransport(host="localhost", port=port)
    await session.bind(transport)
    print(f"SERVER_READY seed={seed} mode={mode}", flush=True)

    device = asyncio.create_task(run_device(session, rng, commits, interval, mode))
    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        pass
    finally:
        device.cancel()
        await session.unbind()


if __name__ == "__main__":
    asyncio.run(main())
