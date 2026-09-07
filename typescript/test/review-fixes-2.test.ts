import { describe, it, expect, vi } from "vitest";
import { z } from "zod";
import { LocalDoc, ListenerError } from "../src/thick/local-doc.js";
import { withTransaction } from "../src/thick/local-transaction.js";
import { UndoManager } from "../src/thick/undo-manager.js";
import { ThickAtomDocClient } from "../src/thick/thick-client.js";
import { SchemaRegistry } from "../src/schema.js";
import { getSlotChildren } from "../src/thick/doc-node.js";
import type { AtomDocSchema, JsonDoc, ServerMsg, WireOperations } from "../src/types.js";

const schema: AtomDocSchema = {
  version: 1,
  root_type: "Scene",
  node_types: {
    Scene: {
      json_schema: {},
      field_tiers: { title: "mergeable" },
      slots: { children: { allowed_type: "Item" } },
      field_defaults: { title: "" },
    },
    Item: {
      json_schema: {
        properties: {
          name: { type: "string" },
          src: {},
          srcs: { type: "array" },
          byName: { type: "object", additionalProperties: {} },
        },
      },
      field_tiers: {
        name: "mergeable",
        src: "atomic",
        srcs: "atomic",
        byName: "mergeable",
      },
      slots: { kids: { allowed_type: "Item" } },
      field_defaults: { name: "", src: null, srcs: [], byName: {} },
      handles: {
        src: { value_type: "Blob", strength: "strong" },
        srcs: { value_type: "Blob", strength: "strong" },
        byName: { value_type: "Blob", strength: "weak" },
      },
    },
  },
  value_types: {
    Blob: { json_schema: {}, frozen: true, handle: { strength: "strong" } },
  },
};

const snapshot: JsonDoc = [
  "01jqp00000000000000000000",
  "Scene",
  {},
  {
    children: [
      ["a", "Item", { name: "a" }],
      ["b", "Item", { name: "b" }],
      ["c", "Item", { name: "c" }],
      ["d", "Item", { name: "d" }],
    ],
  },
];

const make = () => new LocalDoc(schema, snapshot);
const ids = (doc: LocalDoc) => getSlotChildren(doc.root, "children").map((n) => n.id);
const insert = (id: string, prev: string | 0 = 0): WireOperations["ordered"][number] =>
  [0, [[id, "Item"]], 0, "children", prev, 0];

describe("second review: LocalDoc transactions", () => {
  it("one failing op rolls back the whole batch and keeps the flags", () => {
    const doc = make();
    const events: unknown[] = [];
    doc.onChange((e) => events.push(e.flags));
    const um = new UndoManager(doc, 100);
    doc.applyOperations(
      { ordered: [insert("n1", "a"), insert("a"), insert("n2")], state: { n1: { name: "one" } } },
      { skipUndo: true },
    );
    expect(ids(doc)).toEqual(["a", "b", "c", "d"]);
    expect(doc.getNode("n1")).toBeUndefined();
    expect(events).toEqual([]);
    expect(um.canUndo).toBe(false);
    expect(() =>
      doc.applyOperations({ ordered: [insert("a")], state: {} }, undefined, true),
    ).toThrow(/already exists/);
  });

  it("a failing nested apply aborts the enclosing transaction as a whole", () => {
    const doc = make();
    expect(() =>
      withTransaction(doc, () => {
        doc.setNodeState("a", "name", "eleven");
        doc.applyOperations({ ordered: [insert("a")], state: {} });
        doc.setNodeState("a", "name", "twenty-two");
      }),
    ).toThrow(/already exists/);
    expect(doc.getNode("a")!.state.name).toBe("a");
    expect(doc._lifecycleStage).toBe("idle");
  });

  it("a caught failure inside a transaction leaves it consistent", () => {
    const doc = make();
    const events: unknown[] = [];
    doc.onChange((e) => events.push(e));
    withTransaction(doc, () => {
      doc.setNodeState("a", "name", "x");
      expect(() => doc.deleteRange("b", "a")).toThrow(/later sibling/);
      doc.setNodeState("a", "name", "y");
    });
    expect(doc.getNode("a")!.state.name).toBe("y");
    expect(ids(doc)).toEqual(["a", "b", "c", "d"]);
    expect(events.length).toBe(1);
  });

  it("a skipped move conflict does not lose other ops or split events", () => {
    const doc = make();
    const events: unknown[] = [];
    doc.onChange((e) => events.push(e));
    doc.applyOperations({
      ordered: [
        [2, "c", 0, "a", "kids", 0, 0],
        [2, "a", 0, "b", "kids", 0, 0],
        [2, "a", 0, "c", "kids", 0, 0], // cycle: c is inside a
        [1, "d", 0],
      ],
      state: {},
    });
    expect(ids(doc)).toEqual(["b"]);
    expect(getSlotChildren(doc.getNode("b")!, "kids").map((n) => n.id)).toEqual(["a"]);
    expect(getSlotChildren(doc.getNode("a")!, "kids").map((n) => n.id)).toEqual(["c"]);
    expect(events.length).toBe(1);
  });

  it("an undo that cannot apply is kept and reported", () => {
    const doc = make();
    const um = new UndoManager(doc, 100, { mergeInterval: 1_000_000 });
    doc.deleteRange("d");
    doc.deleteRange("c");
    doc.deleteRange("b");
    // A remote echo re-creates "c" before the user undoes.
    doc.applyOperations({ ordered: [insert("c")], state: {} }, { skipUndo: true });
    expect(() => um.undo()).toThrow(/already exists/);
    expect(ids(doc)).toEqual(["a", "c"]);
    expect(um.canUndo).toBe(true);
    expect(um.canRedo).toBe(false);
    // The conflicting node goes away again (a remote delete); the kept
    // step then applies.
    doc.applyOperations({ ordered: [[1, "c", 0]], state: {} }, { skipUndo: true });
    um.undo();
    expect(ids(doc)).toEqual(["a", "b", "c", "d"]);
    expect(um.canRedo).toBe(true);
  });

  it("rejects an unknown slot, an unknown field, and an unknown opcode", () => {
    const doc = make();
    const n = doc.createNode("Item", { name: "ghost" });
    expect(() => doc.insertIntoSlot(doc.root, "typo", "append", [n])).toThrow(/Slot 'typo'/);
    expect(doc.getNode(n.id)).toBeUndefined();
    expect(() => doc.setNodeState("a", "nope", 1)).toThrow(/no field/);
    expect(() => doc.createNode("Item", { nope: 1 })).toThrow(/no field/);
    expect(() =>
      doc.applyOperations({ ordered: [], state: { a: { nope: 1 } } }, undefined, true),
    ).toThrow(/no field/);
    expect(() =>
      doc.applyOperations(
        { ordered: [[99, "x", 0] as unknown as WireOperations["ordered"][number]], state: {} },
        undefined,
        true,
      ),
    ).toThrow(/Unknown operation code/);
    expect(doc.toSnapshot()).toEqual(make().toSnapshot());
  });

  it("change listeners are post-commit observers", () => {
    const doc = make();
    const um = new UndoManager(doc, 100);
    const first: unknown[] = [];
    const last: unknown[] = [];
    doc.onChange((e) => first.push(structuredClone(e.operations)));
    const unsubscribe = doc.onChange(() => {
      throw new Error("listener boom");
    });
    doc.onChange((e) => last.push(e));
    let caught: unknown;
    try {
      doc.setNodeState("a", "name", "CHANGED");
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ListenerError);
    expect((caught as ListenerError).cause).toEqual(new Error("listener boom"));
    // The commit stands, every listener ran, and undo knows about it.
    expect(doc.getNode("a")!.state.name).toBe("CHANGED");
    expect(doc._lifecycleStage).toBe("idle");
    expect(first).toEqual([{ ordered: [], state: { a: { name: "CHANGED" } } }]);
    expect(last.length).toBe(1);
    expect(um.canUndo).toBe(true);
    unsubscribe();
    um.undo();
    expect(doc.getNode("a")!.state.name).toBe("a");
    // A listener failing during an undo does not put the step back.
    doc.onChange(() => {
      throw new Error("again");
    });
    expect(() => um.redo()).toThrow(ListenerError);
    expect(doc.getNode("a")!.state.name).toBe("CHANGED");
    expect(um.canRedo).toBe(false);
    expect(um.canUndo).toBe(true);
    // Lenient applyOperations never swallows a listener failure.
    expect(() =>
      doc.applyOperations({ ordered: [], state: { b: { name: "x" } } }),
    ).toThrow(ListenerError);
    expect(doc.getNode("b")!.state.name).toBe("x");
  });

  it("a node handle survives undo and rollback", () => {
    const doc = make();
    const um = new UndoManager(doc, 100);
    const b = doc.getNode("b")!;
    doc.deleteRange("b");
    um.undo();
    expect(doc.getNode("b")).toBe(b);
    expect(b.parent).toBe(doc.root);
    doc.setNodeState("b", "name", "still me");
    expect(doc.getNode("b")!.state.name).toBe("still me");
    expect(() =>
      withTransaction(doc, () => {
        doc.deleteRange("c");
        throw new Error("boom");
      }),
    ).toThrow("boom");
    expect(ids(doc)).toEqual(["a", "b", "c", "d"]);
    // A stale object for a live ID is refused rather than corrupting the tree.
    const stale = { ...b, slotFirst: new Map(b.slotFirst), slotLast: new Map(b.slotLast) };
    expect(() => doc.insertIntoSlot(doc.root, "children", "after", [doc.createNode("Item")], stale)).toThrow(/stale/);
  });

  it("finds handles held in arrays and maps", () => {
    const doc = make();
    doc.setNodeState("a", "src", { uri: "f://1" });
    doc.setNodeState("b", "srcs", [{ uri: "f://2" }, { uri: "f://3" }]);
    doc.setNodeState("c", "byName", { x: { uri: "f://4" } });
    const uris = doc.handles().map((h) => h.handle.uri).sort();
    expect(uris).toEqual(["f://1", "f://2", "f://3", "f://4"]);
    expect(doc.handles("strong").length).toBe(3);
  });
});

// --- Thick client with a fake socket ---

class FakeWS {
  static instances: FakeWS[] = [];
  onopen: (() => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  sent: string[] = [];
  closed = false;
  constructor(public url: string) {
    FakeWS.instances.push(this);
  }
  send(text: string) {
    this.sent.push(text);
  }
  close() {
    this.closed = true;
  }
  deliver(msg: ServerMsg) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
}

async function withFakeWebSocket<T>(fn: () => Promise<T>): Promise<T> {
  const g = globalThis as { WebSocket?: unknown };
  const saved = g.WebSocket;
  g.WebSocket = FakeWS;
  FakeWS.instances = [];
  try {
    return await fn();
  } finally {
    g.WebSocket = saved;
  }
}

const boot = (ws: FakeWS, version = 0, data: JsonDoc = snapshot) => {
  ws.deliver({ type: "schema", schema });
  ws.deliver({ type: "snapshot", doc_id: snapshot[0], version, data, client_id: "me" });
};

const sentOps = (ws: FakeWS): Array<{ ref?: string; operations?: WireOperations }> =>
  ws.sent.map((t) => JSON.parse(t));

describe("second review: thick client", () => {
  it("ignores a late close from a socket that was already replaced", async () => {
    await withFakeWebSocket(async () => {
      const c = new ThickAtomDocClient({ url: "ws://x" });
      const first = c.connect();
      const ws1 = FakeWS.instances[0];
      ws1.onerror?.(new Error("refused"));
      await expect(first).rejects.toBeTruthy();
      const second = c.connect();
      const ws2 = FakeWS.instances[1];
      ws2.onopen?.();
      await second;
      boot(ws2);
      const offline = vi.fn();
      c.onOffline(offline);
      ws1.onclose?.();
      expect(c.isOnline()).toBe(true);
      expect(offline).not.toHaveBeenCalled();
      c.setField("a", "name", "sent");
      expect(sentOps(ws2).length).toBe(1);
    });
  });

  it("resends operations that were in flight when the socket dropped", async () => {
    await withFakeWebSocket(async () => {
      const c = new ThickAtomDocClient({ url: "ws://x" });
      const p = c.connect();
      const ws1 = FakeWS.instances[0];
      ws1.onopen?.();
      await p;
      boot(ws1);
      c.setField("a", "name", "typed");
      expect(sentOps(ws1).length).toBe(1);
      ws1.onclose?.(); // the server never saw it
      expect(c.isOnline()).toBe(false);
      const p2 = c.connect();
      const ws2 = FakeWS.instances[1];
      ws2.onopen?.();
      await p2;
      boot(ws2, 5); // the snapshot still has the old value
      expect(sentOps(ws2).length).toBe(1);
      expect(sentOps(ws2)[0].operations!.state.a.name).toBe("typed");
      expect(c.getDoc()!.getNode("a")!.state.name).toBe("typed");
    });
  });

  it("matches echoes by ref, not by count", async () => {
    await withFakeWebSocket(async () => {
      const c = new ThickAtomDocClient({ url: "ws://x" });
      const p = c.connect();
      const ws = FakeWS.instances[0];
      ws.onopen?.();
      await p;
      boot(ws);
      c.setField("a", "name", "A");
      c.setField("b", "name", "B");
      const [op1, op2] = sentOps(ws);
      expect(op1.ref).toBeDefined();
      ws.deliver({
        type: "patch", version: 1, source_client: "me", ref: op1.ref,
        operations: { ordered: [], state: { a: { name: "A" } } },
      });
      // The server normalized the same request into a second commit.
      ws.deliver({
        type: "patch", version: 2, source_client: null, ref: op1.ref,
        operations: { ordered: [], state: { a: { name: "A-normalized" } } },
      });
      expect(c.getDoc()!.getNode("a")!.state.name).toBe("A-normalized");
      expect(c.getStore().getNode("a")!.state.name).toBe("A-normalized");
      // Another client's change arrives before our second echo.
      ws.deliver({
        type: "patch", version: 3, source_client: "other", ref: "their-op",
        operations: { ordered: [], state: { c: { name: "C" } } },
      });
      ws.deliver({
        type: "patch", version: 4, source_client: "me", ref: op2.ref,
        operations: { ordered: [], state: { b: { name: "B" } } },
      });
      const internals = c as unknown as { pendingOps: unknown[] };
      expect(internals.pendingOps).toEqual([]);
      expect(sentOps(ws).length).toBe(2);
      expect(c.getDoc()!.getNode("c")!.state.name).toBe("C");
    });
  });

  it("another client's ref never retires our pending work", async () => {
    await withFakeWebSocket(async () => {
      const c = new ThickAtomDocClient({ url: "ws://x" });
      const p = c.connect();
      const ws1 = FakeWS.instances[0];
      ws1.onopen?.();
      await p;
      boot(ws1);
      c.setField("a", "name", "mine");
      const [op] = sentOps(ws1);
      expect(op.ref).toBe("me:1");
      // Another client's first request also numbered 1.
      ws1.deliver({
        type: "patch", version: 1, source_client: "other", ref: "other:1",
        operations: { ordered: [], state: { b: { name: "theirs" } } },
      });
      const internals = c as unknown as { pendingOps: Array<{ ref: string }> };
      expect(internals.pendingOps.map((x) => x.ref)).toEqual(["me:1"]);
      // Even a ref that collides textually with ours is ignored unless
      // it is ours: the prefix is the server-assigned client id.
      ws1.deliver({
        type: "patch", version: 2, source_client: "other", ref: "op-1",
        operations: { ordered: [], state: { c: { name: "c2" } } },
      });
      expect(internals.pendingOps.length).toBe(1);
      // The socket drops before our op is acknowledged: it is replayed.
      ws1.onclose?.();
      const p2 = c.connect();
      const ws2 = FakeWS.instances[1];
      ws2.onopen?.();
      await p2;
      boot(ws2, 5);
      expect(sentOps(ws2).map((x) => x.operations!.state.a?.name)).toEqual(["mine"]);
    });
  });

  it("converges on the server's order when a remote change lands before our echo", async () => {
    await withFakeWebSocket(async () => {
      const c = new ThickAtomDocClient({ url: "ws://x" });
      const p = c.connect();
      const ws = FakeWS.instances[0];
      ws.onopen?.();
      await p;
      boot(ws);
      // The reviewer's case: we write "mine", a device wrote "theirs" just
      // before on the server, so the server ends at "mine".
      c.setField("a", "name", "mine");
      const [op1] = sentOps(ws);
      ws.deliver({
        type: "patch", version: 1, source_client: null, ref: null,
        operations: { ordered: [], state: { a: { name: "theirs" }, b: { name: "b2" } } },
      });
      // The remote write to a field we have pending is masked: the local
      // document never shows "theirs". Other fields apply.
      expect(c.getDoc()!.getNode("a")!.state.name).toBe("mine");
      expect(c.getDoc()!.getNode("b")!.state.name).toBe("b2");
      ws.deliver({
        type: "patch", version: 2, source_client: "me", ref: op1.ref,
        operations: { ordered: [], state: { a: { name: "mine" } } },
      });
      expect(c.getDoc()!.getNode("a")!.state.name).toBe("mine");
      expect(c.getStore().getNode("a")!.state.name).toBe("mine");
      expect(sentOps(ws).length).toBe(1); // the reconcile is not sent back
      expect(c.getUndoManager()!.canUndo).toBe(true); // and not a new undo step
      // Undoing our edit reveals what the other party wrote underneath,
      // not what we saw before editing.
      c.undo();
      expect(c.getDoc()!.getNode("a")!.state.name).toBe("theirs");
      expect(sentOps(ws).length).toBe(2);
      ws.deliver({
        type: "patch", version: 3, source_client: "me", ref: sentOps(ws)[1].ref,
        operations: { ordered: [], state: { a: { name: "theirs" } } },
      });

      // Ordering: we append "u" after "d"; the device appended "x" after
      // "d" first, so the server placed "u" between "d" and "x".
      const uId = c.createNode("Item", { name: "u" }, c.getDoc()!.root.id, "children");
      const op2 = sentOps(ws).at(-1)!;
      ws.deliver({
        type: "patch", version: 3, source_client: null, ref: null,
        operations: { ordered: [[0, [["x", "Item"]], 0, "children", "d", 0]], state: {} },
      });
      expect(ids(c.getDoc()!)).toEqual(["a", "b", "c", "d", "x", uId]);
      // The server recorded the insert with a different neighbor than we
      // sent, so it is not a verbatim echo: source_client is null, but
      // the ref is ours and that is what identifies our request.
      ws.deliver({
        type: "patch", version: 4, source_client: null, ref: op2.ref,
        operations: { ordered: [[0, [[uId, "Item"]], 0, "children", "d", "x"]], state: { [uId]: { name: "u" } } },
      });
      expect(ids(c.getDoc()!)).toEqual(["a", "b", "c", "d", uId, "x"]);
      expect(sentOps(ws).length).toBe(3); // mine, the undo, the insert
      expect((c as unknown as { pendingOps: unknown[] }).pendingOps).toEqual([]);
      // A node a server-side normalizer added alongside ours is inserted.
      c.setField("a", "name", "again");
      const op3 = sentOps(ws).at(-1)!;
      ws.deliver({
        type: "patch", version: 5, source_client: null, ref: op3.ref,
        operations: {
          ordered: [[0, [["norm", "Item"]], 0, "children", 0, "a"]],
          state: { a: { name: "again" }, norm: { name: "n" } },
        },
      });
      expect(ids(c.getDoc()!)).toEqual(["norm", "a", "b", "c", "d", uId, "x"]);
      expect(c.getDoc()!.getNode("norm")!.state.name).toBe("n");
    });
  });

  it("masks under merged undo entries and pending moves", async () => {
    await withFakeWebSocket(async () => {
      const c = new ThickAtomDocClient({ url: "ws://x", mergeInterval: 1_000_000 });
      const p = c.connect();
      const ws = FakeWS.instances[0];
      ws.onopen?.();
      await p;
      boot(ws);
      // Two pending writes to the same field, merged into one undo step,
      // plus a pending move of "d" to the front.
      c.setField("a", "name", "first");
      c.setField("a", "name", "second");
      c.moveNodeRelative("d", "a", "before");
      expect(ids(c.getDoc()!)).toEqual(["d", "a", "b", "c"]);
      const [op1, op2, op3] = sentOps(ws);
      ws.deliver({
        type: "patch", version: 1, source_client: null, ref: null,
        operations: {
          ordered: [[2, "d", 0, 0, "children", "b", "c"]], // remote moved d after b
          state: { a: { name: "remote" } },
        },
      });
      // Both the write and the move are masked: our pending edits win on
      // the server, so the local document already shows the outcome.
      expect(c.getDoc()!.getNode("a")!.state.name).toBe("second");
      expect(ids(c.getDoc()!)).toEqual(["d", "a", "b", "c"]);
      // The echo of the older write does not overwrite the newer pending one.
      ws.deliver({
        type: "patch", version: 2, source_client: "me", ref: op1.ref,
        operations: { ordered: [], state: { a: { name: "first" } } },
      });
      expect(c.getDoc()!.getNode("a")!.state.name).toBe("second");
      ws.deliver({
        type: "patch", version: 3, source_client: "me", ref: op2.ref,
        operations: { ordered: [], state: { a: { name: "second" } } },
      });
      ws.deliver({
        type: "patch", version: 4, source_client: "me", ref: op3.ref,
        operations: { ordered: [[2, "d", 0, 0, "children", 0, "a"]], state: {} },
      });
      expect((c as unknown as { pendingOps: unknown[] }).pendingOps).toEqual([]);
      expect(c.getDoc()!.getNode("a")!.state.name).toBe("second");
      // The merged undo step restores the field to the remote's value.
      c.undo();
      expect(c.getDoc()!.getNode("a")!.state.name).toBe("remote");
    });
  });

  it("does not resurrect a node a later pending op deleted, and keeps the echo's next hint", async () => {
    await withFakeWebSocket(async () => {
      const c = new ThickAtomDocClient({ url: "ws://x" });
      const p = c.connect();
      const ws = FakeWS.instances[0];
      ws.onopen?.();
      await p;
      boot(ws);
      // Create then delete before the create's echo arrives (create + Ctrl-Z).
      const ghost = c.createNode("Item", { name: "ghost" }, c.getDoc()!.root.id, "children");
      c.deleteNode(ghost);
      const [op1, op2] = sentOps(ws);
      ws.deliver({ type: "patch", version: 1, source_client: "me", ref: op1.ref, operations: op1.operations! });
      expect(c.getDoc()!.getNode(ghost)).toBeUndefined();
      ws.deliver({ type: "patch", version: 2, source_client: "me", ref: op2.ref, operations: op2.operations! });
      expect(ids(c.getDoc()!)).toEqual(["a", "b", "c", "d"]);
      // Insert after "a", then delete "a"; the verbatim echoes must be no-ops
      // because the echo's `next` hint places the node when `prev` is gone.
      const n = c.getDoc()!.createNode("Item", { name: "n1" });
      c.getDoc()!.insertIntoSlot(c.getDoc()!.root, "children", "after", [n], c.getDoc()!.getNode("a")!);
      c.deleteNode("a");
      const [, , op3, op4] = sentOps(ws);
      expect(ids(c.getDoc()!)).toEqual([n.id, "b", "c", "d"]);
      ws.deliver({ type: "patch", version: 3, source_client: "me", ref: op3.ref, operations: op3.operations! });
      ws.deliver({ type: "patch", version: 4, source_client: "me", ref: op4.ref, operations: op4.operations! });
      expect(ids(c.getDoc()!)).toEqual([n.id, "b", "c", "d"]);
    });
  });

  it("applies a no-op echo as a correction to the server's placement", async () => {
    await withFakeWebSocket(async () => {
      const c = new ThickAtomDocClient({ url: "ws://x" });
      const p = c.connect();
      const ws = FakeWS.instances[0];
      ws.onopen?.();
      await p;
      boot(ws);
      // We move d before a; another client moved a after d first, so on
      // the server our move was already satisfied and committed nothing.
      c.moveNodeRelative("d", "a", "before");
      const [op1] = sentOps(ws);
      expect(ids(c.getDoc()!)).toEqual(["d", "a", "b", "c"]);
      ws.deliver({
        type: "patch", version: 1, source_client: null, ref: "other:1",
        operations: { ordered: [[2, "a", 0, 0, "children", "d", 0]], state: {} },
      });
      // Masked: we have a pending move of d, but not of a — a moves after d locally.
      expect(ids(c.getDoc()!)).toEqual(["d", "a", "b", "c"]);
      // The server answers at the same version with the slot's full order
      // as a chain of moves: the remote move was vacuous in our frame, so
      // more than the moved node is out of place.
      ws.deliver({
        type: "patch", version: 1, source_client: null, ref: op1.ref,
        operations: {
          ordered: [
            [2, "b", 0, 0, "children", 0, 0],
            [2, "c", 0, 0, "children", "b", 0],
            [2, "d", 0, 0, "children", "c", 0],
            [2, "a", 0, 0, "children", "d", 0],
          ],
          state: {},
        },
      });
      expect(ids(c.getDoc()!)).toEqual(["b", "c", "d", "a"]);
      expect(c.getVersion()).toBe(1);
    });
  });

  it("fires online callbacks after the reconnect snapshot is applied", async () => {
    await withFakeWebSocket(async () => {
      const c = new ThickAtomDocClient({ url: "ws://x" });
      const p = c.connect();
      const ws1 = FakeWS.instances[0];
      ws1.onopen?.();
      await p;
      boot(ws1);
      ws1.onclose?.();
      const seen: unknown[] = [];
      c.onOnline(() => seen.push(c.getDoc()!.getNode("a")!.state.name));
      const p2 = c.connect();
      const ws2 = FakeWS.instances[1];
      ws2.onopen?.();
      await p2;
      expect(seen).toEqual([]);
      const changed = structuredClone(snapshot);
      changed[3]!.children[0][2].name = "SERVER";
      boot(ws2, 9, changed);
      expect(seen).toEqual(["SERVER"]);
      expect(c.isOnline()).toBe(true);
    });
  });
});

describe("second review: schema conversion", () => {
  const registry = (props: Record<string, unknown>) =>
    new SchemaRegistry({
      version: 1,
      root_type: "N",
      node_types: {
        N: {
          json_schema: { type: "object", properties: props, required: [] },
          field_tiers: {},
          slots: {},
          field_defaults: {},
        },
      },
      value_types: {},
    });

  it("translates Python regex syntax and tolerates what it cannot compile", () => {
    const r = registry({
      code: { type: "string", pattern: "(?P<n>\\d+)" },
      exact: { type: "string", pattern: "\\Afoo\\Z" },
      flagged: { type: "string", pattern: "(?i)abc" },
    });
    expect(() => r.getZodSchema("N")).not.toThrow();
    expect(() => r.validate("N", { code: "12", exact: "foo", flagged: "zzz" })).not.toThrow();
    expect(() => r.validate("N", { code: "xx" })).toThrow();
    expect(() => r.validate("N", { exact: "xfoo" })).toThrow();
  });

  it("handles type arrays and allOf instead of accepting anything", () => {
    const r = registry({
      maybe: { type: ["string", "null"] },
      both: { allOf: [{ type: "object", properties: { x: { type: "number" } }, required: ["x"] }, { type: "object", properties: { y: { type: "string" } }, required: ["y"] }] },
    });
    expect(() => r.validate("N", { maybe: null })).not.toThrow();
    expect(() => r.validate("N", { maybe: "s" })).not.toThrow();
    expect(() => r.validate("N", { maybe: 42 })).toThrow();
    expect(() => r.validate("N", { both: { x: 1, y: "a" } })).not.toThrow();
    expect(() => r.validate("N", { both: "totally wrong" })).toThrow();
    expect(() => r.validate("N", { both: { x: 1 } })).toThrow();
  });

  it("detects a discriminator whose tag has a default", () => {
    const r = registry({
      shape: {
        discriminator: { propertyName: "kind" },
        oneOf: [
          { type: "object", properties: { kind: { const: "circle", default: "circle" }, r: { type: "number" } }, required: ["r"] },
          { type: "object", properties: { kind: { const: "square", default: "square" }, s: { type: "number" } }, required: ["s"] },
        ],
      },
    });
    const obj = r.getZodSchema("N") as z.ZodObject<Record<string, z.ZodType>>;
    let inner: z.ZodType = obj.shape.shape;
    while (inner instanceof z.ZodOptional || inner instanceof z.ZodDefault) {
      inner = (inner._def as { innerType: z.ZodType }).innerType;
    }
    expect(inner).toBeInstanceOf(z.ZodDiscriminatedUnion);
  });
});
