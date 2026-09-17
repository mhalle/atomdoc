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


def test_without_the_extra(doc, monkeypatch):
    from atomdoc import _select
    _select._environment.cache_clear()
    _select._compile.cache_clear()
    monkeypatch.setitem(sys.modules, "jsonpath_rfc9535", None)
    try:
        with pytest.raises(ImportError, match=r"atomdoc\[query\]"):
            doc.select("$")
    finally:
        _select._environment.cache_clear()
        _select._compile.cache_clear()
