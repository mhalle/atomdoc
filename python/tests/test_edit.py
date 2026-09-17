"""atomdoc.editing: the model-facing read and edit operations."""

from __future__ import annotations

import asyncio
from typing import Literal

import pytest
from pydantic import BaseModel, model_validator

from atomdoc import Array, Doc, MemoryStore, Ref, node
from atomdoc.editing import DocumentEditor, EditError, apply_edits, describe_schema, read_view

pytest.importorskip("jsonpath_rfc9535")


class Estimate(BaseModel, frozen=True):
    days: float = 1.0
    confidence: Literal["low", "high"] = "high"


@node
class Person(BaseModel):
    name: str = ""


@node
class Task(BaseModel):
    title: str = ""
    status: Literal["todo", "in_progress", "done"] = "todo"
    assignee: Ref[Person] | None = None
    reviewers: list[Ref[Person]] = []
    estimate: Estimate | None = None
    tags: list[str] = []

    @model_validator(mode="after")
    def done_needs_an_estimate(self):
        if self.status == "done" and self.estimate is None:
            raise ValueError("a done task needs an estimate")
        return self


@node
class Milestone(BaseModel):
    name: str = ""
    tasks: Array[Task] = []


@node
class Project(BaseModel):
    people: Array[Person] = []
    milestones: Array[Milestone] = []


def build() -> Doc:
    d = Doc(Project())
    with d.transaction():
        alice, bob = d.create_node(Person, name="Alice"), d.create_node(Person, name="Bob")
        d.root.people.append(alice)
        d.root.people.append(bob)
        for name, tasks in {"setup": ["repo", "ci"], "launch": ["docs", "deploy", "announce"]}.items():
            m = d.create_node(Milestone, name=name)
            d.root.milestones.append(m)
            for i, title in enumerate(tasks):
                t = d.create_node(Task, title=title, assignee=alice if i % 2 == 0 else bob)
                m.tasks.append(t)
    return d


@pytest.fixture
def doc():
    return build()


def edit(doc, *ops, **kw):
    return apply_edits(doc, list(ops), **kw)


def refused(doc, *ops, **kw) -> EditError:
    before = doc.dump()
    with pytest.raises(EditError) as caught:
        apply_edits(doc, list(ops), **kw)
    assert doc.dump() == before, "a refused edit must change nothing"
    return caught.value


def task(doc, title):
    return doc.select(f"$..tasks[?@.title == '{title}']")[0]


DEPLOY = "$..tasks[?@.title == 'deploy']"


# ── read ──────────────────────────────────────────────────────────────────────

def test_read_a_node_with_children_to_a_depth(doc):
    view = read_view(doc, "$.milestones[?@.name == 'launch']", depth=0)
    [launch] = view["results"]
    assert launch["$type"] == "Milestone" and launch["name"] == "launch"
    assert [c["$label"] for c in launch["tasks"]] == [
        f'Task {task(doc, t).id} "{t}"' for t in ("docs", "deploy", "announce")]
    deep = read_view(doc, "$.milestones[?@.name == 'launch']", depth=1)["results"][0]
    assert deep["tasks"][1]["status"] == "todo"                      # defaults are shown


def test_read_labels_what_references_name(doc):
    [deploy] = read_view(doc, DEPLOY)["results"]
    bob = doc.root.people[1]
    assert deploy["assignee"] == bob.id and deploy["$refs"][bob.id] == f'Person {bob.id} "Bob"'


def test_read_a_value_and_inside_one(doc):
    edit(doc, {"op": "set", "path": f"{DEPLOY}.estimate", "value": {"days": 2}})
    [whole] = read_view(doc, f"{DEPLOY}.estimate")["results"]
    [part] = read_view(doc, f"{DEPLOY}.estimate.days")["results"]
    assert whole["value"] == {"days": 2.0, "confidence": "high"} and whole["settable"]
    assert part["value"] == 2.0 and part["settable"] is False


def test_read_from_a_node_and_a_limit(doc):
    launch = doc.select("$.milestones[?@.name == 'launch']")[0]
    view = read_view(doc, "$.tasks[*].title", node=launch.id, limit=2)
    assert view["matched"] == 3 and len(view["results"]) == 2 and "truncated" in view


def test_read_a_bad_path(doc):
    with pytest.raises(EditError) as caught:
        read_view(doc, "$.milestones[")
    assert caught.value.code == "bad_path"


# ── set ───────────────────────────────────────────────────────────────────────

def test_set_one_field(doc):
    result = edit(doc, {"op": "set", "path": f"{DEPLOY}.title", "value": "ship"})
    assert result["changed"] and task(doc, "ship")
    assert result["changes"] == [f'set title of Task {task(doc, "ship").id} "deploy": "deploy" → "ship"']


def test_a_write_expects_one_place_unless_told(doc):
    err = refused(doc, {"op": "set", "path": "$..tasks[*].status", "value": "in_progress"})
    assert (err.code, err.details["matched"], err.op) == ("count_mismatch", 5, 0)
    assert "expect=5" in str(err)
    edit(doc, {"op": "set", "path": "$..tasks[*].status", "value": "in_progress", "expect": 5})
    assert {t.status for t in doc.select("$..tasks[*]")} == {"in_progress"}
    assert refused(doc, {"op": "set", "path": "$..tasks[*].status", "value": "todo",
                         "expect": 4}).code == "count_mismatch"


def test_expect_all_accepts_none(doc):
    result = edit(doc, {"op": "set", "path": "$..tasks[?@.title == 'nope'].status",
                        "value": "done", "expect": "all"})
    assert result["changed"] is False
    assert refused(doc, {"op": "set", "path": "$..tasks[?@.title == 'nope'].status",
                         "value": "done"}).code == "no_match"


def test_a_frozen_value_is_set_whole_never_in_part(doc):
    edit(doc, {"op": "set", "path": f"{DEPLOY}.estimate", "value": {"days": 2, "confidence": "low"}})
    assert task(doc, "deploy").estimate == Estimate(days=2, confidence="low")
    err = refused(doc, {"op": "set", "path": f"{DEPLOY}.estimate.days", "value": 5})
    assert err.code == "not_settable" and "atomic" in str(err)
    assert err.details["current"] == {"days": 2.0, "confidence": "low"}    # what to resubmit


def test_inside_a_list_is_refused_with_a_way_forward(doc):
    edit(doc, {"op": "set", "path": f"{DEPLOY}.tags", "value": ["a", "b"]})
    err = refused(doc, {"op": "set", "path": f"{DEPLOY}.tags[1]", "value": "c"})
    assert err.code == "not_settable" and "add/remove" in str(err)


@pytest.mark.parametrize("path,code", [
    ("$.milestones[0].tasks", "not_a_field"),
    (DEPLOY, "not_a_field"),
    (f"{DEPLOY}['$id']", "read_only"),
    (f"{DEPLOY}['$type']", "read_only"),
])
def test_what_set_cannot_write(doc, path, code):
    assert refused(doc, {"op": "set", "path": path, "value": "x"}).code == code


def test_a_value_the_type_refuses(doc):
    err = refused(doc, {"op": "set", "path": f"{DEPLOY}.status", "value": "urgent"})
    assert err.code == "invalid_value" and "status of Task" in str(err)
    assert "errors.pydantic.dev" not in str(err) and "urgent" not in str(err)   # no noise, no echo


def test_references_by_id_by_select_and_by_name(doc):
    bob, alice = doc.root.people[1], doc.root.people[0]
    edit(doc, {"op": "set", "path": f"{DEPLOY}.assignee", "value": alice.id})
    assert task(doc, "deploy").assignee is alice
    edit(doc, {"op": "set", "path": f"{DEPLOY}.assignee", "value": {"select": "$.people[?@.name == 'Bob']"}})
    assert task(doc, "deploy").assignee is bob
    err = refused(doc, {"op": "set", "path": f"{DEPLOY}.assignee", "value": {"select": "$.people[*]"}})
    assert err.code == "count_mismatch"
    assert refused(doc, {"op": "set", "path": f"{DEPLOY}.assignee", "value": "-nope.0"}).code == "no_node"
    edit(doc,
         {"op": "insert", "path": "$.people", "value": {"$type": "Person", "name": "Eve", "$as": "eve"}},
         {"op": "set", "path": f"{DEPLOY}.assignee", "value": "@eve"})
    assert task(doc, "deploy").assignee.name == "Eve"


def test_if_current(doc):
    err = refused(doc, {"op": "set", "path": f"{DEPLOY}.status", "value": "in_progress",
                        "if_current": "in_progress"})
    assert err.code == "stale" and err.details["current"] == "todo"
    edit(doc, {"op": "set", "path": f"{DEPLOY}.status", "value": "in_progress", "if_current": "todo"})
    edit(doc, {"op": "set", "path": f"{DEPLOY}.estimate", "value": {"days": 1}, "if_current": None})
    assert task(doc, "deploy").estimate == Estimate(days=1)


# ── add / remove ──────────────────────────────────────────────────────────────

def test_add_and_remove_list_elements(doc):
    edit(doc, {"op": "add", "path": f"{DEPLOY}.tags", "value": "infra"},
         {"op": "add", "path": f"{DEPLOY}.tags", "value": "urgent"})
    edit(doc, {"op": "remove", "path": f"{DEPLOY}.tags", "value": "infra"})
    assert task(doc, "deploy").tags == ["urgent"]
    assert refused(doc, {"op": "remove", "path": f"{DEPLOY}.tags", "value": "nope"}).code == "not_present"
    assert refused(doc, {"op": "add", "path": f"{DEPLOY}.title", "value": "x"}).code == "not_a_list"


def test_add_and_remove_references(doc):
    alice, bob = doc.root.people
    edit(doc, {"op": "add", "path": f"{DEPLOY}.reviewers", "value": {"select": "$.people[?@.name == 'Alice']"}},
         {"op": "add", "path": f"{DEPLOY}.reviewers", "value": bob.id})
    assert task(doc, "deploy").reviewers == [alice, bob]
    edit(doc, {"op": "remove", "path": f"{DEPLOY}.reviewers", "value": alice.id})
    assert task(doc, "deploy").reviewers == [bob]


# ── insert ────────────────────────────────────────────────────────────────────

def test_insert_with_children_and_names(doc):
    result = edit(doc, {"op": "insert", "path": "$.milestones", "value": {
        "$type": "Milestone", "name": "beta", "$as": "beta",
        "tasks": [{"$type": "Task", "title": "smoke", "$as": "smoke"}, {"$type": "Task", "title": "soak"}]}})
    beta = doc.select("$.milestones[?@.name == 'beta']")[0]
    assert [t.title for t in beta.tasks] == ["smoke", "soak"]
    assert result["created"] == {"@beta": beta.id, "@smoke": beta.tasks[0].id}
    assert result["counts"]["inserted"] == 3


def test_insert_next_to_a_sibling(doc):
    edit(doc, {"op": "insert", "path": "$.milestones[?@.name == 'launch'].tasks",
               "value": {"$type": "Task", "title": "smoke"}, "after": DEPLOY})
    edit(doc, {"op": "insert", "path": "$.milestones[?@.name == 'launch'].tasks",
               "value": {"$type": "Task", "title": "first"}, "position": "prepend"})
    launch = doc.select("$.milestones[?@.name == 'launch']")[0]
    assert [t.title for t in launch.tasks] == ["first", "docs", "deploy", "smoke", "announce"]
    err = refused(doc, {"op": "insert", "path": "$.milestones[?@.name == 'launch'].tasks",
                        "value": {"$type": "Task"}, "before": "$..tasks[?@.title == 'repo']"})
    assert err.code == "bad_request" and "is not in" in str(err)


@pytest.mark.parametrize("value,code", [
    ({"$type": "Nope"}, "unknown_type"),
    ({"$type": "Task", "colour": "red"}, "unknown_field"),
    ({"$type": "Person", "name": "Eve"}, "invalid"),                  # the slot holds Tasks
    ({"$type": "Task", "status": "urgent"}, "invalid_value"),
    ({"title": "no type"}, "bad_value"),
])
def test_what_insert_refuses(doc, value, code):
    err = refused(doc, {"op": "insert", "path": "$.milestones[0].tasks", "value": value})
    assert err.code == code


def test_insert_needs_a_slot(doc):
    assert refused(doc, {"op": "insert", "path": "$.milestones[0]",
                         "value": {"$type": "Task"}}).code == "not_a_slot"


# ── move ──────────────────────────────────────────────────────────────────────

def test_move_several_keeps_their_order(doc):
    setup = doc.select("$.milestones[?@.name == 'setup']")[0]
    edit(doc, {"op": "move", "path": "$.milestones[?@.name == 'launch'].tasks[?@.title != 'deploy']",
               "expect": 2, "to": "$.milestones[?@.name == 'setup'].tasks", "position": "prepend"})
    assert [t.title for t in setup.tasks] == ["docs", "announce", "repo", "ci"]
    edit(doc, {"op": "move", "path": "$..tasks[?@.title == 'docs' || @.title == 'announce']",
               "expect": 2, "after": "$..tasks[?@.title == 'ci']"})
    assert [t.title for t in setup.tasks] == ["repo", "ci", "docs", "announce"]
    ids = {t.title: t.id for t in setup.tasks}
    edit(doc, {"op": "move", "path": "$..tasks[?@.title == 'announce']", "before": {"node": ids["repo"]}})
    assert [t.title for t in setup.tasks] == ["announce", "repo", "ci", "docs"]
    assert {t.title: t.id for t in setup.tasks} == ids                  # a move keeps IDs


def test_move_needs_one_destination(doc):
    assert refused(doc, {"op": "move", "path": DEPLOY}).code == "bad_request"
    assert refused(doc, {"op": "move", "path": DEPLOY, "to": "$.milestones[0].tasks",
                         "after": "$..tasks[0]"}).code == "bad_request"


def test_a_node_cannot_move_into_itself(doc):
    err = refused(doc, {"op": "move", "path": "$.milestones[0]", "to": "$.milestones[0].tasks[0]"})
    assert err.code in ("not_a_slot", "invalid")


# ── delete ────────────────────────────────────────────────────────────────────

def test_delete(doc):
    edit(doc, {"op": "delete", "path": "$..tasks[?@.title == 'ci' || @.title == 'docs']", "expect": 2})
    assert [t.title for t in doc.select("$..tasks[*]")] == ["repo", "deploy", "announce"]


def test_delete_refuses_what_is_still_referenced(doc):
    err = refused(doc, {"op": "delete", "path": "$.people[?@.name == 'Bob']"})
    assert err.code == "still_referenced"
    assert len(err.details["references"]) == 2 and "assignee of Task" in err.details["references"][0]
    edit(doc,
         {"op": "set", "path": "$..tasks[?deref(@.assignee, 'name') == 'Bob'].assignee",
          "value": None, "expect": "all"},
         {"op": "delete", "path": "$.people[?@.name == 'Bob']"})
    assert [p.name for p in doc.root.people] == ["Alice"]


def test_delete_a_node_and_something_under_it(doc):
    edit(doc, {"op": "delete", "path": "$..[?@.name == 'setup' || @.title == 'repo']", "expect": 2})
    assert [m.name for m in doc.root.milestones] == ["launch"]


# ── the batch ─────────────────────────────────────────────────────────────────

def test_all_or_nothing(doc):
    err = refused(doc,
                  {"op": "set", "path": f"{DEPLOY}.estimate", "value": {"days": 3}},
                  {"op": "add", "path": f"{DEPLOY}.tags", "value": "x"},
                  {"op": "set", "path": f"{DEPLOY}.status", "value": "urgent"})
    assert err.op == 2 and task(doc, "deploy").tags == [] and task(doc, "deploy").estimate is None


def test_later_operations_see_earlier_ones(doc):
    edit(doc, {"op": "set", "path": f"{DEPLOY}.title", "value": "ship"},
         {"op": "add", "path": "$..tasks[?@.title == 'ship'].tags", "value": "renamed"})
    assert task(doc, "ship").tags == ["renamed"]


def test_the_documents_own_rules_name_the_node_and_the_op(doc):
    err = refused(doc,
                  {"op": "set", "path": f"{DEPLOY}.title", "value": "ship"},
                  {"op": "set", "path": "$..tasks[?@.title == 'ship'].status", "value": "done"})
    assert err.code == "invalid_document" and "needs an estimate" in str(err)
    [failure] = err.details["failures"]
    assert failure["ops"] == [0, 1] and '"ship"' in failure["node"]
    edit(doc, {"op": "set", "path": f"{DEPLOY}.estimate", "value": {"days": 1}},
         {"op": "set", "path": f"{DEPLOY}.status", "value": "done"})       # in either order


def test_dry_run_checks_everything_and_keeps_nothing(doc):
    before = doc.dump()
    result = edit(doc, {"op": "delete", "path": DEPLOY}, dry_run=True)
    assert result["dry_run"] and not result["changed"] and result["counts"]["deleted"] == 1
    assert doc.dump() == before
    assert refused(doc, {"op": "set", "path": f"{DEPLOY}.status", "value": "done"},
                   dry_run=True).code == "invalid_document"


@pytest.mark.parametrize("ops", [[], "set", [{"op": "rename", "path": "$"}],
                                 [{"op": "set", "path": "$", "value": 1, "colour": 2}]])
def test_malformed_requests(doc, ops):
    with pytest.raises(EditError) as caught:
        apply_edits(doc, ops)                                            # type: ignore[arg-type]
    assert caught.value.code == "bad_request"


def test_error_as_data(doc):
    err = refused(doc, {"op": "set", "path": "$..tasks[*].status", "value": "done"})
    data = err.to_dict()
    assert data["ok"] is False and data["changed"] is False
    assert data["error"]["code"] == "count_mismatch" and data["error"]["op"] == 0
    assert len(data["error"]["matches"]) == 5


def test_schema_for_a_model(doc):
    schema = describe_schema(doc)
    task_type = schema["node_types"]["Task"]
    assert schema["root_type"] == "Project"
    assert task_type["fields"]["estimate"]["tier"] == "atomic"
    assert task_type["fields"]["reviewers"]["references"] == "Person[]"
    assert schema["node_types"]["Milestone"]["slots"] == {"tasks": ["Task"]}
    assert "title" not in str(task_type["fields"]["title"]["schema"].keys())


# ── over a store ──────────────────────────────────────────────────────────────

def run(coro):
    return asyncio.run(coro)


def test_a_stored_document_round_trip():
    async def go():
        editor = DocumentEditor(MemoryStore(), Project)
        v1 = await editor.create("plan", build())
        read = await editor.read("plan", DEPLOY)
        assert read["version"] == v1
        result = await editor.edit("plan", [{"op": "set", "path": f"{DEPLOY}.status",
                                             "value": "in_progress"}], version=v1)
        assert result["version"] != v1
        again = await editor.read("plan", f"{DEPLOY}.status")
        assert again["results"][0]["value"] == "in_progress" and again["version"] == result["version"]
    run(go())


def test_a_stale_version_is_refused_and_nothing_saved():
    async def go():
        editor = DocumentEditor(MemoryStore(), Project)
        v1 = await editor.create("plan", build())
        await editor.edit("plan", [{"op": "set", "path": f"{DEPLOY}.title", "value": "ship"}])
        with pytest.raises(EditError) as caught:
            await editor.edit("plan", [{"op": "delete", "path": "$..tasks[?@.title == 'ship']"}],
                              version=v1)
        assert caught.value.code == "conflict"
        assert (await editor.read("plan", "$..tasks[?@.title == 'ship']"))["matched"] == 1
    run(go())


def test_a_save_that_loses_a_race_is_reported_not_retried():
    class Racing(MemoryStore):
        """Another writer saves between this edit's read and its write."""
        async def _put(self, key, data, if_match, ttl):
            if getattr(self, "race", False):
                self.race = False
                await super()._put(key, data, None, None)      # same bytes, a new version
            return await super()._put(key, data, if_match, ttl)

    async def go():
        store = Racing()
        editor = DocumentEditor(store, Project)
        await editor.create("plan", build())
        store.race = True
        with pytest.raises(EditError) as caught:
            await editor.edit("plan", [{"op": "set", "path": f"{DEPLOY}.title", "value": "ship"}])
        assert caught.value.code == "conflict"
    run(go())


def test_what_is_not_saved():
    async def go():
        store = MemoryStore()
        editor = DocumentEditor(store, Project)
        v1 = await editor.create("plan", build())
        dry = await editor.edit("plan", [{"op": "delete", "path": DEPLOY}], dry_run=True)
        same = await editor.edit("plan", [{"op": "set", "path": f"{DEPLOY}.title", "value": "deploy"}])
        assert dry["version"] == v1 and same["version"] == v1              # no write happened
        for doc_id, code in (("missing", "not_found"), ("../escape", "bad_document_id")):
            with pytest.raises(EditError) as caught:
                await editor.read(doc_id)
            assert caught.value.code == code
        with pytest.raises(EditError) as caught:
            await editor.create("plan", build())
        assert caught.value.code == "exists"
    run(go())
