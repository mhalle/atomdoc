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
from ._store import (ABSENT, MAX_KEY_BYTES, CapabilityError, DocumentNotFound,
                     DocumentStore, Entry, InvalidKey, MemoryStore, StaleWrite,
                     StoreBase, StoreCapabilities, StoreClosed, StoreError,
                     StoreUnavailable, ValueTooLarge, check_key, check_prefix)
from ._store_file import FileStore
from ._store_sqlite import SqliteStore
from ._transport import ClientConnection, Transport
from ._ws_transport import WebSocketTransport
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
    "DocumentStore",
    "MemoryStore",
    "FileStore",
    "SqliteStore",
    "StoreCapabilities",
    "StoreClosed",
    "StoreError",
    "DocumentNotFound",
    "StaleWrite",
    "CapabilityError",
    "ABSENT",
    "Entry",
    "InvalidKey",
    "ValueTooLarge",
    "StoreUnavailable",
    "StoreBase",
    "check_key",
    "check_prefix",
    "MAX_KEY_BYTES",
    "TransactionFlags",
    "Transport",
    "WebSocketTransport",
    "default_node_id_generator",
    "merge_operations",
    "node",
    "operations_from_wire",
    "operations_to_wire",
]
