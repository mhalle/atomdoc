"""Reading and editing a document on behalf of a model.

Three operations, with no transport in them — an MCP server, an HTTP API or a
test calls the same functions:

* ``read_view(doc, path)`` — what a JSONPath query finds, compact and with node
  IDs, so what was read can be addressed again;
* ``apply_edits(doc, ops)`` — a batch of ``set`` / ``add`` / ``remove`` /
  ``insert`` / ``move`` / ``delete`` in one transaction: all of it lands or
  none of it does;
* ``describe_schema(doc)`` — the node and value types, sized for a context.

``DocumentEditor`` does the same over a ``DocumentStore``, loading and saving
per call.

The rules a model is held to, each of which exists because the alternative
failed somewhere:

* a write lands on a node, a whole field, or a child slot — never inside a
  frozen value (a ``Color``'s red channel) or inside a list;
* a write affects exactly one place unless it says otherwise (``expect``), so
  a filter slightly too broad is refused rather than obeyed;
* ``if_current`` states what a write expects to find, and ``version`` what
  document it read, so an edit decided on content that has since changed is
  refused rather than re-applied;
* every value is validated by the document's own types, and nothing is
  trusted because it is already the right class.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from ._ref import RefIntegrityError

if TYPE_CHECKING:
    from ._doc import Doc
    from ._node import AtomNode
    from ._select import Location
    from ._store import DocumentStore

__all__ = [
    "AddOp", "DeleteOp", "DocumentEditor", "EditError", "EditOp", "InsertOp", "MoveOp",
    "RemoveOp", "SetOp", "Target", "TOOL_DESCRIPTIONS", "apply_edits", "describe_schema",
    "read_view",
]

_META = ("$id", "$type")


# ── errors ────────────────────────────────────────────────────────────────────


class EditError(Exception):
    """An edit or read refused, in terms a model can act on. ``code`` is stable
    for programs; ``message`` is written for the model and says what to do;
    ``details`` carries what it needs to do it (the matches, the current
    value). Nothing was changed when this is raised."""

    def __init__(self, code: str, message: str, *, op: int | None = None, **details: Any):
        super().__init__(message)
        self.code, self.message, self.op, self.details = code, message, op, details

    def __str__(self) -> str:
        where = f"edit[{self.op}]: " if self.op is not None else ""
        return f"{where}{self.message}"

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.op is not None:
            error["op"] = self.op
        error.update(self.details)
        return {"ok": False, "error": error, "changed": False}


def _validation_message(exc: ValidationError) -> str:
    """Pydantic's error, without its documentation URLs or the rejected input
    echoed back."""
    parts = []
    for err in exc.errors(include_url=False, include_input=False, include_context=False):
        loc = ".".join(str(x) for x in err["loc"])
        msg = str(err["msg"]).removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


# ── the operations, as data ───────────────────────────────────────────────────

# path, node, expect and targets are explained once, in TOOL_DESCRIPTIONS["edit"]:
# described per field they were repeated in every operation, 2 KB of schema.
_Expect = Annotated[int, Field(ge=0)] | Literal["all"]


class Target(BaseModel):
    """A place named from a node rather than from the root."""

    model_config = ConfigDict(extra="forbid")
    node: str
    path: str = "$"


_TargetSpec = str | Target


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    node: str | None = None


class SetOp(_Op):
    """Write a whole field. A path inside a frozen value or a list is refused:
    set the whole value."""

    op: Literal["set"]
    value: JsonValue = Field(description="the new value; for a reference, a node ID, "
                                         "@name, or {\"select\": path} matching one node")
    expect: _Expect | None = None
    if_current: JsonValue = Field(None, description="refuse unless the field currently holds this "
                                                    "value (omit to skip the check)")


class AddOp(_Op):
    """Append one element to a list field (a list of references included)."""

    op: Literal["add"]
    value: JsonValue
    expect: _Expect | None = None


class RemoveOp(_Op):
    """Remove the first element equal to ``value`` from a list field."""

    op: Literal["remove"]
    value: JsonValue
    expect: _Expect | None = None


class InsertOp(_Op):
    """Create a node, with any children, in a child slot. ``path`` names the
    slot (``$.milestones[0].tasks``); ``value`` is ``{"$type": ..., fields...,
    slot: [child, ...]}``, and ``"$as": name`` lets later operations in the same
    edit refer to a created node as ``@name``."""

    op: Literal["insert"]
    value: dict[str, JsonValue]
    position: Literal["append", "prepend"] = "append"
    before: _TargetSpec | None = None
    after: _TargetSpec | None = None


class MoveOp(_Op):
    """Move nodes to a child slot (``to``) or next to a sibling (``before`` /
    ``after``). IDs are unchanged."""

    op: Literal["move"]
    expect: _Expect | None = None
    to: _TargetSpec | None = None
    position: Literal["append", "prepend"] = "append"
    before: _TargetSpec | None = None
    after: _TargetSpec | None = None


class DeleteOp(_Op):
    """Delete nodes and everything under them. A node something still refers
    to is refused."""

    op: Literal["delete"]
    expect: _Expect | None = None


EditOp = Annotated[Union[SetOp, AddOp, RemoveOp, InsertOp, MoveOp, DeleteOp],
                   Field(discriminator="op")]
_OPS: TypeAdapter[list[Any]] = TypeAdapter(list[EditOp])


# ── resolving paths to places ─────────────────────────────────────────────────


def _label(node: AtomNode) -> str:
    for key in ("name", "title", "label"):
        value = node._state.get(key)
        if isinstance(value, str) and value:
            return f'{node._node_type} {node.id} "{value}"'
    return f"{node._node_type} {node.id}"


def _short(value: Any, limit: int = 80) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _plain(node: AtomNode, field: str) -> Any:
    return node._state_to_json_plain(include_defaults=True).get(field)


class _Batch:
    def __init__(self, doc: Doc) -> None:
        self.doc = doc
        self.aliases: dict[str, AtomNode] = {}
        self.changes: list[str] = []
        self.touched: dict[str, set[int]] = {}          # node ID -> ops that changed it

    def touch(self, node: AtomNode, op: int) -> None:
        self.touched.setdefault(node.id, set()).add(op)

    def node(self, ref: str) -> AtomNode:
        if not isinstance(ref, str) or not ref:
            raise EditError("bad_request", "a node is named by its ID or @name")
        if ref.startswith("@"):
            found = self.aliases.get(ref[1:])
            if found is None:
                raise EditError("unknown_name", f"no node was created as {ref} earlier in this edit",
                                names=sorted(f"@{n}" for n in self.aliases))
        else:
            found = self.doc.get_node_by_id(ref)
            if found is None:
                raise EditError("no_node", f"no node {ref!r} in this document "
                                "(deleted, or an ID from another document)")
        if self.doc._node_map.get(found.id) is not found:
            raise EditError("no_node", f"{_label(found)} was deleted earlier in this edit")
        return found

    def locate(self, path: str, node: str | None) -> list[Location]:
        start = self.node(node) if node is not None else None
        try:
            return self.doc.locate(path, node=start)
        except ValueError as exc:
            raise EditError("bad_path", str(exc)) from None

    def target(self, spec: str | Target) -> list[Location]:
        if isinstance(spec, Target):
            return self.locate(spec.path, spec.node)
        return self.locate(spec, None)

    @staticmethod
    def counted(locs: list[Location], expect: int | str | None, path: str) -> list[Location]:
        if expect == "all":
            return locs
        want = 1 if expect is None else expect
        if len(locs) == want:
            return locs
        matches = [str(loc) for loc in locs[:10]]
        if not locs:
            raise EditError("no_match", f"{path} matches nothing", matched=0)
        hint = ("narrow the path" if want == 1 else "check the path") + \
               f", or pass expect={len(locs)} (or \"all\")"
        raise EditError("count_mismatch", f"{path} matches {len(locs)} places, but this write "
                        f"expects {want}; nothing changed. {hint}",
                        matched=len(locs), matches=matches)

    def one(self, locs: list[Location], what: str, spec: object) -> Location:
        if len(locs) != 1:
            where = spec.path if isinstance(spec, Target) else spec
            raise EditError("count_mismatch" if locs else "no_match",
                            f"{what} {where!r} must name exactly one place; it matches {len(locs)}",
                            matched=len(locs), matches=[str(loc) for loc in locs[:10]])
        return locs[0]

    def reference(self, value: Any) -> str | None:
        """A reference value as the target's ID."""
        if value is None:
            return None
        if isinstance(value, str):
            return self.node(value).id
        if isinstance(value, dict) and set(value) == {"select"} and isinstance(value["select"], str):
            locs = self.locate(value["select"], None)
            loc = self.one(locs, "a reference's select", value["select"])
            if loc.kind != "node":
                raise EditError("not_a_node", f"a reference names a node; {loc} is not one")
            return loc.node.id
        raise EditError("bad_value", "a reference is a node ID, @name, or {\"select\": path}")

    def field_value(self, cls: type[AtomNode], field: str, value: Any) -> Any:
        rdef = cls._ref_defs.get(field)
        if rdef is None:
            return value
        if rdef.many:
            if not isinstance(value, list):
                raise EditError("bad_value", f"{cls._node_type}.{field} is a list of references")
            return [self.reference(item) for item in value]
        return self.reference(value)


def _write(node: AtomNode, field: str, value: Any) -> None:
    try:
        setattr(node, field, value)
    except ValidationError as exc:
        raise EditError("invalid_value", f"{field} of {_label(node)}: {_validation_message(exc)}; "
                        "nothing changed", field=field) from None


def _field_location(loc: Location, verb: str) -> None:
    """Refuse anything that is not a whole, writable field."""
    if loc.kind == "slot":
        raise EditError("not_a_field", f"{loc} is a child slot, not a field; use insert, move or delete")
    if loc.kind == "node":
        raise EditError("not_a_field", f"the path selects a node ({_label(loc.node)}), not a field; "
                        f"end the path with the field to {verb}, e.g. …['title']")
    if loc.field in _META:
        raise EditError("read_only", f"{loc.field} is read-only")
    if loc.inner:
        whole = _plain(loc.node, loc.field or "")
        if loc.tier == "atomic":
            message = (f"{loc.field} of {_label(loc.node)} is an atomic value: its parts cannot be "
                       f"changed one at a time. Set the whole {loc.field} instead")
        else:
            message = (f"{loc.field} of {_label(loc.node)} is written whole; set the whole value"
                       + (", or use add/remove for one element" if isinstance(whole, list) else ""))
        raise EditError("not_settable", message, field=loc.field, current=whole)


# ── applying one operation ────────────────────────────────────────────────────


def _set(batch: _Batch, op: SetOp, i: int) -> None:
    locs = batch.counted(batch.locate(op.path, op.node), op.expect, op.path)
    for loc in locs:
        _field_location(loc, "set")
    for loc in locs:
        node, field = loc.node, loc.field or ""
        before = _plain(node, field)
        if "if_current" in op.model_fields_set and before != op.if_current:
            raise EditError("stale", f"{field} of {_label(node)} is {_short(before)}, not "
                            f"{_short(op.if_current)}: it changed since you read it; nothing changed",
                            current=before)
        label = _label(node)                            # as it was named before the write
        _write(node, field, batch.field_value(type(node), field, op.value))
        batch.touch(node, i)
        after = _plain(node, field)
        batch.changes.append(f"set {field} of {label}: " + (
            f"{_short(before)} (unchanged)" if after == before
            else f"{_short(before)} → {_short(after)}"))


def _add_or_remove(batch: _Batch, op: AddOp | RemoveOp, i: int) -> None:
    locs = batch.counted(batch.locate(op.path, op.node), op.expect, op.path)
    for loc in locs:
        _field_location(loc, op.op)
    for loc in locs:
        node, field = loc.node, loc.field or ""
        current = _plain(node, field)
        if not isinstance(current, list):
            raise EditError("not_a_list", f"{field} of {_label(node)} is not a list; use set",
                            current=current)
        rdef = type(node)._ref_defs.get(field)
        element = batch.reference(op.value) if rdef is not None else op.value
        if op.op == "add":
            updated = [*current, element]
        else:
            if element not in current:
                raise EditError("not_present", f"{_short(element)} is not in {field} of "
                                f"{_label(node)}; nothing changed", current=current)
            updated = list(current)
            updated.remove(element)
        _write(node, field, updated)
        batch.touch(node, i)
        verb = "added" if op.op == "add" else "removed"
        batch.changes.append(f"{verb} {_short(element)} {'to' if op.op == 'add' else 'from'} "
                             f"{field} of {_label(node)}")


def _create(batch: _Batch, spec: Any, i: int) -> tuple[AtomNode, dict[str, list[Any]]]:
    if not isinstance(spec, dict) or not isinstance(spec.get("$type"), str):
        raise EditError("bad_value", "a node to insert is {\"$type\": ..., fields..., slot: [children]}")
    cls = batch.doc._node_types.get(spec["$type"])
    if cls is None:
        raise EditError("unknown_type", f"no node type {spec['$type']!r} in this document",
                        types=sorted(batch.doc._node_types))
    fields: dict[str, Any] = {}
    children: dict[str, list[Any]] = {}
    for key, value in spec.items():
        if key in ("$type", "$as"):
            continue
        if key in cls._slot_order:
            if not isinstance(value, list):
                raise EditError("bad_value", f"{cls._node_type}.{key} is a child slot: a list of nodes")
            children[key] = value
        elif key in cls._field_adapters:
            fields[key] = batch.field_value(cls, key, value)
        else:
            raise EditError("unknown_field", f"{cls._node_type} has no field or slot {key!r}",
                            fields=sorted(cls._field_adapters), slots=list(cls._slot_order))
    try:
        created = batch.doc.create_node(cls, **fields)
    except ValidationError as exc:
        raise EditError("invalid_value", f"new {cls._node_type}: {_validation_message(exc)}; "
                        "nothing changed") from None
    alias = spec.get("$as")
    if alias is not None:
        if not isinstance(alias, str) or not alias or alias in batch.aliases:
            raise EditError("bad_name", f"$as must be a new name; {alias!r} is not")
        batch.aliases[alias] = created
    batch.touch(created, i)
    return created, children


def _attach_children(batch: _Batch, parent: AtomNode, children: dict[str, list[Any]], i: int) -> int:
    count = 0
    for slot, specs in children.items():
        for spec in specs:
            child, grandchildren = _create(batch, spec, i)
            getattr(parent, slot).append(child)
            count += 1 + _attach_children(batch, child, grandchildren, i)
    return count


def _sibling(batch: _Batch, spec: str | Target, what: str) -> AtomNode:
    loc = batch.one(batch.target(spec), what, spec)
    if loc.kind != "node":
        raise EditError("not_a_node", f"{what} names a node; {loc} is not one")
    return loc.node


def _insert(batch: _Batch, op: InsertOp, i: int) -> None:
    loc = batch.one(batch.locate(op.path, op.node), "insert's path", op.path)
    if loc.kind != "slot":
        raise EditError("not_a_slot", f"insert goes into a child slot; {loc} is not one "
                        "(end the path with the slot, e.g. …['tasks'])")
    parent, slot = loc.node, loc.slot or ""
    if op.before is not None and op.after is not None:
        raise EditError("bad_request", "give before or after, not both")
    sibling = None
    for spec, where in ((op.before, "before"), (op.after, "after")):
        if spec is not None:
            sibling = _sibling(batch, spec, where)
            if sibling._parent is not parent or sibling._slot_name != slot:
                raise EditError("bad_request", f"{_label(sibling)} is not in {loc}")
    created, children = _create(batch, op.value, i)
    if sibling is None:
        getattr(parent, slot).append(created) if op.position == "append" \
            else getattr(parent, slot).prepend(created)
    elif op.before is not None:
        sibling.insert_before(created)
    else:
        sibling.insert_after(created)
    batch.touch(parent, i)
    extra = _attach_children(batch, created, children, i)
    tail = f" with {extra} descendant{'s' if extra != 1 else ''}" if extra else ""
    batch.changes.append(f"inserted {_label(created)}{tail} into {loc}")


def _nodes(batch: _Batch, op: MoveOp | DeleteOp) -> list[AtomNode]:
    locs = batch.counted(batch.locate(op.path, op.node), op.expect, op.path)
    for loc in locs:
        if loc.kind != "node":
            raise EditError("not_a_node", f"{op.op} acts on nodes; {loc} is not one "
                            "(remove the field from the end of the path)")
    return [loc.node for loc in locs]


def _move(batch: _Batch, op: MoveOp, i: int) -> None:
    nodes = _nodes(batch, op)
    given = [spec for spec in (op.to, op.before, op.after) if spec is not None]
    if len(given) != 1:
        raise EditError("bad_request", "move needs exactly one of to, before or after")
    if op.to is not None:
        loc = batch.one(batch.target(op.to), "move's to", op.to)
        if loc.kind != "slot":
            raise EditError("not_a_slot", f"to names a child slot; {loc} is not one")
        ordered = nodes if op.position == "append" else list(reversed(nodes))
        for n in ordered:
            n.move(loc.node, loc.slot, op.position)
            batch.touch(n, i)
        where = str(loc)
    else:
        before = op.before is not None
        sibling = _sibling(batch, op.before if before else op.after, "before" if before else "after")  # type: ignore[arg-type]
        for n in (nodes if before else list(reversed(nodes))):
            n.move(sibling, None, "before" if before else "after")
            batch.touch(n, i)
        where = f"{'before' if before else 'after'} {_label(sibling)}"
    names = ", ".join(_label(n) for n in nodes)
    batch.changes.append(f"moved {names} to {where}")


def _delete(batch: _Batch, op: DeleteOp, i: int) -> None:
    nodes = _nodes(batch, op)
    labels = [_label(n) for n in nodes]
    doomed: dict[str, AtomNode] = {}
    for n in nodes:
        doomed[n.id] = n
        for below in batch.doc.descendants(n):
            doomed[below.id] = below
    holders = []
    for target in doomed.values():
        for referrer_id, field in batch.doc._ref_index.get(target.id, {}):
            if referrer_id not in doomed:
                referrer = batch.doc.get_node_by_id(referrer_id)
                if referrer is not None:
                    holders.append(f"{field} of {_label(referrer)} → {_label(target)}")
    if holders:
        raise EditError("still_referenced",
                        f"cannot delete: {len(holders)} reference{'s' if len(holders) != 1 else ''} "
                        "still point into what would be deleted. Change or clear them earlier in "
                        "this same edit, then delete", references=sorted(holders)[:20])
    for n in nodes:
        if batch.doc._node_map.get(n.id) is n:          # an ancestor's delete may have taken it
            n.delete()
    batch.changes.append(f"deleted {', '.join(labels)}")


_APPLY: dict[str, Callable[[_Batch, Any, int], None]] = {
    "set": _set, "add": _add_or_remove, "remove": _add_or_remove,
    "insert": _insert, "move": _move, "delete": _delete,
}


# ── the batch ─────────────────────────────────────────────────────────────────


class _DryRun(Exception):
    pass


def _parse(ops: Any) -> list[Any]:
    if not isinstance(ops, list) or not ops:
        raise EditError("bad_request", "an edit is a non-empty list of operations")
    parsed: list[Any] = []
    for i, raw in enumerate(ops):
        if isinstance(raw, BaseModel):
            parsed.append(raw)
            continue
        try:
            parsed.append(_OPS.validate_python([raw])[0])
        except ValidationError as exc:
            raise EditError("bad_request", f"not an operation this editor knows: "
                            f"{_validation_message(exc)}", op=i) from None
    return parsed


def apply_edits(doc: Doc, ops: list[Any], *, dry_run: bool = False) -> dict[str, Any]:
    """Apply a batch of operations in one transaction and describe what
    changed, or raise ``EditError`` with nothing changed. ``dry_run`` does all
    of it — every check, every validation — and then rolls back."""
    parsed = _parse(ops)
    batch = _Batch(doc)
    summary: dict[str, Any] = {}
    try:
        with doc.transaction():
            for i, op in enumerate(parsed):
                try:
                    _APPLY[op.op](batch, op, i)
                except EditError as exc:
                    exc.op = i
                    raise
                except ValidationError as exc:
                    raise EditError("invalid_value", _validation_message(exc), op=i) from None
                except RefIntegrityError as exc:
                    raise EditError("still_referenced", str(exc), op=i) from None
                except (TypeError, ValueError, LookupError, RuntimeError) as exc:
                    raise EditError("invalid", str(exc), op=i) from None
            failures: list[dict[str, Any]] = []
            for node_id in sorted(doc._diff.updated | doc._diff.inserted):
                node = doc._node_map.get(node_id)
                if node is None:
                    continue
                try:
                    doc._validate_node(node)
                except ValidationError as exc:
                    ops_here = sorted(batch.touched.get(node_id, ()))
                    failures.append({"node": _label(node), "ops": ops_here,
                                     "problem": _validation_message(exc)})
            if failures:
                first = failures[0]
                raise EditError(
                    "invalid_document",
                    f"the edit leaves {first['node']} invalid: {first['problem']}; nothing changed",
                    op=first["ops"][0] if first["ops"] else None, failures=failures)
            summary = {"inserted": len(doc._diff.inserted), "updated": len(doc._diff.updated),
                       "deleted": len(doc._diff.deleted)}
            if dry_run:
                raise _DryRun
    except _DryRun:
        pass
    except ValidationError as exc:                      # anything commit still finds
        raise EditError("invalid_document", _validation_message(exc)) from None
    except RefIntegrityError as exc:
        raise EditError("still_referenced", str(exc)) from None
    return {"ok": True, "changed": not dry_run and any(summary.values()), "dry_run": dry_run,
            "changes": batch.changes, "counts": summary,
            "created": {f"@{name}": n.id for name, n in batch.aliases.items()}}


# ── reading ───────────────────────────────────────────────────────────────────


def _entry(doc: Doc, node: AtomNode, depth: int, width: int) -> dict[str, Any]:
    entry: dict[str, Any] = {"$id": node.id, "$type": node._node_type}
    state = node._state_to_json_plain(include_defaults=True)
    entry.update(state)
    refs = {}
    for field, rdef in type(node)._ref_defs.items():
        ids = state.get(field)
        for ref_id in (ids if isinstance(ids, list) else [ids]):
            target = doc.get_node_by_id(ref_id) if isinstance(ref_id, str) else None
            if target is not None:
                refs[ref_id] = _label(target)
    if refs:
        entry["$refs"] = refs
    for slot in node._slot_order:
        children = []
        child = node._slot_first.get(slot)
        while child is not None:
            children.append(child)
            child = child._next_sibling
        shown = children[:width]
        if depth > 0:
            entry[slot] = [_entry(doc, c, depth - 1, width) for c in shown]
        else:
            entry[slot] = [{"$id": c.id, "$type": c._node_type, "$label": _label(c)} for c in shown]
        if len(children) > width:
            entry[slot].append({"$more": len(children) - width})
    return entry


def read_view(doc: Doc, path: str = "$", *, node: str | None = None, depth: int = 1,
              limit: int = 50, width: int = 50) -> dict[str, Any]:
    """What ``path`` finds. A node comes back with its fields (defaults
    included), labels for the nodes its references name, and its children to
    ``depth`` levels — deeper ones as ``{$id, $type, $label}``, so they can be
    read or edited next. A field, or a place inside one, comes back as its
    value with whether an edit could set it."""
    batch = _Batch(doc)
    locs = batch.locate(path, node)
    results: list[dict[str, Any]] = []
    for loc in locs[:limit]:
        if loc.kind == "node":
            results.append(_entry(doc, loc.node, depth, width))
        elif loc.kind == "slot":
            owner = _entry(doc, loc.node, depth + 1, width)
            results.append({"$slot": loc.slot, "of": loc.node.id, "children": owner[loc.slot or ""]})
        else:
            value: Any = _plain(loc.node, loc.field or "") if loc.field not in _META else (
                loc.node.id if loc.field == "$id" else loc.node._node_type)
            for key in loc.inner:
                value = value[key]
            results.append({"at": str(loc), "node": loc.node.id, "field": loc.field,
                            "inner": list(loc.inner), "value": value, "settable": loc.settable})
    out: dict[str, Any] = {"matched": len(locs), "results": results}
    if len(locs) > limit:
        out["truncated"] = f"showing {limit} of {len(locs)}; narrow the path"
    return out


def _strip(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _strip(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_strip(v) for v in schema]
    return schema


def describe_schema(doc: Doc) -> dict[str, Any]:
    """The document's types, compact: each node type's fields with their tier
    and JSON Schema, what its references point at, and what its child slots
    accept."""
    full = doc.atomdoc_schema()
    node_types: dict[str, Any] = {}
    for name, t in full["node_types"].items():
        fields: dict[str, Any] = {}
        for field, prop in t["json_schema"].get("properties", {}).items():
            if field in t["slots"]:
                continue
            info: dict[str, Any] = {"tier": t["field_tiers"].get(field), "schema": _strip(prop)}
            ref = t.get("refs", {}).get(field)
            if ref:
                info["references"] = ref["target_type"] + ("[]" if ref["many"] else "")
            fields[field] = info
        slots = {slot: (s.get("allowed_types") or ([s["allowed_type"]] if s.get("allowed_type")
                                                    else ["any node"]))
                 for slot, s in t["slots"].items()}
        node_types[name] = {"fields": fields, "slots": slots}
    return {"root_type": full["root_type"], "node_types": node_types,
            "value_types": {name: _strip(v["json_schema"]) for name, v in full["value_types"].items()}}


# ── over a store ──────────────────────────────────────────────────────────────


def _default_encode(doc: Doc) -> bytes:
    return json.dumps({"format": "atomdoc", "doc": doc.dump()}).encode()


class DocumentEditor:
    """``read_view``, ``apply_edits`` and ``describe_schema`` for documents
    kept in a ``DocumentStore``: each call loads, acts, and — for an edit that
    changed something — saves only if nobody saved in between.

    A conflicting save is reported, never retried: a retry would re-apply an
    edit the model decided on content that may have changed. Every result
    carries the document's ``version``; passing it back to ``edit`` refuses the
    edit if the document has changed since that read.
    """

    def __init__(self, store: DocumentStore, root_type: type[AtomNode], *,
                 nodes: list[type[AtomNode]] | None = None,
                 key: Callable[[str], str] = lambda doc_id: f"documents/{doc_id}",
                 encode: Callable[[Doc], bytes] = _default_encode,
                 decode: Callable[[bytes], Doc] | None = None) -> None:
        self.store, self.root_type, self.nodes, self.key = store, root_type, nodes, key
        self.encode = encode
        self.decode = decode or self._default_decode

    def _default_decode(self, data: bytes) -> Doc:
        from ._doc import Doc

        envelope = json.loads(data)
        wire = envelope["doc"] if isinstance(envelope, dict) and "doc" in envelope else envelope
        return Doc.restore(wire, root_type=self.root_type, nodes=self.nodes)

    async def _load(self, doc_id: str) -> tuple[Doc, str]:
        from ._store import DocumentNotFound, InvalidKey

        try:
            data, token = await self.store.read(self.key(doc_id))
        except InvalidKey as exc:
            raise EditError("bad_document_id", str(exc)) from None
        except DocumentNotFound:
            raise EditError("not_found", f"no document {doc_id!r}") from None
        return self.decode(data), token

    async def create(self, doc_id: str, doc: Doc) -> str:
        """Store a new document; refuse if one exists under ``doc_id``."""
        from ._store import ABSENT, StaleWrite

        try:
            return await self.store.put(self.key(doc_id), self.encode(doc), if_match=ABSENT)
        except StaleWrite:
            raise EditError("exists", f"a document {doc_id!r} already exists") from None

    async def read(self, doc_id: str, path: str = "$", *, node: str | None = None,
                   depth: int = 1, limit: int = 50) -> dict[str, Any]:
        doc, token = await self._load(doc_id)
        return {**read_view(doc, path, node=node, depth=depth, limit=limit), "version": token}

    async def schema(self, doc_id: str) -> dict[str, Any]:
        doc, _ = await self._load(doc_id)
        return describe_schema(doc)

    async def edit(self, doc_id: str, ops: list[Any], *, dry_run: bool = False,
                   version: str | None = None) -> dict[str, Any]:
        from ._store import StaleWrite

        doc, token = await self._load(doc_id)
        if version is not None and version != token:
            raise EditError("conflict", "the document has changed since the read that version "
                            "came from; read it again before deciding this edit. Nothing changed",
                            version=token)
        result = apply_edits(doc, ops, dry_run=dry_run)
        if result["changed"]:
            try:
                token = await self.store.put(self.key(doc_id), self.encode(doc), if_match=token)
            except StaleWrite:
                raise EditError("conflict", "someone else saved this document while the edit "
                                "was being applied, so it was not saved. Read it again and re-send "
                                "the edit if it still makes sense", ) from None
        return {**result, "version": token}


TOOL_DESCRIPTIONS: dict[str, str] = {
    "read": (
        "Read part of a structured document with a JSONPath query (RFC 9535). $ is the document "
        "root, or `node` (a node ID) when given. Every node has $id and $type; references hold "
        "node IDs, with labels under $refs. Examples: $.milestones[?@.name=='launch'] ; "
        "$..tasks[?@.status!='done'] ; $..[?@['$type']=='Person'] ; "
        "$..tasks[?deref(@.assignee,'name')=='Alice']. `depth` is how many levels of children "
        "to include in full; deeper children come back as {$id, $type, $label}. The result's "
        "`version` can be passed to edit to refuse the edit if the document changes meanwhile."
    ),
    "edit": (
        "Change a structured document. `ops` is a list applied in one transaction: all of it or "
        "none of it. Operations: set (a whole field), add/remove (one element of a list field), "
        "insert (a new node, with children, into a child slot), move (nodes to a slot, or "
        "before/after a sibling), delete (nodes). Each finds its target with a JSONPath `path` "
        "(from the root, or from `node`). A write must match exactly one place unless `expect` "
        "says how many (a number, or \"all\"). Fields are written whole: a path inside a frozen "
        "value or a list is refused — set the whole value, or add/remove one list element. "
        "References take a node ID, @name (a node created earlier in the same edit with "
        "\"$as\": name), or {\"select\": path}. Use if_current to require a field's present "
        "value, version to require the document you read, and dry_run to check without saving. "
        "Errors say what to change; nothing is saved when one is returned."
    ),
    "schema": (
        "The document's node types: each field's JSON Schema and tier (mergeable fields are plain "
        "values; atomic ones are frozen values replaced whole; ref fields name other nodes), what "
        "references point at, and which node types each child slot accepts."
    ),
}
