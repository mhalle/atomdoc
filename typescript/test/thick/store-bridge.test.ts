import { describe, it, expect } from "vitest";
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
    bridgeDocToStore(doc, store);

    expect(store.getRootId()).toBe("01jqp00000000000000000000");
    expect(store.getRoot()!.state.title).toBe("Hello");
    expect(store.getChildren(store.getRootId(), "items")).toEqual(["i1"]);
  });

  it("syncs state changes", () => {
    const doc = new LocalDoc(schema, snapshot);
    const store = new NodeStore();
    bridgeDocToStore(doc, store);

    doc.setNodeState(doc.id, "title", "Updated");
    expect(store.getRoot()!.state.title).toBe("Updated");
  });

  it("syncs inserts", () => {
    const doc = new LocalDoc(schema, snapshot);
    const store = new NodeStore();
    bridgeDocToStore(doc, store);

    const node = doc.createNode("Item", { label: "Second" });
    doc.insertIntoSlot(doc.root, "items", "append", [node]);

    const children = store.getChildren(store.getRootId(), "items");
    expect(children.length).toBe(2);
    expect(store.getNode(node.id)!.state.label).toBe("Second");
  });

  it("syncs deletes", () => {
    const doc = new LocalDoc(schema, snapshot);
    const store = new NodeStore();
    bridgeDocToStore(doc, store);

    doc.deleteRange("i1");
    expect(store.getNode("i1")).toBeUndefined();
    expect(store.getChildren(store.getRootId(), "items")).toEqual([]);
  });

  it("unsubscribe stops syncing", () => {
    const doc = new LocalDoc(schema, snapshot);
    const store = new NodeStore();
    const unsub = bridgeDocToStore(doc, store);

    unsub();
    doc.setNodeState(doc.id, "title", "Should not sync");
    expect(store.getRoot()!.state.title).toBe("Hello");
  });
});
