# atomdoc-ts

TypeScript client for the [AtomDoc](../README.md) document protocol. Connect to a Python AtomDoc server (the [`python/`](../python/README.md) package in this repository), render documents reactively, and send operations back. The wire protocol both sides speak is [PROTOCOL.md](../PROTOCOL.md).

> **Note:** This package was previously called `atomdoc-client`. It has been renamed to `atomdoc-ts`.

Two client modes:

- **Thin client** — every operation goes to the server. Simple, no local state beyond the reactive store.
- **Thick client** — keeps a local copy of the document. Field writes apply locally at once; structural edits apply when the server confirms them. Local undo/redo. Store updates coalesce per animation frame.

Both use the same `NodeStore` for reactivity. UI code (React hooks, Solid signals, Vue composables) works identically with either.

## Install

```bash
npm install atomdoc-ts
```

## Quick Start

### Thin Client

```ts
import { AtomDocClient } from "atomdoc-ts";

const client = new AtomDocClient("ws://localhost:8765");

client.onConnected(() => {
  const store = client.getStore();
  const root = store.getRoot();
  console.log("Document loaded:", root.state.title);

  // Read children
  const annotations = store.getChildren(root.id, "annotations");
  for (const id of annotations) {
    const node = store.getNode(id);
    console.log("  -", node.state.label);
  }
});

client.onPatch((version) => {
  console.log("Document updated to version", version);
});

await client.connect();
```

### Thick Client

```ts
import { ThickAtomDocClient } from "atomdoc-ts";

const client = new ThickAtomDocClient({ url: "ws://localhost:8765" });

await client.connect(); // resolves when the socket opens...
await client.ready();   // ...and this once the schema and snapshot are in
// (or do the work inside client.onConnected(() => { ... }))

// Field writes apply at once — no round-trip
const rootId = client.getStore().getRootId();
client.setField(rootId, "title", "Updated");
console.log(client.getDoc()!.root.state.title); // "Updated" immediately

// Structural edits apply when the server confirms them
const id = client.createNode("Annotation", { label: "New" }, rootId, "annotations");
client.onPatch(() => {
  if (!client.pendingStructure()) console.log("confirmed:", client.getDoc()!.getNode(id));
});

// Local undo
client.undo();

client.onOffline(() => console.log("Offline — edits wait for the reconnect"));
client.onOnline(() => console.log("Back online"));
```

## Architecture

```
Python Server (authoritative)
  | WebSocket (schema / snapshot / patch / op / create / undo / redo)
TypeScript Client
  |-- NodeStore        — reactive flat map of nodes, subscriptions
  |-- SchemaRegistry   — Zod validators from server schema
  |-- [thin] direct send/receive
  +-- [thick] LocalDoc — local replica, undo
       +-- bridge -> NodeStore (same reactive API)
```

## Core Concepts

### NodeStore

A flat `Map<id, StoreNode>` with per-node and global subscriptions. Both thin and thick clients expose the same store.

```ts
interface StoreNode {
  id: string;
  type: string;
  state: Record<string, unknown>;
  slots: Record<string, string[]>;  // slot name -> ordered child IDs
  parentId: string | null;
  slotName: string | null;
}
```

Reading:

```ts
const store = client.getStore();

store.getNode(id);                      // single node
store.getRoot();                        // root node
store.getRootId();                      // root ID
store.getChildren(nodeId, slotName);    // ordered child IDs
store.getAllNodeIds();                   // all IDs
client.getState(id);                     // the node's state with schema defaults filled in
                                         // (a snapshot and a patch omit fields at their default)
```

Subscribing:

```ts
// Per-node — fires when this node's state or children change
const unsub = store.subscribe(nodeId, () => {
  console.log("Node changed:", store.getNode(nodeId));
});

// Global — fires on any change
const unsub = store.subscribeAll(() => {
  console.log("Something changed");
});

// Unsubscribe
unsub();
```

### SchemaRegistry

Built automatically from the server's schema-on-connect message. Provides type information and Zod validators.

```ts
const schema = client.getSchema();

schema.nodeTypeNames();                         // ["Page", "Annotation"]
schema.valueTypeNames();                        // ["Color"]
schema.getFieldTier("Annotation", "color");     // "atomic"
schema.getSlots("Page");                        // { annotations: { allowed_type: "Annotation", allowed_types: ["Annotation"] } }
                                                // (an unknown type name gives {} from getSlots and getDefaults)
schema.getNodeType("Page"); schema.getValueType("Color"); schema.getRef("Volume", "transform"); schema.getHandles("Volume");
schema.getRefs("Volume");                       // { transform: { target_type: "Transform", many: false, policy: "restrict" } }
schema.getDefaults("Annotation");               // { label: "", color: { r: 0, g: 0, b: 0 } }

// Validate data against a type
const color = schema.validate("Color", { r: 255, g: 0, b: 0 });

// Get Zod schema for direct use
const zodSchema = schema.getZodSchema("Color");
```

### Schema Definition

Define document schemas directly in TypeScript using `defineNode()`, `defineValue()`, and `buildSchema()`. The generated schema uses the same wire format as the Python `@node` decorator and `doc.atomdoc_schema()`, so schemas defined in TypeScript are fully compatible with the Python server.

```ts
import { defineNode, defineValue, defineHandle, buildSchema } from "atomdoc-ts";

const Color = defineValue("Color", {
  r: { type: "integer", default: 0 },
  g: { type: "integer", default: 0 },
  b: { type: "integer", default: 0 },
}, { frozen: true });

const Annotation = defineNode("Annotation", {
  label: { type: "string", default: "" },
  color: { type: "object", schema: Color, tier: "atomic", default: { r: 0, g: 0, b: 0 } },
});

const Page = defineNode("Page", {
  title: { type: "string", default: "" },
  // A reference to another node in the same document — stored as its ID
  cover: { type: "ref", target: "Annotation", default: null },
  // Several targets: an array of IDs
  related: { type: "ref", target: "Annotation", many: true, default: [] },
}, {
  slots: { annotations: "Annotation" }   // or ["A", "B"] (Python Array[A | B]), or null (any node type),
});

const schema = buildSchema("Page", [Page, Annotation], [Color]);
// schema is identical in format to Python's doc.atomdoc_schema()
```

Handles name things outside the document. `defineHandle(name, strength)`
builds a frozen `uri` / `media_type` / `digest` value type; an `object`
field whose `schema` is such a value type makes the node type export a
`handles` block for it. An `object` field with a `schema` defaults to
tier `"atomic"` (the value is replaced whole, as Python does for frozen
models); every other field defaults to `"mergeable"`.
`doc.handles("strong")` on a `LocalDoc` is the dependency list a consumer
checks before opening a document: objects `{ node, field, handle, strength }`
(the Python `doc.handles()` yields `(node, field, handle)` tuples).

A `ref` field is the TypeScript spelling of Python's `Ref[T]` (`many: true`
for `list[Ref[T]]`). Slots are ownership; references are association and
never control a node's lifetime. `LocalDoc` keeps a reverse index
(`doc.referrers(nodeId, field?)`) and checks referential integrity when a
transaction commits: a reference must resolve to a node of the declared
type, and a node that is still referenced cannot be deleted. A violation
throws `RefIntegrityError` and rolls the transaction back, so re-point the
referrers and delete the old target together: on a `LocalDoc`, in one
`applyOperations` call carrying both (the thick client has no multi-step
transaction API of its own). Moving a node is not a delete.

This is useful for:

- Defining schemas in a shared TypeScript module that both client and tests can use
- Writing integration tests that validate schema compatibility between TS and Python
- Local-only document creation without a server

## Thin Client API

### AtomDocClient

```ts
import { AtomDocClient } from "atomdoc-ts";

const client = new AtomDocClient("ws://localhost:8765");
```

#### Connection

```ts
await client.connect();     // connect and wait for WebSocket open
client.disconnect();        // close connection
```

#### Sending Operations

```ts
// Set a field on a node
client.setField(nodeId, "title", "New Title");

// Set a frozen value (replaced atomically)
client.setField(nodeId, "color", { r: 255, g: 0, b: 0 });

// Create a new node (server assigns ID). Positions: "append", "prepend";
// "before"/"after" need a target_id, so build a `create` message by hand for those.
client.createNode("Annotation", { label: "New" }, parentId, "annotations");
client.createNode("Annotation", { label: "First" }, parentId, "annotations", "prepend");

// Delete a node
client.deleteNode(nodeId);

// Move a node: after prevId if given, else before nextId, else to the end
client.moveNode(nodeId, parentId, "annotations", prevId);
client.moveNode(nodeId, "", "annotations");          // "" or "0" is the root

// Anything else: build the message (see Operation Constructors) and client.send(msg)

// Undo / redo (server-side)
client.undo();
client.redo();
client.undo(3);   // undo 3 steps
```

#### Events

```ts
client.onConnected(() => { ... });          // schema + snapshot received
client.onPatch((version) => { ... });       // document updated
client.onError((err) => {                   // server rejected an operation
  console.error(err.code, err.message);
});
```

### Transactions

Buffer multiple operations and send as one atomic batch (one `op`
message, one undo step). A transaction carries `setField`, `deleteNode`,
and `moveNode`; `createNode` is a separate message type on the wire and
cannot join one (the thick client, which mints node IDs itself, has no
such limit).

```ts
const tx = client.begin();

tx.setField(nodeId, "title", "New Title");
tx.setField(nodeId, "color", { r: 255, g: 0, b: 0 });
tx.deleteNode(otherNodeId);

// All three operations sent as one message, one undo step
client.commit(tx);

// Or discard everything
// tx.abort();
```

Transactions are chainable:

```ts
const tx = client.begin();
tx.setField(id, "a", 1).setField(id, "b", 2).deleteNode(otherId);
client.commit(tx);
```

Check if a transaction has changes:

```ts
if (tx.dirty) {
  client.commit(tx);
}
```

An uncommitted transaction is disposable -- if you lose the reference or navigate away, nothing was sent. No cleanup needed.

### Operation Constructors

For lower-level control, build wire messages directly:

```ts
import { setField, deleteNode, moveNode, createNode, undo, redo } from "atomdoc-ts";

// These return message objects — send them with client.send()
client.send(setField(nodeId, "title", "Hello"));
client.send(deleteNode(nodeId));
client.send(moveNode(nodeId, newParentId, "children"));
client.send(createNode("Annotation", { label: "New" }, parentId, "annotations"));
client.send(undo());
client.send(redo(3));
```

## Thick Client API

### ThickAtomDocClient

```ts
import { ThickAtomDocClient } from "atomdoc-ts";

const client = new ThickAtomDocClient({
  url: "ws://localhost:8765",
  maxUndoSteps: 100,          // optional, default 100; 0 disables undo
  mergeInterval: 500,         // optional ms, default 0; collapses quick edits into one undo step
  coalesce: true,             // optional, default true; store updates once per animation frame
                              // (a number is a window in ms: patches within it notify once, frames or not)
  validate: true,             // optional, default true; a field value the schema rejects throws at setField
});
```

The client uses the global `WebSocket` (browsers, Node 22 and later);
pass `webSocket: WS` (the `ws` package, say) on a runtime without one.
Both clients take that option.

The local document is always current. The store, which the UI
subscribes to, is updated once per animation frame (a macrotask outside a
browser), so a burst of patches that arrive within one frame, or a
transaction touching many nodes, notifies each subscriber once. Patches
spaced further apart than a frame (a device at 50 Hz in Node, where a
frame is one macrotask) each notify; `coalesce: 16` (a window in
milliseconds) batches them regardless of frames. `client.flushStore()` applies
queued changes now, for code that reads the store right after an edit;
`coalesce: false` restores synchronous store updates.

#### Same Read API

```ts
client.getStore();       // NodeStore (same as thin)
client.getSchema();      // SchemaRegistry
client.getVersion();     // server version
```

#### Additional State

```ts
await client.ready();     // resolves once the document is loaded (connect() resolves on socket open)
await client.settled();   // resolves once every edit has been answered (echoed, rejected, or
                          // answered as a no-op) and the store is flushed; edits made while
                          // disconnected count and are answered after the reconnect
client.getState(nodeId);  // the node's state with schema defaults filled in
client.getDoc();          // LocalDoc | null — the local document model (null until the snapshot)
client.getUndoManager();  // UndoManager | null
client.isOnline();        // connection status
client.pendingStructure(); // a structural edit is awaiting confirmation
client.disconnect();      // close the socket; edits queue until connect() again
```

#### Mutations

Field writes apply at once to the local document and are sent; the
server's echo confirms them. Structural edits (create, delete, move) are
built from the local tree, sent, and applied when the server echoes them,
so the local tree only ever holds the server's order. Consecutive edits
compose before any echo returns: two appends in a row keep their order, a
child can be created under a parent that is itself still pending, and a
field written on a pending node lands with it.

```ts
// Instant — no round-trip needed
client.setField(nodeId, "title", "Updated");

// Returns the locally-generated node ID at once; the node appears when confirmed
const newId = client.createNode("Annotation", { label: "New" }, parentId, "annotations");
client.setField(newId, "label", "Renamed");   // sent; lands with the node

client.deleteNode(nodeId);
client.moveNode(nodeId, newParentId, "children");          // append to a slot
client.moveNodeRelative(nodeId, siblingId, "after");        // position next to a sibling

client.pendingStructure();   // true while a structural edit awaits confirmation
client.onPatch(() => { ... }); // fires after every applied patch, echoes included
```

What goes wrong is caught at one of three points, and an editor needs
different handling for each:

| Caught | Cases | What happens |
|---|---|---|
| Locally, synchronous throw | unknown or already-deleted node (including one another client deleted a moment ago), node pending deletion, unknown field or slot, unsupported `createNode` position, `deleteNode` of a node another node still references, `setField` of a ref to a node that does not exist (`RefIntegrityError`, the checks the server also runs) | nothing sent, nothing changed |
| Locally applied, then rejected by the server | a field value that violates a constraint the exported schema does not carry (with `validate: false`, any invalid value), a node type the slot does not accept | `onError` with code `rejected`, then a resync: the local document is rebuilt from the server's snapshot and **this view's undo history is dropped** |
| Rejected by the server only | a structural edit whose target was deleted on the server first | same `error` + resync |

Wrap mutations in `try`/`catch` where other parties edit too. By
default `setField` validates the value against the exported schema
(`ge`, `le`, enums, required fields of a value type) before applying it
and throws a `ZodError`, so an invalid value never reaches the local
document or the server; `validate: false` sends values as given, and
`schema.validateField(type, field, value)` runs the same check by hand.
A field the exported JSON Schema does not describe (a schema built
without `properties`) is not checked; the server still validates it. `createNode` takes `"append"` or `"prepend"` only; place a
node next to a sibling with `moveNodeRelative` afterwards.

#### Local Undo/Redo

Undo and redo are the client's own. Patches received from other clients
are applied with `skipUndo`, so undo only ever reverts this client's own
edits, echoes of its structural edits included. A step that only writes
fields applies at once; a step that contains structure is sent like any
structural edit and takes effect when the server confirms it.

```ts
client.undo();
client.redo();
client.undo(3);  // undo 3 steps at once

// Check availability
client.getUndoManager()!.canUndo;   // false while any step awaits confirmation
client.getUndoManager()!.canRedo;
client.getUndoManager()!.undoDepth; // steps on each stack, reservations excluded
client.getUndoManager()!.redoDepth;
```

Steps enter history in the order the user acted, not the order the
server's echoes arrive: a structural edit holds its place from the
moment it is sent. While any step awaits the server (a structural edit,
or an undo or redo step containing one), `canUndo` and `canRedo` are
both false. A resync (a rejected edit, or a reconnect) rebuilds the
document and drops this client's undo history. The `UndoManager` API behind this (`reserve`,
`commitInto`, `cancel`, the `dispatch` option and `commitAs`) is
described in [PROTOCOL.md](../PROTOCOL.md) §10 and §12; `clear()` drops
the history and `dispose()` detaches the manager from the document.

Undo history can be carried over when a document is rebuilt from a newer
snapshot, as long as the ID and root type match:

```ts
const history = client.getUndoManager().exportHistory();
// ... later, on a new LocalDoc/UndoManager for the same document
undoManager.importHistory(history);
```

The exported history is plain JSON. `lastUpdate`, present when the last
recorded step was an edit rather than an undo or redo, comes from the
exporting manager's clock (`Date.now` by default), so a merge window can
continue across the transfer only when both managers share a clock.
`client.getUndoManager()` is a new object after every resync (rejection
or reconnect): import into the one you get *after* the resync. A
standalone manager is `new UndoManager(doc, maxSteps?, options?)` on a
`LocalDoc`.

On a `LocalDoc` directly, `applyOperations(ops, { skipUndo: true })` runs
operations in a transaction the undo manager ignores, and every
`ChangeEvent` carries `flags.skipUndo`. A `skipUndo` transaction is always
isolated: if one is already open it is committed first, so your own pending
edits keep their undo entry.

#### Events

```ts
client.onConnected(() => { ... });     // initial load complete
client.onPatch((version) => { ... });  // remote change applied
client.onError((err) => { ... });      // server error
client.onResync((info) => { ... });    // server replaced the local doc with a
                                       // fresh snapshot: info.reason is "rejected",
                                       // "reconnect", or "snapshot"; the undo history
                                       // is dropped (info.undoStepsDropped,
                                       // info.redoStepsDropped) and getUndoManager() is new.
client.onOffline(() => { ... });       // connection lost
client.onOnline(() => { ... });        // reconnected
```

#### Disconnection

The thick client is not an offline editor. When the connection drops,
field writes still apply locally and every edit is buffered — including
any that were sent but not yet acknowledged when the socket dropped. On
reconnect the server's snapshot replaces the local document and the
undo history restarts from it: the buffered edits are sent in order and
each appears, as a new undoable step, as the server confirms it; one the
server rejects comes back as a resync. `onOnline` fires after the
snapshot has landed.

```ts
client.onOffline(() => { /* show a banner; edits queue until reconnect */ });
client.onOnline(() => { /* the document is the server's again */ });
```

### LocalDoc (Advanced)

The thick client's local document model is accessible for advanced use
cases. Read it freely. Structural edits made on it directly (its
`insertIntoSlot`, `deleteRange`, `moveRange`) are applied at once and
sent, but their echo is not reconciled against concurrent changes; use
the client's `createNode`/`deleteNode`/`moveNode` for structure.

```ts
const doc = client.getDoc();

// Read the tree
doc.getNode(id);           // DocNode or undefined
doc.root;                  // root DocNode
doc.nodeMap;               // Map<string, DocNode>

// Subscribe to changes
// Listeners are post-commit observers: every one runs, the commit stands,
// and a listener that throws surfaces afterwards as a ListenerError.
doc.onChange((event) => {
  console.log("Forward ops:", event.operations);
  console.log("Inverse ops:", event.inverseOperations);
  console.log("Diff:", event.diff);   // Sets of ids (inserted, moved, updated) and a
                                      // Map of deleted nodes: not JSON-serializable as is
});

// Mutate (each call is its own transaction; several ops in one go: applyOperations).
// insertIntoSlot takes DocNode objects (from doc.getNode / doc.createNode);
// the range methods take ids.
doc.setNodeState(id, "title", "x");
const parent = doc.getNode(parentId)!;
doc.insertIntoSlot(parent, "items", "append", [doc.createNode("Item", { label: "n" })]);
doc.insertIntoSlot(parent, "items", "before", [doc.createNode("Item")], doc.getNode(targetId)!);   // or "after"
doc.deleteRange(startId, endId?);
doc.moveRange(startId, endId, parentId, slot);                   // to the end of the slot
doc.moveRangeRelative(startId, endId, targetId, "before" | "after");
doc.applyOperations({ ordered: [[1, id, 0]], state: { [rootId]: { featured: null } } });

// Serialize
const snapshot = doc.toSnapshot();  // wire format [id, type, state, slots]: byte-identical to
                                    // Python doc.dump() of the same document (defaulted fields
                                    // omitted, fields in schema order), so the two compare directly
```

Reference fields (tier `"ref"`) are resolved through the node map, and the
document answers the reverse question:

```ts
const vol = doc.getNode(volId)!;
doc.getNode(vol.state.transform as string);     // the referenced node
doc.referrers(transformId);                     // nodes that point at it
doc.referrers(transformId, "transform");        // only via that field

doc.deleteRange(transformId);                   // throws RefIntegrityError while referenced
```

## Framework Integration

The `NodeStore` is framework-agnostic. Here are patterns for popular frameworks.

### React

```tsx
import { useSyncExternalStore, useCallback } from "react";
import type { NodeStore, StoreNode } from "atomdoc-ts";

function useNode(store: NodeStore, nodeId: string): StoreNode | undefined {
  return useSyncExternalStore(
    (cb) => store.subscribe(nodeId, cb),
    () => store.getNode(nodeId),
  );
}

function useChildren(store: NodeStore, nodeId: string, slot: string): string[] {
  // getChildren returns the stored array (or one shared empty array), so
  // the snapshot is reference-stable between changes, as React requires.
  return useSyncExternalStore(
    (cb) => store.subscribe(nodeId, cb),
    () => store.getChildren(nodeId, slot),
  );
}

// Usage
function AnnotationView({ store, nodeId, client }) {
  const node = useNode(store, nodeId);
  if (!node) return null;

  return (
    <div>
      <input
        value={node.state.label as string}
        onChange={(e) => client.setField(nodeId, "label", e.target.value)}
      />
      <button onClick={() => client.deleteNode(nodeId)}>Delete</button>
    </div>
  );
}

function PageView({ store, client }) {
  const root = useNode(store, store.getRootId());
  const children = useChildren(store, store.getRootId(), "annotations");

  return (
    <div>
      <h1>{root?.state.title as string}</h1>
      {children.map((id) => (
        <AnnotationView key={id} store={store} nodeId={id} client={client} />
      ))}
      <button onClick={() =>
        client.createNode("Annotation", { label: "New" }, store.getRootId(), "annotations")
      }>
        Add Annotation
      </button>
      <button onClick={() => client.undo()}>Undo</button>
      <button onClick={() => client.redo()}>Redo</button>
    </div>
  );
}
```

### Solid

```tsx
import { createSignal, onCleanup } from "solid-js";
import type { NodeStore, StoreNode } from "atomdoc-ts";

function useNode(store: NodeStore, nodeId: string) {
  const [node, setNode] = createSignal(store.getNode(nodeId));
  const unsub = store.subscribe(nodeId, () => setNode(store.getNode(nodeId)));
  onCleanup(unsub);
  return node;
}

function AnnotationView(props: { store: NodeStore; nodeId: string; client: any }) {
  const node = useNode(props.store, props.nodeId);

  return (
    <div>
      <input
        value={node()?.state.label as string}
        onInput={(e) => props.client.setField(props.nodeId, "label", e.target.value)}
      />
    </div>
  );
}
```

### Vue

```vue
<script setup>
import { ref, onMounted, onUnmounted } from "vue";

const props = defineProps(["store", "nodeId", "client"]);
const node = ref(props.store.getNode(props.nodeId));

let unsub;
onMounted(() => {
  unsub = props.store.subscribe(props.nodeId, () => {
    node.value = props.store.getNode(props.nodeId);
  });
});
onUnmounted(() => unsub?.());
</script>

<template>
  <div v-if="node">
    <input
      :value="node.state.label"
      @input="client.setField(nodeId, 'label', $event.target.value)"
    />
  </div>
</template>
```

### Draft Pattern (Any Framework)

Buffer edits locally and commit on explicit submit:

```ts
// React example
function ColorEditor({ store, nodeId, client }) {
  const node = useNode(store, nodeId);
  const [draftR, setDraftR] = useState(node?.state.color?.r ?? 0);
  const [draftG, setDraftG] = useState(node?.state.color?.g ?? 0);
  const [draftB, setDraftB] = useState(node?.state.color?.b ?? 0);

  const apply = () => {
    client.setField(nodeId, "color", { r: draftR, g: draftG, b: draftB });
  };

  const reset = () => {
    setDraftR(node?.state.color?.r ?? 0);
    setDraftG(node?.state.color?.g ?? 0);
    setDraftB(node?.state.color?.b ?? 0);
  };

  return (
    <div>
      <input type="range" min={0} max={255} value={draftR}
        onInput={(e) => setDraftR(+e.target.value)} />
      <input type="range" min={0} max={255} value={draftG}
        onInput={(e) => setDraftG(+e.target.value)} />
      <input type="range" min={0} max={255} value={draftB}
        onInput={(e) => setDraftB(+e.target.value)} />
      <button onClick={apply}>Apply</button>
      <button onClick={reset}>Reset</button>
    </div>
  );
}
```

The color is edited locally with draft state. One `setField` call on apply -- one operation, one undo step, atomically replacing the entire frozen `Color` value.

A draft that spans several fields is applied the same way, as one
operation and one undo step, through the local document (the thick
client has no multi-step transaction API of its own; a state-only
`applyOperations` is safe, since field writes are optimistic and masked
exactly like `setField`):

```ts
client.getDoc()!.applyOperations({ ordered: [], state: { [nodeId]: { name, color } } });
```

## Python Server Setup

The client connects to an AtomDoc Python server (the [`python/`](../python/README.md) package):

```python
import asyncio
from pydantic import BaseModel
from atomdoc import Array, Doc, Session, WebSocketTransport, node


class Color(BaseModel, frozen=True):
    r: int = 0
    g: int = 0
    b: int = 0


@node
class Annotation:
    label: str = ""
    color: Color = Color()


@node
class Page:
    title: str = ""
    annotations: Array[Annotation] = []


async def main():
    doc = Doc(Page(
        title="Hello World",
        annotations=[
            Annotation(label="Important", color=Color(r=255)),
            Annotation(label="Draft"),
        ],
    ))

    session = Session(doc)
    transport = WebSocketTransport(host="localhost", port=8765)
    await session.bind(transport)

    print("Server running on ws://localhost:8765")
    await asyncio.Future()  # run forever

asyncio.run(main())
```

## Wire Protocol Reference

See [PROTOCOL.md](../PROTOCOL.md) for the full wire protocol specification.

### Server -> Client

| Message | Fields | When |
|---------|--------|------|
| `schema` | `schema: AtomDocSchema` | On connect |
| `snapshot` | `doc_id`, `version`, `data: JsonDoc`, `client_id` (this connection's id; thick clients prefix their refs with it) | On connect, after schema; on reconnect; after a rejected `op` |
| `patch` | `version`, `operations: WireOperations`, `source_client` (set only for the verbatim echo of that client's `op`), `ref` (the request's `ref`) | After each commit; also, to the requester alone at the current version, for an `op` that changed nothing |
| `error` | `ref` (or `null`), `code`, `message` | On invalid operation |

### Client -> Server

| Message | Fields | When |
|---------|--------|------|
| `op` | `ref?`, `operations: WireOperations` | Apply operations |
| `create` | `ref?`, `node_type`, `state`, `parent_id?`, `slot`, `position?`, `target_id?` | Create new node (thin client) |
| `undo` | `ref?`, `steps?` | Undo (thin client only) |
| `redo` | `ref?`, `steps?` | Redo (thin client only) |

### WireOperations Format

```ts
{
  ordered: [
    [0, [["id", "type"], ...], parentId|0, slotName, prevId|0, nextId|0],  // insert
    [1, startId, endId|0],                                                   // delete
    [2, startId, endId|0, parentId|0, slotName, prevId|0, nextId|0],       // move
  ],
  state: {
    "nodeId": { "field": value }   // native JSON, never a stringified string
  }
}
```

The `0` sentinel represents null (root parent, no positioning).

## Choosing Thin vs Thick

| | Thin | Thick |
|---|---|---|
| **Latency** | Round-trip per operation | Field writes instant; structure on confirmation |
| **Undo** | Server-side (shared stack) | Client-side (per-client) |
| **Offline** | No | Edits queue until reconnect |
| **Complexity** | Store and senders | Tree model, transactions, undo, pending structure |
| **Memory** | Flat store only | Full tree model |
| **Use case** | Dashboards, simple views | Editors, device-driven scenes |

For read-heavy UIs with occasional edits, thin is simpler. For interactive editors where responsiveness matters, thick.

## Related

- [`../python`](../python/README.md) -- Python server and document model
- [`../PROTOCOL.md`](../PROTOCOL.md) -- the wire protocol and a guide to building clients

## License

MIT
