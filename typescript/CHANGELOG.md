# Changelog

## 0.4.2

Fixes from a fourth adversarial review and an end-to-end integration
pass, plus a performance suite. Pairs with atomdoc 0.4.2, which sends
slot-order corrections after moves; works with 0.4.1 minus that
guarantee.

### Fixed

- **`applyPatch` deleted the wrong subtree when an earlier operation in
  the same patch had moved or inserted a child** (a regression of the
  per-patch slot coalescing below): a node moved out of the deleted
  subtree was lost, one inserted into it leaked. Subtrees are now walked
  through the patch's working child lists.
- **Echo reconciliation resurrected a node the client had already
  deleted** (create, then delete or undo before the create's echo). A
  node missing locally is re-inserted only if no still-pending op of the
  client's deleted it.
- **Echo reconciliation dropped the echo's `next` neighbor**, so a
  verbatim echo whose `prev` was deleted locally meanwhile appended the
  node to the end instead of leaving it in place.
- **Concurrent moves could leave two clients in different orders for
  good.** A remote move anchored at a node the client had a pending move
  for was vacuous in the client's frame while real on the server, and a
  move the server found already satisfied got no reply at all. The
  server now follows every move echo, and answers every no-op request,
  with the touched slot's full order for the requester (see PROTOCOL.md,
  "Slot-order corrections"). A two-thick-client convergence harness
  (`test/integration/two-clients.test.ts`) covers concurrent moves,
  creates, deletes and writes with and without a host-side device.
- **Filling a slot node by node through the thin store was quadratic.**
  `applyPatch` copied the parent's child array on every insert, delete
  or move. A patch now edits each touched child list once and writes it
  to the store at the end, so one transaction that appends n nodes is
  linear; a single-operation patch still costs the length of the slot,
  because the store keeps child lists as immutable arrays.
- **A tree thousands of levels deep overflowed the call stack** in
  `LocalDoc` (snapshot load, `toSnapshot`, `descendants`, delete) and in
  `NodeStore.loadSnapshot` / `applyPatch`. Every tree walk is iterative.

### Added

- **Performance suite.** `test/perf/bench.test.ts` (run with `BENCH=1`)
  sweeps Slicer-like scenes across sizes and reports per-item cost and
  the scaling ratio; `test/perf/scaling.test.ts` runs in the normal
  suite and fails if any core operation turns quadratic, plus a
  5000-deep chain round trip.

## 0.4.1

Fixes from an outside review of the synchronization layer: the thick
client now reconciles its echoes and masks remote writes under pending
edits instead of skipping and applying blindly. Requires atomdoc >= 0.4.0
(every patch carries the request `ref`); atomdoc 0.4.1 adds the matching
`ListenerError` semantics (change listeners are post-commit observers).

### Fixed

- **A throwing change listener left the document inconsistent with what
  other listeners had seen.** Change listeners are now post-commit
  observers: every listener runs, the commit stands, events are copies,
  and failures are thrown afterwards as `ListenerError` (with `errors`
  and `cause`). An undo whose observer fails is not put back.
- **Concurrent edits could diverge for good.** A client that wrote a
  field, received an earlier host-side or remote write to the same field,
  and then skipped its own echo stayed at the remote value while the
  server kept the client's. An echo is now reconciled instead of
  skipped: state fields are set to the echoed values and inserted nodes
  are moved to the neighbors the server recorded, so the local document
  converges on the server's order. A convergence harness
  (`test/integration/convergence.test.ts`) drives a real thick client
  against the real Python session while a fake device commits host-side
  changes, with disjoint and overlapping ownership of fields and nodes.
  A remote write to a field (or a move of a node) that a pending local
  edit also touches is masked rather than applied, since the server's
  final state is the local edit's; the undo entry for that edit is
  refreshed so undoing it reveals the remote value (`UndoManager.
  refreshOriginal`). The harness checks on every patch that a field with
  a pending write shows the pending value.
- **Another client's acknowledgment could retire this client's pending
  work.** Refs were `op-N` in every client, and a patch's `ref` was
  matched before its source was checked. Refs are now
  `<client_id>:<n>` and only a ref this client minted can match.

## 0.4.0

Thick-client parity with atomdoc (Python) 0.4.0, which ports the DocNode v0.4
undo and lifecycle improvements from DocuKit. No wire protocol changes; works
with atomdoc >= 0.3.0.

### Changed

- **Server undo is per-client by default.** atomdoc 0.4.0's `Session`
  keeps an undo history per connected client, so a thin client's `undo`
  reverts only its own commits — the same rule the thick client's local
  undo already follows. A step that no longer applies comes back as
  `error` code `rejected` with no snapshot; a session with undo disabled
  answers `unsupported`. See PROTOCOL.md.

### Fixed

- **`abort()` applied inverse operations in the wrong order** and bypassed
  the reverse reference index. A throwing transaction body could delete a
  pre-existing node for good, and after any rollback a still-referenced
  node could be deleted. Rollback now runs in reverse through the tracked
  mutators.
- **A duplicate node ID was accepted and silently shadowed a live node.**
  Rejected now, as is the same node twice in one insert.
- **`NodeStore.loadSnapshot` notified nobody**, so a UI rendered stale
  data after a resync. Every subscriber is notified.
- **An accepted op in flight behind a rejected one was lost.** Its echo
  arrived after the resync snapshot and was skipped as a self-echo. Echoes
  are now matched by the request `ref` the server returns on every patch:
  a patch is skipped only when it carries the `ref` of a pending op and
  `source_client` is this client; everything else applies.
- **Offline edits were never replayed and `onOnline` never fired.**
  Buffered operations are replayed as fresh local transactions once the
  reconnect snapshot lands.
- **The Zod converter dropped array/object defaults and enforced no
  constraints.** It now handles `enum`, `const`, string and numeric
  bounds, `prefixItems`, `additionalProperties`, `required`, and
  discriminated unions, and a `null` default makes a field nullable.
  `defineNode` fields take `nullable: true`; `defineValue` exports a
  `required` list. The `defineNode` docstring example now validates.
- **One failing op in an `applyOperations` batch rolled back the whole
  open transaction and let the remaining ops commit on their own**,
  without the batch's `skipUndo` flag — so a remote patch could enter
  local undo history, and an undo could strand a step. A nested
  transaction failure now propagates and only the outermost boundary
  rolls back; a batch is atomic; an undo that cannot apply throws and
  keeps its step.
- **A handle to a node that was deleted and restored (undo, rollback)
  went stale**; the restore now revives the same object, and a stale
  object is refused rather than corrupting the tree.
- **`insertIntoSlot` accepted an unknown slot**, leaving a node reachable
  by ID but absent from every snapshot. Unknown slots, unknown fields
  and unknown operation codes are rejected.
- **`handles()` missed handles held in array and map fields.**
- **A late `close` from a replaced socket knocked the live connection
  offline for good**, and operations in flight when a socket dropped
  were discarded. Events from a socket that is no longer current are
  ignored; in-flight operations are re-queued ahead of offline edits and
  replayed on reconnect; `onOnline` fires after the reconnect snapshot.
- **A store subscriber was dead once its node left a snapshot** even if
  the node came back; listeners are kept until unsubscribed.
- **A Python-legal regex (`(?P<name>...)`, `\A`, `\Z`) threw out of the
  schema registry**, and `allOf` / `type: [...]` accepted anything.
  Patterns are translated and, if still not compilable, left
  unconstrained; `allOf` intersects; a type list is a union; a
  discriminator whose tag has a default is still detected.
- **Remote patches polluted local undo.** `ThickAtomDocClient` applied patches
  from other clients through the same path as local edits, so undo could
  revert another user's change. Remote patches are now applied with
  `{ skipUndo: true }`.
- **Move replay lost position.** Applying a move operation (from undo/redo or
  a remote patch) ignored the recorded `prev`/`next` siblings and always
  appended to the target slot. Moves now land where they were recorded.
- **Undo stack eviction was inverted.** A full stack discarded the new entry
  instead of the oldest one.

### Added

- **Node ID sessions carry 5 random characters instead of 3**, matching
  atomdoc 0.4.0. Same-millisecond session collisions drop from 1 in
  262,144 to 1 in ~1.07 billion; IDs grow by two characters.
- **References.** Field type `"ref"` in `defineNode` (`target`, `many`),
  exported as tier `"ref"` plus a `refs` block, matching atomdoc 0.4.0's
  `Ref[T]`. `SchemaRegistry.getRefs()` / `getRef()`. `LocalDoc` keeps a
  reverse index (`referrers(nodeId, field?)`, rebuilt from the snapshot) and
  checks referential integrity at commit: references must resolve to a node
  of the declared type, and a referenced node cannot be deleted (policy
  `restrict`). Violations throw `RefIntegrityError` and roll the
  transaction back. Moving a node is not a delete.
- **Handles.** `defineHandle(name, strength)`; node types export a
  `handles` block and value types a `handle.strength`;
  `SchemaRegistry.getHandles()`; `LocalDoc.handles(strength?)` lists the
  document's dependencies without resolving them.
- **Tagged unions.** `SchemaRegistry` builds `z.union` for `anyOf` /
  `oneOf` properties (Python unions of frozen value types, Optionals).
- **Resync after rejection.** The server now answers a request that is
  invalid against the current document with error code `rejected` followed
  by a fresh `snapshot`. `ThickAtomDocClient` rebuilds the local doc, store,
  and undo history from any snapshot received after the first, drops pending
  ops, and fires `onResync`.
- **Transaction flags.** `ChangeEvent.flags` (`TransactionFlags`);
  `LocalDoc.applyOperations(ops, { skipUndo: true })` runs operations in a
  transaction the undo manager ignores (any open transaction is committed
  first).
- **Merge interval.** `new UndoManager(doc, maxSteps, { mergeInterval })` and
  `ThickClientOptions.mergeInterval` (ms) collapse consecutive local
  transactions into one undo step. Off by default.
- **Undo history transfer.** `undoManager.exportHistory()` /
  `importHistory()` move undo and redo state between documents with the same
  ID and root type — for example when a newer snapshot replaces the document.
- `LocalDoc.moveRangeRelative(startId, endId, targetId, "before" | "after")`
  and `ThickAtomDocClient.moveNodeRelative(nodeId, targetId, position)`.
- `mergeOperations(...ops)`, `UndoManager.clear()`, `UndoManager.isEnabled`
  (`maxSteps: 0` disables undo).

### Changed

- `LocalDoc.moveRange` is a no-op (no change event) when the range is
  already at the end of the target slot, and validates that the slot exists.

## 0.3.0

### Breaking Changes

- **Wire protocol**: state values in `op` and `patch` messages are now native
  JSON rather than JSON-stringified strings. `op`/`patch` now match the
  encoding already used by `snapshot` and `create`. Opaque/bytes fields
  continue to travel as base64-encoded JSON strings; receivers decode based
  on the field's schema tier. Requires atomdoc (Python) >= 0.3.0.
- `WireOperations.state` typed as `Record<string, Record<string, unknown>>`.
- Fixed move-op reactivity bug in `applyPatch`: the thin-client patch applier
  now creates a new node object when updating `parentId`/`slotName` during a
  move, instead of mutating in place. Reactive subscribers using reference
  equality will now detect the change.

### Migration

If you hand-build state patches in tests or tooling, drop the `JSON.stringify`
wrapping of values. Receivers that were calling `JSON.parse` on values can
remove the call.

## 0.2.0

### Breaking Changes

- Package renamed from `atomdoc-client` to `atomdoc-ts`. Update your imports accordingly.

### Added

- **Schema definition system:** `defineNode()`, `defineValue()`, and `buildSchema()` let you define document schemas directly in TypeScript. The output uses the same wire format as Python's `@node` decorator and `doc.atomdoc_schema()`, so schemas defined in TS are fully compatible with the Python server.
- **Thick client:** `ThickAtomDocClient` -- offline-capable client with local-first operations, local undo/redo, and automatic sync on reconnect.
  - `LocalDoc` -- linked-list tree model with O(1) insert/delete.
  - `DocNode` -- tree node with parent/sibling pointers.
  - Local operations with forward/inverse tracking for undo.
  - Local transactions with commit/abort.
  - `UndoManager` -- per-client undo/redo stack.
  - `bridgeDocToStore()` -- projects `LocalDoc` changes into `NodeStore`.
  - `createNodeIdFactory()` -- Lamport timestamp ID generation for offline node creation.
- **Full test suite:** 175+ tests covering thin client, thick client, schema definition, integration tests against the Python server, and schema compatibility tests between TS and Python.

### Existing (from 0.1.0)

- **Thin client:** `AtomDocClient` (WebSocket), `NodeStore`, `applyPatch`, operation constructors (`setField`, `deleteNode`, `moveNode`, `createNode`, `undo`, `redo`), `SchemaRegistry` (Zod validators), `Transaction`.

## 0.1.0

- Initial release as `atomdoc-client`.
- Thin client: `AtomDocClient`, `NodeStore`, `SchemaRegistry`, `Transaction`, patch applier, operation constructors.
