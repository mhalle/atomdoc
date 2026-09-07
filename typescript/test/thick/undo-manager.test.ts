import { describe, it, expect } from "vitest";
import { LocalDoc } from "../../src/thick/local-doc.js";
import { UndoManager } from "../../src/thick/undo-manager.js";
import { getSlotChildren } from "../../src/thick/doc-node.js";
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
      field_tiers: { value: "mergeable" },
      slots: {},
      field_defaults: { value: "" },
    },
  },
  value_types: {},
};

const snapshot: JsonDoc = [
  "01jqp00000000000000000000",
  "Page",
  { title: "Hello" },
  { items: [] },
];

function setup() {
  const doc = new LocalDoc(schema, snapshot);
  const undo = new UndoManager(doc);
  return { doc, undo };
}

describe("UndoManager", () => {
  it("starts with empty stacks", () => {
    const { undo } = setup();
    expect(undo.canUndo).toBe(false);
    expect(undo.canRedo).toBe(false);
  });

  it("undo reverses state change", () => {
    const { doc, undo } = setup();
    doc.setNodeState(doc.id, "title", "Changed");
    expect(doc.root.state.title).toBe("Changed");
    expect(undo.canUndo).toBe(true);

    undo.undo();
    expect(doc.root.state.title).toBe("Hello");
    expect(undo.canUndo).toBe(false);
    expect(undo.canRedo).toBe(true);
  });

  it("redo restores change", () => {
    const { doc, undo } = setup();
    doc.setNodeState(doc.id, "title", "Changed");
    undo.undo();
    undo.redo();
    expect(doc.root.state.title).toBe("Changed");
  });

  it("undo reverses insert", () => {
    const { doc, undo } = setup();
    const node = doc.createNode("Item", { value: "test" });
    doc.insertIntoSlot(doc.root, "items", "append", [node]);
    expect(getSlotChildren(doc.root, "items").length).toBe(1);

    undo.undo();
    expect(getSlotChildren(doc.root, "items").length).toBe(0);
  });

  it("undo reverses delete", () => {
    const { doc, undo } = setup();

    // Insert then delete
    const node = doc.createNode("Item", { value: "test" });
    doc.insertIntoSlot(doc.root, "items", "append", [node]);
    const nodeId = node.id;

    doc.deleteRange(nodeId);
    expect(doc.nodeMap.has(nodeId)).toBe(false);

    // Undo delete (should restore)
    undo.undo();
    expect(doc.nodeMap.has(nodeId)).toBe(true);
  });

  it("new change clears redo stack", () => {
    const { doc, undo } = setup();
    doc.setNodeState(doc.id, "title", "A");
    undo.undo();
    expect(undo.canRedo).toBe(true);

    doc.setNodeState(doc.id, "title", "B");
    expect(undo.canRedo).toBe(false);
  });

  it("multiple undo steps", () => {
    const { doc, undo } = setup();
    doc.setNodeState(doc.id, "title", "A");
    doc.setNodeState(doc.id, "title", "B");
    doc.setNodeState(doc.id, "title", "C");

    undo.undo();
    expect(doc.root.state.title).toBe("B");
    undo.undo();
    expect(doc.root.state.title).toBe("A");
    undo.undo();
    expect(doc.root.state.title).toBe("Hello");
  });

  it("respects maxSteps", () => {
    const doc = new LocalDoc(schema, snapshot);
    const undo = new UndoManager(doc, 2);

    doc.setNodeState(doc.id, "title", "A");
    doc.setNodeState(doc.id, "title", "B");
    doc.setNodeState(doc.id, "title", "C");

    // maxSteps=2: the oldest inverse (Hello→A) was evicted, the two
    // newest (A→B, B→C) are kept
    undo.undo();
    expect(doc.root.state.title).toBe("B");
    undo.undo();
    expect(doc.root.state.title).toBe("A");
    // No more
    expect(undo.canUndo).toBe(false);
  });

  it("undo on empty stack is no-op", () => {
    const { doc, undo } = setup();
    undo.undo(); // should not throw
    expect(doc.root.state.title).toBe("Hello");
  });

  it("redo on empty stack is no-op", () => {
    const { doc, undo } = setup();
    undo.redo(); // should not throw
    expect(doc.root.state.title).toBe("Hello");
  });
});
