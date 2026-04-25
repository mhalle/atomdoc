# Changelog

All notable changes to this project will be documented in this file.

## [0.3.0] - 2026-04-20

### Changed

- **Breaking wire protocol change**: state values in `op` and `patch` messages
  are now **native JSON** (strings, numbers, booleans, arrays, objects, null)
  rather than JSON-stringified strings. This aligns `op`/`patch` with `snapshot`
  and `create` messages, which already used native JSON. Opaque/bytes fields
  continue to travel as base64-encoded JSON strings; receivers decode based on
  the field's schema tier.
- `StatePatch` type alias is now `dict[str, dict[str, Any]]`.
- Renamed `AtomNode._stringify_state_key` → `AtomNode._state_key_to_json`.
  `_state_to_json` and `_parse_state_key` now delegate to the existing
  `_state_to_json_plain` / `_parse_json_value` helpers.

### Migration

Clients speaking the old protocol will not interoperate with this release. Bump
both server and client together. If you hand-build `state` patches in tests or
tooling, drop the outer `json.dumps(...)` call.

## [0.2.0] - 2026-03-30

### Added

- **Server protocol layer**: `Session` class manages a `Doc` and its connected
  clients, handling schema delivery, snapshots, patches, and operation routing.
- **Abstract transport**: `Transport` and `ClientConnection` abstract base
  classes for pluggable communication channels.
- **WebSocket transport**: `WebSocketTransport` built on the `websockets`
  library (install with `pip install atomdoc[server]`).
- **Wire protocol**: message types for server-to-client (`schema`, `snapshot`,
  `patch`, `error`) and client-to-server (`op`, `create`, `undo`, `redo`).
- **Schema export**: `Doc.atomdoc_schema()` produces JSON Schema with
  `x-atomdoc` extensions describing field tiers, slots, and frozen value types
  for language-agnostic clients.
- **Operation serialization**: `operations_to_wire()` and
  `operations_from_wire()` for converting operations to/from JSON.

## [0.1.0] - 2026-03-15

### Added

- **Core document model**: `Doc` class with tree-structured nodes, automatic
  node type discovery, and clean JSON / wire-format serialization.
- **`@node` decorator**: converts plain classes or Pydantic `BaseModel`
  subclasses into document node types with optional custom type names.
- **`Array[T]` slots**: ordered child collections with `append`, `prepend`,
  `insert`, `delete`, `clear`, indexing, slicing, and iteration.
- **Frozen value types**: Pydantic `frozen=True` models as atomic fields,
  replaced as a unit with last-write-wins semantics.
- **Transactions**: context-manager API for batching multiple mutations into a
  single change event, with automatic rollback on exceptions.
- **Undo/redo**: `UndoManager` with full forward and inverse operation tracking.
- **Change events**: `on_change` callback fires once per transaction with
  `Diff` (inserted, deleted, updated nodes) and `Operations`.
- **Validation**: full Pydantic validation at transaction commit time, including
  field constraints, field validators, and cross-field model validators.
- **Tree navigation**: `parent`, `next_sibling`, `prev_sibling`, `ancestors`,
  `descendants` via the `Doc` instance.
- **Field tiers**: automatic tier inference from type annotations (mergeable,
  atomic, opaque, structure).
- **Extensions**: bundle node types and normalization hooks with idempotency
  checking.
- **285 tests** covering the full API surface.
