import { describe, it, expect, vi } from "vitest";
import { ThickAtomDocClient } from "../../src/thick/thick-client.js";
import { RefIntegrityError } from "../../src/thick/local-doc.js";
import { getSlotChildren } from "../../src/thick/doc-node.js";
import type {
  AtomDocSchema,
  JsonDoc,
  SchemaMsg,
  SnapshotMsg,
  PatchMsg,
  WireOperations,
} from "../../src/types.js";

const schema: AtomDocSchema = {
  version: 1,
  root_type: "Page",
  node_types: {
    Page: {
      json_schema: {},
      field_tiers: { title: "mergeable", featured: "ref" },
      slots: { items: { allowed_type: "Item" } },
      field_defaults: { title: "", featured: null },
      refs: { featured: { target_type: "Item", many: false, policy: "restrict" } },
    },
    Item: {
      json_schema: {},
      field_tiers: { label: "mergeable" },
      slots: { children: { allowed_type: "Item" } },
      field_defaults: { label: "" },
    },
  },
  value_types: {},
};

const ROOT = "01jqp00000000000000000000";
type Sent = { ref: string; operations: WireOperations };

/**
 * A client whose socket records what it sends. `echo` answers a sent op
 * the way the server does when it recorded the request verbatim.
 */
function onlineClient(
  options: { coalesce?: boolean; mergeInterval?: number } = { coalesce: false },
  data: JsonDoc = snapshot,
): { client: ThickAtomDocClient; sent: Sent[]; echo: (op: Sent, ops?: WireOperations) => void } {
  const client = new ThickAtomDocClient({ url: "ws://unused", ...options });
  client._injectMessage({ type: "schema", schema } as SchemaMsg);
  client._injectMessage({ type: "snapshot", doc_id: ROOT, version: 0, data, client_id: "me" } as SnapshotMsg);
  const sent: Sent[] = [];
  const internals = client as unknown as { online: boolean; ws: unknown };
  internals.online = true;
  internals.ws = { send: (text: string) => sent.push(JSON.parse(text)) };
  let version = 0;
  const echo = (op: Sent, ops: WireOperations = op.operations) => {
    client._injectMessage({
      type: "patch",
      version: ++version,
      ref: op.ref,
      source_client: ops === op.operations ? "me" : null,
      operations: ops,
    } as PatchMsg);
  };
  return { client, sent, echo };
}

const items = (client: ThickAtomDocClient, parentId = ROOT, slot = "items") =>
  getSlotChildren(client.getDoc()!.getNode(parentId)!, slot).map((n) => n.id);

const snapshot: JsonDoc = [
  "01jqp00000000000000000000",
  "Page",
  { title: "Hello" },
  { items: [["i1", "Item", { label: "First" }]] },
];

function setupClient(options: { coalesce?: boolean } = { coalesce: false }): ThickAtomDocClient {
  const client = new ThickAtomDocClient({ url: "ws://unused", ...options });
  client._injectMessage({ type: "schema", schema } as SchemaMsg);
  client._injectMessage({
    type: "snapshot",
    doc_id: "01jqp00000000000000000000",
    version: 0,
    data: snapshot,
  } as SnapshotMsg);
  return client;
}

describe("ThickAtomDocClient", () => {
  it("initializes from schema + snapshot", () => {
    const client = setupClient();
    expect(client.getDoc()).not.toBeNull();
    expect(client.getSchema()).not.toBeNull();
    expect(client.getStore().getRootId()).toBe("01jqp00000000000000000000");
    expect(client.getStore().getRoot()!.state.title).toBe("Hello");
  });

  it("fires connected callback", () => {
    const client = new ThickAtomDocClient({ url: "ws://unused" });
    const cb = vi.fn();
    client.onConnected(cb);
    client._injectMessage({ type: "schema", schema } as SchemaMsg);
    client._injectMessage({
      type: "snapshot",
      doc_id: "01jqp00000000000000000000",
      version: 0,
      data: snapshot,
    } as SnapshotMsg);
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it("setField applies locally and updates store", () => {
    const client = setupClient();
    client.setField("01jqp00000000000000000000", "title", "Updated");

    // Doc updated
    expect(client.getDoc()!.root.state.title).toBe("Updated");
    // Store updated via bridge
    expect(client.getStore().getRoot()!.state.title).toBe("Updated");
  });

  it("createNode is applied when the server confirms it", () => {
    const { client, sent, echo } = onlineClient();
    const newId = client.createNode("Item", { label: "New" }, ROOT, "items");

    expect(newId).toBeTruthy();
    expect(client.getDoc()!.getNode(newId)).toBeUndefined();
    expect(client.getStore().getNode(newId)).toBeUndefined();
    expect(client.pendingStructure()).toBe(true);
    expect(sent.length).toBe(1);
    expect(sent[0].operations).toEqual({
      ordered: [[0, [[newId, "Item"]], 0, "items", "i1", 0]],
      state: { [newId]: { label: "New" } },
    });

    echo(sent[0]);
    expect(client.getDoc()!.getNode(newId)!.state.label).toBe("New");
    expect(client.getStore().getNode(newId)!.state.label).toBe("New");
    expect(items(client)).toEqual(["i1", newId]);
    expect(client.pendingStructure()).toBe(false);
    expect(sent.length).toBe(1); // the echo is not sent back
    expect(client.getUndoManager()!.canUndo).toBe(true); // but it is this user's work
  });

  it("deleteNode is applied when the server confirms it", () => {
    const { client, sent, echo } = onlineClient();
    client.deleteNode("i1");

    expect(client.getDoc()!.getNode("i1")).toBeDefined();
    expect(sent[0].operations).toEqual({ ordered: [[1, "i1", 0]], state: {} });
    echo(sent[0]);
    expect(client.getDoc()!.getNode("i1")).toBeUndefined();
    expect(client.getStore().getNode("i1")).toBeUndefined();
  });

  it("undo/redo works locally", () => {
    const client = setupClient();
    const rootId = client.getStore().getRootId();

    client.setField(rootId, "title", "Changed");
    expect(client.getStore().getRoot()!.state.title).toBe("Changed");

    client.undo();
    expect(client.getStore().getRoot()!.state.title).toBe("Hello");

    client.redo();
    expect(client.getStore().getRoot()!.state.title).toBe("Changed");
  });

  it("multi-step undo", () => {
    const client = setupClient();
    const rootId = client.getStore().getRootId();

    client.setField(rootId, "title", "A");
    client.setField(rootId, "title", "B");
    client.setField(rootId, "title", "C");

    client.undo(3);
    expect(client.getStore().getRoot()!.state.title).toBe("Hello");
  });

  it("applies remote patch from another client", () => {
    const client = setupClient();

    client._injectMessage({
      type: "patch",
      version: 1,
      operations: {
        ordered: [],
        state: { i1: { label: "Remote" } },
      },
      source_client: "other-client",
    } as PatchMsg);

    expect(client.getDoc()!.getNode("i1")!.state.label).toBe("Remote");
    expect(client.getStore().getNode("i1")!.state.label).toBe("Remote");
    expect(client.getVersion()).toBe(1);
  });

  it("handles error message", () => {
    const client = setupClient();
    const cb = vi.fn();
    client.onError(cb);
    client._injectMessage({
      type: "error",
      code: "invalid_op",
      message: "bad",
    });
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it("fires patch callback on remote changes", () => {
    const client = setupClient();
    const cb = vi.fn();
    client.onPatch(cb);

    client._injectMessage({
      type: "patch",
      version: 2,
      operations: { ordered: [], state: {} },
      source_client: "other",
    } as PatchMsg);

    expect(cb).toHaveBeenCalledWith(2);
  });
});

describe("ThickAtomDocClient store coalescing", () => {
  it("updates the store once per frame by default", async () => {
    const client = setupClient({});
    const store = client.getStore();
    const rootId = store.getRootId();
    const notified = vi.fn();
    store.subscribe(rootId, notified);

    client.setField(rootId, "title", "A");
    client.setField(rootId, "title", "B");
    client.setField(rootId, "title", "C");
    // The document is current; the store waits for the frame.
    expect(client.getDoc()!.root.state.title).toBe("C");
    expect(store.getRoot()!.state.title).toBe("Hello");
    expect(notified).not.toHaveBeenCalled();

    await new Promise((r) => setTimeout(r, 5));
    expect(store.getRoot()!.state.title).toBe("C");
    expect(notified).toHaveBeenCalledTimes(1);
  });

  it("flushStore brings the store up to date now", () => {
    const { client, sent, echo } = onlineClient({});
    const store = client.getStore();
    client.setField(ROOT, "title", "typed");
    client.deleteNode("i1");
    echo(sent[1]);
    expect(store.getRoot()!.state.title).toBe("Hello");
    expect(store.getNode("i1")).toBeDefined();
    client.flushStore();
    expect(store.getRoot()!.state.title).toBe("typed");
    expect(store.getNode("i1")).toBeUndefined();
    expect(store.getChildren(ROOT, "items")).toEqual([]);
  });

  it("a resync discards queued store updates", async () => {
    const client = setupClient({});
    const store = client.getStore();
    const rootId = store.getRootId();
    // Online with a socket that swallows sends: the edit is in flight,
    // not buffered for replay after the snapshot.
    (client as unknown as { online: boolean; ws: unknown }).online = true;
    (client as unknown as { ws: unknown }).ws = { send() {} };
    client.setField(rootId, "title", "stale");
    const corrected: JsonDoc = [rootId, "Page", { title: "server" }, { items: [] }];
    client._injectMessage({ type: "snapshot", doc_id: rootId, version: 2, data: corrected });
    expect(store.getRoot()!.state.title).toBe("server");
    await new Promise((r) => setTimeout(r, 5));
    expect(store.getRoot()!.state.title).toBe("server");
    expect(store.getNode("i1")).toBeUndefined();
  });
});

describe("ThickAtomDocClient confirmed structure", () => {
  const three: JsonDoc = [
    ROOT,
    "Page",
    { title: "Hello" },
    { items: [["i1", "Item", {}], ["i2", "Item", {}], ["i3", "Item", {}]] },
  ];

  it("two appends in a row keep their order", () => {
    const { client, sent, echo } = onlineClient();
    const first = client.createNode("Item", {}, ROOT, "items");
    const second = client.createNode("Item", {}, ROOT, "items");
    expect(sent[0].operations.ordered[0]).toEqual([0, [[first, "Item"]], 0, "items", "i1", 0]);
    expect(sent[1].operations.ordered[0]).toEqual([0, [[second, "Item"]], 0, "items", first, 0]);
    echo(sent[0]);
    echo(sent[1]);
    expect(items(client)).toEqual(["i1", first, second]);
  });

  it("prepends anchor on the pending first node", () => {
    const { client, sent, echo } = onlineClient();
    const first = client.createNode("Item", {}, ROOT, "items", "prepend");
    const second = client.createNode("Item", {}, ROOT, "items", "prepend");
    expect(sent[0].operations.ordered[0]).toEqual([0, [[first, "Item"]], 0, "items", 0, "i1"]);
    expect(sent[1].operations.ordered[0]).toEqual([0, [[second, "Item"]], 0, "items", 0, first]);
    echo(sent[0]);
    echo(sent[1]);
    expect(items(client)).toEqual([second, first, "i1"]);
    expect(() => client.createNode("Item", {}, ROOT, "items", "sideways")).toThrow(/position/);
  });

  it("a child can be created under a parent that is still pending", () => {
    const { client, sent, echo } = onlineClient();
    const parent = client.createNode("Item", { label: "p" }, ROOT, "items");
    const child = client.createNode("Item", { label: "c" }, parent, "children");
    expect(sent[1].operations.ordered[0]).toEqual([0, [[child, "Item"]], parent, "children", 0, 0]);
    expect(() => client.createNode("Item", {}, parent, "nope")).toThrow(/Slot 'nope'/);
    expect(() => client.createNode("Item", {}, "ghost", "children")).toThrow(/Parent not found/);
    echo(sent[0]);
    echo(sent[1]);
    expect(items(client, parent, "children")).toEqual([child]);
    expect(client.getDoc()!.getNode(child)!.parent!.id).toBe(parent);
  });

  it("a field written on a pending node is sent and lands with the node", () => {
    const { client, sent, echo } = onlineClient();
    const id = client.createNode("Item", { label: "first" }, ROOT, "items");
    client.setField(id, "label", "second");
    expect(sent[1].operations).toEqual({ ordered: [], state: { [id]: { label: "second" } } });
    expect(() => client.setField(id, "nope", 1)).toThrow(/Unknown field/);
    expect(() => client.setField("ghost", "label", 1)).toThrow(/Node not found/);
    echo(sent[0]);
    expect(client.getDoc()!.getNode(id)!.state.label).toBe("first");
    echo(sent[1]);
    expect(client.getDoc()!.getNode(id)!.state.label).toBe("second");
  });

  it("undo and redo of confirmed structure are themselves confirmed", () => {
    const { client, sent, echo } = onlineClient();
    const id = client.createNode("Item", { label: "New" }, ROOT, "items");
    echo(sent[0]);
    const undoMgr = client.getUndoManager()!;
    expect(undoMgr.canUndo).toBe(true);

    client.undo();
    // Sent, not applied: the node is still there until the server answers.
    expect(client.getDoc()!.getNode(id)).toBeDefined();
    expect(sent[1].operations.ordered).toEqual([[1, id, 0]]);
    expect(undoMgr.canUndo).toBe(false);
    expect(undoMgr.canRedo).toBe(false); // filed when the echo commits
    echo(sent[1]);
    expect(client.getDoc()!.getNode(id)).toBeUndefined();
    expect(undoMgr.canRedo).toBe(true);
    expect(undoMgr.canUndo).toBe(false);

    client.redo();
    expect(sent[2].operations.ordered[0]).toEqual([0, [[id, "Item"]], 0, "items", "i1", 0]);
    expect(sent[2].operations.state).toEqual({ [id]: { label: "New" } });
    echo(sent[2]);
    expect(items(client)).toEqual(["i1", id]);
    expect(undoMgr.canUndo).toBe(true);
    expect(undoMgr.canRedo).toBe(false);
    expect(sent.length).toBe(3);
  });

  it("undo of a field write stays local", () => {
    const { client, sent } = onlineClient();
    client.setField(ROOT, "title", "typed");
    client.undo();
    expect(client.getDoc()!.root.state.title).toBe("Hello");
    expect(sent.length).toBe(2);
    expect(sent[1].operations).toEqual({ ordered: [], state: { [ROOT]: { title: "Hello" } } });
    expect(client.getUndoManager()!.canRedo).toBe(true);
  });

  it("moves anchor around structure that is still pending", () => {
    const { client, sent, echo } = onlineClient({ coalesce: false }, three);
    client.deleteNode("i3");
    // The last stable sibling is i2: i3 is on its way out.
    const added = client.createNode("Item", {}, ROOT, "items");
    expect(sent[1].operations.ordered[0]).toEqual([0, [[added, "Item"]], 0, "items", "i2", 0]);
    expect(() => client.moveNodeRelative("i1", "i3", "after")).toThrow(/Node not found/);
    expect(() => client.deleteNode("i3")).toThrow(/Node not found/);
    // Append i1: after the pending append, not after i2.
    client.moveNode("i1", ROOT, "items");
    expect(sent[2].operations.ordered[0]).toEqual([2, "i1", 0, 0, "items", added, 0]);
    // A move relative to a pending node anchors on the projected order:
    // i2 already precedes the new node, so that is not a move at all.
    client.moveNodeRelative("i2", added, "before");
    expect(sent.length).toBe(3);
    client.moveNodeRelative("i2", added, "after");
    expect(sent[3].operations.ordered[0]).toEqual([2, "i2", 0, 0, "items", added, "i1"]);
    for (const op of sent) echo(op);
    expect(items(client)).toEqual([added, "i2", "i1"]);
  });

  it("a move to where the node already is sends nothing", () => {
    const { client, sent } = onlineClient({ coalesce: false }, three);
    client.moveNode("i3", ROOT, "items");
    client.moveNodeRelative("i2", "i3", "before");
    client.moveNodeRelative("i2", "i1", "after");
    expect(sent).toEqual([]);
    expect(() => client.moveNodeRelative("i1", "i1", "after")).toThrow(/in the range/);
    expect(() => client.moveNode(ROOT, ROOT, "items")).toThrow(/root/);
  });

  it("a structural edit made offline is sent after the reconnect snapshot", () => {
    const { client, sent, echo } = onlineClient();
    const internals = client as unknown as { online: boolean };
    internals.online = false;
    const id = client.createNode("Item", { label: "later" }, ROOT, "items");
    client.setField(ROOT, "title", "offline"); // applied locally, buffered
    expect(sent).toEqual([]);
    expect(client.pendingStructure()).toBe(true);
    internals.online = true;
    client._injectMessage({ type: "snapshot", doc_id: ROOT, version: 3, data: snapshot } as SnapshotMsg);
    // Both are sent in order; the new document shows neither until echoed.
    expect(sent.map((s) => s.operations.ordered.length)).toEqual([1, 0]);
    expect(client.getDoc()!.getNode(id)).toBeUndefined();
    expect(client.getDoc()!.root.state.title).toBe("Hello");
    echo(sent[0]);
    echo(sent[1]);
    expect(client.getDoc()!.getNode(id)!.state.label).toBe("later");
    expect(client.getDoc()!.root.state.title).toBe("offline");
  });

  it("a rejected structural edit leaves the document, and undo history, as they were", () => {
    const { client, sent } = onlineClient();
    const errors = vi.fn();
    client.onError(errors);
    const id = client.createNode("Item", {}, ROOT, "items");
    expect(client.getUndoManager()!.canUndo).toBe(false); // its place is held, not usable yet
    client._injectMessage({ type: "error", ref: sent[0].ref, code: "rejected", message: "no" });
    expect(errors).toHaveBeenCalledTimes(1);
    expect(client.pendingStructure()).toBe(false);
    expect(client.getDoc()!.getNode(id)).toBeUndefined();
    expect(items(client)).toEqual(["i1"]);
    expect(client.getUndoManager()!.canUndo).toBe(false);
    client.setField(ROOT, "title", "x");
    expect(client.getUndoManager()!.canUndo).toBe(true);
  });

  it("an append after a relative move follows the moved node", () => {
    const { client, sent, echo } = onlineClient({ coalesce: false }, three);
    client.moveNodeRelative("i1", "i3", "after"); // -> i2, i3, i1
    const added = client.createNode("Item", {}, ROOT, "items"); // after i1, the new last
    expect(sent[1].operations.ordered[0]).toEqual([0, [[added, "Item"]], 0, "items", "i1", 0]);
    // A move relative to a node that is itself pending a move anchors on
    // the projected order, and is not mistaken for "already there".
    client.moveNodeRelative("i2", "i1", "after"); // -> i3, i1, i2, added
    expect(sent[2].operations.ordered[0]).toEqual([2, "i2", 0, 0, "items", "i1", added]);
    client.moveNodeRelative("i2", "i1", "after"); // now it is already there
    expect(sent.length).toBe(3);
    for (const op of sent) echo(op);
    expect(items(client)).toEqual(["i3", "i1", "i2", added]);
  });

  it("a field cannot be written on a node pending deletion", () => {
    const { client, sent } = onlineClient({ coalesce: false }, three);
    client.deleteNode("i2");
    expect(() => client.setField("i2", "label", "late")).toThrow(/Node not found/);
    expect(sent.length).toBe(1);
  });

  it("undo history keeps the order the user acted in", () => {
    const { client, sent, echo } = onlineClient();
    const undoMgr = client.getUndoManager()!;
    const id = client.createNode("Item", { label: "new" }, ROOT, "items"); // place held
    client.setField("i1", "label", "edited"); // applied at once, on top
    expect(undoMgr.canUndo).toBe(true);
    client.undo(); // the newest action: the field write
    expect(client.getDoc()!.getNode("i1")!.state.label).toBe("First");
    // The create is next, but it is not confirmed yet.
    expect(undoMgr.canUndo).toBe(false);
    echo(sent[0]);
    expect(client.getDoc()!.getNode(id)).toBeDefined();
    expect(undoMgr.canUndo).toBe(true);
    client.undo();
    expect(sent.at(-1)!.operations.ordered).toEqual([[1, id, 0]]);
  });

  it("the merge window is measured from the user's action, not the echo", () => {
    const { client, sent, echo } = onlineClient({ coalesce: false, mergeInterval: 60_000 });
    client.setField("i1", "label", "typed");
    const id = client.createNode("Item", { label: "added" }, ROOT, "items");
    echo(sent[1]);
    // One step: the write and the create were made together.
    client.undo();
    const step = sent[2].operations;
    expect(step.ordered).toEqual([[1, id, 0]]);
    expect(step.state).toEqual({ i1: { label: "First" } });
    echo(sent[2]);
    expect(client.getDoc()!.getNode(id)).toBeUndefined();
    expect(client.getDoc()!.getNode("i1")!.state.label).toBe("First");
    expect(client.getUndoManager()!.canUndo).toBe(false);
    expect(client.getUndoManager()!.canRedo).toBe(true);
  });

  it("an edit made while an undo is in flight invalidates redo", () => {
    const { client, sent, echo } = onlineClient();
    const id = client.createNode("Item", {}, ROOT, "items");
    echo(sent[0]);
    client.undo(); // sent
    client.setField("i1", "label", "later"); // a new step
    echo(sent[1]); // the undo lands
    expect(client.getDoc()!.getNode(id)).toBeUndefined();
    expect(client.getUndoManager()!.canRedo).toBe(false);
    expect(client.getUndoManager()!.canUndo).toBe(true);
  });

  it("an undo step the server had nothing to apply for is consumed", () => {
    const { client, sent, echo } = onlineClient();
    client.createNode("Item", {}, ROOT, "items");
    echo(sent[0]);
    client.undo();
    echo(sent[1], { ordered: [], state: {} });
    expect(client.getUndoManager()!.canUndo).toBe(false);
    expect(client.getUndoManager()!.canRedo).toBe(false);
    expect(client.pendingStructure()).toBe(false);
  });

  it("a remote change to structure is not undoable; an own echo is", () => {
    const { client, sent, echo } = onlineClient();
    client._injectMessage({
      type: "patch",
      version: 1,
      operations: { ordered: [[0, [["r1", "Item"]], 0, "items", "i1", 0]], state: {} },
      source_client: "other",
    } as PatchMsg);
    expect(items(client)).toEqual(["i1", "r1"]);
    expect(client.getUndoManager()!.canUndo).toBe(false);
    client.deleteNode("r1");
    echo(sent[0]);
    expect(items(client)).toEqual(["i1"]);
    expect(client.getUndoManager()!.canUndo).toBe(true);
  });
});

describe("ThickAtomDocClient conveniences", () => {
  const typed: AtomDocSchema = {
    version: 1,
    root_type: "Page",
    node_types: {
      Page: {
        json_schema: { type: "object", properties: { count: { type: "integer" }, title: { type: "string" } } },
        field_tiers: { count: "mergeable", title: "mergeable" },
        slots: { items: { allowed_type: "Item" } },
        field_defaults: { count: 0, title: "" },
      },
      Item: { json_schema: {}, field_tiers: { label: "mergeable" }, slots: {}, field_defaults: { label: "" } },
    },
    value_types: {},
  };
  const typedSnapshot: JsonDoc = [ROOT, "Page", { title: "Hello" }, { items: [["i1", "Item", {}]] }];

  function typedClient(options: { validate?: boolean } = {}) {
    const client = new ThickAtomDocClient({ url: "ws://unused", coalesce: false, ...options });
    client._injectMessage({ type: "schema", schema: typed } as SchemaMsg);
    client._injectMessage({ type: "snapshot", doc_id: ROOT, version: 0, data: typedSnapshot, client_id: "me" } as SnapshotMsg);
    const sent: Sent[] = [];
    const internals = client as unknown as { online: boolean; ws: unknown };
    internals.online = true;
    internals.ws = { send: (text: string) => sent.push(JSON.parse(text)) };
    return { client, sent };
  }

  it("validates a field value before applying and sending it", () => {
    const { client, sent } = typedClient();
    expect(() => client.setField(ROOT, "count", "seven")).toThrow();
    expect(client.getDoc()!.root.state.count).toBe(0);
    expect(sent.length).toBe(0);
    client.setField(ROOT, "count", 7);
    expect(sent.length).toBe(1);
    // Off: sent as given, for the server to judge.
    const raw = typedClient({ validate: false });
    raw.client.setField(ROOT, "count", "seven");
    expect(raw.sent.length).toBe(1);
  });

  it("getState fills in schema defaults", () => {
    const { client } = typedClient();
    expect(client.getState("i1")).toEqual({ label: "" });
    expect(client.getState(ROOT)).toEqual({ count: 0, title: "Hello" });
    expect(client.getState("nope")).toBeUndefined();
  });

  it("settled() resolves once every edit is answered", async () => {
    const { client, sent, echo } = onlineClient({});
    let done = false;
    await client.settled(); // nothing pending
    client.setField(ROOT, "title", "a");
    const id = client.createNode("Item", {}, ROOT, "items");
    client.settled().then(() => { done = true; });
    await Promise.resolve();
    expect(done).toBe(false);
    echo(sent[0]);
    await Promise.resolve();
    expect(done).toBe(false);
    echo(sent[1]);
    await Promise.resolve();
    expect(done).toBe(true);
    // The store was flushed as part of settling.
    expect(client.getStore().getNode(id)).toBeDefined();
  });

  it("onResync reports why and what was lost", () => {
    const { client, sent } = onlineClient();
    const seen: unknown[] = [];
    client.onResync((info) => seen.push(info));
    client.setField(ROOT, "title", "one");
    client.setField(ROOT, "title", "two");
    client.undo();
    client._injectMessage({ type: "error", ref: sent[0].ref, code: "rejected", message: "no" });
    client._injectMessage({ type: "snapshot", doc_id: ROOT, version: 5, data: snapshot } as SnapshotMsg);
    expect(seen).toEqual([
      { reason: "rejected", undoStepsDropped: 1, redoStepsDropped: 1, schemaChanged: false, partial: false },
    ]);
  });

  it("onResync says when a reconnect brought a different schema", () => {
    const { client } = onlineClient();
    const seen: Array<{ reason: string; schemaChanged: boolean }> = [];
    client.onResync((info) => seen.push({ reason: info.reason, schemaChanged: info.schemaChanged }));
    (client as unknown as { onlinePending: boolean }).onlinePending = true;
    client._injectMessage({ type: "schema", schema } as SchemaMsg); // the same schema again
    client._injectMessage({ type: "snapshot", doc_id: ROOT, version: 3, data: snapshot } as SnapshotMsg);
    const grown: AtomDocSchema = {
      ...schema,
      node_types: {
        ...schema.node_types,
        Item: { ...schema.node_types.Item, field_tiers: { label: "mergeable", note: "mergeable" }, field_defaults: { label: "", note: "" } },
      },
    };
    (client as unknown as { onlinePending: boolean }).onlinePending = true;
    client._injectMessage({ type: "schema", schema: grown } as SchemaMsg);
    client._injectMessage({ type: "snapshot", doc_id: ROOT, version: 4, data: snapshot } as SnapshotMsg);
    expect(seen).toEqual([
      { reason: "reconnect", schemaChanged: false },
      { reason: "reconnect", schemaChanged: true },
    ]);
    expect(client.getSchema()!.getDefaults("Item")).toEqual({ label: "", note: "" });
  });

  it("onSchemaMismatch: \"disconnect\" refuses a reconnect that brings a different schema", () => {
    const client = new ThickAtomDocClient({ url: "ws://unused", coalesce: false, onSchemaMismatch: "disconnect" });
    client._injectMessage({ type: "schema", schema } as SchemaMsg);
    client._injectMessage({ type: "snapshot", doc_id: ROOT, version: 0, data: snapshot, client_id: "me" } as SnapshotMsg);
    const internals = client as unknown as { online: boolean; ws: unknown; onlinePending: boolean };
    let closed = false;
    internals.online = true;
    internals.ws = { send() {}, close: () => { closed = true; } };
    const errors: string[] = [];
    const resyncs: unknown[] = [];
    const offline = vi.fn();
    client.onError((e) => errors.push(e.code));
    client.onResync((info) => resyncs.push(info));
    client.onOffline(offline);

    // The same schema again: fine.
    internals.onlinePending = true;
    client._injectMessage({ type: "schema", schema } as SchemaMsg);
    client._injectMessage({ type: "snapshot", doc_id: ROOT, version: 2, data: snapshot } as SnapshotMsg);
    expect(errors).toEqual([]);
    expect(resyncs.length).toBe(1);

    // A different one: refused; the old schema and document stay.
    const grown: AtomDocSchema = {
      ...schema,
      node_types: { ...schema.node_types, Extra: { json_schema: {}, field_tiers: {}, slots: {}, field_defaults: {} } },
    };
    client.setField(ROOT, "title", "kept");
    internals.online = true;
    internals.ws = { send() {}, close: () => { closed = true; } };
    internals.onlinePending = true;
    client._injectMessage({ type: "schema", schema: grown } as SchemaMsg);
    expect(errors).toEqual(["schema_changed"]);
    expect(closed).toBe(true);
    expect(client.isOnline()).toBe(false);
    expect(offline).toHaveBeenCalled();
    expect(client.getSchema()!.getNodeType("Extra")).toBeUndefined();
    expect(client.getDoc()!.root.state.title).toBe("kept");
    expect(resyncs.length).toBe(1);
  });
});

describe("ThickAtomDocClient readiness and integrity", () => {
  it("ready() resolves once the snapshot is in; mutators explain the wait", async () => {
    const client = new ThickAtomDocClient({ url: "ws://unused", coalesce: false });
    expect(() => client.setField(ROOT, "title", "x")).toThrow(/not loaded yet/);
    let resolved = false;
    const p = client.ready().then(() => { resolved = true; });
    client._injectMessage({ type: "schema", schema } as SchemaMsg);
    expect(resolved).toBe(false);
    client._injectMessage({ type: "snapshot", doc_id: ROOT, version: 0, data: snapshot, client_id: "me" } as SnapshotMsg);
    await p;
    expect(resolved).toBe(true);
    await client.ready(); // already loaded: resolves at once
  });

  it("deleteNode refuses a node another node still references", () => {
    const { client, sent, echo } = onlineClient();
    client.setField(ROOT, "featured", "i1");
    expect(() => client.deleteNode("i1")).toThrow(RefIntegrityError);
    expect(sent.length).toBe(1); // only the field write
    client.setField(ROOT, "featured", null);
    client.deleteNode("i1");
    expect(sent.length).toBe(3);
    echo(sent[2]);
    expect(items(client)).toEqual([]);
  });
});

describe("ThickAtomDocClient resync", () => {
  it("rebuilds the document from a second snapshot and drops pending ops", () => {
    const client = new ThickAtomDocClient({ url: "ws://test", coalesce: false });
    client._injectMessage({ type: "schema", schema });
    client._injectMessage({
      type: "snapshot",
      doc_id: "01jqp00000000000000000000",
      version: 1,
      data: snapshot,
      client_id: "c1",
    });
    const resynced = vi.fn();
    client.onResync(resynced);
    // Online, with a socket that swallows sends: the edit is sent, not buffered.
    (client as unknown as { online: boolean; ws: unknown }).online = true;
    (client as unknown as { ws: unknown }).ws = { send() {} };

    // A local edit that the server will reject.
    client.setField("i1", "label", "optimistic");
    expect(client.getDoc()!.getNode("i1")!.state.label).toBe("optimistic");
    expect(client.getUndoManager()!.canUndo).toBe(true);

    client._injectMessage({ type: "error", ref: "x", code: "rejected", message: "no" });
    const corrected: JsonDoc = [
      "01jqp00000000000000000000",
      "Page",
      { title: "Hello" },
      { items: [["i1", "Item", { label: "server" }]] },
    ];
    client._injectMessage({ type: "snapshot", doc_id: "01jqp00000000000000000000", version: 7, data: corrected });

    expect(resynced).toHaveBeenCalledTimes(1);
    expect(client.getVersion()).toBe(7);
    expect(client.getDoc()!.getNode("i1")!.state.label).toBe("server");
    expect(client.getStore().getNode("i1")!.state.label).toBe("server");
    expect(client.getUndoManager()!.canUndo).toBe(false);
    // A self-echo after resync must not be mistaken for an older pending op.
    client._injectMessage({
      type: "patch",
      version: 8,
      operations: { ordered: [], state: { i1: { label: "later" } } },
      source_client: "other",
    });
    expect(client.getDoc()!.getNode("i1")!.state.label).toBe("later");
  });
});
