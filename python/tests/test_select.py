"""Doc.select: JSONPath (RFC 9535) queries that return nodes."""

from __future__ import annotations

import sys
from typing import Literal

import pytest
from pydantic import BaseModel

from atomdoc import Array, Doc, Ref, node

pytest.importorskip("jsonpath_rfc9535")


class Estimate(BaseModel, frozen=True):
    days: float = 1.0


@node
class Person:
    name: str = ""


@node
class Task:
    title: str = ""
    status: Literal["todo", "in_progress", "done"] = "todo"
    assignee: Ref[Person] | None = None
    reviewers: list[Ref[Person]] = []
    estimate: Estimate | None = None
    tags: list[str] = []


@node
class Milestone:
    name: str = ""
    tasks: Array[Task] = []


@node
class Project:
    people: Array[Person] = []
    milestones: Array[Milestone] = []


@pytest.fixture
def doc():
    d = Doc(Project())
    with d.transaction():
        alice = d.create_node(Person, name="Alice")
        bob = d.create_node(Person, name="Bob")
        d.root.people.append(alice)
        d.root.people.append(bob)
        plan = {"setup": [("repo", "done", alice), ("ci", None, bob)],
                "launch": [("docs", "done", alice), ("deploy", "in_progress", bob),
                           ("announce", None, alice)]}
        for name, tasks in plan.items():
            m = d.create_node(Milestone, name=name)
            d.root.milestones.append(m)
            for title, status, who in tasks:
                fields = {"title": title} if status is None else {"title": title, "status": status}
                t = d.create_node(Task, **fields)             # None: status left at its default
                m.tasks.append(t)
                t.assignee = who
    return d


def titles(nodes):
    return [n.title for n in nodes]


def test_a_filter_on_a_path(doc):
    q = "$.milestones[?@.name == 'launch'].tasks[?@.status != 'done']"
    assert titles(doc.select(q)) == ["deploy", "announce"]


def test_a_default_is_matched_though_nobody_set_it(doc):
    assert titles(doc.select("$..tasks[?@.status == 'todo']")) == ["ci", "announce"]


def test_by_type_anywhere(doc):
    assert [n.name for n in doc.select("$..[?@['$type'] == 'Person']")] == ["Alice", "Bob"]
    assert len(doc.select("$..[?@['$type'] == 'Task']")) == 5


def test_following_a_reference(doc):
    q = "$..tasks[?deref(@.assignee, 'name') == 'Alice']"
    assert titles(doc.select(q)) == ["repo", "docs", "announce"]


def test_following_one_of_many_references(doc):
    alice, bob = doc.root.people
    with doc.transaction():
        doc.select("$..tasks[?@.title == 'deploy']")[0].reviewers = [alice, bob]
    assert titles(doc.select("$..tasks[?deref(@.reviewers[1], 'name') == 'Bob']")) == ["deploy"]


def test_a_reference_survives_its_target_moving(doc):
    alice = doc.root.people[0]
    with doc.transaction():
        alice.move(doc.root.people[1], position="after")    # Alice is now second
    assert titles(doc.select("$..tasks[?deref(@.assignee, 'name') == 'Alice']")) == \
        ["repo", "docs", "announce"]


def test_nodes_in_document_order_each_once(doc):
    everything = doc.select("$..tasks[*]")
    assert titles(everything) == ["repo", "ci", "docs", "deploy", "announce"]
    twice = doc.select("$.milestones[0, 0, 1].tasks[0]")     # a union may name one node twice
    assert titles(twice) == ["repo", "docs"]


def test_the_root(doc):
    assert doc.select("$") == [doc.root]


def test_nothing_matches(doc):
    assert doc.select("$.milestones[?@.name == 'nope']") == []


def test_a_value_is_not_a_node(doc):
    with pytest.raises(TypeError, match="a value rather than a node"):
        doc.select("$..title")


def test_a_frozen_value_is_not_a_node(doc):
    with doc.transaction():
        doc.select("$..tasks[0]")[0].estimate = Estimate(days=2)
    with pytest.raises(TypeError):
        doc.select("$..estimate")


def test_a_malformed_query(doc):
    with pytest.raises(ValueError, match="JSONPath"):
        doc.select("$.milestones[")


def test_select_then_edit_in_one_transaction(doc):
    with doc.transaction():
        for task in doc.select("$.milestones[?@.name == 'launch'].tasks[*]"):
            task.status = "done"
        assert len(doc.select("$..tasks[?@.status == 'done']")) == 4   # sees its own edits
    assert titles(doc.select("$..tasks[?@.status != 'done']")) == ["ci"]


def test_the_selected_part_as_a_partial_view(doc):
    snapshot, detached = doc.dump_selected("$.milestones[?@.name == 'launch']", depth=1)
    full = doc.dump()
    assert snapshot[0] == full[0] and snapshot[1] == "Project"
    held = {}

    def walk(entry):
        held[entry[0]] = entry[2]
        for children in (entry[3] if len(entry) > 3 else {}).values():
            for child in children:
                walk(child)
    walk(snapshot)
    launch = doc.select("$.milestones[?@.name == 'launch']")[0]
    setup = doc.select("$.milestones[?@.name == 'setup']")[0]
    assert held[launch.id] is not None                      # the selection, in full
    assert all(held[t.id] is not None for t in launch.tasks) # and its children, to depth 1
    assert held[doc.root.id] is None                        # its ancestor, a stub
    assert setup.id not in held or held[setup.id] is None   # outside it: absent or a stub
    assert len(str(snapshot)) < len(str(full))


def test_a_partial_view_of_nothing(doc):
    snapshot, detached = doc.dump_selected("$.milestones[?@.name == 'nope']")
    assert detached == []


def test_queries_are_independent(doc):
    q = "$..tasks[?deref(@.assignee, 'name') == 'Bob']"
    other = Doc(Project())
    assert other.select(q) == []                             # another document's view never leaks
    assert titles(doc.select(q)) == ["ci", "deploy"]


def test_without_jsonpath_installed(doc, monkeypatch):
    from atomdoc import _select
    _select._environment.cache_clear()
    _select._compile.cache_clear()
    monkeypatch.setitem(sys.modules, "jsonpath_rfc9535", None)
    try:
        with pytest.raises(ImportError, match=r"jsonpath-rfc9535"):
            doc.select("$")
    finally:
        _select._environment.cache_clear()
        _select._compile.cache_clear()


# ── node=: a query that starts below the root ─────────────────────────────────

def milestone(doc, name):
    return doc.select(f"$.milestones[?@.name == '{name}']")[0]


def task(doc, title):
    return doc.select(f"$..tasks[?@.title == '{title}']")[0]


def test_node_is_dollar(doc):
    launch = milestone(doc, "launch")
    assert titles(doc.select("$.tasks[*]", node=launch)) == ["docs", "deploy", "announce"]
    assert doc.select("$", node=launch) == [launch]


def test_node_by_id_or_instance(doc):
    launch = milestone(doc, "launch")
    assert doc.select("$.tasks[*]", node=launch.id) == doc.select("$.tasks[*]", node=launch)


def test_a_query_below_the_root_stays_below_it(doc):
    setup = milestone(doc, "setup")
    assert titles(doc.select("$..[?@['$type'] == 'Task']", node=setup)) == ["repo", "ci"]
    assert doc.select("$..[?@['$type'] == 'Person']", node=setup) == []


def test_references_are_followed_out_of_the_subtree(doc):
    launch = milestone(doc, "launch")          # Alice and Bob live under the root, not here
    q = "$.tasks[?deref(@.assignee, 'name') == 'Alice']"
    assert titles(doc.select(q, node=launch)) == ["docs", "announce"]


def test_an_unknown_id_never_widens_to_the_whole_document(doc):
    with pytest.raises(LookupError, match="no node"):
        doc.select("$..tasks[*]", node="-nope.0")
    with pytest.raises(LookupError):
        doc.dump("-nope.0")
    with pytest.raises(LookupError):
        doc.to_json("-nope.0")


def test_a_node_from_another_document(doc):
    other = Doc(Project())
    with pytest.raises(ValueError, match="another document"):
        doc.select("$", node=other.root)


def test_a_copy_of_the_document_shares_ids_but_not_nodes(doc):
    """A restored copy has the same IDs. Its node must not be taken for this
    document's node of the same ID: that would query the wrong document."""
    copy = Doc.restore(doc.dump(), root_type=Project)
    theirs = copy.root.milestones[0]
    assert doc.get_node_by_id(theirs.id) is not None           # the ID exists here too
    with pytest.raises(ValueError, match="another document"):
        doc.select("$", node=theirs)
    assert doc.select("$", node=theirs.id) == [doc.root.milestones[0]]   # an ID names ours


def test_a_deleted_node(doc):
    gone = task(doc, "ci")
    with doc.transaction():
        gone.delete()
    with pytest.raises(ValueError, match="not in this document's tree"):
        doc.select("$", node=gone)
    with pytest.raises(LookupError):
        doc.select("$", node=gone.id)


def test_a_node_not_yet_attached(doc):
    with doc.transaction():
        loose = doc.create_node(Task, title="loose")
        with pytest.raises(ValueError, match="not yet attached"):
            doc.select("$", node=loose)
        doc.root.milestones[0].tasks.append(loose)


def test_node_must_be_a_node_or_an_id(doc):
    with pytest.raises(TypeError, match="node="):
        doc.select("$", node=3)                                  # type: ignore[arg-type]


def test_dump_and_to_json_take_an_id_too(doc):
    deploy = task(doc, "deploy")
    assert doc.dump(deploy.id) == doc.dump(deploy)
    assert doc.to_json(deploy.id) == doc.to_json(deploy)
    assert doc.dump() == doc.dump(None) == doc.dump(doc.root.id)


def test_dump_selected_below_the_root(doc):
    launch = milestone(doc, "launch")
    snapshot, _ = doc.dump_selected("$.tasks[?@.title == 'deploy']", depth=0, node=launch)
    assert "deploy" in str(snapshot) and "repo" not in str(snapshot)


# ── locate: where a query lands, as the unit an edit acts on ──────────────────

def test_a_node(doc):
    [loc] = doc.locate("$..tasks[?@.title == 'deploy']")
    assert (loc.kind, loc.node, loc.settable) == ("node", task(doc, "deploy"), False)


def test_a_plain_field(doc):
    [loc] = doc.locate("$..tasks[?@.title == 'deploy'].status")
    assert (loc.kind, loc.field, loc.inner, loc.tier, loc.settable) == \
        ("field", "status", (), "mergeable", True)
    assert loc.node is task(doc, "deploy")


def test_a_frozen_value_whole(doc):
    with doc.transaction():
        task(doc, "deploy").estimate = Estimate(days=2)
    [loc] = doc.locate("$..tasks[?@.title == 'deploy'].estimate")
    assert (loc.field, loc.tier, loc.inner, loc.settable) == ("estimate", "atomic", (), True)


def test_inside_a_frozen_value_is_the_field_and_not_settable(doc):
    """The red channel of a Color: readable, located, never writable alone."""
    with doc.transaction():
        task(doc, "deploy").estimate = Estimate(days=2)
    [loc] = doc.locate("$..tasks[?@.title == 'deploy'].estimate.days")
    assert (loc.kind, loc.field, loc.inner, loc.tier) == ("field", "estimate", ("days",), "atomic")
    assert loc.settable is False
    assert str(loc).endswith("estimate.days (atomic)")


def test_inside_an_absent_value_matches_nothing(doc):
    assert doc.locate("$..tasks[?@.title == 'deploy'].estimate.days") == []   # estimate is None


def test_inside_a_list_is_the_field_and_not_settable(doc):
    with doc.transaction():
        task(doc, "deploy").tags = ["infra", "urgent"]
    [loc] = doc.locate("$..tasks[?@.title == 'deploy'].tags[1]")
    assert (loc.field, loc.inner, loc.tier, loc.settable) == ("tags", (1,), "mergeable", False)
    [whole] = doc.locate("$..tasks[?@.title == 'deploy'].tags")
    assert whole.settable


def test_references(doc):
    alice, bob = doc.root.people
    with doc.transaction():
        task(doc, "deploy").reviewers = [alice, bob]
    [one] = doc.locate("$..tasks[?@.title == 'deploy'].assignee")
    assert (one.field, one.tier, one.settable) == ("assignee", "ref", True)
    [member] = doc.locate("$..tasks[?@.title == 'deploy'].reviewers[1]")
    assert (member.field, member.inner, member.tier, member.settable) == \
        ("reviewers", (1,), "ref", False)


def test_a_child_slot(doc):
    [loc] = doc.locate("$.milestones[?@.name == 'launch'].tasks")
    assert (loc.kind, loc.slot, loc.field, loc.settable) == ("slot", "tasks", None, False)
    assert loc.node is milestone(doc, "launch")
    assert str(loc).endswith("slot tasks")


def test_id_and_type_are_never_settable(doc):
    for key in ("$id", "$type"):
        [loc] = doc.locate(f"$..tasks[?@.title == 'deploy']['{key}']")
        assert (loc.field, loc.tier, loc.settable) == (key, None, False)


def test_many_in_document_order_each_once(doc):
    locs = doc.locate("$..tasks[*].status")
    assert [loc.node.title for loc in locs] == ["repo", "ci", "docs", "deploy", "announce"]
    assert len(doc.locate("$.milestones[0, 0].name")) == 1


def test_locate_below_a_node(doc):
    deploy = task(doc, "deploy")
    [loc] = doc.locate("$.status", node=deploy.id)
    assert (loc.node, loc.field) == (deploy, "status")


def test_locate_and_select_agree_on_nodes(doc):
    q = "$..tasks[?@.status != 'done']"
    assert [loc.node for loc in doc.locate(q)] == doc.select(q)
    assert all(loc.kind == "node" for loc in doc.locate(q))
