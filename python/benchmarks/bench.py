"""Performance benchmarks for atomdoc.

Run from the repo root:

    uv run python benchmarks/bench.py            # default sizes
    uv run python benchmarks/bench.py 1000 4000  # sizes to sweep
    uv run python benchmarks/bench.py --profile build 4000

Each scenario is timed at every size; the ``x`` column is the time ratio
between consecutive sizes (linear work shows the size ratio, quadratic
work shows its square). The document is a Slicer-like scene: transforms
with a 4x4 matrix and a parent reference, volumes with a transform
reference and a strong handle, markups with a point list.
"""

from __future__ import annotations

import asyncio
import cProfile
import json
import pstats
import sys
import time
from collections.abc import Callable
from typing import Any

from atomdoc import Array, Doc, Handle, Ref, UndoManagerConfig, node
from atomdoc._session import Session
from atomdoc._transport import ClientConnection, Transport

IDENTITY = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]


class Voxels(Handle):
    strength = "strong"


@node
class Transform:
    name: str = ""
    matrix: list[float] = IDENTITY
    parent: Ref["Transform"] | None = None


@node
class Volume:
    name: str = ""
    window: float = 100.0
    level: float = 50.0
    transform: Ref[Transform] | None = None
    voxels: Voxels | None = None


@node
class Markup:
    label: str = ""
    points: list[list[float]] = []
    transform: Ref[Transform] | None = None


@node
class Folder:
    name: str = ""
    items: Array["Folder"] = []


@node
class Scene:
    title: str = ""
    transforms: Array[Transform] = []
    volumes: Array[Volume] = []
    markups: Array[Markup] = []
    folders: Array[Folder] = []


def make_scene(n: int, *, undo: bool = True) -> Doc:
    doc = Doc(Scene, undo_manager=UndoManagerConfig(max_steps=100) if undo else None)
    with doc.transaction():
        hub = doc.create_node(Transform, name="hub")
        doc.root.transforms.append(hub)
        for i in range(n):
            t = doc.create_node(Transform, name=f"t{i}", parent=hub)
            doc.root.transforms.append(t)
            v = doc.create_node(
                Volume, name=f"v{i}", transform=t, voxels=Voxels(uri=f"file://v{i}.nrrd")
            )
            doc.root.volumes.append(v)
    return doc


WORKING_SET = 259  # 1 + 6 + 36 + 216: a three-level, six-ary subtree


def make_tree(n: int, fanout: int = 6, levels: int = 3) -> Doc:
    """A scene whose folders form a forest of about n nodes: top-level
    folders, each the root of a full ``levels``-deep ``fanout``-ary
    subtree of a fixed size (259 nodes at the defaults). A scoped client
    anchored on one top folder holds that working set whatever n is."""
    doc = Doc(Scene, undo_manager=None)
    per_tree = sum(fanout**k for k in range(levels + 1))
    with doc.transaction():
        count = 0
        while count < n:
            top = doc.create_node(Folder, name=f"f{count}")
            doc.root.folders.append(top)
            count += 1
            frontier = [top]
            for _ in range(levels):
                next_frontier = []
                for parent in frontier:
                    for _ in range(fanout):
                        child = doc.create_node(Folder, name=f"f{count}")
                        parent.items.append(child)
                        next_frontier.append(child)
                        count += 1
                frontier = next_frontier
    assert per_tree == WORKING_SET or fanout != 6 or levels != 3
    return doc


# --- scenarios: each returns seconds for the timed part only ---


def timed(fn: Callable[[], Any]) -> float:
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def bench_build(n: int) -> float:
    return timed(lambda: make_scene(n, undo=False))


def bench_build_autocommit(n: int) -> float:
    doc = Doc(Scene)

    def go() -> None:
        for i in range(n):
            doc.root.transforms.append(doc.create_node(Transform, name=f"t{i}"))

    return timed(go)


def bench_iterate(n: int) -> float:
    doc = make_scene(n)
    return timed(lambda: sum(1 for _ in doc.root.volumes))


def bench_index(n: int) -> float:
    doc = make_scene(n)
    vols = doc.root.volumes

    def go() -> None:
        for i in range(n):
            vols[i]

    return timed(go)


def bench_lookup(n: int) -> float:
    doc = make_scene(n)
    ids = [v.id for v in doc.root.volumes]
    return timed(lambda: [doc.get_node_by_id(i) for i in ids])


def bench_write_autocommit(n: int) -> float:
    doc = make_scene(n)
    vols = list(doc.root.volumes)

    def go() -> None:
        for i, v in enumerate(vols):
            v.window = float(i)

    return timed(go)


def bench_write_one_tx(n: int) -> float:
    doc = make_scene(n)
    vols = list(doc.root.volumes)

    def go() -> None:
        with doc.transaction():
            for i, v in enumerate(vols):
                v.window = float(i)

    return timed(go)


def bench_write_big_list(k: int) -> float:
    """Rewrite a k-point markup 20 times (each write serializes the list)."""
    doc = Doc(Scene)
    m = doc.create_node(Markup, label="m", points=[[float(i), 0.0, 0.0] for i in range(k)])
    doc.root.markups.append(m)

    def go() -> None:
        for j in range(20):
            pts = list(m.points)
            pts.append([float(j), 1.0, 1.0])
            m.points = pts

    return timed(go)


def bench_undo_redo(n: int) -> float:
    doc = make_scene(n)
    vols = list(doc.root.volumes)
    with doc.transaction():
        for i, v in enumerate(vols):
            v.window = float(i)

    def go() -> None:
        doc.undo_manager.undo()
        doc.undo_manager.redo()

    return timed(go)


def bench_abort(n: int) -> float:
    doc = make_scene(n)
    vols = list(doc.root.volumes)

    def go() -> None:
        try:
            with doc.transaction():
                for i, v in enumerate(vols):
                    v.window = float(i)
                raise RuntimeError("abort")
        except RuntimeError:
            pass

    return timed(go)


def bench_dump(n: int) -> float:
    doc = make_scene(n)
    return timed(doc.dump)


def bench_dump_json(n: int) -> float:
    doc = make_scene(n)
    data = doc.dump()
    return timed(lambda: json.dumps(data))


def bench_restore(n: int) -> float:
    data = make_scene(n).dump()
    return timed(lambda: Doc.restore(data, root_type=Scene))


def bench_apply_ops(n: int) -> float:
    doc = make_scene(n)
    patch = {v.id: {"window": float(i)} for i, v in enumerate(doc.root.volumes)}
    return timed(lambda: doc.apply_operations(([], patch), strict=True))


def bench_referrers(n: int) -> float:
    doc = make_scene(n)
    hub = doc.root.transforms[0]
    return timed(lambda: doc.referrers(hub))


def bench_delete_restrict(n: int) -> float:
    """Delete of the hub every volume's transform points at: rejected."""
    doc = make_scene(n)
    hub = doc.root.transforms[0]

    def go() -> None:
        try:
            hub.delete()
        except Exception:
            pass

    return timed(go)


def bench_delete_range(n: int) -> float:
    doc = make_scene(n)
    vols = list(doc.root.volumes)
    return timed(lambda: vols[0].to(vols[-1]).delete())


def bench_handles(n: int) -> float:
    doc = make_scene(n)
    return timed(lambda: doc.handles(strength="strong"))


def bench_deep_tree(depth: int) -> float:
    """A nesting chain ``depth`` folders deep: build, then dump."""
    doc = Doc(Scene)

    def go() -> None:
        with doc.transaction():
            parent = doc.root
            slot = "folders"
            for i in range(depth):
                f = doc.create_node(Folder, name=f"f{i}")
                getattr(parent, slot).append(f)
                parent, slot = f, "items"
        doc.dump()
        list(doc.descendants(doc.root))

    return timed(go)


def bench_merge_undo(k: int) -> float:
    """k separate inserts merged into one undo step (a drag, keystrokes)."""
    doc = Doc(Scene, undo_manager=UndoManagerConfig(max_steps=100, merge_interval=1e9))

    def go() -> None:
        for i in range(k):
            doc.root.transforms.append(doc.create_node(Transform, name=f"t{i}"))

    return timed(go)


class _Client(ClientConnection):
    def __init__(self, cid: str) -> None:
        self._id = cid
        self.bytes = 0

    @property
    def client_id(self) -> str:
        return self._id

    async def send(self, message: dict[str, Any]) -> None:
        self.bytes += len(json.dumps(message))

    async def close(self) -> None:
        pass


class _Transport(Transport):
    async def start(self, on_connect, on_message, on_disconnect) -> None:  # type: ignore[no-untyped-def]
        self.on_connect, self.on_message = on_connect, on_message

    async def stop(self) -> None:
        pass


def bench_session_connect(n: int) -> float:
    """One client connecting to an n-volume scene (schema + snapshot)."""
    doc = make_scene(n)
    session = Session(doc)
    transport = _Transport()

    async def go() -> None:
        await session.bind(transport)
        await transport.on_connect(_Client("a"))

    return timed(lambda: asyncio.run(go()))


def bench_session_patch(n: int) -> float:
    """A client op patching n volumes, broadcast to 4 clients."""
    doc = make_scene(n)
    session = Session(doc)
    transport = _Transport()
    clients = [_Client(f"c{i}") for i in range(4)]
    patch = {v.id: {"window": 1.0} for v in doc.root.volumes}
    msg = {"type": "op", "ref": "r", "operations": {"ordered": [], "state": patch}}

    async def setup() -> None:
        await session.bind(transport)
        for c in clients:
            await transport.on_connect(c)

    asyncio.run(setup())
    return timed(lambda: asyncio.run(transport.on_message(clients[0], msg)))


class _PartialClient(_Client):
    @property
    def wants_partial(self) -> bool:
        return True


def _scoped_session(n: int) -> tuple[Session, _Transport, _Client, list[Any]]:
    """A tree of n folders with one whole-document client connected;
    returns the session, transport, that client, and the top folders."""
    doc = make_tree(n)
    session = Session(doc)
    transport = _Transport()
    whole = _Client("whole")

    async def setup() -> None:
        await session.bind(transport)
        await transport.on_connect(whole)

    asyncio.run(setup())
    return session, transport, whole, list(doc.root.folders)


def bench_tree_connect(n: int) -> float:
    """A whole-document client joining an n-folder tree: the baseline."""
    session, transport, whole, tops = _scoped_session(n)
    return timed(lambda: asyncio.run(transport.on_connect(_Client("b"))))


def bench_scope_join(n: int) -> float:
    """A scoped client joining an n-folder tree, anchored on one top
    folder (a working set of ~259 nodes): schema, scope, partial snapshot.
    Should not grow with n."""
    session, transport, whole, tops = _scoped_session(n)
    client = _PartialClient("p")
    scope = {"type": "scope", "ref": "s", "anchors": [{"id": tops[0].id}]}

    async def join() -> None:
        await transport.on_connect(client)
        await transport.on_message(client, scope)

    return timed(lambda: asyncio.run(join()))


def bench_project(n: int) -> float:
    """One field write by the whole-document client, projected onto a
    scoped client holding a ~259-node subtree of an n-folder tree, and
    broadcast. Should not grow with n."""
    session, transport, whole, tops = _scoped_session(n)
    client = _PartialClient("p")
    target = tops[0].items[0]

    async def join() -> None:
        await transport.on_connect(client)
        await transport.on_message(client, {"type": "scope", "ref": "s", "anchors": [{"id": tops[0].id}]})

    asyncio.run(join())
    msg = {"type": "op", "ref": "w", "operations": {"ordered": [], "state": {target.id: {"name": "x"}}}}
    return timed(lambda: asyncio.run(transport.on_message(whole, msg)))


def bench_project_view(k: int) -> float:
    """The same write, projected onto a scoped client whose view holds
    the whole k-folder tree: the cost of a projection in the size of the
    view (the placement recompute)."""
    session, transport, whole, tops = _scoped_session(k)
    client = _PartialClient("p")
    target = tops[0]

    async def join() -> None:
        await transport.on_connect(client)
        await transport.on_message(client, {"type": "scope", "ref": "s", "anchors": [{"id": session.doc.root.id}]})

    asyncio.run(join())
    msg = {"type": "op", "ref": "w", "operations": {"ordered": [], "state": {target.id: {"name": "x"}}}}
    return timed(lambda: asyncio.run(transport.on_message(whole, msg)))


def bench_scope_change(n: int) -> float:
    """A scoped client moving its anchor from one top folder to another
    (~259 nodes out, ~259 in) in an n-folder tree. Should not grow with n."""
    session, transport, whole, tops = _scoped_session(n)
    client = _PartialClient("p")

    async def join() -> None:
        await transport.on_connect(client)
        await transport.on_message(client, {"type": "scope", "ref": "s", "anchors": [{"id": tops[0].id}]})

    asyncio.run(join())
    change = {"type": "scope", "ref": "t", "anchors": [{"id": tops[1].id}]}
    return timed(lambda: asyncio.run(transport.on_message(client, change)))


def bench_session_small_ops(n: int) -> float:
    """n one-field ops from a client, each broadcast to 4 clients."""
    doc = make_scene(64)
    session = Session(doc)
    transport = _Transport()
    clients = [_Client(f"c{i}") for i in range(4)]
    vid = doc.root.volumes[0].id

    async def setup() -> None:
        await session.bind(transport)
        for c in clients:
            await transport.on_connect(c)

    async def go() -> None:
        for i in range(n):
            await transport.on_message(clients[0], {
                "type": "op", "ref": f"r{i}",
                "operations": {"ordered": [], "state": {vid: {"window": float(i)}}},
            })

    asyncio.run(setup())
    return timed(lambda: asyncio.run(go()))


SCENARIOS: dict[str, tuple[Callable[[int], float], str]] = {
    "build": (bench_build, "n transforms + n volumes, one transaction"),
    "build_autocommit": (bench_build_autocommit, "n appends, one commit each"),
    "iterate": (bench_iterate, "iterate n children"),
    "index": (bench_index, "children[i] for i in range(n)"),
    "lookup": (bench_lookup, "get_node_by_id x n"),
    "write_autocommit": (bench_write_autocommit, "n field writes, one commit each"),
    "write_one_tx": (bench_write_one_tx, "n field writes, one transaction"),
    "write_big_list": (bench_write_big_list, "20 rewrites of a k-point list field"),
    "undo_redo": (bench_undo_redo, "undo + redo of an n-write transaction"),
    "abort": (bench_abort, "roll back an n-write transaction"),
    "dump": (bench_dump, "dump() of 2n+1 nodes"),
    "dump_json": (bench_dump_json, "json.dumps of that dump"),
    "restore": (bench_restore, "Doc.restore of that dump"),
    "apply_ops": (bench_apply_ops, "apply_operations, n state entries, strict"),
    "referrers": (bench_referrers, "referrers() of a node with n referrers"),
    "delete_restrict": (bench_delete_restrict, "rejected delete of that node"),
    "delete_range": (bench_delete_range, "delete a range of n siblings"),
    "handles": (bench_handles, "handles(strong) over 2n+1 nodes"),
    "deep_tree": (bench_deep_tree, "build + dump a chain n folders deep"),
    "merge_undo": (bench_merge_undo, "n inserts merged into one undo step"),
    "session_connect": (bench_session_connect, "connect to an n-volume scene"),
    "session_patch": (bench_session_patch, "one op patching n nodes, 4 clients"),
    "session_small_ops": (bench_session_small_ops, "n one-field ops, 4 clients"),
    "tree_connect": (bench_tree_connect, "whole-document join of an n-folder forest"),
    "scope_join": (bench_scope_join, "scoped join, 259-node working set, n-folder forest"),
    "project": (bench_project, "one write projected onto that view, n-folder forest"),
    "project_view": (bench_project_view, "one write projected onto a view of n folders"),
    "scope_change": (bench_scope_change, "move the anchor to another 259-node subtree"),
}

# Sizes that keep a scenario within a few seconds at the default sweep.
SIZE_SCALE: dict[str, float] = {
    "index": 0.5,
    "write_big_list": 2.0,
    "deep_tree": 0.25,
    "merge_undo": 0.5,
    "session_small_ops": 0.5,
}


def run(sizes: list[int], only: set[str] | None = None) -> None:
    print(f"{'scenario':<20}{'n':>8}{'ms':>10}{'us/n':>10}{'x':>7}  description")
    for name, (fn, desc) in SCENARIOS.items():
        if only and name not in only:
            continue
        prev: float | None = None
        for size in sizes:
            n = max(1, int(size * SIZE_SCALE.get(name, 1.0)))
            fn(n)  # warm up (imports, caches)
            secs = min(fn(n) for _ in range(2))
            ratio = f"{secs / prev:.1f}" if prev else ""
            print(f"{name:<20}{n:>8}{secs * 1000:>10.1f}{secs / n * 1e6:>10.1f}{ratio:>7}  {desc}")
            prev = secs
        sys.stdout.flush()


def profile(name: str, n: int) -> None:
    fn, _ = SCENARIOS[name]
    fn(n)
    prof = cProfile.Profile()
    prof.enable()
    fn(n)
    prof.disable()
    pstats.Stats(prof).sort_stats("cumulative").print_stats(25)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--profile":
        profile(args[1], int(args[2]) if len(args) > 2 else 2000)
    else:
        sizes = [int(a) for a in args if a.isdigit()] or [1000, 4000]
        only = {a for a in args if not a.isdigit()} or None
        run(sizes, only)
