# Changelog

All notable changes to this project will be documented in this file.

## [0.4.2] - 2026-09-07

Fixes from a fourth adversarial review and an end-to-end integration
pass. Adds a performance suite. Wire format unchanged; a thick client
now receives slot-order corrections after moves (atomdoc-ts >= 0.4.2
applies them; older clients apply them as ordinary patches).

### Fixed

- **Concurrent moves could leave a thick client's slot order wrong for
  good.** A remote move anchored at a node the client had a pending move
  for is vacuous in the client's frame while real on the server, and a
  move the server found already satisfied produced no patch at all. The
  session now follows the echo of every `op` containing a move, and
  answers every `op` that commits nothing, with a patch to the requester
  alone at the current version giving the full order of each slot it
  moved into (and, for a no-op, the values it holds).
- **The request context leaked across the awaits of an error reply**, so
  a host-side commit landing meanwhile was labeled with the failed
  request's `ref`, credited to that client's undo history, and left
  queued until some later request flushed it. The context now lives only
  for the synchronous dispatch.
- **A connecting client could receive versions out of order**: its
  buffered patches were drained without the flush lock. Promotion and
  drain now hold it.
- **An empty state entry (`{"id": {}}`) committed a version** and an
  undo step. It is ignored.
- **A listener failure truncated a multi-step undo and a multi-entry
  journal.** Both now finish and report the failures together.
- **A `KeyboardInterrupt` in a change listener wedged the document** in
  the change stage. The commit is closed before it propagates.
- **The WebSocket transport dropped the connection on a malformed
  frame** without a word, and swallowed session errors. It replies
  `invalid_op` and keeps the connection; session errors are logged.
- **A dead client kept its per-client undo manager** subscribed to the
  document; `unbind()` left scheduled flushes running.
- **`children[i]` could return the wrong node after an insert, delete or
  move** (a regression of the index cursor below): the cursor is now
  tied to a document-wide tree version and discarded on any structural
  change. **Per-node default copies shared nested model instances** (a
  regression of the structural copy below): models inside container
  defaults are deep-copied again.
- **An undo manager with an `accept` filter dropped the redo entry for
  its own undo** when the filter declined that commit.

- **A tree about a thousand levels deep crashed with `RecursionError`**
  on `dump()`, `restore()`, `descendants()`, `adopt()`, and deleting the
  chain. Every tree walk is now iterative. (Serializing such a dump with
  the standard `json` module still hits the interpreter's recursion
  limit; that is json's limit, not the document's.)
- **`children[i]` in a loop was quadratic.** Children are a linked list,
  so each index walked from the start. The slot view is now cached per
  node and keeps a cursor, so a sequential scan costs O(1) per step.
  `len()` still walks the list; iterate instead of indexing.
- **Per-node default copies used `copy.deepcopy`**, a quarter of the cost
  of creating a node with a 4x4 matrix default. Defaults are now copied
  structurally.

### Added

- **Performance suite.** `benchmarks/bench.py` sweeps Slicer-like scenes
  across sizes and reports per-item cost and the scaling ratio;
  `--profile <scenario>` prints a cProfile. `tests/test_scaling.py` runs
  in the normal suite and fails if any of sixteen operations turns
  quadratic, plus a 5000-deep chain round trip.

## [0.4.1] - 2026-09-07

Fixes from an outside review of the synchronization layer. No wire
format changes; works with atomdoc-ts >= 0.4.1 (whose thick client
relies on every patch carrying the request `ref`).

### Changed

- **Change listeners are post-commit observers.** A listener that raises
  no longer rolls the commit back; failures surface afterwards as
  `ListenerError`. See the first Fixed entry.

### Fixed

- **A failing change listener rolled back a commit that other listeners
  had already acted on.** A session had already queued the broadcast, so
  other clients received a change the server then reverted; the undo
  manager kept an entry for it; and events exposed live internal buffers
  that the rollback rewrote. Change listeners are now post-commit
  observers: every listener runs, the commit stands, events are copies,
  and the failures are raised afterwards as `ListenerError` (with
  `errors` and `__cause__`). Validation that should veto a commit belongs
  in model validators or normalizers, which run before.
- **A broadcast patch carried the client's raw values, not the validated
  ones.** Sending `"7"` to an `int` field stored `7` and broadcast
  `"7"`. Forward patches now serialize the value as stored. Such a commit
  is not the sender's verbatim echo, so it goes out with `source_client:
  null` and the sender applies it too.

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
- **Rollback applied inverse operations in the wrong order.** A
  transaction body that raised after inserting a node and moving an
  existing node into it deleted the existing node for good. `abort()` now
  rolls back in reverse and always returns the document to idle, even if
  an inverse operation fails.
- **Rolling back a write to a required `Ref[T]` wedged the document.** An
  unset required field serializes as `null`, which the reference adapter
  refused on the way back. `null` now round-trips as "unset" for every
  field type, and an unset required field reads as `None` rather than an
  internal sentinel.
- **`Field(default_factory=...)` ran once per class**, so every node shared
  one mutable default and mutating it corrupted the class default and the
  schema export. Factories now run per node, mutable literal defaults are
  copied per node, and `default_factory` / `default=None` on a `BaseModel`
  source are no longer dropped.
- **`Field(alias=...)` with a constraint disabled commit validation.**
- **Non-JSON defaults (`datetime`, `Decimal`, `Enum`, `set`) broke the
  schema export**, which broke the WebSocket handshake.
- **A handle to a node that was deleted and then restored (undo,
  rollback) went stale.** The restore created a new object under the same
  ID; the old handle still passed the "is attached" check, so writes
  through it were broadcast but never applied, and a delete through it
  corrupted the tree. A restore now revives the original object, so
  handles survive undo and rollback; attachment is checked by identity,
  and a stale object is refused.
- **A failing nested transaction aborted the enclosing one and left the
  document idle**, so the rest of the outer block auto-committed
  statement by statement. A nested failure now propagates and only the
  outermost transaction rolls back, as a whole. `apply_operations`
  inside an open transaction therefore raises instead of skipping.
- **The root node never got per-node defaults**, so a mutable class
  default (`tags: list[str] = []`) was shared by every document in the
  process and mutations to it were not saved.
- **`null` in a state patch skipped type validation** for a field whose
  default is not `None`, even in strict mode.
- **Unknown state keys, unknown operation codes, and nodes of the wrong
  type for a slot were accepted** from operations and from the local
  API. All three now raise; `Array[T]` is enforced on insert and move.
- **Handles nested in a composite frozen value (a handle inside a
  material) were invisible** to `doc.handles()` and the schema export.
- **Two node classes sharing a `node_type` were silently merged** when
  discovered through slots; this now raises like the explicit `nodes=`
  path does.
- **Session: `create`, `undo` and `redo` patches were labeled as the
  requester's echo** although the requester never applied those
  operations locally, so a thick client dropped them. Only an `op` whose
  operations the commit carries verbatim is an echo; every patch
  produced by a request now carries the request's `ref`.
- **Session: a client connecting during a commit could receive the same
  change in its snapshot and as a patch**, or miss it. The handshake
  now takes the snapshot atomically and holds newer patches until the
  snapshot is sent; a failed handshake leaves nothing registered.
- **Session: commits made outside a request (the host editing
  `session.doc`) were not broadcast** until the next client message, and
  a resync snapshot ignored them. They are now flushed as they happen;
  pending broadcasts go out before a resync snapshot.
- **Session: a malformed frame could leave the request context set**,
  labeling later host-side commits as that client's echo.
- **The same node twice in one insert linked it to itself**, hanging every
  later traversal. Rejected now, as is a duplicate ID within an adopted
  fragment or a dump. Adopting the same fragment twice in one call yields
  two distinct copies, and `adopt()` reseeds the ID session.
- **Moving a node into a detached parent** orphaned it while it stayed in
  the node map and the reference index. Rejected now.
- **`Doc(snapshot)` never checked references**, so dangling ones could be
  born at construction. Checked now, like restore.
- **`UndoManager.undo()` dropped a step it could not apply** (for example
  one that would delete a node a peer has since referenced). The step is
  kept and the error propagates.
- **`doc.handles()` missed handles inside lists and dicts**, and a union
  of handle types exported the first one's strength rather than the
  strongest.
- **Same-named value types from different modules** silently overwrote
  each other in the export. Now an error.
- **Exported schemas leaked `$defs` for recursive value types** and a
  stranded `discriminator.mapping`.
- **Session: a well-formed `op` that failed was dropped in silence.**
  `apply_operations` swallowed the failure inside the transaction the
  session wrapped around it, so no `rejected` error, no snapshot, and no
  patch went out. The session now applies strictly: a missing target is a
  failure, and any failure is a rejection with a resync snapshot.
- **Session: a multi-step undo broadcast only the last patch**, and a
  change committed outside any request was either dropped or sent to a
  client that had connected after it (and already had it in its snapshot).
  Every commit is broadcast, to the clients connected when it happened.
- **Session: a normalizer's additions were invisible to the sender.** A
  patch that carries more than the client sent is no longer labeled as
  that client's echo.
- **Session: a non-object frame dropped the connection** instead of
  returning `invalid_op`.
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

- **Session undo policy.** `Session(doc, undo=...)` chooses what a
  client's `undo`/`redo` means: `"per-client"` (default) keeps a history
  per connected client and reverts only that client's own commits, which
  is what a thick client does locally; `"global"` is the previous
  behavior (any client reverts the document's last commit, whoever made
  it), right for one user with several views; `"none"` refuses the
  requests with error code `unsupported`. A per-client step that no
  longer applies is answered with `rejected` (no snapshot) and kept for
  a retry. `UndoManager` gained an `accept` filter to support this.
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
- **Handles.** `Handle` (frozen: `uri`, `media_type`, `digest`) with a
  class-level `strength` of `"weak"` (default) or `"strong"`.
  `doc.handles(strength=...)` lists what the document depends on without
  resolving anything. Exported per field (`handles`) and per value type.
- **Tagged unions of values.** A union of frozen models is now tier
  `atomic` (it was `mergeable`), its members are discovered as value
  types, and it exports as an inlined `anyOf`/`oneOf` with any Pydantic
  discriminator. `JsonValue` is re-exported for open-ended values.
- **Composition.** `dump(node)` serializes a subtree; `doc.adopt(fragment,
  parent, slot, position, target)` inserts it keeping its IDs and internal
  references, re-minting only IDs that collide with the receiving document.
- **Rejected requests resync the sender.** When a well-formed `op` or
  `create` fails against the current document, the session replies with
  `error` code `rejected` and then a fresh `snapshot` for that client only,
  instead of `invalid_op` and silence. Malformed requests still get
  `invalid_op`.
- Exported JSON schemas are self-contained: local `$defs` are inlined
  (a recursive definition becomes `{}`).
- `apply_operations(..., strict=..., raise_on_error=...)`: `strict` makes a
  missing target a failure (what a server wants); `raise_on_error`
  propagates a failure after the rollback instead of skipping the entry.
- `apply_operations(..., strict=..., raise_on_error=...)`: `strict` makes a
  missing target a failure (what a server wants); `raise_on_error`
  propagates a failure after the rollback instead of skipping the entry.
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
