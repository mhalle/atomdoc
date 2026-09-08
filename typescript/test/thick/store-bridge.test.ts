import { describe, it, expect, vi } from "vitest";
import { NodeStore } from "../../src/store.js";
import { LocalDoc } from "../../src/thick/local-doc.js";
import { bridgeDocToStore } from "../../src/thick/store-bridge.js";
import type { AtomDocSchema, JsonDoc } from "../../src/types.js";

const schema: AtomDocSchema = {
  version: 1,
  root_type: "Page",
  node_types: {
    Page: {
      json_schema: {},
      field_tiers: { title: "mergeable" },
      slots: { items: { allowed_type: "Item" } },
      field_defaults: { title: "" },
    },
    Item: {
      json_schema: {},
      field_tiers: { label: "mergeable" },
      slots: {},
      field_defaults: { label: "" },
    },
  },
  value_types: {},
};

const snapshot: JsonDoc = [
  "01jqp00000000000000000000",
  "Page",
  { title: "Hello" },
  {
    items: [["i1", "Item", { label: "First" }]],
  },
];

describe("bridgeDocToStore", () => {
  it("loads initial state into store", () => {
    const doc = new LocalDoc(schema, snapshot);
    const store = new NodeStore();
    bridgeDocToStore(doc, store, { coalesce: false });

    expect(store.getRootId()).toBe("01jqp00000000000000000000");
    expect(store.getRoot()!.state.title).toBe("Hello");
    expect(store.getChildren(store.getRootId(), "items")).toEqual(["i1"]);
  });

  it("syncs state changes", () => {
    const doc = new LocalDoc(schema, snapshot);
    const store = new NodeStore();
    bridgeDocToStore(doc, store, { coalesce: false });

    doc.setNodeState(doc.id, "title", "Updated");
    expect(store.getRoot()!.state.title).toBe("Updated");
  });

  it("syncs inserts", () => {
    const doc = new LocalDoc(schema, snapshot);
    const store = new NodeStore();
    bridgeDocToStore(doc, store, { coalesce: false });

    const node = doc.createNode("Item", { label: "Second" });
    doc.insertIntoSlot(doc.root, "items", "append", [node]);

    const children = store.getChildren(store.getRootId(), "items");
    expect(children.length).toBe(2);
    expect(store.getNode(node.id)!.state.label).toBe("Second");
  });

  it("syncs deletes", () => {
    const doc = new LocalDoc(schema, snapshot);
    const store = new NodeStore();
    bridgeDocToStore(doc, store, { coalesce: false });

    doc.deleteRange("i1");
    expect(store.getNode("i1")).toBeUndefined();
    expect(store.getChildren(store.getRootId(), "items")).toEqual([]);
  });

  it("unsubscribe stops syncing", () => {
    const doc = new LocalDoc(schema, snapshot);
    const store = new NodeStore();
    const unsub = bridgeDocToStore(doc, store, { coalesce: false });

    unsub();
    doc.setNodeState(doc.id, "title", "Should not sync");
    expect(store.getRoot()!.state.title).toBe("Hello");
  });
});

describe("bridgeDocToStore coalescing", () => {
  const frame = () => new Promise((r) => setTimeout(r, 5));

  it("applies a burst of changes to the store once per frame", async () => {
    const doc = new LocalDoc(schema, snapshot);
    const store = new NodeStore();
    const bridge = bridgeDocToStore(doc, store);
    const rootNotified = vi.fn();
    const allNotified = vi.fn();
    store.subscribe(doc.id, rootNotified);
    store.subscribeAll(allNotified);

    for (let i = 0; i < 50; i++) doc.setNodeState(doc.id, "title", `t${i}`);
    doc.setNodeState("i1", "label", "changed");
    expect(bridge.pending).toBe(true);
    expect(store.getRoot()!.state.title).toBe("Hello");
    expect(allNotified).not.toHaveBeenCalled();

    await frame();
    expect(bridge.pending).toBe(false);
    expect(store.getRoot()!.state.title).toBe("t49");
    expect(store.getNode("i1")!.state.label).toBe("changed");
    expect(rootNotified).toHaveBeenCalledTimes(1);
    expect(allNotified).toHaveBeenCalledTimes(1);
  });

  it("keeps the store consistent across inserts, moves, and deletes in one frame", async () => {
    const doc = new LocalDoc(schema, snapshot);
    const store = new NodeStore();
    const bridge = bridgeDocToStore(doc, store);
    const a = doc.createNode("Item", { label: "a" });
    const b = doc.createNode("Item", { label: "b" });
    doc.insertIntoSlot(doc.root, "items", "append", [a, b]);
    doc.moveRangeRelative(b.id, undefined, "i1", "before");
    doc.deleteRange(a.id);
    doc.setNodeState(b.id, "label", "b2");
    bridge.flush();
    expect(store.getChildren(doc.id, "items")).toEqual([b.id, "i1"]);
    expect(store.getNode(a.id)).toBeUndefined();
    expect(store.getNode(b.id)!.state.label).toBe("b2");
    // Nothing left for the frame.
    expect(bridge.pending).toBe(false);
  });

  it("flush is idempotent and a flushed frame does not fire again", async () => {
    const doc = new LocalDoc(schema, snapshot);
    const store = new NodeStore();
    const bridge = bridgeDocToStore(doc, store);
    const notified = vi.fn();
    store.subscribe(doc.id, notified);
    doc.setNodeState(doc.id, "title", "x");
    bridge.flush();
    bridge.flush();
    await frame();
    expect(notified).toHaveBeenCalledTimes(1);
  });

  it("dispose drops queued changes", async () => {
    const doc = new LocalDoc(schema, snapshot);
    const store = new NodeStore();
    const bridge = bridgeDocToStore(doc, store);
    doc.setNodeState(doc.id, "title", "queued");
    bridge.dispose();
    await frame();
    expect(store.getRoot()!.state.title).toBe("Hello");
    doc.setNodeState(doc.id, "title", "after");
    await frame();
    expect(store.getRoot()!.state.title).toBe("Hello");
  });

  it("a numeric coalesce is a fixed window, frames or not", async () => {
    const doc = new LocalDoc(schema, snapshot);
    const store = new NodeStore();
    bridgeDocToStore(doc, store, { coalesce: 30 });
    const notified = vi.fn();
    store.subscribe(doc.id, notified);
    doc.setNodeState(doc.id, "title", "a");
    await new Promise((r) => setTimeout(r, 10));
    doc.setNodeState(doc.id, "title", "b"); // within the window
    expect(notified).not.toHaveBeenCalled();
    await new Promise((r) => setTimeout(r, 40));
    expect(notified).toHaveBeenCalledTimes(1);
    expect(store.getRoot()!.state.title).toBe("b");
  });

  it("uses requestAnimationFrame when the host provides one", async () => {
    const g = globalThis as { requestAnimationFrame?: unknown; cancelAnimationFrame?: unknown };
    const frames: Array<() => void> = [];
    g.requestAnimationFrame = (cb: () => void) => frames.push(cb);
    g.cancelAnimationFrame = () => {};
    try {
      const doc = new LocalDoc(schema, snapshot);
      const store = new NodeStore();
      bridgeDocToStore(doc, store);
      doc.setNodeState(doc.id, "title", "raf");
      expect(frames.length).toBe(1);
      expect(store.getRoot()!.state.title).toBe("Hello");
      frames[0]();
      expect(store.getRoot()!.state.title).toBe("raf");
    } finally {
      delete g.requestAnimationFrame;
      delete g.cancelAnimationFrame;
    }
  });
});
