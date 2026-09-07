import { describe, it, expect, vi } from "vitest";
import { LocalDoc, type ChangeEvent } from "../../src/thick/local-doc.js";
import { getSlotChildren } from "../../src/thick/doc-node.js";
import type { AtomDocSchema, JsonDoc } from "../../src/types.js";

const schema: AtomDocSchema = {
  version: 1,
  root_type: "Page",
  node_types: {
    Page: {
      json_schema: {},
      field_tiers: { title: "mergeable" },
      slots: { annotations: { allowed_type: "Annotation" } },
      field_defaults: { title: "" },
    },
    Annotation: {
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
    annotations: [
      ["a1", "Annotation", { label: "First" }],
      ["a2", "Annotation", { label: "Second" }],
    ],
  },
];

function makeDoc(): LocalDoc {
  return new LocalDoc(schema, snapshot);
}

describe("LocalDoc", () => {
  it("loads from snapshot", () => {
    const doc = makeDoc();
    expect(doc.id).toBe("01jqp00000000000000000000");
    expect(doc.root.type).toBe("Page");
    expect(doc.root.state.title).toBe("Hello");
  });

  it("loads children", () => {
    const doc = makeDoc();
    const children = getSlotChildren(doc.root, "annotations");
    expect(children.length).toBe(2);
    expect(children[0].state.label).toBe("First");
    expect(children[1].state.label).toBe("Second");
  });

  it("nodes are in nodeMap", () => {
    const doc = makeDoc();
    expect(doc.nodeMap.size).toBe(3); // root + 2 annotations
    expect(doc.getNode("a1")).toBeDefined();
    expect(doc.getNode("a2")).toBeDefined();
  });

  it("sets node state", () => {
    const doc = makeDoc();
    doc.setNodeState(doc.id, "title", "Updated");
    expect(doc.root.state.title).toBe("Updated");
  });

  it("fires change event on state set", () => {
    const doc = makeDoc();
    const cb = vi.fn();
    doc.onChange(cb);
    doc.setNodeState(doc.id, "title", "New");
    expect(cb).toHaveBeenCalledTimes(1);
    const event: ChangeEvent = cb.mock.calls[0][0];
    expect(Object.keys(event.operations.state)).toContain(doc.id);
  });

  it("creates a node with local ID", () => {
    const doc = makeDoc();
    const node = doc.createNode("Annotation", { label: "New" });
    expect(node.id).toBeTruthy();
    expect(node.type).toBe("Annotation");
    expect(node.state.label).toBe("New");
  });

  it("applies defaults on create", () => {
    const doc = makeDoc();
    const node = doc.createNode("Annotation");
    expect(node.state.label).toBe("");
  });

  it("inserts a node into a slot", () => {
    const doc = makeDoc();
    const node = doc.createNode("Annotation", { label: "Third" });
    doc.insertIntoSlot(doc.root, "annotations", "append", [node]);

    const children = getSlotChildren(doc.root, "annotations");
    expect(children.length).toBe(3);
    expect(children[2].state.label).toBe("Third");
    expect(doc.nodeMap.has(node.id)).toBe(true);
  });

  it("fires change event on insert", () => {
    const doc = makeDoc();
    const cb = vi.fn();
    doc.onChange(cb);

    const node = doc.createNode("Annotation", { label: "New" });
    doc.insertIntoSlot(doc.root, "annotations", "append", [node]);

    expect(cb).toHaveBeenCalledTimes(1);
    const event: ChangeEvent = cb.mock.calls[0][0];
    expect(event.operations.ordered.length).toBeGreaterThan(0);
  });

  it("deletes a node", () => {
    const doc = makeDoc();
    doc.deleteRange("a1");
    expect(doc.nodeMap.has("a1")).toBe(false);
    const children = getSlotChildren(doc.root, "annotations");
    expect(children.length).toBe(1);
    expect(children[0].id).toBe("a2");
  });

  it("fires change event with inverse on delete", () => {
    const doc = makeDoc();
    const cb = vi.fn();
    doc.onChange(cb);
    doc.deleteRange("a1");

    const event: ChangeEvent = cb.mock.calls[0][0];
    expect(event.inverseOperations.ordered.length).toBeGreaterThan(0);
  });

  it("undo via inverse operations", () => {
    const doc = makeDoc();
    const events: ChangeEvent[] = [];
    doc.onChange((e) => events.push(e));

    // Delete a1
    doc.deleteRange("a1");
    expect(doc.nodeMap.has("a1")).toBe(false);

    // Undo by applying inverse
    const inverse = events[0].inverseOperations;
    doc.applyOperations(inverse);

    expect(doc.nodeMap.has("a1")).toBe(true);
    const children = getSlotChildren(doc.root, "annotations");
    expect(children.length).toBe(2);
  });

  it("toSnapshot round-trips", () => {
    const doc = makeDoc();
    const wire = doc.toSnapshot();

    const doc2 = new LocalDoc(schema, wire);
    expect(doc2.root.state.title).toBe("Hello");
    expect(getSlotChildren(doc2.root, "annotations").length).toBe(2);
  });

  it("no-op state set does not fire event", () => {
    const doc = makeDoc();
    const cb = vi.fn();
    doc.onChange(cb);
    doc.setNodeState(doc.id, "title", "Hello"); // same value
    expect(cb).not.toHaveBeenCalled();
  });

  it("insert prepend", () => {
    const doc = makeDoc();
    const node = doc.createNode("Annotation", { label: "Zero" });
    doc.insertIntoSlot(doc.root, "annotations", "prepend", [node]);

    const children = getSlotChildren(doc.root, "annotations");
    expect(children[0].state.label).toBe("Zero");
    expect(children.length).toBe(3);
  });

  it("insert before", () => {
    const doc = makeDoc();
    const node = doc.createNode("Annotation", { label: "Middle" });
    const a2 = doc.getNode("a2")!;
    doc.insertIntoSlot(doc.root, "annotations", "before", [node], a2);

    const children = getSlotChildren(doc.root, "annotations");
    expect(children.map((c) => c.state.label)).toEqual([
      "First",
      "Middle",
      "Second",
    ]);
  });

  it("applies remote operations", () => {
    const doc = makeDoc();
    doc.applyOperations({
      ordered: [],
      state: { a1: { label: "Remote" } },
    });
    expect(doc.getNode("a1")!.state.label).toBe("Remote");
  });

  it("unsubscribe works", () => {
    const doc = makeDoc();
    const cb = vi.fn();
    const unsub = doc.onChange(cb);
    unsub();
    doc.setNodeState(doc.id, "title", "X");
    expect(cb).not.toHaveBeenCalled();
  });
});
