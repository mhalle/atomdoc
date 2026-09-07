import { describe, it, expect, vi } from "vitest";
import { NodeStore } from "../src/store.js";
import type { JsonDoc } from "../src/types.js";

const snapshot: JsonDoc = [
  "doc-1",
  "Page",
  { title: "Hello" },
  {
    annotations: [
      ["ann-1", "Annotation", { label: "First" }],
      ["ann-2", "Annotation", { label: "Second" }],
    ],
  },
];

describe("NodeStore", () => {
  it("loads a snapshot", () => {
    const store = new NodeStore();
    store.loadSnapshot(snapshot);

    expect(store.getRootId()).toBe("doc-1");
    const root = store.getRoot();
    expect(root).toBeDefined();
    expect(root!.type).toBe("Page");
    expect(root!.state.title).toBe("Hello");
  });

  it("flattens children into store", () => {
    const store = new NodeStore();
    store.loadSnapshot(snapshot);

    const ann1 = store.getNode("ann-1");
    expect(ann1).toBeDefined();
    expect(ann1!.type).toBe("Annotation");
    expect(ann1!.state.label).toBe("First");
    expect(ann1!.parentId).toBe("doc-1");
    expect(ann1!.slotName).toBe("annotations");
  });

  it("tracks children IDs in parent slots", () => {
    const store = new NodeStore();
    store.loadSnapshot(snapshot);

    const children = store.getChildren("doc-1", "annotations");
    expect(children).toEqual(["ann-1", "ann-2"]);
  });

  it("returns empty for unknown node", () => {
    const store = new NodeStore();
    expect(store.getNode("nope")).toBeUndefined();
  });

  it("returns empty for unknown slot", () => {
    const store = new NodeStore();
    store.loadSnapshot(snapshot);
    expect(store.getChildren("doc-1", "bogus")).toEqual([]);
  });

  it("updates state", () => {
    const store = new NodeStore();
    store.loadSnapshot(snapshot);
    store._updateState("doc-1", "title", "Updated");
    expect(store.getRoot()!.state.title).toBe("Updated");
  });

  it("sets children", () => {
    const store = new NodeStore();
    store.loadSnapshot(snapshot);
    store._setChildren("doc-1", "annotations", ["ann-2"]);
    expect(store.getChildren("doc-1", "annotations")).toEqual(["ann-2"]);
  });

  it("removes node", () => {
    const store = new NodeStore();
    store.loadSnapshot(snapshot);
    store._removeNode("ann-1");
    expect(store.getNode("ann-1")).toBeUndefined();
  });

  it("fires per-node listener on update", () => {
    const store = new NodeStore();
    store.loadSnapshot(snapshot);
    const cb = vi.fn();
    store.subscribe("doc-1", cb);
    store._updateState("doc-1", "title", "New");
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it("fires global listener on any update", () => {
    const store = new NodeStore();
    store.loadSnapshot(snapshot);
    const cb = vi.fn();
    store.subscribeAll(cb);
    store._updateState("ann-1", "label", "Changed");
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it("batches notifications", () => {
    const store = new NodeStore();
    store.loadSnapshot(snapshot);
    const cb = vi.fn();
    store.subscribe("doc-1", cb);
    store.batch(() => {
      store._updateState("doc-1", "title", "A");
      store._updateState("doc-1", "title", "B");
    });
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it("unsubscribe works", () => {
    const store = new NodeStore();
    store.loadSnapshot(snapshot);
    const cb = vi.fn();
    const unsub = store.subscribe("doc-1", cb);
    unsub();
    store._updateState("doc-1", "title", "X");
    expect(cb).not.toHaveBeenCalled();
  });

  it("loads nested snapshot", () => {
    const nested: JsonDoc = [
      "root",
      "App",
      {},
      {
        pages: [
          [
            "p1",
            "Page",
            { title: "P1" },
            {
              items: [["i1", "Item", { label: "I1" }]],
            },
          ],
        ],
      },
    ];
    const store = new NodeStore();
    store.loadSnapshot(nested);
    expect(store.getNode("p1")!.parentId).toBe("root");
    expect(store.getNode("i1")!.parentId).toBe("p1");
    expect(store.getChildren("p1", "items")).toEqual(["i1"]);
  });
});
