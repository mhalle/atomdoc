"""Finding nodes, and the places inside them, with JSONPath (RFC 9535).

A query runs over a view of the document shaped like ``Doc.to_json()``:
fields as keys (defaults included, so ``@.status == 'todo'`` matches a
status nobody set), child slots as arrays. Three differences make the view
something a query can act on:

* every node carries ``$id`` and ``$type``, so ``[?@['$type'] == 'Task']``
  filters by type and a match names the node it came from;
* a reference is the target's node ID rather than a document path, so it
  stays the same when the target moves;
* ``deref(ref, 'field')`` reads a field of the node a reference points at,
  wherever that node is — a query confined to a subtree still follows
  references out of it.

``$`` is the document root, or the node a query is given. Needs the
``query`` extra: ``pip install atomdoc[query]``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ._doc import Doc
    from ._node import AtomNode

_META = ("$id", "$type")


@dataclass(frozen=True)
class Location:
    """Where a query landed, as the unit an edit acts on.

    A match on a node is that node. A match on a field, or anywhere inside
    one, is the node and the field — plus ``inner``, the rest of the path —
    because a field is only ever written whole: ``Color.r`` is not a thing an
    edit can change, and neither is element 2 of a list. A match on a child
    slot's array is the node and the slot, the place an insert or a move
    goes.
    """

    node: AtomNode
    field: str | None = None
    slot: str | None = None
    inner: tuple[str | int, ...] = ()
    tier: str | None = None

    @property
    def kind(self) -> Literal["node", "field", "slot"]:
        if self.slot is not None:
            return "slot"
        return "node" if self.field is None else "field"

    @property
    def settable(self) -> bool:
        """Whether an edit may write this location as it stands: a whole
        field, not ``$id`` or ``$type``, and not a part of a field's value."""
        return self.kind == "field" and not self.inner and self.field not in _META

    def __str__(self) -> str:
        where = f"{self.node._node_type} {self.node.id}"
        if self.slot is not None:
            return f"{where} · slot {self.slot}"
        if self.field is None:
            return where
        part = "".join(f"[{k}]" if isinstance(k, int) else f".{k}" for k in self.inner)
        tier = f" ({self.tier})" if self.tier else ""
        return f"{where} · {self.field}{part}{tier}"


# How deref() finds an entry by node ID while a query runs. A context
# variable, not an attribute, so two threads or tasks querying at once each
# see their own document.
_LOOKUP: ContextVar[_Entries] = ContextVar("atomdoc_select_lookup")


def _jsonpath() -> Any:
    try:
        import jsonpath_rfc9535
    except ImportError as exc:
        raise ImportError(
            "Doc.select needs the jsonpath-rfc9535 package. "
            "Install it with: pip install atomdoc[query]"
        ) from exc
    return jsonpath_rfc9535


@lru_cache(maxsize=1)
def _environment() -> Any:
    jp = _jsonpath()
    from jsonpath_rfc9535 import JSONPathEnvironment
    from jsonpath_rfc9535.function_extensions import ExpressionType, FilterFunction

    class Deref(FilterFunction):
        """``deref(ref, field)``: a field of the node a reference names."""

        arg_types = [ExpressionType.VALUE, ExpressionType.VALUE]
        return_type = ExpressionType.VALUE

        def __call__(self, ref: object, field: object) -> object:
            if not isinstance(ref, str) or not isinstance(field, str):
                return jp.NOTHING
            lookup = _LOOKUP.get(None)
            target = lookup.get_entry(ref) if lookup is not None else None
            if target is None or field not in target:
                return jp.NOTHING
            return target[field]

    class Environment(JSONPathEnvironment):
        def setup_function_extensions(self) -> None:
            super().setup_function_extensions()
            self.function_extensions["deref"] = Deref()

    return Environment()


@lru_cache(maxsize=256)
def _compile(query: str) -> Any:
    jp = _jsonpath()
    if not isinstance(query, str):
        raise TypeError(f"a query is a JSONPath string, not {type(query).__name__}")
    try:
        return _environment().compile(query)
    except jp.JSONPathError as exc:
        raise ValueError(f"not a JSONPath query this document can run: {exc}") from None


def _entry(n: AtomNode) -> dict[str, Any]:
    entry: dict[str, Any] = {"$id": n.id, "$type": n._node_type}
    entry.update(n._state_to_json_plain(include_defaults=True))
    return entry


class _Entries:
    """The view under ``$``, and — for deref() — any other node, built when
    first asked for. A reference may leave the subtree a query runs in."""

    def __init__(self, doc: Doc, start: AtomNode) -> None:
        self.doc = doc
        self.node_of: dict[int, AtomNode] = {}
        self.by_id: dict[str, dict[str, Any]] = {}
        self.root = self._build(start)

    def _build(self, start: AtomNode) -> dict[str, Any]:
        def entry_for(n: AtomNode) -> dict[str, Any]:
            entry = _entry(n)
            for slot_name in n._slot_order:
                entry[slot_name] = []
            self.node_of[id(entry)] = n
            self.by_id[n.id] = entry
            return entry

        root_entry = entry_for(start)
        stack: list[tuple[AtomNode, dict[str, Any]]] = [(start, root_entry)]
        while stack:
            current, entry = stack.pop()
            for slot_name in current._slot_order:
                child: AtomNode | None = current._slot_first.get(slot_name)
                while child is not None:
                    child_entry = entry_for(child)
                    entry[slot_name].append(child_entry)
                    stack.append((child, child_entry))
                    child = child._next_sibling
        return root_entry

    def get_entry(self, node_id: str) -> dict[str, Any] | None:
        entry = self.by_id.get(node_id)
        if entry is None:
            target = self.doc.get_node_by_id(node_id)
            if target is None:
                return None
            entry = self.by_id[node_id] = _entry(target)      # fields only: deref reads fields
        return entry


def _run(doc: Doc, query: str, start: AtomNode) -> tuple[_Entries, list[Any]]:
    compiled = _compile(query)
    entries = _Entries(doc, start)
    token = _LOOKUP.set(entries)
    try:
        return entries, list(compiled.find(entries.root))
    finally:
        _LOOKUP.reset(token)


def _location(entries: _Entries, keys: tuple[str | int, ...]) -> Location:
    """Walk a match's key path down the view to the unit an edit acts on."""
    obj = entries.root
    node = entries.node_of[id(obj)]
    i = 0
    while i < len(keys):
        key = keys[i]
        if isinstance(key, str) and key in node._slot_order:
            if i + 1 == len(keys):
                return Location(node, slot=key)
            obj = obj[key][keys[i + 1]]                       # a child entry
            node = entries.node_of[id(obj)]
            i += 2
            continue
        name = str(key)
        tier = None if name in _META else type(node)._field_tiers.get(name)
        return Location(node, field=name, inner=tuple(keys[i + 1:]), tier=tier)
    return Location(node)


def _unique(locations: Iterator[Location]) -> list[Location]:
    seen: set[tuple[Any, ...]] = set()
    out: list[Location] = []
    for loc in locations:
        key = (loc.node.id, loc.field, loc.slot, loc.inner)
        if key not in seen:                                   # `..` may reach one place twice
            seen.add(key)
            out.append(loc)
    return out


def locate(doc: Doc, query: str, start: AtomNode) -> list[Location]:
    entries, matches = _run(doc, query, start)
    return _unique(_location(entries, tuple(m.location)) for m in matches)


def select(doc: Doc, query: str, start: AtomNode) -> list[AtomNode]:
    entries, matches = _run(doc, query, start)
    nodes: list[AtomNode] = []
    seen: set[str] = set()
    for match in matches:
        found = entries.node_of.get(id(match.value))
        if found is None:
            raise TypeError(
                f"{query!r} selects {match.path()}, a value rather than a node; "
                "select the node that holds it, or use locate()")
        if found.id not in seen:
            seen.add(found.id)
            nodes.append(found)
    return nodes
