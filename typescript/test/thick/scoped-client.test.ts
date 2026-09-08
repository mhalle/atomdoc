/**
 * Partial replication, client side: the visibility operations a scoped
 * client applies (`[3]` demote, `[4]` exit, `[5]` detached stub, `[6]`
 * fill, and an insert that places a detached stub), the partial
 * handshake, `setScope()`, server-side undo, and error routing.
 */
import { describe, it, expect, vi } from "vitest";
import { LocalDoc, OutOfScopeError } from "../../src/thick/local-doc.js";
import { getSlotChildren } from "../../src/thick/doc-node.js";
import { NodeStore } from "../../src/store.js";
import { applyPatch } from "../../src/patch.js";
import { bridgeDocToStore } from "../../src/thick/store-bridge.js";
import { ThickAtomDocClient } from "../../src/thick/thick-client.js";
import type {
  AtomDocSchema,
  ClientMsg,
  ErrorMsg,
  JsonDoc,
  PatchMsg,
  SchemaMsg,
  ScopeAckMsg,
  SnapshotMsg,
  WireOperations,
} from "../../src/types.js";

const schema: AtomDocSchema = {
  version: 1,
  root_type: "Page",
  node_types: {
    Page: {
      json_schema: {},
      field_tiers: { title: "mergeable" },
      slots: { sections: { allowed_type: "Section" } },
      field_defaults: { title: "" },
    },
    Section: {
      json_schema: {},
      field_tiers: { heading: "mergeable", related: "ref" },
      slots: { items: { allowed_type: "Item" } },
      field_defaults: { heading: "", related: null },
      refs: { related: { target_type: "Item", many: false, policy: "restrict" } },
    },
    Item: {
      json_schema: {},
      field_tiers: { label: "mergeable" },
      slots: { notes: { allowed_type: "Note" } },
      field_defaults: { label: "" },
    },
    Note: {
      json_schema: {},
      field_tiers: { text: "mergeable" },
      slots: {},
      field_defaults: { text: "" },
    },
  },
  value_types: {},
};

const ROOT = "01jqp00000000000000000000";

/** root(stub) > s1(full) > [i1(full) > [n1 stub], i2(stub)]; s1.related -> i3 (detached). */
const partial: JsonDoc = [
  ROOT,
  "Page",
  null,
  {
    sections: [
      [
        "s1",
        "Section",
        { heading: "One", related: "i3" },
        {
          items: [
            ["i1", "Item", { label: "A" }, { notes: [["n1", "Note", null]] }],
            ["i2", "Item", null],
          ],
        },
      ],
    ],
  },
];
const referents: [string, string][] = [["i3", "Item"]];

function makeDoc(): LocalDoc {
  return new LocalDoc(schema, partial, { partial: true, stubs: referents });
}

function apply(doc: LocalDoc, ops: WireOperations): WireOperations[] {
  const events: WireOperations[] = [];
  const off = doc.onChange((e) => events.push(e.operations));
  doc.applyOperations(ops, undefined, true);
  off();
  return events;
}

describe("LocalDoc visibility operations", () => {
  it("[6] fills a stub in place, with the state that follows", () => {
    const doc = makeDoc();
    const i2 = doc.getNode("i2")!;
    const [event] = apply(doc, { ordered: [[6, "i2"]], state: { i2: { label: "B" } } });
    expect(doc.getNode("i2")).toBe(i2);
    expect(i2.stub).toBe(false);
    expect(i2.state.label).toBe("B");
    expect(event.ordered).toEqual([[6, "i2"]]);
    expect(event.state).toEqual({ i2: { label: "B" } });
  });

  it("[6] of a full node or an unknown one is a protocol error", () => {
    const doc = makeDoc();
    expect(() => doc.applyOperations({ ordered: [[6, "i1"]], state: {} }, undefined, true)).toThrow(
      /not a stub/,
    );
    expect(() => doc.applyOperations({ ordered: [[6, "zz"]], state: {} }, undefined, true)).toThrow(
      /does not hold/,
    );
  });

  it("[3] demotes a full node to a stub, dropping its state and references", () => {
    const doc = makeDoc();
    const s1 = doc.getNode("s1")!;
    expect(doc.referrers("i3").map((n) => n.id)).toEqual(["s1"]);
    const [event] = apply(doc, { ordered: [[3, "s1"]], state: {} });
    expect(s1.stub).toBe(true);
    expect(() => s1.state.heading).toThrow(OutOfScopeError);
    expect(doc.referrers("i3")).toEqual([]);
    expect(getSlotChildren(s1, "items").map((n) => n.id)).toEqual(["i1", "i2"]); // children stay
    expect(event.ordered).toEqual([[3, "s1"]]);
  });

  it("a rolled-back demote restores the node's state", () => {
    const doc = makeDoc();
    expect(() =>
      doc.applyOperations(
        { ordered: [[3, "s1"], [6, "i1"]], state: {} }, // the fill of a full node fails
        undefined,
        true,
      ),
    ).toThrow(/not a stub/);
    expect(doc.getNode("s1")!.stub).toBe(false);
    expect(doc.getNode("s1")!.state).toEqual({ heading: "One", related: "i3" });
  });

  it("[4] removes a node and its subtree without a reference-integrity objection", () => {
    const doc = makeDoc();
    // s1 references i3; exiting i3 is not a delete, so it is fine.
    const [event] = apply(doc, { ordered: [[4, "i3"]], state: {} });
    expect(doc.getNode("i3")).toBeUndefined();
    expect(event.ordered).toEqual([[4, "i3"]]);
    // A subtree, too, and an unknown id is ignored.
    const diffs: Array<{ exited: Set<string>; deleted: Map<string, unknown> }> = [];
    doc.onChange((e) => diffs.push(e.diff));
    const [second] = apply(doc, { ordered: [[4, "i1"], [4, "nope"]], state: {} });
    expect(doc.getNode("i1")).toBeUndefined();
    expect(doc.getNode("n1")).toBeUndefined();
    expect(second.ordered).toEqual([[4, "i1"]]);
    expect(diffs[0].exited.has("n1")).toBe(true);
    expect(diffs[0].deleted.size).toBe(0);
  });

  it("[5] takes a node out of the tree as a detached stub, or creates one", () => {
    const doc = makeDoc();
    const [event] = apply(doc, { ordered: [[5, "i1", "Item"], [5, "x9", "Item"]], state: {} });
    const i1 = doc.getNode("i1")!;
    expect(i1.stub).toBe(true);
    expect(i1.parent).toBeNull();
    expect(doc.getNode("n1")).toBeUndefined();
    expect(getSlotChildren(doc.getNode("s1")!, "items").map((n) => n.id)).toEqual(["i2"]);
    expect(doc.getNode("x9")!.stub).toBe(true);
    expect(doc.detachedStubs()).toEqual([["i3", "Item"], ["i1", "Item"], ["x9", "Item"]]);
    expect(event.ordered).toEqual([[5, "i1", "Item"], [5, "x9", "Item"]]);
    // Idempotent on a detached stub.
    expect(apply(doc, { ordered: [[5, "i1", "Item"]], state: {} })).toEqual([]);
  });

  it("an insert may place (and fill) a detached stub, keeping the object", () => {
    const doc = makeDoc();
    const i3 = doc.getNode("i3")!;
    const [event] = apply(doc, {
      ordered: [[0, [["i3", "Item"]], "s1", "items", "i1", "i2"]],
      state: { i3: { label: "C" } },
    });
    expect(doc.getNode("i3")).toBe(i3);
    expect(i3.stub).toBe(false);
    expect(i3.state.label).toBe("C");
    expect(getSlotChildren(doc.getNode("s1")!, "items").map((n) => n.id)).toEqual(["i1", "i3", "i2"]);
    expect(doc.detachedStubs()).toEqual([]);
    expect(event.ordered).toEqual([[0, [["i3", "Item"]], "s1", "items", "i1", "i2"]]);
  });

  it("an insert naming a node already in the tree is refused", () => {
    const doc = makeDoc();
    expect(() =>
      doc.applyOperations({ ordered: [[0, [["i1", "Item"]], "s1", "items", 0, 0]], state: {} }, undefined, true),
    ).toThrow(/already exists/);
  });
});

describe("NodeStore visibility operations", () => {
  function store(): NodeStore {
    const s = new NodeStore();
    s.loadSnapshot(partial, referents);
    return s;
  }

  it("[6] and [3] flip the stub flag", () => {
    const s = store();
    applyPatch(s, { ordered: [[6, "i2"]], state: { i2: { label: "B" } } });
    expect(s.getNode("i2")!.stub).toBeUndefined();
    expect(s.getNode("i2")!.state).toEqual({ label: "B" });
    applyPatch(s, { ordered: [[3, "i2"]], state: {} });
    expect(s.getNode("i2")!.stub).toBe(true);
    expect(s.getNode("i2")!.state).toEqual({});
    expect(s.getChildren("s1", "items")).toEqual(["i1", "i2"]);
  });

  it("[4] removes a subtree from its slot; [5] detaches or creates", () => {
    const s = store();
    applyPatch(s, { ordered: [[4, "i1"], [4, "nope"]], state: {} });
    expect(s.getNode("i1")).toBeUndefined();
    expect(s.getNode("n1")).toBeUndefined();
    expect(s.getChildren("s1", "items")).toEqual(["i2"]);
    applyPatch(s, { ordered: [[5, "i2", "Item"], [5, "x9", "Item"], [5, "i3", "Item"]], state: {} });
    expect(s.getChildren("s1", "items")).toEqual([]);
    expect(s.getNode("i2")).toEqual({
      id: "i2", type: "Item", state: {}, slots: {}, parentId: null, slotName: null, stub: true,
    });
    expect(s.getNode("x9")!.stub).toBe(true);
    expect(s.getNode("i3")!.stub).toBe(true);
  });

  it("an insert places a detached stub, filled when the pair is full", () => {
    const s = store();
    applyPatch(s, {
      ordered: [[0, [["i3", "Item"]], "s1", "items", "i1", "i2"]],
      state: { i3: { label: "C" } },
    });
    expect(s.getChildren("s1", "items")).toEqual(["i1", "i3", "i2"]);
    expect(s.getNode("i3")).toEqual({
      id: "i3", type: "Item", state: { label: "C" }, slots: {}, parentId: "s1", slotName: "items",
    });
  });

  it("an unknown operation throws", () => {
    const s = store();
    expect(() => applyPatch(s, { ordered: [[9, "i1"] as never], state: {} })).toThrow(/Unknown operation/);
  });

  it("the bridge carries every visibility operation to the store", () => {
    const doc = makeDoc();
    const s = new NodeStore();
    const bridge = bridgeDocToStore(doc, s, { coalesce: false });
    doc.applyOperations(
      {
        ordered: [[6, "i2"], [5, "i1", "Item"], [0, [["i3", "Item"]], "s1", "items", 0, "i2"], [3, "s1"]],
        state: { i2: { label: "B" }, i3: { label: "C" } },
      },
      undefined,
      true,
    );
    expect(s.getChildren("s1", "items")).toEqual(["i3", "i2"]);
    expect(s.getNode("i2")!.state).toEqual({ label: "B" });
    expect(s.getNode("i1")!.parentId).toBeNull();
    expect(s.getNode("n1")).toBeUndefined();
    expect(s.getNode("s1")!.stub).toBe(true);
    expect(s.getNode("i3")!.state).toEqual({ label: "C" });
    bridge.dispose();
  });
});

/** A client whose socket is a fake that records what it sends. */
class FakeSocket {
  static instances: FakeSocket[] = [];
  sent: ClientMsg[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }
  send(text: string): void {
    this.sent.push(JSON.parse(text));
  }
  close(): void {
    this.closed = true;
  }
  receive(msg: unknown): void {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
}

async function scopedClient(anchors = [{ id: "s1" }]) {
  FakeSocket.instances = [];
  const client = new ThickAtomDocClient({
    url: "ws://server/doc",
    coalesce: false,
    scope: anchors,
    webSocket: FakeSocket as unknown as new (url: string) => WebSocket,
  });
  const connecting = client.connect();
  const ws = FakeSocket.instances[0];
  ws.onopen?.();
  await connecting;
  ws.receive({ type: "schema", schema } as SchemaMsg);
  return { client, ws };
}

function partialSnapshot(ws: FakeSocket, version = 0): void {
  const scope = ws.sent.find((m) => m.type === "scope") as { ref?: string } | undefined;
  ws.receive({
    type: "snapshot",
    doc_id: ROOT,
    version,
    data: partial,
    partial: true,
    stubs: referents,
    anchors: [{ id: "s1" }],
    client_id: "me",
    ref: scope?.ref ?? null,
  } as SnapshotMsg);
}

describe("ThickAtomDocClient scope", () => {
  it("connects with ?partial=1, sends its scope after the schema, and loads the partial snapshot", async () => {
    const { client, ws } = await scopedClient([{ id: "s1", depth: 1 }]);
    expect(ws.url).toBe("ws://server/doc?partial=1");
    expect(ws.sent).toEqual([{ type: "scope", ref: expect.any(String), anchors: [{ id: "s1", depth: 1 }] }]);
    expect(client.getDoc()).toBeNull();
    partialSnapshot(ws);
    expect(client.getDoc()!.partial).toBe(true);
    expect(client.getUndoManager()).toBeNull();
    expect(client.getScope()).toEqual([{ id: "s1", depth: 1 }]);
    expect(client.getStore().getNode("i3")!.stub).toBe(true);
  });

  it("a URL with a query keeps it", async () => {
    FakeSocket.instances = [];
    const client = new ThickAtomDocClient({
      url: "ws://server/doc?token=x",
      scope: [{ id: "s1" }],
      webSocket: FakeSocket as unknown as new (url: string) => FakeSocket as never,
    });
    void client.connect();
    expect(FakeSocket.instances[0].url).toBe("ws://server/doc?token=x&partial=1");
  });

  it("setScope sends the request and resolves on the ack, which applies as a patch", async () => {
    const { client, ws } = await scopedClient();
    partialSnapshot(ws);
    const patches: number[] = [];
    client.onPatch((v) => patches.push(v));
    const done = client.setScope([{ id: "s1", depth: 1 }]);
    const sent = ws.sent[ws.sent.length - 1] as { type: string; ref: string; anchors: unknown };
    expect(sent.type).toBe("scope");
    expect(sent.anchors).toEqual([{ id: "s1", depth: 1 }]);
    expect(client.getScope()).toEqual([{ id: "s1", depth: 1 }]);
    ws.receive({
      type: "scope_ack",
      ref: sent.ref,
      version: 7,
      operations: { ordered: [[6, "i2"], [4, "n1"]], state: { i2: { label: "B" } } },
      source_client: null,
      anchors: [{ id: "s1", depth: 1 }],
    } as ScopeAckMsg);
    expect(await done).toEqual([{ id: "s1", depth: 1 }]);
    expect(client.getVersion()).toBe(7);
    expect(patches).toEqual([7]);
    expect(client.getState("i2")).toEqual({ label: "B" });
    expect(client.getDoc()!.getNode("n1")).toBeUndefined();
  });

  it("a refused scope rejects the promise", async () => {
    const { client, ws } = await scopedClient();
    partialSnapshot(ws);
    const done = client.setScope([{ id: "s1", depth: -1 }]);
    const sent = ws.sent[ws.sent.length - 1] as { ref: string };
    ws.receive({ type: "error", ref: sent.ref, code: "invalid_op", message: "depth" } as ErrorMsg);
    await expect(done).rejects.toThrow(/invalid_op/);
  });

  it("a scope set before the snapshot is sent with the handshake", async () => {
    FakeSocket.instances = [];
    const client = new ThickAtomDocClient({
      url: "ws://server/doc",
      webSocket: FakeSocket as unknown as new (url: string) => WebSocket,
    });
    void client.setScope([{ id: "s1" }]); // offline: kept for the connection
    const connecting = client.connect();
    const ws = FakeSocket.instances[0];
    ws.onopen?.();
    await connecting;
    expect(ws.url).toBe("ws://server/doc?partial=1");
    ws.receive({ type: "schema", schema } as SchemaMsg);
    expect(ws.sent[0]).toMatchObject({ type: "scope", anchors: [{ id: "s1" }] });
  });

  it("a whole-document client that sets a scope narrows in place", async () => {
    FakeSocket.instances = [];
    const client = new ThickAtomDocClient({
      url: "ws://server/doc",
      coalesce: false,
      webSocket: FakeSocket as unknown as new (url: string) => WebSocket,
    });
    const connecting = client.connect();
    const ws = FakeSocket.instances[0];
    ws.onopen?.();
    await connecting;
    ws.receive({ type: "schema", schema } as SchemaMsg);
    ws.receive({
      type: "snapshot",
      doc_id: ROOT,
      version: 0,
      data: [
        ROOT,
        "Page",
        { title: "T" },
        {
          sections: [
            ["s1", "Section", { heading: "One" }, { items: [] }],
            ["s2", "Section", { heading: "Two" }, { items: [] }],
          ],
        },
      ],
      client_id: "me",
    } as SnapshotMsg);
    expect(client.getUndoManager()).not.toBeNull();
    const done = client.setScope([{ id: "s2" }]);
    const sent = ws.sent[ws.sent.length - 1] as { ref: string };
    ws.receive({
      type: "scope_ack",
      ref: sent.ref,
      version: 1,
      operations: { ordered: [[3, ROOT], [4, "s1"]], state: {} },
      source_client: null,
      anchors: [{ id: "s2" }],
    } as ScopeAckMsg);
    await done;
    expect(client.getDoc()!.partial).toBe(true);
    expect(client.getDoc()!.root.stub).toBe(true);
    expect(client.getDoc()!.getNode("s1")).toBeUndefined();
    expect(client.getState("s2")).toEqual({ heading: "Two", related: null });
    // Local undo is over: the next undo goes to the server.
    client.undo();
    expect(ws.sent[ws.sent.length - 1]).toMatchObject({ type: "undo", steps: 1 });
  });

  it("undo and redo are requests whose answers apply as remote patches", async () => {
    const { client, ws } = await scopedClient();
    partialSnapshot(ws);
    client.setField("i1", "label", "AA");
    const write = ws.sent[ws.sent.length - 1] as { ref: string; operations: WireOperations };
    ws.receive({ type: "patch", version: 1, ref: write.ref, source_client: null, operations: write.operations } as PatchMsg);
    client.undo();
    const undo = ws.sent[ws.sent.length - 1] as { type: string; ref: string };
    expect(undo).toEqual({ type: "undo", ref: undo.ref, steps: 1 });
    const settled = client.settled();
    ws.receive({
      type: "patch",
      version: 2,
      ref: undo.ref,
      source_client: null,
      operations: { ordered: [], state: { i1: { label: "A" } } },
    } as PatchMsg);
    await settled;
    expect(client.getState("i1")).toEqual({ label: "A" });
    client.redo(2);
    expect(ws.sent[ws.sent.length - 1]).toMatchObject({ type: "redo", steps: 2 });
  });

  it("createNode past the depth bound is refused before sending", async () => {
    const { client, ws } = await scopedClient([{ id: "s1", depth: 1 }]);
    partialSnapshot(ws);
    const before = ws.sent.length;
    // i1 is at depth 1 under s1: its children would be stubs.
    expect(() => client.createNode("Note", { text: "x" }, "i1", "notes")).toThrow(OutOfScopeError);
    expect(ws.sent.length).toBe(before);
    // Under s1 itself (depth 0 from the anchor) a child is at depth 1: fine.
    client.createNode("Item", { label: "D" }, "s1", "items");
    expect(ws.sent.length).toBe(before + 1);
  });

  it("out_of_scope is a rejection: the partial snapshot that follows is a resync", async () => {
    const { client, ws } = await scopedClient();
    partialSnapshot(ws);
    const seen: unknown[] = [];
    client.onResync((info) => seen.push(info));
    client.setField("i1", "label", "AA");
    const write = ws.sent[ws.sent.length - 1] as { ref: string };
    ws.receive({ type: "error", ref: write.ref, code: "out_of_scope", message: "no" } as ErrorMsg);
    ws.receive({
      type: "snapshot", doc_id: ROOT, version: 1, data: partial, partial: true, stubs: referents, anchors: [{ id: "s1" }],
    } as SnapshotMsg);
    expect(seen).toEqual([
      { reason: "rejected", undoStepsDropped: 0, redoStepsDropped: 0, schemaChanged: false, partial: true, anchors: [{ id: "s1" }] },
    ]);
    expect(client.getState("i1")).toEqual({ label: "A" });
  });

  it("a patch a scoped view cannot apply is a protocol error and disconnects", async () => {
    const { client, ws } = await scopedClient();
    partialSnapshot(ws);
    const errors: ErrorMsg[] = [];
    client.onError((e) => errors.push(e));
    ws.receive({ type: "patch", version: 1, source_client: null, operations: { ordered: [[6, "i1"]], state: {} } } as PatchMsg);
    expect(errors.map((e) => e.code)).toEqual(["protocol_error"]);
    expect(ws.closed).toBe(true);
    expect(client.isOnline()).toBe(false);
  });

  it("a reconnect re-sends the requested anchors, not the resolved ones", async () => {
    const { client, ws } = await scopedClient([{ id: "s1" }, { id: "gone" }]);
    partialSnapshot(ws); // resolves only s1
    expect(client.getScope()).toEqual([{ id: "s1" }, { id: "gone" }]);
    const reconnecting = client.connect();
    const ws2 = FakeSocket.instances[1];
    ws2.onopen?.();
    await reconnecting;
    ws2.receive({ type: "schema", schema } as SchemaMsg);
    expect(ws2.sent[0]).toMatchObject({ type: "scope", anchors: [{ id: "s1" }, { id: "gone" }] });
    const cb = vi.fn();
    client.onResync(cb);
    partialSnapshot(ws2, 3);
    expect(cb).toHaveBeenCalledWith(expect.objectContaining({ reason: "reconnect", partial: true }));
  });
});
