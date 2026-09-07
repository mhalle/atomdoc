# atomdoc-ts

TypeScript client for the [AtomDoc](https://github.com/mhalle/atomdoc) document protocol. Connect to a Python AtomDoc server, render documents reactively, and send operations back.

> **Note:** This package was previously called `atomdoc-client`. It has been renamed to `atomdoc-ts`.

Two client modes:

- **Thin client** — every operation goes to the server. Simple, no local state beyond the reactive store.
- **Thick client** — operations apply locally first for instant UI. Local undo/redo. Works offline, syncs on reconnect.

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

client.onConnected(() => {
  const store = client.getStore();
  console.log("Document loaded:", store.getRoot().state.title);
});

await client.connect();

// Operations apply instantly — no round-trip
client.setField(rootId, "title", "Updated");
console.log(client.getStore().getRoot().state.title); // "Updated" immediately

// Local undo
client.undo();

// Works offline — ops buffer until reconnect
client.onOffline(() => console.log("Offline — edits still work"));
client.onOnline(() => console.log("Back online — syncing"));
```

## Architecture

```
Python Server (authoritative)
  | WebSocket (schema / snapshot / patch / op / create / undo / redo)
TypeScript Client
  |-- NodeStore        — reactive flat map of nodes, subscriptions
  |-- SchemaRegistry   — Zod validators from server schema
  |-- [thin] direct send/receive
  +-- [thick] LocalDoc — local tree, undo, offline buffer
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
schema.getSlots("Page");                        // { annotations: { allowed_type: "Annotation" } }
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
import { defineNode, defineValue, buildSchema } from "atomdoc-ts";

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
  slots: { annotations: "Annotation" },
});

const schema = buildSchema("Page", [Page, Annotation], [Color]);
// schema is identical in format to Python's doc.atomdoc_schema()
```

Handles name things outside the document. `defineHandle(name, strength)`
builds a frozen `uri` / `media_type` / `digest` value type; put it on a
field with `tier: "atomic"` and the node type exports a `handles` block.
`doc.handles("strong")` on a `LocalDoc` is the dependency list a consumer
checks before opening a document.

A `ref` field is the TypeScript spelling of Python's `Ref[T]` (`many: true`
for `list[Ref[T]]`). Slots are ownership; references are association and
never control a node's lifetime. `LocalDoc` keeps a reverse index
(`doc.referrers(nodeId, field?)`) and checks referential integrity when a
transaction commits: a reference must resolve to a node of the declared
type, and a node that is still referenced cannot be deleted. A violation
throws `RefIntegrityError` and rolls the transaction back, so re-point the
referrers and delete the old target in one transaction. Moving a node is
not a delete.

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

// Create a new node (server assigns ID)
client.createNode("Annotation", { label: "New" }, parentId, "annotations");
client.createNode("Annotation", { label: "First" }, parentId, "annotations", "prepend");

// Delete a node
client.deleteNode(nodeId);

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

Buffer multiple operations and send as one atomic batch:

```ts
import { Transaction } from "atomdoc-ts";

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
});
```

#### Same Read API

```ts
client.getStore();       // NodeStore (same as thin)
client.getSchema();      // SchemaRegistry
client.getVersion();     // server version
```

#### Additional State

```ts
client.getDoc();          // LocalDoc — the local document model
client.getUndoManager();  // UndoManager
client.isOnline();        // connection status
```

#### Mutations -- Local First

All mutations apply instantly to the local document and NodeStore. The forward operations are sent to the server in the background.

```ts
// Instant — no round-trip needed
client.setField(nodeId, "title", "Updated");

// Returns the locally-generated node ID
const newId = client.createNode("Annotation", { label: "New" }, parentId, "annotations");
console.log("Created:", newId); // available immediately

client.deleteNode(nodeId);
client.moveNode(nodeId, newParentId, "children");          // append to a slot
client.moveNodeRelative(nodeId, siblingId, "after");        // position next to a sibling
```

#### Local Undo/Redo

Undo and redo work entirely on the client. No server round-trip. Patches
received from other clients are applied with `skipUndo`, so undo only ever
reverts this client's own edits.

```ts
client.undo();
client.redo();
client.undo(3);  // undo 3 steps at once

// Check availability
client.getUndoManager().canUndo;
client.getUndoManager().canRedo;
```

Undo history can be carried over when a document is rebuilt from a newer
snapshot, as long as the ID and root type match:

```ts
const history = client.getUndoManager().exportHistory();
// ... later, on a new LocalDoc/UndoManager for the same document
undoManager.importHistory(history);
```

The exported history is plain JSON. Its `lastUpdate` timestamp comes from
the exporting manager's clock (`Date.now` by default), so a merge window can
continue across the transfer only when both managers share a clock.

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
client.onResync(() => { ... });        // server replaced the local doc with a
                                       // fresh snapshot after rejecting an op
client.onOffline(() => { ... });       // connection lost
client.onOnline(() => { ... });        // reconnected
```

#### Offline Behavior

When the connection drops, all operations continue to work locally. The UI stays responsive. Operations are buffered — including any that were sent but not yet acknowledged when the socket dropped — and replayed in order once the reconnect snapshot has landed; `onOnline` fires after that replay, so it sees the resynced document. An edit the server then rejects comes back as a resync (`onResync`).

```ts
client.onOffline(() => {
  // Everything still works — setField, createNode, undo, etc.
  // The NodeStore updates immediately. The UI doesn't know the difference.
});
```

### LocalDoc (Advanced)

The thick client's local document model is accessible for advanced use cases.

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
  console.log("Diff:", event.diff);
});

// Serialize
const snapshot = doc.toSnapshot();  // wire format [id, type, state, slots]
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

## Python Server Setup

The client connects to an [AtomDoc](https://github.com/mhalle/atomdoc) Python server:

```python
import asyncio
from pydantic import BaseModel
from atomdoc import Array, Doc, node
from atomdoc._session import Session
from atomdoc._ws_transport import WebSocketTransport


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

See [PROTOCOL.md](PROTOCOL.md) for the full wire protocol specification.

### Server -> Client

| Message | Fields | When |
|---------|--------|------|
| `schema` | `schema: AtomDocSchema` | On connect |
| `snapshot` | `doc_id`, `version`, `data: JsonDoc` | On connect, after schema |
| `patch` | `version`, `operations: WireOperations`, `source_client` (set only for the verbatim echo of that client's `op`), `ref` (the request's `ref`) | After each commit |
| `error` | `ref?`, `code`, `message` | On invalid operation |

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
    "nodeId": { "field": "\"json-stringified-value\"" }
  }
}
```

The `0` sentinel represents null (root parent, no positioning).

## Choosing Thin vs Thick

| | Thin | Thick |
|---|---|---|
| **Latency** | Round-trip per operation | Instant (local-first) |
| **Undo** | Server-side (shared stack) | Client-side (per-client) |
| **Offline** | No | Yes (buffer + rebase) |
| **Complexity** | ~500 lines | ~1500 lines |
| **Memory** | Flat store only | Full tree model |
| **Use case** | Dashboards, simple views | Editors, collaborative tools |

For read-heavy UIs with occasional edits, thin is simpler. For interactive editors where responsiveness matters, thick.

## Related

- [atomdoc](https://github.com/mhalle/atomdoc) -- Python server and document model

## License

MIT
