import { describe, it, expect, vi } from "vitest";
import { LocalDoc, RefIntegrityError } from "../src/thick/local-doc.js";
import { ThickAtomDocClient } from "../src/thick/thick-client.js";
import { NodeStore } from "../src/store.js";
import { applyPatch } from "../src/patch.js";
import { SchemaRegistry } from "../src/schema.js";
import { defineNode, defineValue, buildSchema } from "../src/define.js";
import { getSlotChildren } from "../src/thick/doc-node.js";
import type { AtomDocSchema, JsonDoc, WireOperations } from "../src/types.js";

const schema: AtomDocSchema = {
  version: 1,
  root_type: "Root",
  node_types: {
    Root: {
      json_schema: {},
      field_tiers: {},
      slots: { items: { allowed_type: "Item" } },
      field_defaults: {},
    },
    Item: {
      json_schema: {},
      field_tiers: { name: "mergeable", friend: "ref" },
      slots: { kids: { allowed_type: "Item" } },
      field_defaults: { name: "", friend: null },
      refs: { friend: { target_type: "Item", many: false, policy: "restrict" } },
    },
  },
  value_types: {},
};

const snapshot: JsonDoc = [
  "01jqp00000000000000000000",
  "Root",
  {},
  {
    items: [
      ["n1", "Item", { name: "one" }],
      ["n2", "Item", { name: "two", friend: "n1" }],
      ["n3", "Item", { name: "three" }],
    ],
  },
];

const make = () => new LocalDoc(schema, snapshot);

// Reverse-index entries are `${referrerId}\u0000${field}`.
const SEP = "\u0000";

/** target id -> sorted referrer ids, rebuilt from the node map. */
const rebuilt = (doc: LocalDoc) => {
  const out: Record<string, string[]> = {};
  for (const n of doc.nodeMap.values()) {
    const f = n.state.friend;
    if (typeof f === "string") (out[f] ??= []).push(n.id);
  }
  for (const k of Object.keys(out)) out[k].sort();
  return out;
};

/** The same view of the private reverse index. */
const indexOf = (doc: LocalDoc) => {
  const out: Record<string, string[]> = {};
  const index = (doc as unknown as { refIndex: Map<string, Set<string>> }).refIndex;
  for (const [target, entries] of index) {
    out[target] = [...entries].map((e) => e.split(SEP)[0]).sort();
  }
  return out;
};

describe("review fixes: LocalDoc", () => {
  it("a throwing transaction body rolls back in reverse order", () => {
    const doc = make();
    const before = JSON.stringify(doc.toSnapshot());
    doc._lifecycleStage = "update";
    const box = doc.createNode("Item", { name: "box" });
    doc.insertIntoSlot(doc.root, "items", "append", [box]);
    doc.moveRange("n3", undefined, box.id, "kids");
    doc.abort();
    expect(JSON.stringify(doc.toSnapshot())).toBe(before);
    expect(doc.getNode("n3")).toBeDefined();
    expect(doc._lifecycleStage).toBe("idle");
  });

  it("abort keeps the reverse index exact", () => {
    const doc = make();
    doc._lifecycleStage = "update";
    doc.setNodeState("n2", "friend", "n3");
    doc.abort();
    expect(indexOf(doc)).toEqual(rebuilt(doc));
    expect(doc.referrers("n1").map((n) => n.id)).toEqual(["n2"]);

    doc._lifecycleStage = "update";
    doc.deleteRange("n2");
    doc.abort();
    expect(indexOf(doc)).toEqual(rebuilt(doc));
    expect(() => doc.deleteRange("n1")).toThrow(RefIntegrityError);
  });

  it("rejects a duplicate node id and the same node twice", () => {
    const doc = make();
    const dup = doc.createNode("Item", { name: "impostor" });
    (dup as { id: string }).id = "n1";
    expect(() => doc.insertIntoSlot(doc.root, "items", "append", [dup])).toThrow(/already exists/);
    expect(doc.getNode("n1")!.state.name).toBe("one");
    const x = doc.createNode("Item");
    expect(() => doc.insertIntoSlot(doc.root, "items", "append", [x, x])).toThrow(/already exists/);
    expect(getSlotChildren(doc.root, "items").length).toBe(3);
    // A remote insert op carrying an existing id is skipped, not applied
    doc.applyOperations({ ordered: [[0, [["n1", "Item"]], 0, "items", 0, 0]], state: {} });
    expect(getSlotChildren(doc.root, "items").map((n) => n.id)).toEqual(["n1", "n2", "n3"]);
  });

  it("fuzz: refIndex stays consistent across rolled-back transactions", () => {
    let seed = 7;
    const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
    for (let round = 0; round < 60; round++) {
      const doc = make();
      for (let step = 0; step < 12; step++) {
        const ids = [...doc.nodeMap.keys()].filter((i) => i !== doc.root.id);
        const pick = () => ids[Math.floor(rnd() * ids.length)];
        try {
          doc._lifecycleStage = "update";
          const r = rnd();
          if (r < 0.4 && ids.length) {
            doc.setNodeState(pick(), "friend", rnd() < 0.2 ? null : pick());
          } else if (r < 0.6 && ids.length) {
            doc.deleteRange(pick());
          } else if (r < 0.8) {
            const n = doc.createNode("Item", { friend: ids.length && rnd() < 0.5 ? pick() : null });
            doc.insertIntoSlot(doc.root, "items", "append", [n]);
          } else if (ids.length) {
            doc.moveRange(pick(), undefined, "", "items");
          }
          if (rnd() < 0.3) throw new Error("body failure");
          doc.forceCommit();
        } catch {
          if (doc._lifecycleStage !== "idle") doc.abort();
        }
        expect(indexOf(doc)).toEqual(rebuilt(doc));
      }
    }
  });
});

describe("review fixes: store and client", () => {
  it("loadSnapshot notifies subscribers and keeps listeners of removed nodes", () => {
    const store = new NodeStore();
    store.loadSnapshot(snapshot);
    const onN1 = vi.fn();
    const onN3 = vi.fn();
    const onAll = vi.fn();
    store.subscribe("n1", onN1);
    store.subscribe("n3", onN3);
    store.subscribeAll(onAll);
    store.loadSnapshot([
      "01jqp00000000000000000000",
      "Root",
      {},
      { items: [["n1", "Item", { name: "uno" }]] },
    ]);
    expect(onN1).toHaveBeenCalledTimes(1);
    expect(onN3).toHaveBeenCalledTimes(1);
    expect(onAll).toHaveBeenCalledTimes(1);
    expect(store.getNode("n3")).toBeUndefined();
    // The node may come back (undo, a later resync); the subscriber
    // must hear about it.
    store.loadSnapshot(snapshot);
    expect(onN3).toHaveBeenCalledTimes(2);
    store._updateState("n3", "name", "changed");
    expect(onN3).toHaveBeenCalledTimes(3);
  });

  it("applyPatch deletes a subtree as it is after earlier ops in the same patch", () => {
    const store = new NodeStore();
    store.loadSnapshot([
      "01jqp00000000000000000000",
      "Root",
      {},
      { items: [["a", "Item", {}, { kids: [["x", "Item", {}]] }], ["b", "Item", {}]] },
    ]);
    // x moved out of a, then a deleted: x survives.
    applyPatch(store, { ordered: [[2, "x", 0, 0, "items", 0, 0], [1, "a", 0]], state: {} });
    expect(store.getChildren(store.getRootId(), "items")).toEqual(["b", "x"]);
    expect(store.getNode("x")).toBeDefined();
    expect(store.getNode("a")).toBeUndefined();
    // n1 inserted under b, then b deleted: n1 goes with it.
    applyPatch(store, { ordered: [[0, [["n1", "Item"]], "b", "kids", 0, 0], [1, "b", 0]], state: {} });
    expect(store.getNode("n1")).toBeUndefined();
    expect(store.getAllNodeIds().sort()).toEqual(["01jqp00000000000000000000", "x"]);
  });

  function onlineClient(sent: WireOperations[]): ThickAtomDocClient {
    const c = new ThickAtomDocClient({ url: "ws://unused" });
    c._injectMessage({ type: "schema", schema });
    c._injectMessage({
      type: "snapshot",
      doc_id: snapshot[0],
      version: 0,
      data: snapshot,
      client_id: "me",
    });
    const internals = c as unknown as { online: boolean; ws: unknown };
    internals.online = true;
    internals.ws = {
      send(text: string) {
        const msg = JSON.parse(text) as { operations?: WireOperations };
        if (msg.operations) sent.push(msg.operations);
      },
    };
    return c;
  }

  it("applies its own echo after a resync dropped the pending list", () => {
    const sent: WireOperations[] = [];
    const c = onlineClient(sent);
    c.setField("n1", "name", "bad"); // op1, rejected by the server
    c.setField("n3", "name", "good"); // op2, accepted after op1
    expect(sent.length).toBe(2);
    c._injectMessage({ type: "error", ref: "1", code: "rejected", message: "no" });
    c._injectMessage({ type: "snapshot", doc_id: snapshot[0], version: 0, data: snapshot });
    expect(c.getDoc()!.getNode("n3")!.state.name).toBe("three");
    c._injectMessage({
      type: "patch",
      version: 1,
      source_client: "me",
      operations: { ordered: [], state: { n3: { name: "good" } } },
    });
    expect(c.getDoc()!.getNode("n3")!.state.name).toBe("good");
    expect(c.getStore().getNode("n3")!.state.name).toBe("good");
    expect(sent.length).toBe(2); // the echo was not re-sent
  });

  it("still skips a normal self-echo", () => {
    const sent: WireOperations[] = [];
    const c = onlineClient(sent);
    c.setField("n1", "name", "local");
    c._injectMessage({
      type: "patch",
      version: 1,
      source_client: "me",
      ref: "me:1",
      operations: { ordered: [], state: { n1: { name: "local" } } },
    });
    expect((c as unknown as { pendingOps: unknown[] }).pendingOps).toEqual([]);
    expect(c.getUndoManager()!.canUndo).toBe(true);
    expect(c.getDoc()!.getNode("n1")!.state.name).toBe("local");
    expect(sent.length).toBe(1);
  });

  it("replays offline edits after reconnecting", () => {
    const sent: WireOperations[] = [];
    const c = onlineClient(sent);
    const internals = c as unknown as { online: boolean };
    internals.online = false;
    c.setField("n1", "name", "offline edit");
    expect(sent.length).toBe(0);
    internals.online = true;
    c._injectMessage({ type: "snapshot", doc_id: snapshot[0], version: 3, data: snapshot });
    expect(sent.length).toBe(1);
    expect(sent[0].state.n1.name).toBe("offline edit");
    expect(c.getDoc()!.getNode("n1")!.state.name).toBe("offline edit");
  });
});

describe("review fixes: schema conversion", () => {
  const Color = defineValue("Color", {
    r: { type: "integer", default: 0 },
    hex: { type: "string" },
  });
  const Thing = defineNode("Thing", {
    tags: { type: "array", items: { type: "string" }, default: [] },
    palette: { type: "object", schema: Color, tier: "atomic", default: { r: 0, hex: "#000" } },
    cover: { type: "ref", target: "Thing", nullable: true, default: null },
  });
  const built = buildSchema("Thing", [Thing], [Color]);

  it("nullable fields export as anyOf with null, value types carry required", () => {
    const props = built.node_types.Thing.json_schema.properties as Record<string, unknown>;
    expect(props.cover).toEqual({ anyOf: [{ type: "string" }, { type: "null" }], default: null });
    expect(built.value_types.Color.json_schema.required).toEqual(["hex"]);
    const reg = new SchemaRegistry(built);
    expect(reg.validate("Thing", {})).toEqual({
      tags: [],
      palette: { r: 0, hex: "#000" },
      cover: null,
    });
    expect(reg.validate("Thing", { cover: "x" })).toMatchObject({ cover: "x" });
    expect(() => reg.validate("Color", { r: 1 })).toThrow();
  });

  it("enforces constraints, enums, consts, records, tuples and discriminators", () => {
    const s: AtomDocSchema = {
      version: 1,
      root_type: "N",
      value_types: {},
      node_types: {
        N: {
          json_schema: {
            type: "object",
            properties: {
              label: { type: "string", minLength: 2, default: "ab" },
              count: { type: "integer", minimum: 0, maximum: 10, default: 0 },
              mode: { type: "string", enum: ["a", "b"], default: "a" },
              palette: {
                type: "object",
                additionalProperties: {
                  type: "object",
                  properties: { r: { type: "integer", default: 0 } },
                },
                default: {},
              },
              pair: {
                type: "array",
                prefixItems: [{ type: "integer" }, { type: "integer" }],
                minItems: 2,
                maxItems: 2,
                default: [0, 0],
              },
              variant: {
                oneOf: [
                  { type: "object", properties: { kind: { const: "sc" }, v: { type: "number", default: 0 } } },
                  { type: "object", properties: { kind: { const: "vec" }, x: { type: "number", default: 0 } } },
                ],
                discriminator: { propertyName: "kind" },
                default: { kind: "sc", v: 0 },
              },
            },
          },
          field_tiers: {},
          slots: {},
          field_defaults: {},
        },
      },
    };
    const reg = new SchemaRegistry(s);
    expect(reg.validate("N", {})).toEqual({
      label: "ab",
      count: 0,
      mode: "a",
      palette: {},
      pair: [0, 0],
      variant: { kind: "sc", v: 0 },
    });
    const bad = [
      { label: "a" },
      { count: -1 },
      { count: 99 },
      { mode: "zzz" },
      { palette: { k: "nope" } },
      { pair: [1, 2, 3] },
      { variant: { kind: "wrong" } },
      { variant: { kind: "vec", x: "s" } },
    ];
    for (const input of bad) {
      expect(() => reg.validate("N", input), JSON.stringify(input)).toThrow();
    }
    expect(
      reg.validate("N", { palette: { a: { r: 1 } }, variant: { kind: "vec", x: 2 } }),
    ).toMatchObject({ palette: { a: { r: 1 } }, variant: { kind: "vec", x: 2 } });
  });
});
