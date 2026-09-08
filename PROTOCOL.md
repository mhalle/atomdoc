# AtomDoc Client Protocol Guide

This document describes everything needed to build an AtomDoc client in any language or framework. It covers the wire protocol, the client architecture, and the patterns that make it work.

## Overview

An AtomDoc client connects to a Python server over WebSocket. The server is authoritative — it holds the document, validates operations, and manages undo. The client renders the document and sends user edits as operations.

There are two client architectures:

- **Thin client** — sends operations to the server, waits for patches. Simple: a store, a patch applier, and senders.
- **Thick client** — keeps a local copy of the document. Field writes apply locally at once; structural edits apply when the server confirms them. Local undo.

Both expose the same reactive store to the UI layer.

## Connection Lifecycle

```
1. Client opens WebSocket to server
2. Server sends: schema message
3. Server sends: snapshot message (includes client_id)
4. Client is ready — UI can render

5. On user edit: client sends op/create message
6. Server broadcasts: patch message to ALL clients
7. Thin client: applies patch to store → UI updates
   Thick client: applies every patch; one carrying its own `ref` confirms a request → UI updates

8. On disconnect: thick client buffers ops locally
9. On reconnect: server sends fresh schema + snapshot
```

## Wire Protocol

All messages are JSON objects with a `type` field.

### Server → Client

#### `schema`

Sent once on connect. Contains the full document schema.

```json
{
  "type": "schema",
  "schema": {
    "version": 1,
    "root_type": "Page",
    "node_types": {
      "Page": {
        "json_schema": {
          "type": "object",
          "properties": {
            "title": { "type": "string", "default": "" }
          }
        },
        "field_tiers": { "title": "mergeable" },
        "slots": {
          "annotations": { "allowed_type": "Annotation", "allowed_types": ["Annotation"] }
        },
        "field_defaults": { "title": "" }
      },
      "Annotation": {
        "json_schema": {
          "type": "object",
          "properties": {
            "label": { "type": "string", "default": "" },
            "color": {
              "type": "object",
              "properties": {
                "r": { "type": "integer", "default": 0 },
                "g": { "type": "integer", "default": 0 },
                "b": { "type": "integer", "default": 0 }
              }
            }
          }
        },
        "field_tiers": { "label": "mergeable", "color": "atomic" },
        "slots": {},
        "field_defaults": { "label": "", "color": { "r": 0, "g": 0, "b": 0 } }
      }
    },
    "value_types": {
      "Color": {
        "json_schema": { "type": "object", "properties": { "r": {}, "g": {}, "b": {} } },
        "frozen": true
      }
    }
  }
}
```

**Schema fields:**

- `node_types` — each node type the document can contain
  - `json_schema` — JSON Schema for the node's state fields
  - `field_tiers` — how each field behaves in merges:
    - `"mergeable"` — independent fields, concurrent edits to different fields merge cleanly
    - `"atomic"` — frozen value, replaced as a unit (e.g., Color)
    - `"opaque"` — binary data (bytes), base64 encoded
    - `"ref"` — a reference to another node in the same document; the value is that node's ID (or an array of IDs)
  - `slots` — named ordered child collections, with allowed child type
  - `field_defaults` — default values for fields
  - `handles` — fields holding a handle to something outside the document (bulk data, another document, an ontology term), keyed by field name: `{ "value_type": "VoxelData", "strength": "strong" | "weak" }`. A `strong` handle must resolve for the document to be usable; a `weak` one need not. Nothing in the protocol resolves handles; the list is what a consumer needs to decide whether it can open the document.
  - `refs` — reference fields (tier `"ref"`), keyed by field name:
    - `target_type` — node type the reference must point at, or `null` for any
    - `many` — `true` when the value is an array of IDs
    - `policy` — delete policy; `"restrict"` means a node that is still referenced cannot be deleted

    ```json
    "refs": { "transform": { "target_type": "Transform", "many": false, "policy": "restrict" } }
    ```

    A reference is association, not ownership: the target lives in a slot
    somewhere and is never deleted through the reference. The schema
    describes the field's shape; the *document* enforces integrity at commit
    (every reference resolves to a node of the declared type; deleting a
    referenced node fails). A thick client should keep a reverse index and
    run the same check locally so a violating transaction is rolled back
    before it is sent. The server rejects one that slips through with an
    `error` (`rejected`, followed by a snapshot) and does not broadcast it.
- `value_types` — frozen compound types (like Color) that are replaced atomically. A handle type carries `"handle": { "strength": ... }`. A field whose type is a union of value types exports as an inlined `anyOf`/`oneOf` (with a `discriminator` when the server declared one); it is still one atomic value on the wire.
- `root_type` — the type name of the root node

#### `snapshot`

Sent once on connect, after schema. Contains the full document state.

```json
{
  "type": "snapshot",
  "doc_id": "01jqp00000000000000000000",
  "version": 5,
  "data": [
    "01jqp00000000000000000000",
    "Page",
    { "title": "Hello" },
    {
      "annotations": [
        ["ann-1", "Annotation", { "label": "First" }, {}],
        ["ann-2", "Annotation", { "label": "Second" }]
      ]
    }
  ],
  "client_id": "e99065b8-8503-4bae-a3f8-a47205a93cbb"
}
```

**Snapshot format (JsonDoc):** `[id, type, state, slots?]`

- `id` — unique node identifier (string)
- `type` — node type name
- `state` — field values (only non-default values included)
- `slots` — optional dict of `{ slot_name: [child, child, ...] }`, each child is another JsonDoc

The root node's `id` is also the document ID.

`client_id` is the server-assigned identifier for this connection. Thick
clients prefix their request `ref`s with it (`<client_id>:<n>`), which is
how a patch is recognized as the answer to one of their own requests.

#### `patch`

Sent after every committed transaction.

```json
{
  "type": "patch",
  "version": 6,
  "operations": {
    "ordered": [
      [0, [["ann-3", "Annotation"]], 0, "annotations", "ann-2", 0]
    ],
    "state": {
      "ann-3": { "label": "Third" }
    }
  },
  "source_client": "e99065b8-8503-4bae-a3f8-a47205a93cbb",
  "ref": "op-17"
}
```

`ref` is the `ref` of the client request that produced the patch (`null`
for a change the host made directly). A request that commits more than
once (a multi-step `undo`, an `op` a server-side normalizer split) produces
one `patch` per commit, all carrying the same `ref`. Requests from one
client are answered strictly in the order they were sent, and every
request that carries a `ref` is answered: by its patches, by an `error`,
or — when it committed nothing (a move to where the node already is, a
write of the value already held, an undo with nothing left to revert) —
by a `patch` to the requester alone at the *current* version, carrying
the `ref`, `source_client: null`, no ordered operations, and (for an
`op`) the stored values of the fields the request wrote. A client must
accept a patch whose version equals its current one.

`source_client` is set only when the patch is the verbatim echo of that
client's `op`: the commit carries exactly the operations sent, with the
root spelled as `0` (a request naming the root by its ID is compared as
`0`). It is `null` whenever the commit differs in any way — a `create`,
`undo` or `redo` result, a value the server coerced (`"7"` sent to an
integer field), an insert or move whose neighbors the server filled in
(an append sent as `prev = next = 0` is recorded after the actual last
node, so it is never verbatim; name the neighbor to get a verbatim echo),
a normalizer's additions — and for a host-side change. Clients match
their requests by `ref`, not by this field. A
client that connects during a commit receives the change either in its
snapshot or as a patch after it, never both.

`version` is a monotonically increasing integer.

#### `error`

Sent to one client when its request could not be applied. Nothing is broadcast.

```json
{ "type": "error", "ref": "op-17", "code": "rejected", "message": "Cannot delete node 'x': still referenced by Volume.transform on node 'y'" }
```

`code` is one of:

- `unknown_type` — unrecognized message type.
- `invalid_op` — malformed request: missing fields, an unknown node
  type, an unknown operation code, a field the node's type does not
  have, an unknown `create` position. Nothing was applied and no
  snapshot follows; resending the same frame will fail the same way.
- `unsupported` — a request kind this session refuses (`undo`/`redo`
  when the session's undo policy is `none`).
- `rejected` — a well-formed request that is invalid against the current
  document: a reference that does not resolve, a node that is still
  referenced, a validation failure, a target that another client deleted
  first, or an undo step that fails validation or referential integrity.
  The server rolled the failing request back and sent no `patch` for it.
  For an `op` or `create` a `snapshot` follows immediately, for this
  client only, so a thick client (which may hold a field write it applied
  locally) can replace its local document with the server's state; thin
  clients simply reload the store. For an `undo`/`redo` nothing follows:
  the client applied nothing itself and the failing step is kept. A
  multi-step `undo` applies (and broadcasts) the steps before the failing
  one; only that one is rolled back.

`ref` is the request's `ref`, or `null` when it sent none.

A request that a host-side change listener fails on after the commit
(the document's `ListenerError`) is applied and broadcast normally; the
failure is logged on the server and no error reaches the client.

### Client → Server

#### `op` — Apply operations

```json
{
  "type": "op",
  "ref": "msg-123",
  "operations": {
    "ordered": [],
    "state": { "node-id": { "title": "New Title", "color": { "r": 255, "g": 0, "b": 0 } } }
  }
}
```

#### `create` — Create a new node

```json
{
  "type": "create",
  "ref": "msg-124",
  "node_type": "Annotation",
  "state": { "label": "New", "color": { "r": 255, "g": 0, "b": 0 } },
  "parent_id": "root-id",
  "slot": "annotations",
  "position": "append",
  "target_id": null
}
```

`position` is one of: `"append"`, `"prepend"`, `"before"`, `"after"`. If `"before"` or `"after"`, `target_id` specifies the reference node.

The server assigns the node ID. The client learns it from the resulting patch.

#### `undo` / `redo`

```json
{ "type": "undo", "ref": "msg-125", "steps": 1 }
{ "type": "redo", "ref": "msg-126", "steps": 3 }
```

`steps` defaults to 1 if omitted and must be a positive integer.

What these revert depends on the session's undo policy. By default
(`per-client`) the server keeps a history per connected client and an
`undo` reverts only that client's own commits; another client's edits are
untouched, and the client's redo survives them. A step whose targets are
gone (someone else deleted what it would revert) applies as far as it
can and is consumed: there is nothing left to retry. A step that fails
validation or referential integrity is answered with an `error` of code
`rejected` and kept for a retry; no snapshot follows, since the client
applied nothing itself. A client with nothing to undo, or whose step
applied nothing, gets an empty `patch` at the current version carrying
its `ref` (nothing, if it sent none). Thick clients never send these
messages (nor `create`): they undo locally
and send every change as an `op` with client-minted node IDs, because a
thick client needs an ID before the echo to anchor its next edit. Under the `global` policy an `undo` reverts the
document's last commit, whoever made it. Under `none` the request is
answered with code `unsupported`. The history is dropped when the client
disconnects. Thick clients undo locally and never send these messages.

**Note:** `ref` is optional on all client messages. If provided, it is echoed back in the `error` reply and in every `patch` the request produces. Refs must be unique across clients — prefix them with the server-assigned `client_id` (the thick client sends `<client_id>:<n>`) — because the server does not check ownership: a client must only treat a `ref` it minted itself as an acknowledgment of its own pending work.

## Operations Format

Operations have two parts: ordered tree operations and state patches.

```json
{
  "ordered": [ ... ],
  "state": { ... }
}
```

### Ordered Operations

Tree structure changes. Applied in order.

**Insert:** `[0, [[id, type], ...], parent_id, slot_name, prev_id, next_id]`

- `0` — operation type (insert)
- `[[id, type], ...]` — nodes to insert (ID + type name pairs)
- `parent_id` — parent node ID, or `0` for root
- `slot_name` — which slot to insert into
- `prev_id` — insert after this node, or `0`
- `next_id` — insert before this node, or `0`
- If both `prev_id` and `next_id` are `0`, append to end

**Delete:** `[1, start_id, end_id]`

- `1` — operation type (delete)
- `start_id` — first node in the contiguous range to delete
- `end_id` — last node in range, or `0` for single node

**Move:** `[2, start_id, end_id, parent_id, slot_name, prev_id, next_id]`

- `2` — operation type (move)
- Same positioning semantics as insert

### State Patches

Field value changes. Applied after ordered operations.

```json
{
  "node-id": {
    "label": "Third",
    "count": 42,
    "color": { "r": 255, "g": 0, "b": 0 }
  }
}
```

Values are **native JSON** — strings, numbers, booleans, arrays, objects, or `null`. Same encoding used by `snapshot` and `create` messages.

**Opaque fields** (tier `"opaque"`): the value is a JSON string containing base64-encoded bytes. The receiver decodes based on the field's schema tier (not on the shape of the value).

**Reference fields** (tier `"ref"`): the value is a node ID string, an array of ID strings (`many`), or `null`. Nothing is resolved on the wire; the receiver looks the ID up in its own node map.

To apply: use the value as-is for mergeable/atomic/ref fields; `base64` decode for opaque fields.

### The `0` Sentinel

The integer `0` is used as a null marker throughout operations:

- `parent_id = 0` → the root node
- `prev_id = 0` → no previous sibling (insert at start or append)
- `next_id = 0` → no next sibling (append)
- `end_id = 0` → single node (not a range)

## Building a Thin Client

A thin client needs five components:

### 1. Node Store

A flat map of nodes with subscriptions for reactivity.

```
NodeStore:
  nodes: Map<string, StoreNode>
  rootId: string

  getNode(id) → StoreNode | null
  getRoot() → StoreNode | null
  getChildren(nodeId, slotName) → string[]

  subscribe(nodeId, callback) → unsubscribe
  subscribeAll(callback) → unsubscribe
```

Each `StoreNode` is:

```
StoreNode:
  id: string
  type: string
  state: Map<string, any>       # field values
  slots: Map<string, string[]>  # slot name → ordered child IDs
  parentId: string | null
  slotName: string | null
```

**Critical:** When updating a node's state or slots, replace the StoreNode object with a new one (immutable update). UI frameworks detect changes via reference equality. Mutating in place will break reactivity.

```
# Wrong — mutates in place, framework won't detect change
node.state["title"] = "New"

# Right — replace with new object
nodes[id] = { ...node, state: { ...node.state, title: "New" } }
```

### 2. Snapshot Loader

Parse the `snapshot` message's `data` field (JsonDoc) into the store.

```
function loadSnapshot(data: JsonDoc):
  clear the store
  rootId = data[0]
  recursively walk data:
    for each [id, type, state, slots?]:
      create StoreNode with id, type, state
      set parentId and slotName from parent context
      if slots:
        for each slot_name, children:
          node.slots[slot_name] = [child IDs]
          recurse into children
      store.set(id, node)
```

### 3. Patch Applier

Apply a `WireOperations` object to the store.

```
function applyPatch(store, operations):
  batch notifications (defer until all changes applied):

    for each ordered op:
      if op[0] == 0:  # Insert
        create new StoreNodes for each [id, type] pair
        find parent node
        insert child IDs into parent's slot at the right position:
          if prev_id is in the slot: insert after prev_id
          elif next_id is in the slot: insert before next_id
          else: append

      if op[0] == 1:  # Delete
        find start and end nodes
        remove their IDs from parent's slot
        recursively remove nodes and all descendants from store

      if op[0] == 2:  # Move
        remove from old parent's slot
        update parentId/slotName on moved nodes
        insert into new parent's slot at position

    for each state patch { nodeId: { field: value } }:   # native JSON
      node = store.get(nodeId)
      node.state[field] = value
      replace node in store (new object)

  flush notifications
```

### 4. Message Handler

Connect to WebSocket, route messages.

```
on "schema":  store the schema for later use
on "snapshot": store client_id, load snapshot into store, fire "connected"
on "patch":   apply patch to store, update version
on "error":   fire error callbacks
```

### 5. Operation Senders

Functions that build and send client messages.

```
setField(nodeId, field, value):
  send { type: "op", operations: { ordered: [], state: { nodeId: { field: value } } } }

createNode(type, state, parentId, slot, position):
  send { type: "create", node_type: type, state, parent_id: parentId, slot, position }

deleteNode(nodeId):
  send { type: "op", operations: { ordered: [[1, nodeId, 0]], state: {} } }

moveNode(nodeId, parentId, slot, prevId, nextId):
  send { type: "op", operations: { ordered: [[2, nodeId, 0, parentId, slot, prevId ?? 0, nextId ?? 0]], state: {} } }

undo(steps):
  send { type: "undo", steps }

redo(steps):
  send { type: "redo", steps }
```

## Building a Thick Client

A thick client adds a local document model between the UI and the network.
The local document is a replica of the server's: field writes apply to it
at once (and are confirmed by their echo), structural edits are sent and
applied when the server echoes them, so the tree only ever holds the
server's order. It is not a peer in a collaborative session — there is no
reconciliation of concurrent structural edits — and it is not meant for
offline use: edits made while disconnected are sent, in order, once the
reconnect snapshot is in, and appear as the server confirms them.

### Additional Components

#### 6. DocNode (Linked-List Tree Node)

The thick client needs a proper tree with O(1) insert/delete. Each node has:

```
DocNode:
  id: string
  type: string
  state: Map<string, any>
  parent: DocNode | null
  slotName: string | null
  prevSibling: DocNode | null
  nextSibling: DocNode | null
  slotFirst: Map<string, DocNode | null>   # first child per slot
  slotLast: Map<string, DocNode | null>    # last child per slot
  slotOrder: string[]                       # ordered slot names from schema
```

This is a doubly-linked sibling list per slot, with parent pointers.

#### 7. Local ID Generation

Every node a thick client creates gets its ID here, before the server
has seen it, so the next edit can refer to it. Port of the Lamport
timestamp system.

Format: `{sessionId}.{clock}`

- `sessionId` = base64(elapsed_ms_since_doc_creation) + random(5 chars)
- `clock` = monotonically incrementing base64 counter, starts at `"-"` (first char of alphabet)

Base64 alphabet (RFC 4648 §5, lexicographically sorted):
```
-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz
```

The document ID is a ULID. Extract its millisecond timestamp from the first 10 Crockford base32 characters.

#### 8. Operation Tracking

When the local doc mutates, record forward operations (for sending to server) and inverse operations (for undo).

**State tracking:**

```
onSetStateInverse(node, key):
  if node was inserted this transaction: skip
  if we already recorded this key's original: skip
  save current value as the inverse

onSetStateForward(node, key):
  record the new value
  if it reverted to the original: remove from forward ops
```

**Insert/delete/move tracking:**

Each tree mutation records:
- Forward op: the operation tuple (insert/delete/move)
- Inverse op: the reverse operation (delete→insert, insert→delete, move→move-back)

These accumulate in `forwardOps` and `inverseOps` accumulators during a transaction.

#### 9. Transactions

```
withTransaction(doc, fn):
  if doc is idle:
    set stage to "update"
    run fn()
    on success: forceCommit()
    on error: abort()
  if doc is already updating:
    just run fn() (join existing transaction)
```

`forceCommit()`:
1. Reverse the ordered inverse ops (they were built in reverse order)
2. Fire `onChange` listeners with `{ operations, inverseOperations, diff }`
3. Clear accumulators

`abort()`:
1. Apply the inverse operations to undo all changes
2. Clear accumulators

#### 10. UndoManager

Every change event carries the flags of the committed transaction
(`flags.skipUndo`). Another client's changes are applied with `skipUndo`
so they never enter this client's undo history; a `skipUndo` transaction
is isolated (an open transaction is committed first). The echo of this
client's own structural edit is applied *without* it: see §12 for how
such a commit takes the place reserved for it, and how an undo step
containing structure is dispatched and committed later.

```
UndoManager(doc, maxSteps = 100, { mergeInterval = 0, clock = Date.now, dispatch? }):
  undoStack: list of { operations: WireOperations, meta }
  redoStack: list of { operations: WireOperations, meta }
  txType: "update" | "undo" | "redo"
  lastUpdate: timestamp of the last recorded local transaction

  on doc.onChange(event):
    if event.flags.skipUndo: return
    if txType == "update":
      if lastUpdate is set and clock() - lastUpdate < mergeInterval:
        merge inverseOps into the top undo item (newest inverse first)
      else:
        if undoStack is full: drop the OLDEST item
        push inverseOps to undoStack
      clear redoStack; lastUpdate = clock()
    if txType == "undo": push inverseOps to redoStack
    if txType == "redo": push inverseOps to undoStack

  undo():
    doc.forceCommit()
    pop from undoStack; txType = "undo"; lastUpdate = unset
    doc.applyOperations(popped)
    # onChange fires, pushes to redoStack

  redo():
    same with redoStack / txType = "redo"

  exportHistory() -> { docId, docType, undoStack, redoStack, lastUpdate? }
  importHistory(history):   # docId and docType must match; truncated to maxSteps

  # For commits that arrive later than the user's action (§12):
  reserve() -> id           # hold the next place in history; clears redo;
                            # canUndo is false while the newest step is a placeholder
  commitInto(id, fn)        # fn's commit fills the placeholder (or merges it into
                            # the step before, if reserved within mergeInterval);
                            # an empty commit removes it
  cancel(id)                # the request was refused: drop the placeholder
  dispatch(ops, kind, token) -> bool   # option: take an undo/redo step over
  commitAs(token, fn)       # fn's commit is that step: filed on the opposite stack,
                            # or discarded if a newer local step made an undo's redo stale
  refreshOriginal(source, nodeId, key, value)  # see "Masking" in §12
```

Replaying a move operation must honor its `prev_id` / `next_id` (after
prev, else before next, else append), otherwise undoing a move that landed
mid-slot restores it at the end.

#### 11. Store Bridge

Projects LocalDoc changes into the NodeStore:

```
bridgeDocToStore(doc, store, {coalesce = true}):
  store.loadSnapshot(doc.toSnapshot())
  doc.onChange(event =>
    if coalesce: queue event.operations; schedule flush on the next frame
    else: applyPatch(store, event.operations))
  flush():
    store.batch(for ops in queue: applyPatch(store, ops))
```

Reuses the same `applyPatch` as the thin client. The document is always
current; the store is the UI's view of it and, by default, catches up
once per animation frame (`requestAnimationFrame`, with a short timer
fallback so a hidden tab still flushes; a macrotask where there is no
frame API). A device streaming patches or a transaction touching many
nodes therefore notifies each subscriber once per frame rather than once
per patch. `flush()` on the handle (or `client.flushStore()`) applies the
queue immediately; disposing the bridge discards it, which is what a
resync does before loading the new snapshot.

Note that neither a snapshot nor a `create` patch carries fields at
their default (an insert `op` carries whatever state its sender put in
it), so a thin `NodeStore` holds `undefined` for a defaulted field. Read
defaults through `SchemaRegistry.getDefaults()` rather than comparing
raw store state.

#### 12. Self-Echo Handling

The server broadcasts patches to ALL clients, including the source. A
patch carrying one of the client's own `ref`s answers that request:

```
on patch message:
  update version
  entry = pending op msg.ref names, if any
    (forget it and every pending op before it: the server answers in order)
  if entry was applied locally (a field write):
    apply the state part, masked by later pending writes, with skipUndo
    (plus any node the server inserted alongside — a normalizer's)
  elif entry was built and sent unapplied (create / delete / move,
      an undo or redo step containing one):
    apply msg.operations as this client's own commit: it enters undo
    history (as the undo or redo step it was sent as, if it was one)
    but is not sent again
  else:
    doc.applyOperations(msg.operations, skipUndo)  # a remote change
```

Ownership is the `ref`: only a ref this client minted can match. Do not
require `source_client` as well — the server sets it only when it recorded
the request verbatim, and it is `null` when a normalizer changed something
or the request committed nothing. Matching by `ref` rather than by count
matters when one request produces several patches and after a resync,
when the pending list was dropped and a late echo must be applied as a
remote change. An `error` carrying a pending `ref` forgets that op too.
The `client_id` comes from the `snapshot` message.

**Confirmed structure.** Create, delete, and move are built from the
local tree — parent, slot, and the neighbors the node should sit between
— sent, and applied when their echo arrives, exactly as the server
recorded them. The local tree therefore never holds an order the server
did not: a device appending into the same slot moments earlier lands
first on both sides. Consecutive edits compose before any echo returns:
the client anchors each new edit against a model of the structure still
pending (two appends in a row follow each other; a child can be created
under a parent that is itself pending; a field written on a pending node
is sent and lands with it). `createNode` returns the ID at once; the node
appears on echo, and `pendingStructure()` says whether anything is still
waiting.

The confirmation of an *applied* op replays only what the client lacks:
inserts of nodes it does not have (a normalizer's additions, each
re-anchored after the node before it in the echo), plus the echo's moves
and deletes, which change nothing against a document that already
applied them. An `op` made on the `LocalDoc` directly is such an applied
op: it is sent, but never reconciled against concurrent structure.

**Masking remote writes under pending field writes.** A remote patch that
arrives while one of our field writes is pending was committed before
that write, so for that field the server's final value is ours, and the
remote value is only an intermediate the server passed through. Applying
it would show the wrong value until our echo arrived. So before applying
a remote patch, drop every state entry whose node and field a pending
write also sets. If the server rejects our write instead, the resync
snapshot brings the remote value in.

A masked write still matters to undo: undoing our edit should leave the
field at what others last wrote, not at what we saw before editing. The
client refreshes the recorded original of the oldest pending edit of
that field to the masked value, in the undo entry that edit landed in
(merged entries included).

**Requests that commit nothing.** A move to where the node already is,
or a write of the value already held, commits nothing and so produces no
echo. The server answers the requester alone with a `patch` at the
*current* version carrying the request's `ref`, `source_client: null`,
no ordered operations, and the stored values of the fields the request
wrote. A client must accept a patch whose version equals its current one.
Such an answer retires the request; a built op it answers applies
nothing.

**Undo.** Structural edits enter history in the order the user made
them, not the order their echoes arrive: when a built op is sent, the
undo manager reserves the next place (`reserve`), which its echo fills
(`commitInto`); a reservation made within the merge window joins the
step before it; a refused request cancels its reservation. `canUndo` is
false while the newest step is still a reservation. A step that only
writes fields applies locally like any edit. A step that contains
structure is dispatched: sent unapplied like any structural edit and
committed, as that step, when its echo arrives (the inverse it produces
then goes on the opposite stack). A dispatched undo overtaken by a newer
local step is applied but files no redo, since the newer step already
invalidated it; a dispatched step the server had nothing to apply for,
or refused, is consumed. If the socket drops before an echo, pending ops
are kept ahead of anything buffered since and sent again after the
reconnect snapshot; the snapshot rebuilds the document and drops the undo
history.

The thick client's API is narrower than the wire format on purpose:
`createNode` takes `"append"` or `"prepend"`, `moveNode` appends, and
`moveNodeRelative` places a node before or after a sibling. All of them
anchor on the projected order, so a relative move against a node whose
own move is still pending is placed where both will end up.

Together these keep one client converged with host-side changes (a
device writing into the session's document) and with other clients under
the server's total order. `test/integration/convergence.test.ts` and
`test/integration/two-clients.test.ts` check this with a device and with
two thick clients editing the same fields and slots.

#### 13. Remote Echo Guard

When applying a remote patch to the local doc, the `onChange` listener must NOT send it back to the server:

```
applyingRemote = false

doc.onChange(event => {
  if (!applyingRemote):
    sendToServer(event.operations)
})

on remote patch:
  applyingRemote = true
  doc.applyOperations(patch.operations)
  applyingRemote = false
```

## UI Framework Integration

The NodeStore is framework-agnostic. Each framework needs a thin adapter.

### Pattern: Version-Counter Hook

The recommended pattern for any framework:

1. Subscribe to the node ID in the store
2. When notified, increment a local version counter
3. Return an accessor/getter that reads the version (for reactivity) then reads the store

This avoids writing to reactive state during subscription setup, which causes loops in some frameworks.

### Solid.js

```ts
function useNode(store, nodeId) {
  const [ver, setVer] = createSignal(0);
  const unsub = store.subscribe(nodeId(), () => setVer(v => v + 1));
  onCleanup(() => unsub());
  return () => { ver(); return store.getNode(nodeId()); };
}
```

### React

```ts
function useNode(store, nodeId) {
  const ref = useRef(undefined);
  return useSyncExternalStore(
    (cb) => store.subscribe(nodeId, cb),
    () => {
      const node = store.getNode(nodeId);
      if (ref.current === node) return ref.current;
      ref.current = node;
      return node;
    },
  );
}
```

Note: `useSyncExternalStore` requires the snapshot to be referentially stable when unchanged. Cache the previous result in a ref.

### Vue

```ts
function useNode(store, nodeId) {
  const node = ref(store.getNode(nodeId));
  const unsub = store.subscribe(nodeId, () => {
    node.value = store.getNode(nodeId);
  });
  onUnmounted(() => unsub());
  return node;
}
```

### Svelte

```ts
function useNode(store, nodeId) {
  const node = writable(store.getNode(nodeId));
  const unsub = store.subscribe(nodeId, () => {
    node.set(store.getNode(nodeId));
  });
  onDestroy(() => unsub());
  return node;
}
```

### Qt (Python)

```python
class NodeProxy(QObject):
    changed = Signal(str)

    def __init__(self, store, node_id):
        super().__init__()
        self._store = store
        self._node_id = node_id
        store.subscribe(node_id, lambda: self.changed.emit(node_id))
```

### Terminal / CLI

No reactivity needed. Just read the store after each patch.

```python
client.on_patch(lambda v: render(client.get_store()))
```

### LLM

No UI at all. The LLM receives snapshots or patches as context, reasons about them, and emits operations.

```
System: Here is the document schema: { ... }
System: Here is the current document: { ... }
User: Add a red annotation labeled "Important"
LLM: { "type": "create", "node_type": "Annotation", "state": { "label": "Important", "color": { "r": 255, "g": 0, "b": 0 } }, "parent_id": "root-id", "slot": "annotations", "position": "append" }
```

## Key Patterns

### Immutable Store Updates

When the store updates a node, it must produce a **new object reference**. UI frameworks use reference equality to detect changes. If you mutate in place, the framework won't re-render.

```
# On state update:
oldNode = store.get(id)
newNode = clone(oldNode)
newNode.state[field] = value
store.set(id, newNode)
notify(id)
```

### Batch Notifications

When applying a patch with multiple operations, defer notifications until all changes are applied. Otherwise, intermediate states cause unnecessary re-renders and potential inconsistencies.

```
store.batch(() => {
  applyInsert(...)
  applyInsert(...)
  updateState(...)
})
# All subscribers fire once here
```

### Draft Pattern

For form-style editing where changes should commit atomically:

```
draft = { label: node.label, color: node.color }   # local copy
# User edits draft freely (no operations sent)
# On "Apply": send all draft fields as one op
# On "Reset": restore draft from current node state
```

This gives you:
- Smooth editing (no round-trips during slider drag)
- Atomic commit (one undo step for all field changes)
- Cancel support (reset discards uncommitted changes)

### Keyed Conditional Rendering

When showing a detail view for a selected item, use keyed/conditional rendering so the component remounts when the selection changes:

```
# Solid
<Show when={selected()} keyed>
  {(id) => <Inspector nodeId={id} />}
</Show>

# React
{selected && <Inspector key={selected} nodeId={selected} />}
```

Without keying, the framework may reuse the component instance with stale hook state.

## Building a Client in Another Language

The protocol is JSON over WebSocket. Any language with a WebSocket client and JSON parser can implement a thin client. Here's what you need:

1. **WebSocket client** — connect, send JSON, receive JSON
2. **JSON parser** — parse messages
3. **Node map** — `HashMap<String, Node>` or equivalent
4. **Snapshot parser** — recursive walk of nested arrays
5. **Patch applier** — handle insert/delete/move/state ops
6. **Operation builders** — construct JSON messages
7. **Reactivity adapter** — framework-specific subscription mechanism

Languages with existing WebSocket + JSON support (effectively all modern languages):

| Language | WebSocket | Reactivity |
|----------|-----------|------------|
| Python | `websockets` | Qt signals, Tkinter `after()`, asyncio callbacks |
| Rust | `tokio-tungstenite` | Channels, signals crate |
| Swift | `URLSessionWebSocketTask` | `@Observable`, Combine |
| Kotlin | `OkHttp` | `StateFlow`, Compose state |
| Dart | `web_socket_channel` | `ChangeNotifier`, streams |
| C# | `ClientWebSocket` | `INotifyPropertyChanged`, WPF bindings |
| Go | `gorilla/websocket` | Channels |

The thin client is small in any of these. The thick client adds the tree model, transactions, undo, and the pending-structure bookkeeping of §12.

## Troubleshooting

### UI doesn't update after operations

- Check that the store produces **new object references** on mutation (not in-place mutation)
- Check that batch notifications flush at the end of patch application
- Check that the framework adapter correctly triggers re-renders on subscription callbacks

### Infinite loops / stack overflow

- Check echo handling: a thick client must retire the pending op a patch's `ref` names and apply the patch as §12 describes (never as a fresh local edit); do not key on `source_client`
- Check for re-send: `doc.onChange` listener must NOT send operations that came from remote patches (use `applyingRemote` flag)
- Check that subscription callbacks don't trigger store writes that trigger more callbacks

### Undo doesn't work

- Check that `applyOperations` (for undo/redo) routes through tracked methods (insert/delete/move with forward/inverse recording), not raw tree manipulation
- Check that the UndoManager's `txType` flag correctly routes inverse ops to undo vs redo stack
- Check that inverse ordered operations are reversed at commit time (they're built in reverse order)

### Selection / inspector doesn't reflect current node

- Check that the detail view remounts when selection changes (keyed rendering)
- Check that draft state resets when the target node ID changes
