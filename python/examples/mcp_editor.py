"""A model edits a document through MCP.

A project plan — milestones, tasks, people — kept in a FileStore, and four
tools over it: three generic ones any atomdoc document can have, and one
written for this schema to show how the two layers fit.

    document_schema   what the document's types are
    read_document     what a JSONPath finds, with node IDs
    edit_document     a batch of set/add/remove/insert/move/delete, all or nothing
    complete_task     the domain tool: one intent, built from the same operations

Run it (micromcp is installed from GitHub; uvicorn serves ASGI):

    uv run --with "micromcp @ git+https://github.com/mhalle/micromcp@v0.2.0" \\
        --with uvicorn uvicorn examples.mcp_editor:app

and add http://127.0.0.1:8000/mcp as a connector. The first read of "demo"
creates a sample plan. Documents are kept under ~/.atomdoc-editor, or
$ATOMDOC_EDITOR_DIR.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from micromcp import MCP, ASGIServer, result
from pydantic import BaseModel, model_validator

from atomdoc import Array, Doc, FileStore, Ref, node
from atomdoc.editing import TOOL_DESCRIPTIONS, DocumentEditor, EditError, EditOp

# ── the document ──────────────────────────────────────────────────────────────


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
    name: str = ""
    people: Array[Person] = []
    milestones: Array[Milestone] = []


def sample() -> Doc:
    doc = Doc(Project(name="Atlas launch"))
    with doc.transaction():
        alice, bob = doc.create_node(Person, name="Alice"), doc.create_node(Person, name="Bob")
        doc.root.people.append(alice)
        doc.root.people.append(bob)
        plan = {"setup": [("repo", alice), ("ci", bob)],
                "launch": [("docs", alice), ("deploy", bob), ("announce", alice)]}
        for name, tasks in plan.items():
            milestone = doc.create_node(Milestone, name=name)
            doc.root.milestones.append(milestone)
            for title, who in tasks:
                milestone.tasks.append(doc.create_node(Task, title=title, assignee=who))
    return doc


# ── the server ────────────────────────────────────────────────────────────────

root = Path(os.environ.get("ATOMDOC_EDITOR_DIR", Path.home() / ".atomdoc-editor"))
editor = DocumentEditor(FileStore(root), Project)
mcp = MCP("atomdoc-editor", "0.1.0")


def refused(error: EditError):
    """An EditError as an in-band tool error: the model reads what to change."""
    data = error.to_dict()
    return result([{"type": "text", "text": json.dumps(data)}], structured=data, is_error=True)


async def ensure(document: str) -> None:
    if document == "demo":
        try:
            await editor.create("demo", sample())
        except EditError as exc:
            if exc.code != "exists":
                raise


async def document_schema(document: str) -> dict:
    try:
        await ensure(document)
        return await editor.schema(document)
    except EditError as exc:
        return refused(exc)


async def read_document(document: str, path: str = "$", node: str | None = None,
                        depth: int = 1) -> dict:
    try:
        await ensure(document)
        return await editor.read(document, path, node=node, depth=depth)
    except EditError as exc:
        return refused(exc)


async def edit_document(document: str, ops: list[EditOp], dry_run: bool = False,
                        version: str | None = None) -> dict:
    try:
        await ensure(document)
        return await editor.edit(document, ops, dry_run=dry_run, version=version)
    except EditError as exc:
        return refused(exc)


document_schema.__doc__ = TOOL_DESCRIPTIONS["schema"]
read_document.__doc__ = TOOL_DESCRIPTIONS["read"]
edit_document.__doc__ = TOOL_DESCRIPTIONS["edit"]
mcp.tool(read_only=True, idempotent=True)(document_schema)
mcp.tool(read_only=True, idempotent=True)(read_document)
mcp.tool(destructive=True)(edit_document)


@mcp.tool
async def complete_task(document: str, task: str, days: float | None = None) -> dict:
    """Mark a task done. A done task needs an estimate: give `days` if it has
    none. `task` is the task's $id (from read_document)."""
    at = {"node": task, "path": "$"}
    ops: list[dict] = []
    if days is not None:
        ops.append({"op": "set", **at, "path": "$.estimate", "value": {"days": days}})
    ops.append({"op": "set", **at, "path": "$.status", "value": "done"})
    try:
        await ensure(document)
        return await editor.edit(document, ops)
    except EditError as exc:
        return refused(exc)


app = ASGIServer(mcp)
