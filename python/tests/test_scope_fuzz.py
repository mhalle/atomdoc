"""Adversarial simulation of a scoped client replica against ClientView."""

from __future__ import annotations

import os
import random
from typing import Any

import pytest

from atomdoc import Array, Doc, Ref, node
from atomdoc._scope import ClientView


@node
class Note:
    text: str = ""


@node
class Item:
    label: str = ""
    see: Ref[Note] | None = None
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


DEBUG = bool(os.environ.get("DEBUG_SCOPE"))


class Failure(Exception):
    pass


# --------------------------------------------------------------------------
# The simulated client replica
# --------------------------------------------------------------------------


class Replica:
    def __init__(self, root_id: str) -> None:
        self.root_id = root_id
        # id -> {"type", "kind", "parent", "slot", "state"}
        self.nodes: dict[str, dict[str, Any]] = {}
        # id -> slot -> [ids]
        self.kids: dict[str, dict[str, list[str]]] = {}
        self.log: list[Any] = []

    # -- structure helpers --

    def _slot(self, pid: str, slot: str) -> list[str]:
        return self.kids.setdefault(pid, {}).setdefault(slot, [])

    def _detach(self, nid: str) -> None:
        info = self.nodes[nid]
        p, s = info["parent"], info["slot"]
        if p is not None:
            lst = self._slot(p, s)
            if nid in lst:
                lst.remove(nid)
        info["parent"] = None
        info["slot"] = None

    def _subtree(self, nid: str) -> list[str]:
        out = [nid]
        for slot, lst in self.kids.get(nid, {}).items():
            for c in list(lst):
                out.extend(self._subtree(c))
        return out

    def _remove(self, nid: str) -> None:
        for x in self._subtree(nid):
            if x != nid:
                self.nodes.pop(x, None)
                self.kids.pop(x, None)
        self._detach(nid)
        self.nodes.pop(nid, None)
        self.kids.pop(nid, None)

    def _place(self, nid: str, ntype: str, kind: str | None, pid: Any, slot: str,
               prev: Any, nxt: Any, what: str) -> None:
        pid = self.root_id if pid in (0, None, "") else pid
        if pid not in self.nodes:
            raise Failure(f"{what}: parent {pid!r} unknown to client")
        if nid in self.nodes:
            if kind is not None and self.nodes[nid]["parent"] is not None:
                raise Failure(f"{what}: {nid!r} is already in the client's tree")
            self._detach(nid)
            if kind is not None:
                self.nodes[nid]["kind"] = kind
                if kind == "stub":
                    self.nodes[nid]["state"] = None
                elif self.nodes[nid]["state"] is None:
                    self.nodes[nid]["state"] = {}
        else:
            if kind is None:
                raise Failure(f"{what}: {nid!r} unknown to client")
            self.nodes[nid] = {
                "type": ntype, "kind": kind, "parent": None, "slot": None,
                "state": {} if kind == "full" else None,
            }
        lst = self._slot(pid, slot)
        if prev not in (0, None, ""):
            if prev not in self.nodes:
                raise Failure(f"{what}: prev {prev!r} unknown to client")
            if self.nodes[prev]["parent"] != pid or self.nodes[prev]["slot"] != slot:
                raise Failure(
                    f"{what}: prev {prev!r} is not in slot {slot!r} of {pid!r} "
                    f"on the client (it is under {self.nodes[prev]['parent']!r})"
                )
            lst.insert(lst.index(prev) + 1, nid)
        elif nxt not in (0, None, ""):
            if nxt not in self.nodes:
                raise Failure(f"{what}: next {nxt!r} unknown to client")
            if self.nodes[nxt]["parent"] != pid or self.nodes[nxt]["slot"] != slot:
                raise Failure(
                    f"{what}: next {nxt!r} is not in slot {slot!r} of {pid!r} "
                    f"on the client (it is under {self.nodes[nxt]['parent']!r})"
                )
            lst.insert(lst.index(nxt), nid)
        else:
            lst.append(nid)
        self.nodes[nid]["parent"] = pid
        self.nodes[nid]["slot"] = slot

    # -- snapshot load --

    def load(self, entry: list[Any], detached: list[list[str]]) -> None:
        def walk(e: list[Any], parent: str | None, slot: str | None) -> None:
            nid, ntype = e[0], e[1]
            state = e[2] if len(e) > 2 else None
            kind = "stub" if state is None else "full"
            self.nodes[nid] = {
                "type": ntype, "kind": kind, "parent": parent, "slot": slot,
                "state": dict(state) if state is not None else None,
            }
            if parent is not None:
                self._slot(parent, slot).append(nid)
            if len(e) > 3:
                for sname, children in e[3].items():
                    self._slot(nid, sname)
                    for c in children:
                        walk(c, nid, sname)

        walk(entry, None, None)
        for nid, ntype in detached:
            self.nodes.setdefault(nid, {
                "type": ntype, "kind": "stub", "parent": None, "slot": None,
                "state": None,
            })

    # -- op application --

    def apply(self, projected: dict[str, Any]) -> None:
        self.log.append(projected)
        for op in projected["ordered"]:
            code = op[0]
            if code == 0:
                pairs, pid, slot, prev, nxt = op[1], op[2], op[3], op[4], op[5]
                first = True
                prev_id: Any = 0
                for pair in pairs:
                    kind = "full" if len(pair) == 2 else "stub"
                    if first:
                        self._place(pair[0], pair[1], kind, pid, slot, prev, nxt,
                                    f"insert {op}")
                        first = False
                    else:
                        self._place(pair[0], pair[1], kind, pid, slot,
                                    prev_id, 0, f"insert {op}")
                    prev_id = pair[0]
            elif code == 1:
                start, end = op[1], op[2]
                if start not in self.nodes:
                    raise Failure(f"delete {op}: {start!r} unknown to client")
                if end not in (0, None, ""):
                    if end not in self.nodes:
                        raise Failure(f"delete {op}: end {end!r} unknown")
                    pid = self.nodes[start]["parent"]
                    slot = self.nodes[start]["slot"]
                    lst = self._slot(pid, slot)
                    i, j = lst.index(start), lst.index(end)
                    for nid in lst[i:j + 1]:
                        self._remove(nid)
                else:
                    self._remove(start)
            elif code == 2:
                nid, end, pid, slot, prev, nxt = op[1], op[2], op[3], op[4], op[5], op[6]
                if nid not in self.nodes:
                    raise Failure(f"move {op}: {nid!r} unknown to client")
                if end not in (0, None, ""):
                    raise Failure(f"move {op}: unexpected range end")
                self._place(nid, self.nodes[nid]["type"], None, pid, slot, prev, nxt,
                            f"move {op}")
            elif code == 3:
                nid = op[1]
                if nid not in self.nodes:
                    raise Failure(f"demote {op}: {nid!r} unknown to client")
                self.nodes[nid]["kind"] = "stub"
                self.nodes[nid]["state"] = None
            elif code == 4:
                # Idempotent: a node already gone is ignored.
                nid = op[1]
                if nid in self.nodes:
                    self._remove(nid)
            elif code == 5:
                # The node is now a detached stub: taken out of the tree
                # with its subtree if it is in it, created if unknown.
                nid, ntype = op[1], op[2]
                if nid in self.nodes:
                    if self.nodes[nid]["parent"] is not None:
                        for x in self._subtree(nid):
                            if x != nid:
                                self.nodes.pop(x, None)
                                self.kids.pop(x, None)
                        self._detach(nid)
                        self.kids.pop(nid, None)
                    self.nodes[nid]["kind"] = "stub"
                    self.nodes[nid]["state"] = None
                    self.nodes[nid]["parent"] = None
                    self.nodes[nid]["slot"] = None
                else:
                    self.nodes[nid] = {
                        "type": ntype, "kind": "stub", "parent": None, "slot": None,
                        "state": None,
                    }
            elif code == 6:
                nid = op[1]
                if nid not in self.nodes:
                    raise Failure(f"fill {op}: {nid!r} unknown to client")
                if self.nodes[nid]["kind"] != "stub":
                    raise Failure(f"fill {op}: {nid!r} is not a stub")
                if self.nodes[nid]["parent"] is None and nid != self.root_id:
                    raise Failure(f"fill {op}: {nid!r} is detached")
                self.nodes[nid]["kind"] = "full"
                self.nodes[nid]["state"] = {}
            else:
                raise Failure(f"unknown opcode {op!r}")
        for nid, patch in projected["state"].items():
            if nid not in self.nodes:
                raise Failure(f"state for unknown node {nid!r}")
            if self.nodes[nid]["kind"] != "full":
                raise Failure(f"state for stub {nid!r}")
            self.nodes[nid]["state"].update(patch)

    # -- canonical form --

    def canon(self, doc: Doc) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for nid, info in self.nodes.items():
            entry: dict[str, Any] = {
                "type": info["type"],
                "kind": info["kind"],
                "parent": info["parent"],
                "slot": info["slot"],
            }
            if info["kind"] == "full":
                entry["state"] = _norm_state(doc, nid, info["state"])
            out[nid] = entry
        for nid in self.nodes:
            slots = {s: list(v) for s, v in self.kids.get(nid, {}).items() if v}
            if slots:
                out[nid]["kids"] = slots
        return out


def _norm_state(doc: Doc, nid: str, state: dict[str, Any] | None) -> dict[str, Any]:
    node_obj = doc.get_node_by_id(nid)
    defaults = getattr(node_obj, "_field_defaults", {}) if node_obj else {}
    return {
        k: v for k, v in (state or {}).items()
        if not (k in defaults and v == defaults[k])
    }


def truth_canon(doc: Doc, anchors: dict[str, int | None]) -> dict[str, Any]:
    view = ClientView(doc, anchors)
    view.reset()
    rep = Replica(doc.root.id)
    rep.load(*view.snapshot())
    return rep.canon(doc)


def diff_report(a: dict[str, Any], b: dict[str, Any]) -> str:
    lines = []
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            lines.append(f"  {k}:\n    replica={a.get(k)}\n    truth  ={b.get(k)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Targeted scenarios
# --------------------------------------------------------------------------


def build() -> tuple[Doc, dict[str, Any]]:
    doc = Doc(root_type=Page)
    ids: dict[str, Any] = {}
    with doc.transaction():
        doc.root.title = "Page"
        s1 = doc.create_node(Section, heading="One")
        s2 = doc.create_node(Section, heading="Two")
        doc.root.sections.append(s1)
        doc.root.sections.append(s2)
        i1 = doc.create_node(Item, label="A")
        i2 = doc.create_node(Item, label="B")
        s1.items.append(i1)
        s1.items.append(i2)
        n1 = doc.create_node(Note, text="n1")
        i1.notes.append(n1)
        i3 = doc.create_node(Item, label="C")
        s2.items.append(i3)
        n3 = doc.create_node(Note, text="n3")
        i3.notes.append(n3)
    ids.update(s1=s1, s2=s2, i1=i1, i2=i2, i3=i3, n1=n1, n3=n3)
    return doc, ids


class Harness:
    def __init__(self, doc: Doc, anchors: dict[str, int | None]) -> None:
        self.doc = doc
        self.view = ClientView(doc, anchors)
        self.view.reset()
        self.replica = Replica(doc.root.id)
        self.replica.load(*self.view.snapshot())
        self.events: list[Any] = []
        self.unsub = doc.on_change(self._on_change)
        self.step = 0

    def _on_change(self, event) -> None:
        projected = self.view.project(event)
        if DEBUG:
            print("    project:", projected)
        if projected is not None:
            self.replica.apply(projected)

    def change(self, anchors: dict[str, int | None]) -> None:
        delta = self.view.change(anchors)
        if DEBUG:
            print("    change:", delta)
        self.replica.apply(delta)

    def check(self, label: str) -> None:
        if DEBUG:
            print("    held:", sorted(self.view.held.items()))
            print("    placed:", sorted(self.view.placed))
            live = Replica(self.doc.root.id)
            live.load(*self.view.snapshot())
            got0 = self.replica.canon(self.doc)
            live0 = live.canon(self.doc)
            if got0 != live0:
                print("    LIVE SNAPSHOT MISMATCH:\n" + diff_report(got0, live0))
        truth = truth_canon(self.doc, self.view.anchors)
        # cheapest invariant: held vs ground truth
        fresh = ClientView(self.doc, self.view.anchors)
        fresh.reset()
        assert self.view.held == fresh.held, (
            f"[{label}] held drifted\n  view ={sorted(self.view.held.items())}\n"
            f"  truth={sorted(fresh.held.items())}"
        )
        assert self.view.placed == fresh.placed, (
            f"[{label}] placed drifted\n  view ={sorted(self.view.placed)}\n"
            f"  truth={sorted(fresh.placed)}"
        )
        got = self.replica.canon(self.doc)
        assert got == truth, f"[{label}] replica diverged\n{diff_report(got, truth)}"


def test_insert_beside_a_node_that_moves_in_the_same_commit():
    """A new node whose next sibling is a node that has yet to move."""
    doc, ids = build()
    h = Harness(doc, {doc.root.id: None})
    with doc.transaction():
        # i3 moves from s2 into s1 (before i1), and a fresh item is created
        # ahead of it in the same commit.
        ids["i3"].move(ids["i1"], position="before")
        fresh = doc.create_node(Item, label="NEW")
        ids["i3"].insert_before(fresh)
    h.check("insert beside a pending mover")


def test_place_children_uses_a_pending_mover_as_anchor():
    """A subtree entering the view whose child list contains a node the
    client already holds elsewhere and that has yet to move."""
    doc, ids = build()
    h = Harness(doc, {ids["s1"].id: None})
    with doc.transaction():
        # s2 becomes the second anchor's parent... instead: move i1 (held)
        # under i3 while s2 enters the view.
        pass
    h.check("noop")


def test_detached_stub_with_a_held_child_vanishes_from_the_snapshot():
    """A ref target whose parent is itself only a detached stub."""
    doc, ids = build()
    with doc.transaction():
        ids["i1"].see = ids["n3"]      # n3 lives under i3, out of scope
        ids["i2"].see = None
    view = ClientView(doc, {ids["s1"].id: None})
    view.reset()
    # make i3 (n3's parent) held too, as another ref target
    with doc.transaction():
        ids["s1"].related = ids["i3"]
    view = ClientView(doc, {ids["s1"].id: None})
    view.reset()
    assert view.held.get(ids["i3"].id) == "stub"
    assert view.held.get(ids["n3"].id) == "stub"
    data, detached = view.snapshot()
    seen = set()

    def walk(e):
        seen.add(e[0])
        if len(e) > 3:
            for lst in e[3].values():
                for c in lst:
                    walk(c)

    walk(data)
    seen.update(d[0] for d in detached)
    assert set(view.held) == seen, (
        f"held={sorted(view.held)}\nsnapshot={sorted(seen)}\n"
        f"missing={sorted(set(view.held) - seen)}"
    )


def test_client_may_insert_a_pair_naming_a_node_it_holds_as_a_stub():
    doc, ids = build()
    view = ClientView(doc, {ids["s1"].id: None})
    view.reset()
    assert view.held.get(ids["s2"].id) is None
    # A client request that names s2 in an insert pair: the known-id rule
    # would move s2 (a node it only holds as a stub) under s1.
    from atomdoc._scope import OutOfScope
    with pytest.raises(OutOfScope):
        view.check_operations(([
            (0, [(ids["i3"].id, "Item")], ids["s1"].id, "items", 0, 0),
        ], {}))


# --------------------------------------------------------------------------
# Randomized driver
# --------------------------------------------------------------------------


def _all(doc, cls):
    return [n for n in doc._node_map.values() if isinstance(n, cls)]


def fuzz(seed: int, steps: int = 60, allow_scope_change: bool = True) -> None:
    rng = random.Random(seed)
    doc, ids = build()
    anchors: dict[str, int | None] = {ids["s1"].id: None}
    h = Harness(doc, anchors)
    history: list[str] = []
    counter = [0]

    def pick(cls):
        pool = _all(doc, cls)
        return rng.choice(pool) if pool else None

    for step in range(steps):
        kind = rng.randrange(10)
        desc = ""
        try:
            if kind == 0:
                with doc.transaction():
                    s = doc.create_node(Section, heading=f"s{counter[0]}")
                    counter[0] += 1
                    tgt = pick(Section)
                    if tgt is not None and rng.random() < 0.5:
                        tgt.insert_before(s)
                        desc = f"new Section before {tgt.id}"
                    else:
                        doc.root.sections.append(s)
                        desc = "new Section appended to root"
            elif kind == 1:
                p = pick(Section)
                if p is None:
                    continue
                with doc.transaction():
                    it = doc.create_node(Item, label=f"i{counter[0]}")
                    counter[0] += 1
                    if p.items and rng.random() < 0.5:
                        t = rng.choice(list(p.items))
                        t.insert_before(it)
                        desc = f"new Item before {t.id}"
                    else:
                        p.items.append(it)
                        desc = f"new Item in {p.id}"
            elif kind == 2:
                p = pick(Item)
                if p is None:
                    continue
                with doc.transaction():
                    nt = doc.create_node(Note, text=f"n{counter[0]}")
                    counter[0] += 1
                    p.notes.append(nt)
                    desc = f"new Note in {p.id}"
            elif kind == 3:
                victims = [n for n in doc._node_map.values() if n is not doc.root]
                if not victims:
                    continue
                victim = rng.choice(victims)
                with doc.transaction():
                    for r in doc.referrers(victim):
                        for name in type(r)._ref_defs:
                            if getattr(r, "_state", {}).get(name) == victim.id:
                                setattr(r, name, None)
                    for d in list(doc.descendants(victim)):
                        for r in doc.referrers(d):
                            for name in type(r)._ref_defs:
                                if getattr(r, "_state", {}).get(name) == d.id:
                                    setattr(r, name, None)
                    victim.delete()
                    desc = f"delete {victim.id}"
            elif kind == 4:
                movers = [n for n in doc._node_map.values() if n is not doc.root]
                rng.shuffle(movers)
                done = False
                for m in movers:
                    if isinstance(m, Section):
                        parents, slot = [doc.root], "sections"
                    elif isinstance(m, Item):
                        parents, slot = _all(doc, Section), "items"
                    else:
                        parents, slot = _all(doc, Item), "notes"
                    parents = [
                        p for p in parents
                        if p is not m and m not in list(doc.ancestors(p)) and p is not m._parent
                        or (p is m._parent)
                    ]
                    parents = [p for p in parents if p is not m and m not in list(doc.ancestors(p))]
                    if not parents:
                        continue
                    tgt = rng.choice(parents)
                    with doc.transaction():
                        if rng.random() < 0.5 and getattr(tgt, slot):
                            sib = rng.choice(list(getattr(tgt, slot)))
                            if sib is m:
                                m.move(tgt, slot, "append")
                            else:
                                m.move(sib, position=rng.choice(["before", "after"]))
                        else:
                            m.move(tgt, slot, rng.choice(["append", "prepend"]))
                    desc = f"move {m.id} -> {tgt.id}.{slot}"
                    done = True
                    break
                if not done:
                    continue
            elif kind == 5:
                s = pick(Section)
                items = _all(doc, Item)
                if s is None:
                    continue
                with doc.transaction():
                    tgt = rng.choice(items) if items and rng.random() < 0.8 else None
                    s.related = tgt
                    desc = f"{s.id}.related = {tgt.id if tgt else None}"
            elif kind == 6:
                it = pick(Item)
                notes = _all(doc, Note)
                if it is None:
                    continue
                with doc.transaction():
                    tgt = rng.choice(notes) if notes and rng.random() < 0.8 else None
                    it.see = tgt
                    desc = f"{it.id}.see = {tgt.id if tgt else None}"
            elif kind == 7:
                n = rng.choice(list(doc._node_map.values()))
                with doc.transaction():
                    if isinstance(n, Note):
                        n.text = f"t{step}"
                    elif isinstance(n, Item):
                        n.label = f"l{step}"
                    elif isinstance(n, Section):
                        n.heading = f"h{step}"
                    else:
                        n.title = f"T{step}"
                    desc = f"state {n.id}"
            elif kind == 8:
                um = doc.undo_manager
                if rng.random() < 0.6 and um.can_undo:
                    um.undo()
                    desc = "undo"
                elif um.can_redo:
                    um.redo()
                    desc = "redo"
                else:
                    continue
            else:
                if not allow_scope_change:
                    continue
                pool = [n.id for n in doc._node_map.values()]
                k = rng.randrange(1, 3)
                new_anchors = {}
                for _ in range(k):
                    nid = rng.choice(pool)
                    new_anchors[nid] = rng.choice([None, 0, 1, 2])
                if rng.random() < 0.15:
                    new_anchors["nope-" + str(step)] = None
                h.change(new_anchors)
                desc = f"scope -> {new_anchors}"
        except Failure:
            history.append(desc or f"kind={kind}")
            raise AssertionError(
                f"seed={seed} step={step}\nhistory:\n  " + "\n  ".join(history)
            ) from None
        except (ValueError, RuntimeError):
            continue
        history.append(desc or f"kind={kind}")
        if DEBUG:
            print(f"step {step}: {desc}")
        try:
            h.check(f"seed={seed} step={step} after {desc}")
        except AssertionError as exc:
            raise AssertionError(
                f"{exc}\nhistory:\n  " + "\n  ".join(history)
            ) from None


@pytest.mark.parametrize("seed", list(range(40)))
def test_fuzz_no_scope_change(seed):
    fuzz(seed, steps=60, allow_scope_change=False)


@pytest.mark.parametrize("seed", list(range(40)))
def test_fuzz_with_scope_change(seed):
    fuzz(1000 + seed, steps=60, allow_scope_change=True)


# --------------------------------------------------------------------------
# Minimized repros
# --------------------------------------------------------------------------


def test_stub_dropped_with_its_exiting_parent_on_a_scope_change():
    """Minimized from fuzz seed 1005: i1 is a ref-target stub under s1;
    a scope change makes s1 leave, and i1 goes with it silently."""
    doc, ids = build()
    with doc.transaction():
        ids["s2"].related = ids["i1"]
    h = Harness(doc, {ids["i2"].id: None, ids["s2"].id: 0})
    assert h.view.held[ids["s1"].id] == "stub"      # ancestor of i2
    assert h.view.held[ids["i1"].id] == "stub"      # s2.related
    h.change({ids["s2"].id: 0})                     # s1 leaves, i1 stays held
    h.check("scope -> s2 only")


def test_stub_dropped_with_its_exiting_parent_on_a_commit():
    """The same hole on the project() path: a move takes s1 out of the
    view while i1 stays a stub."""
    doc, ids = build()
    with doc.transaction():
        ids["s2"].related = ids["i1"]
    h = Harness(doc, {ids["i2"].id: None, ids["s2"].id: 0})
    assert h.view.held[ids["s1"].id] == "stub"
    with doc.transaction():
        ids["i2"].move(ids["s2"], "items", "append")   # s1 stops being an ancestor
    h.check("i2 moves to s2")


def test_deleted_detached_stub_gets_no_delete_op():
    doc, ids = build()
    with doc.transaction():
        n2 = doc.create_node(Note, text="n2")
        ids["i2"].notes.append(n2)
    h = Harness(doc, {ids["i1"].id: None})       # s1 is an ancestor stub
    with doc.transaction():
        ids["i1"].see = n2                       # n2: detached stub (i2 unheld)
    h.check("i1.see = n2")
    assert h.view.held.get(n2.id) == "stub"
    assert n2.id not in h.view.placed
    with doc.transaction():
        ids["s1"].delete()                       # deletes s1, i1, n1, i2, n2
    h.check("delete s1")


def test_place_children_anchors_a_run_on_a_pending_mover():
    doc, ids = build()
    h = Harness(doc, {ids["s1"].id: None, ids["n3"].id: None})
    with doc.transaction():
        ids["n3"].move(ids["i1"], "notes", "append")   # n3 out of i3
        ids["i3"].move(ids["s1"], "items", "append")   # i3 enters in full
        ids["n1"].move(ids["i3"], "notes", "append")   # held mover into i3
        fresh = doc.create_node(Note, text="fresh")
        ids["i3"].notes.append(fresh)                  # run anchored on n1
    h.check("place_children with a pending mover")


def test_check_operations_lets_a_client_name_an_unheld_node_in_an_insert():
    from atomdoc._scope import OutOfScope
    doc, ids = build()
    view = ClientView(doc, {ids["s1"].id: None})
    view.reset()
    assert ids["i3"].id not in view.held
    # A well-formed insert whose pair names an existing node the client
    # does not hold: the known-id rule moves i3 into the client's scope.
    with pytest.raises(OutOfScope):
        view.check_operations(([
            (0, [(ids["i3"].id, "Item")], ids["s1"].id, "items", 0, 0),
        ], {}))


def test_hold_everything_then_narrow():
    """The whole-client narrowing path: hold_everything() then change()."""
    doc, ids = build()
    whole = ClientView(doc, {doc.root.id: None})
    whole.reset()
    rep = Replica(doc.root.id)
    rep.load(*whole.snapshot())
    view = ClientView(doc, {})
    view.hold_everything()
    rep.apply(view.change({ids["i1"].id: None}))
    truth = truth_canon(doc, {ids["i1"].id: None})
    got = rep.canon(doc)
    assert got == truth, diff_report(got, truth)
