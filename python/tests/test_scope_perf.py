"""Partial replication must cost the view, not the document.

A scoped join, a projected commit, and a scope change are timed at a
document size n and at 8n with the same working set; the ratio must
stay near 1 (a bound of 3 absorbs noise; anything proportional to the
document shows 8). A commit projected onto a view of n nodes is timed
at n and 4n and must stay well under quadratic. Scope churn across a
large document must reach a steady state in memory.
"""

from __future__ import annotations

import asyncio
import gc
import sys
import tracemalloc
from collections.abc import Callable

import pytest

sys.path.insert(0, "benchmarks")

from bench import (  # noqa: E402
    WORKING_SET,
    _PartialClient,
    _scoped_session,
    bench_project,
    bench_project_view,
    bench_scope_change,
    bench_scope_join,
)

N = 2000
FLAT_BOUND = 3.0
QUADRATIC_BOUND = 10.0


def best(fn: Callable[[int], float], n: int) -> float:
    fn(n)  # warm up
    return min(fn(n) for _ in range(3))


@pytest.mark.parametrize("fn", [bench_scope_join, bench_project, bench_scope_change])
def test_costs_the_view_not_the_document(fn: Callable[[int], float]) -> None:
    small = best(fn, N)
    large = best(fn, 8 * N)
    ratio = large / max(small, 1e-6)
    if ratio >= FLAT_BOUND:  # one more chance on a noisy machine
        ratio = min(ratio, best(fn, 8 * N) / max(best(fn, N), 1e-6))
    assert ratio < FLAT_BOUND, f"{fn.__name__}: {small * 1000:.2f} ms at {N}, {large * 1000:.2f} ms at {8 * N}"


def test_projection_scales_with_the_view() -> None:
    small = best(bench_project_view, N)
    large = best(bench_project_view, 4 * N)
    ratio = large / max(small, 1e-6)
    if ratio >= QUADRATIC_BOUND:
        ratio = min(ratio, best(bench_project_view, 4 * N) / max(best(bench_project_view, N), 1e-6))
    assert ratio < QUADRATIC_BOUND


def test_scope_churn_reaches_steady_state() -> None:
    """Moving a scope across a large document over and over keeps the
    view at working-set size and the process at a steady footprint."""
    session, transport, whole, tops = _scoped_session(8 * N)
    client = _PartialClient("p")
    cid = client.client_id

    async def join() -> None:
        await transport.on_connect(client)
        await transport.on_message(client, {"type": "scope", "ref": "s", "anchors": [{"id": tops[0].id}]})

    asyncio.run(join())

    def churn(rounds: int, start: int) -> None:
        async def go() -> None:
            for i in range(rounds):
                top = tops[(start + i) % len(tops)]
                await transport.on_message(client, {"type": "scope", "ref": f"c{i}", "anchors": [{"id": top.id}]})
                # A write inside the new view, so commits are projected too.
                target = top.items[0]
                await transport.on_message(whole, {
                    "type": "op", "ref": f"w{i}",
                    "operations": {"ordered": [], "state": {target.id: {"name": f"n{i}"}}},
                })

        asyncio.run(go())

    churn(20, 1)
    gc.collect()
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    churn(60, 21)
    gc.collect()
    after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    view = session._views[cid]
    assert len(view.held) == WORKING_SET + 1  # the working set and the root stub
    assert len(view.placed) == len(view.held)
    assert len(view.stubs) == 1
    grown = sum(stat.size_diff for stat in after.compare_to(before, "filename") if stat.size_diff > 0)
    assert grown < 512 * 1024, f"grew by {grown / 1024:.0f} KiB over 60 scope changes"
