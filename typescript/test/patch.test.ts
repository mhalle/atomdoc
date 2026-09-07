import { describe, it, expect, vi } from "vitest";
import { NodeStore } from "../src/store.js";
import { applyPatch } from "../src/patch.js";
import type { JsonDoc, WireOperations } from "../src/types.js";

const snapshot: JsonDoc = [
  "root",
  "Page",
  { title: "Hello" },
  {
    annotations: [
      ["a1", "Annotation", { label: "First" }],
      ["a2", "Annotation", { label: "Second" }],
      ["a3", "Annotation", { label: "Third" }],
    ],
  },
];

function makeStore(): NodeStore {
  const store = new NodeStore();
  store.loadSnapshot(snapshot);
  return store;
}

describe("applyPatch", () => {
  it("applies state patches", () => {
    const store = makeStore();
    applyPatch(store, {
      ordered: [],
      state: { a1: { label: "Updated" } },
    });
    expect(store.getNode("a1")!.state.label).toBe("Updated");
  });

  it("applies multiple state patches", () => {
    const store = makeStore();
    applyPatch(store, {
      ordered: [],
      state: {
        a1: { label: "X" },
        root: { title: "New Title" },
      },
    });
    expect(store.getNode("a1")!.state.label).toBe("X");
    expect(store.getRoot()!.state.title).toBe("New Title");
  });

  it("applies insert operation", () => {
    const store = makeStore();
    applyPatch(store, {
      ordered: [[0, [["a4", "Annotation"]], 0, "annotations", "a3", 0]],
      state: { a4: { label: "Fourth" } },
    });
    expect(store.getNode("a4")).toBeDefined();
    expect(store.getNode("a4")!.type).toBe("Annotation");
    const children = store.getChildren("root", "annotations");
    expect(children).toEqual(["a1", "a2", "a3", "a4"]);
  });

  it("inserts before a target", () => {
    const store = makeStore();
    applyPatch(store, {
      ordered: [[0, [["a0", "Annotation"]], 0, "annotations", 0, "a1"]],
      state: {},
    });
    const children = store.getChildren("root", "annotations");
    expect(children).toEqual(["a0", "a1", "a2", "a3"]);
  });

  it("applies delete operation", () => {
    const store = makeStore();
    applyPatch(store, {
      ordered: [[1, "a2", 0]],
      state: {},
    });
    expect(store.getNode("a2")).toBeUndefined();
    const children = store.getChildren("root", "annotations");
    expect(children).toEqual(["a1", "a3"]);
  });

  it("deletes a range", () => {
    const store = makeStore();
    applyPatch(store, {
      ordered: [[1, "a1", "a2"]],
      state: {},
    });
    expect(store.getNode("a1")).toBeUndefined();
    expect(store.getNode("a2")).toBeUndefined();
    expect(store.getChildren("root", "annotations")).toEqual(["a3"]);
  });

  it("applies move operation", () => {
    // Add a second parent with its own slot
    const snap: JsonDoc = [
      "root",
      "App",
      {},
      {
        pages: [
          ["p1", "Page", {}, { items: [["i1", "Item", { v: 1 }]] }],
          ["p2", "Page", {}, { items: [] }],
        ],
      },
    ];
    const store = new NodeStore();
    store.loadSnapshot(snap);

    // Move i1 from p1 to p2
    applyPatch(store, {
      ordered: [[2, "i1", 0, "p2", "items", 0, 0]],
      state: {},
    });

    expect(store.getChildren("p1", "items")).toEqual([]);
    expect(store.getChildren("p2", "items")).toEqual(["i1"]);
    expect(store.getNode("i1")!.parentId).toBe("p2");
  });

  it("batches notifications", () => {
    const store = makeStore();
    const cb = vi.fn();
    store.subscribe("root", cb);

    applyPatch(store, {
      ordered: [[0, [["a4", "Annotation"]], 0, "annotations", "a3", 0]],
      state: { root: { title: "Changed" } },
    });

    // Should only fire once despite multiple mutations to root
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it("handles empty operations", () => {
    const store = makeStore();
    applyPatch(store, { ordered: [], state: {} });
    expect(store.getChildren("root", "annotations")).toEqual([
      "a1",
      "a2",
      "a3",
    ]);
  });
});
