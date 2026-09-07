import { describe, it, expect } from "vitest";
import { LocalDoc, RefIntegrityError } from "../../src/thick/local-doc.js";
import type { AtomDocSchema, JsonDoc } from "../../src/types.js";

const schema: AtomDocSchema = {
  version: 1,
  root_type: "Scene",
  node_types: {
    Scene: {
      json_schema: {},
      field_tiers: {},
      slots: {
        transforms: { allowed_type: "Transform" },
        volumes: { allowed_type: "Volume" },
        other: { allowed_type: "Transform" },
      },
      field_defaults: {},
    },
    Transform: {
      json_schema: {},
      field_tiers: { name: "mergeable", parent: "ref" },
      slots: {},
      field_defaults: { name: "", parent: null },
      refs: { parent: { target_type: "Transform", many: false, policy: "restrict" } },
    },
    Volume: {
      json_schema: {},
      field_tiers: { transform: "ref", sources: "ref" },
      slots: {},
      field_defaults: { transform: null, sources: [] },
      refs: {
        transform: { target_type: "Transform", many: false, policy: "restrict" },
        sources: { target_type: "Volume", many: true, policy: "restrict" },
      },
    },
  },
  value_types: {},
};

const snapshot: JsonDoc = [
  "01jqp00000000000000000000",
  "Scene",
  {},
  {
    transforms: [
      ["t1", "Transform", { name: "t1" }],
      ["t2", "Transform", { name: "t2", parent: "t1" }],
    ],
    volumes: [
      ["v1", "Volume", { transform: "t2" }],
      ["v2", "Volume", { sources: ["v1"] }],
    ],
    other: [],
  },
];

function makeDoc(): LocalDoc {
  return new LocalDoc(schema, snapshot);
}

describe("LocalDoc references", () => {
  it("builds the reverse index from a snapshot", () => {
    const doc = makeDoc();
    expect(doc.referrers("t2").map((n) => n.id)).toEqual(["v1"]);
    expect(doc.referrers("t1").map((n) => n.id)).toEqual(["t2"]);
    expect(doc.referrers("t1", "transform")).toEqual([]);
    expect(doc.referrers("v1").map((n) => n.id)).toEqual(["v2"]);
  });

  it("restrict: deleting a referenced node throws and rolls back", () => {
    const doc = makeDoc();
    expect(() => doc.deleteRange("t2")).toThrow(RefIntegrityError);
    expect(doc.getNode("t2")).toBeDefined();
    expect(doc.getNode("v1")!.state.transform).toBe("t2");
    expect(doc.referrers("t2").map((n) => n.id)).toEqual(["v1"]);
    expect(doc._lifecycleStage).toBe("idle");
  });

  it("re-pointing and deleting in one transaction commits", () => {
    const doc = makeDoc();
    doc._lifecycleStage = "update";
    doc.setNodeState("v1", "transform", "t1");
    doc.deleteRange("t2");
    doc.forceCommit();
    expect(doc.getNode("t2")).toBeUndefined();
    expect(doc.referrers("t1").map((n) => n.id)).toEqual(["v1"]);
    expect(doc.referrers("t2")).toEqual([]);
  });

  it("undo of a delete restores the node and its referrers", () => {
    const doc = makeDoc();
    let inverse: ReturnType<LocalDoc["toSnapshot"]> extends never ? never : import("../../src/types.js").WireOperations | null = null;
    const unsub = doc.onChange((e) => { inverse = e.inverseOperations; });
    doc._lifecycleStage = "update";
    doc.setNodeState("v1", "transform", null);
    doc.deleteRange("t2");
    doc.forceCommit();
    unsub();
    expect(inverse).not.toBeNull();
    doc.applyOperations(inverse!, { skipUndo: true });
    expect(doc.getNode("t2")).toBeDefined();
    expect(doc.getNode("v1")!.state.transform).toBe("t2");
    expect(doc.referrers("t2").map((n) => n.id)).toEqual(["v1"]);
  });

  it("a reference to a missing node throws at commit", () => {
    const doc = makeDoc();
    expect(() => doc.setNodeState("v1", "transform", "nope")).toThrow(RefIntegrityError);
    expect(doc.getNode("v1")!.state.transform).toBe("t2");
  });

  it("a reference to the wrong node type throws at commit", () => {
    const doc = makeDoc();
    expect(() => doc.setNodeState("v1", "transform", "v2")).toThrow(/expected Transform/);
  });

  it("many-valued references are indexed per id", () => {
    const doc = makeDoc();
    doc.setNodeState("v2", "sources", ["v1", "v1"]);
    expect(doc.referrers("v1").map((n) => n.id)).toEqual(["v2"]);
    doc.setNodeState("v2", "sources", []);
    expect(doc.referrers("v1")).toEqual([]);
    expect(() => doc.deleteRange("v1")).not.toThrow();
  });

  it("moving a referenced node is not a delete", () => {
    const doc = makeDoc();
    doc.moveRange("t2", undefined, "", "other");
    expect(doc.getNode("t2")!.slotName).toBe("other");
    expect(doc.referrers("t2").map((n) => n.id)).toEqual(["v1"]);
  });

  it("inserted nodes carrying references are indexed", () => {
    const doc = makeDoc();
    const v3 = doc.createNode("Volume", { transform: "t1" });
    doc.insertIntoSlot(doc.root, "volumes", "append", [v3]);
    expect(doc.referrers("t1").map((n) => n.id)).toEqual(["t2", v3.id]);
    doc.deleteRange(v3.id);
    expect(doc.referrers("t1").map((n) => n.id)).toEqual(["t2"]);
  });

  it("rejects an inserted node whose reference does not resolve", () => {
    const doc = makeDoc();
    const v3 = doc.createNode("Volume", { transform: "ghost" });
    expect(() => doc.insertIntoSlot(doc.root, "volumes", "append", [v3])).toThrow(
      RefIntegrityError,
    );
    expect(doc.getNode(v3.id)).toBeUndefined();
  });
});
