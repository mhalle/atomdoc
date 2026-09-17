"""Finding nodes with JSONPath (RFC 9535).

A query runs over a view of the document shaped like ``Doc.to_json()``:
fields as keys (defaults included, so ``@.status == 'todo'`` matches a
status nobody set), child slots as arrays. Three differences make the view
something a query can act on:

* every node carries ``$id`` and ``$type``, so ``[?@['$type'] == 'Task']``
  filters by type and a match names the node it came from;
* a reference is the target's node ID rather than a document path, so it
  stays the same when the target moves;
* ``deref(ref, 'field')`` reads a field of the node a reference points at:
  ``$..tasks[?deref(@.assignee, 'name') == 'Alice']``.

Needs the ``query`` extra: ``pip install atomdoc[query]``.
"""

from __future__ import annotations

from contextvars import ContextVar
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._doc import Doc
    from ._node import AtomNode

# The view being queried, by node ID, for deref(). A context variable, not an
# attribute, so two threads or tasks querying at once each see their own.
_ENTRIES: ContextVar[dict[str, dict[str, Any]]] = ContextVar("atomdoc_select_entries")


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
            target = _ENTRIES.get({}).get(ref)
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
    try:
        return _environment().compile(query)
    except jp.JSONPathError as exc:
        raise ValueError(f"not a JSONPath query this document can run: {exc}") from None


def _view(doc: Doc) -> tuple[dict[str, Any], dict[int, AtomNode], dict[str, dict[str, Any]]]:
    """The queryable view: the root entry, each entry's node (by object
    identity), and each entry by node ID."""
    node_of: dict[int, AtomNode] = {}
    by_id: dict[str, dict[str, Any]] = {}

    def entry_for(n: AtomNode) -> dict[str, Any]:
        entry: dict[str, Any] = {"$id": n.id, "$type": n._node_type}
        entry.update(n._state_to_json_plain(include_defaults=True))
        for slot_name in n._slot_order:
            entry[slot_name] = []
        node_of[id(entry)] = n
        by_id[n.id] = entry
        return entry

    root = doc.root
    root_entry = entry_for(root)
    stack: list[tuple[AtomNode, dict[str, Any]]] = [(root, root_entry)]
    while stack:
        current, entry = stack.pop()
        for slot_name in current._slot_order:
            child: AtomNode | None = current._slot_first.get(slot_name)
            while child is not None:
                child_entry = entry_for(child)
                entry[slot_name].append(child_entry)
                stack.append((child, child_entry))
                child = child._next_sibling
    return root_entry, node_of, by_id


def select(doc: Doc, query: str) -> list[AtomNode]:
    compiled = _compile(query)
    root_entry, node_of, by_id = _view(doc)
    token = _ENTRIES.set(by_id)
    try:
        matches = compiled.find(root_entry)
    finally:
        _ENTRIES.reset(token)
    nodes: list[AtomNode] = []
    seen: set[str] = set()
    for match in matches:
        found = node_of.get(id(match.value))
        if found is None:
            raise TypeError(
                f"{query!r} selects {match.path()}, a value rather than a node; "
                "select the node that holds it and read the field from that")
        if found.id not in seen:                       # `..` may reach a node twice
            seen.add(found.id)
            nodes.append(found)
    return nodes
