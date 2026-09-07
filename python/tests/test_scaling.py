"""Scaling tests: work that should be linear must not turn quadratic.

Each case times an operation at a size n and at 4n and asserts the ratio
stays well under 16 (quadratic). Linear work shows about 4; the bound is
loose so machine speed and noise do not matter. Absolute timings live in
``benchmarks/bench.py``.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable

import pytest

sys.path.insert(0, "benchmarks")

from bench import (  # noqa: E402
    Folder,
    Scene,
    bench_abort,
    bench_apply_ops,
    bench_build,
    bench_build_autocommit,
    bench_delete_range,
    bench_delete_restrict,
    bench_dump,
    bench_handles,
    bench_merge_undo,
    bench_referrers,
    bench_restore,
    bench_session_connect,
    bench_session_patch,
    bench_undo_redo,
    bench_write_autocommit,
    bench_write_one_tx,
)
from atomdoc import Doc  # noqa: E402

N = 400
QUADRATIC_BOUND = 10.0


def ratio(fn: Callable[[int], float]) -> float:
    """Time ratio between 4n and n, best of five.

    If the small run is under a millisecond the timer and fixed per-call
    overhead (an event loop start, say) dominate, so n is raised once; a
    quadratic path stays far above the bound either way.
    """
    n = N
    fn(n)  # warm up
    small = min(fn(n) for _ in range(3))
    if small < 1e-3:
        n *= 4
        small = min(fn(n) for _ in range(3))
    large = min(fn(4 * n) for _ in range(3))
    return large / max(small, 1e-6)


@pytest.mark.parametrize(
    "fn",
    [
        bench_build,
        bench_build_autocommit,
        bench_write_autocommit,
        bench_write_one_tx,
        bench_undo_redo,
        bench_abort,
        bench_dump,
        bench_restore,
        bench_apply_ops,
        bench_referrers,
        bench_delete_restrict,
        bench_delete_range,
        bench_handles,
        bench_merge_undo,
        bench_session_connect,
        bench_session_patch,
    ],
    ids=lambda f: f.__name__.removeprefix("bench_"),
)
def test_scales_linearly(fn: Callable[[int], float]) -> None:
    assert ratio(fn) < QUADRATIC_BOUND


def _same_tree(a: object, b: object) -> bool:
    """Structural equality without recursion (``==`` on a deep nest
    exhausts the interpreter's stack, as does ``json.dumps``)."""
    stack = [(a, b)]
    while stack:
        x, y = stack.pop()
        if isinstance(x, list) and isinstance(y, list):
            if len(x) != len(y):
                return False
            stack.extend(zip(x, y))
        elif isinstance(x, dict) and isinstance(y, dict):
            if x.keys() != y.keys():
                return False
            stack.extend((x[k], y[k]) for k in x)
        elif x != y:
            return False
    return True


def test_deep_tree_does_not_overflow():
    """A chain thousands of nodes deep: build, dump, walk, restore, adopt,
    delete. (Serializing such a dump with the standard ``json`` module
    still hits the interpreter's recursion limit; that is json's limit,
    not the document's.)"""
    depth = 5000
    doc = Doc(Scene)
    with doc.transaction():
        parent = doc.root
        slot = "folders"
        for i in range(depth):
            f = doc.create_node(Folder, name=f"f{i}")
            getattr(parent, slot).append(f)
            parent, slot = f, "items"
    data = doc.dump()
    assert sum(1 for _ in doc.descendants(doc.root)) == depth
    restored = Doc.restore(data, root_type=Scene)
    assert _same_tree(restored.dump(), data)
    fragment = doc.dump(doc.root.folders[0])
    adopted = Doc(Scene)
    adopted.adopt(fragment, adopted.root, "folders")
    assert len(adopted._node_map) == depth + 1
    t0 = time.perf_counter()
    doc.root.folders[0].delete()
    assert len(doc._node_map) == 1
    assert time.perf_counter() - t0 < 5
