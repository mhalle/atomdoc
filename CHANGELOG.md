# Changelog

All notable changes to this project will be documented in this file.

## [0.4.0] - 2026-09-06

Ports the DocNode v0.4 lifecycle and undo improvements from
[DocuKit](https://github.com/docukit/docukit), and adds references between
nodes. The operations wire format is unchanged; the schema export gains a
`refs` block, the `"ref"` tier, and `Field(...)` constraints (see Added).

### Fixed

- **`Field(...)` on a plain `@node` class was ignored.** The `FieldInfo`
  object leaked through as the field's default value, and its constraints
  (`ge`, `le`, ...) were never enforced. The default is now unwrapped and
  the constraints are checked at commit, as they already were for a
  `BaseModel` source.
- **Schema export dropped `Field` constraints.** `opacity: float =
  Field(ge=0.0, le=1.0)` exported as `{"type": "number"}` for both plain
  and `BaseModel` nodes. Bounds and other `Annotated` metadata now appear
  in `json_schema`, and each property carries its `default`.
- **Node ID sessions carry 5 random characters instead of 3.** Two
  sessions minted in the same millisecond now collide with probability
  1 in ~1.07 billion rather than 1 in 262,144. IDs grow by two
  characters; old and new widths coexist, IDs are opaque strings.
  This diverges from DocuKit's generator, which is intentional.
- **`Doc.restore` could mint a colliding ID session.** The session was
  minted before the nodes were loaded; if it matched a session already in
  the dump, new nodes would silently overwrite existing ones. Restore now
  checks the loaded IDs and re-mints on collision.

- **Move replay lost position.** Applying a move operation (from undo/redo
  or a remote peer) ignored the recorded `prev`/`next` siblings and always
  appended to the target slot. Moves now land where they were recorded.
- **Undo stack eviction was inverted.** A full undo stack discarded the new
  entry instead of the oldest one.
- **`on_normalize` was unreachable.** `Extension` had no registration hook,
  so nothing user-written could run during the `init` stage.
- Mutating the document during the `init` stage raised; extension
  registration may now mutate the document.
- **Normalizer changes bypassed validation.** Pydantic validation ran before
  normalizers, so nodes a normalizer inserted or edited were never checked.
  Validation now runs after normalization, before the change event.

### Added

- **References.** `Ref[T]` (one target), `list[Ref[T]]` (many), either
  `| None`. Reading resolves to the node; assigning accepts a node or an
  ID; the stored value is the target's ID. The document keeps a reverse
  index (`doc.referrers(node, field=...)`, never serialized, rebuilt on
  restore) and checks referential integrity at commit: references must
  resolve to a node of the declared type, and a node that is still
  referenced cannot be deleted (policy `restrict`). Violations raise
  `RefIntegrityError` and roll the transaction back. `node.ref_id(name)`
  returns the unresolved ID. A dump with dangling references fails to
  restore in strict mode and warns otherwise.
- **Schema export:** field tier `"ref"` and a per-node-type `refs` block
  (`target_type`, `many`, `policy`).
- `mint_session_id(created_at_ms, existing)`, `session_prefix(node_id)`,
  and `node_id_factory(..., existing_sessions=...)` in `atomdoc._id`.
- **Doc-owned undo manager.** Every `Doc` has `doc.undo_manager`, configured
  with `Doc(undo_manager=UndoManagerConfig(max_steps=..., merge_interval=...))`.
  It is disabled by default (`max_steps=0`). The standalone
  `UndoManager(doc)` constructor still works and still defaults to 100 steps.
- **Merge interval.** Transactions committed within `merge_interval` seconds
  of each other collapse into a single undo step. Off by default.
- **Transaction flags.** `ChangeEvent.flags` carries `TransactionFlags`.
  `doc.transaction(skip_undo=True)` and
  `doc.apply_operations(ops, skip_undo=True)` mark transactions the undo
  manager ignores — use them when applying operations from a remote peer.
- **Undo history transfer.** `undo_manager.export_history()` /
  `import_history()` move undo and redo state between matching documents
  (same ID and root type), for example when a document is rebuilt from a
  newer snapshot.
- **Normalizers run on construction** (and after `Doc.restore`), so
  extensions can establish invariants such as a default child. Nothing done
  during initialization enters undo history.
- **`Extension(register=...)`** receives the `Doc` during the `init` stage and
  may call `doc.on_normalize`, `doc.on_change`, and mutate the document.
- **Pluggable node IDs.** `Doc(node_id_generator=NodeIdGenerator(generate,
  validate, extract_time=None))`. Without `extract_time`, `generate` is used
  for every node and all IDs are validated on `Doc.restore`; with it, child
  nodes keep the compact Lamport-style IDs. `default_node_id_generator()`
  returns the lowercase-ULID default.
- `NodeRange.move` / `AtomNode.move` accept `position="before"` / `"after"`
  with a sibling as the target.
- `merge_operations(*ops)` concatenates operation sets.
- `UndoManager.clear()`, `UndoManager.dispose()`, `is_enabled`, `max_steps`,
  `merge_interval`.
- `Session` uses `doc.undo_manager` when it is enabled, otherwise it creates
  a standalone 100-step manager as before.

### Changed

- `Doc.restore` runs normalizers after the tree is loaded rather than on the
  empty document.
- `AtomNode.move(target, slot_name, position)`: `slot_name` is now optional
  and only required for `append`/`prepend`.

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
