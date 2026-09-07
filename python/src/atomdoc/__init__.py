"""AtomDoc — Local-first document models with semantic atomicity."""

from ._array import Array
from pydantic import JsonValue

from ._doc import validation_causes, Doc, Extension, node
from ._handle import Handle
from ._id import NodeIdGenerator, default_node_id_generator
from ._node import AtomNode
from ._operations import merge_operations
from ._protocol import operations_from_wire, operations_to_wire
from ._ref import Ref, RefIntegrityError
from ._session import Session
from ._transport import ClientConnection, Transport
from ._types import ChangeEvent, Diff, ListenerError, Operations, TransactionFlags
from ._undo import UndoHistory, UndoManager, UndoManagerConfig

__all__ = [
    "validation_causes",
    "Array",
    "Doc",
    "AtomNode",
    "Extension",
    "Handle",
    "JsonValue",
    "UndoManager",
    "UndoManagerConfig",
    "UndoHistory",
    "ChangeEvent",
    "ClientConnection",
    "Diff",
    "NodeIdGenerator",
    "Operations",
    "Ref",
    "RefIntegrityError",
    "ListenerError",
    "Session",
    "TransactionFlags",
    "Transport",
    "default_node_id_generator",
    "merge_operations",
    "node",
    "operations_from_wire",
    "operations_to_wire",
]
