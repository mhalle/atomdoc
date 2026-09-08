/**
 * Stubs: nodes a scoped client holds by identity only (`[id, type, null]`
 * in a snapshot). Reading a stub's state is an error, never a default;
 * local writes to or under a stub are refused; references may point at
 * stubs; the store carries the flag and `getState` returns undefined.
 */
import { describe, it, expect, vi } from "vitest";
import { inspect } from "node:util";
import { LocalDoc, OutOfScopeError, RefIntegrityError } from "../../src/thick/local-doc.js";
import { createDocNode, getSlotChildren } from "../../src/thick/doc-node.js";
import { NodeStore } from "../../src/store.js";
import { applyPatch } from "../../src/patch.js";
import { AtomDocClient } from "../../src/client.js";
import { bridgeDocToStore } from "../../src/thick/store-bridge.js";
import { ThickAtomDocClient } from "../../src/thick/thick-client.js";
import type {
  AtomDocSchema,
  ErrorMsg,
  JsonDoc,
  PatchMsg,
  SchemaMsg,
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
      refs: { related: { target_type: "Section", many: false, policy: "restrict" } },
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

const ROOT = "01jqp00000000000000000000";

/**
 * A scoped view. The root is an ancestor stub; s1 is the held anchor with
 * held items i1 and i2 and a boundary stub i3; s2 is a stub sibling of
 * the anchor; s9 is a detached referent stub (s1.related points at it).
 */
const partial: JsonDoc = [
  ROOT,
  "Page",
  null,
  {
    sections: [
      [
        "s1",
        "Section",
        { heading: "One", related: "s9" },
        {
          items: [
            ["i1", "Item", { label: "A" }],
            ["i2", "Item", { label: "B" }],
            ["i3", "Item", null],
          ],
        },
      ],
      ["s2", "Section", null],
    ],
  },
];
const referents: [string, string][] = [["s9", "Section"]];

function makeDoc(): LocalDoc {
  return new LocalDoc(schema, partial, { partial: true, stubs: referents });
}

describe("DocNode stubs", () => {
  it("createDocNode makes a stub whose state throws on every access", () => {
    const node = createDocNode("x", "Item", [], true);
    expect(node.stub).toBe(true);
    expect(() => node.state.label).toThrow(OutOfScopeError);
    expect(() => {
      node.state.label = "no";
    }).toThrow(OutOfScopeError);
    expect(() => Object.keys(node.state)).toThrow(OutOfScopeError);
    expect(() => "label" in node.state).toThrow(OutOfScopeError);
    expect(() => JSON.stringify(node.state)).toThrow(OutOfScopeError);
  });

  it("a stub's state can be inspected without throwing", () => {
    const node = createDocNode("x", "Item", [], true);
    expect(inspect(node.state)).toContain("stub");
    expect(String(node.state)).toMatch(/stub x/);
  });

  it("a full node is not a stub", () => {
    const node = createDocNode("x", "Item", []);
    expect(node.stub).toBe(false);
    expect(node.state.label).toBeUndefined();
  });
});

describe("LocalDoc with stubs", () => {
  it("loads a partial snapshot: stubs in the tree and detached referents", () => {
    const doc = makeDoc();
    expect(doc.partial).toBe(true);
    expect(doc.root.stub).toBe(true);
    expect(doc.getNode("s1")!.stub).toBe(false);
    expect(doc.getNode("s1")!.state.heading).toBe("One");
    expect(doc.getNode("s2")!.stub).toBe(true);
    expect(doc.getNode("s9")!.stub).toBe(true);
    expect(doc.getNode("s9")!.parent).toBeNull();
    expect(getSlotChildren(doc.root, "sections").map((n) => n.id)).toEqual(["s1", "s2"]);
    expect(doc.getNode("i3")!.stub).toBe(true);
    expect(doc.getNode("i3")!.parent!.id).toBe("s1");
    expect(doc.nodeMap.size).toBe(7);
  });

  it("a whole-document snapshot is not partial", () => {
    const doc = new LocalDoc(schema, [ROOT, "Page", { title: "T" }, { sections: [] }]);
    expect(doc.partial).toBe(false);
    expect(doc.root.stub).toBe(false);
    expect(doc.detachedStubs()).toEqual([]);
  });

  it("a snapshot containing a stub is partial even without the flag", () => {
    const doc = new LocalDoc(schema, partial);
    expect(doc.partial).toBe(true);
  });

  it("round-trips the partial snapshot", () => {
    const doc = makeDoc();
    expect(doc.toSnapshot()).toEqual(partial);
    expect(doc.detachedStubs()).toEqual([["s9", "Section"]]);
  });

  it("reading a stub's state throws, never a default", () => {
    const doc = makeDoc();
    expect(() => doc.root.state.title).toThrow(OutOfScopeError);
    expect(() => doc.getNode("s2")!.state.heading).toThrow(/s2.*stub/);
  });

  it("references resolve to stubs and are indexed", () => {
    const doc = makeDoc();
    expect(doc.referrers("s9").map((n) => n.id)).toEqual(["s1"]);
    // Re-pointing a reference at another stub passes integrity.
    doc.setNodeState("s1", "related", "s2");
    expect(doc.getNode("s1")!.state.related).toBe("s2");
    expect(doc.referrers("s2").map((n) => n.id)).toEqual(["s1"]);
    expect(doc.referrers("s9")).toEqual([]);
  });

  it("a reference to a stub of the wrong type is still refused", () => {
    const doc = makeDoc();
    expect(() => doc.setNodeState("s1", "related", "i3")).toThrow(RefIntegrityError);
  });

  it("handles() skips stubs", () => {
    const doc = makeDoc();
    expect(doc.handles()).toEqual([]);
  });

  it("writing a stub's field is refused", () => {
    const doc = makeDoc();
    expect(() => doc.setNodeState("s2", "heading", "x")).toThrow(OutOfScopeError);
    expect(() => doc.setNodeState(ROOT, "title", "x")).toThrow(OutOfScopeError);
  });

  it("inserting under a stub is refused; under a held node it works", () => {
    const doc = makeDoc();
    const node = doc.createNode("Section", { heading: "New" });
    expect(() => doc.insertIntoSlot(doc.root, "sections", "append", [node])).toThrow(OutOfScopeError);
    const item = doc.createNode("Item", { label: "C" });
    doc.insertIntoSlot(doc.getNode("s1")!, "items", "append", [item]);
    expect(getSlotChildren(doc.getNode("s1")!, "items").length).toBe(4);
  });

  it("deleting a stub is refused; a held node goes with its stub children", () => {
    const doc = makeDoc();
    expect(() => doc.deleteRange("s2")).toThrow(OutOfScopeError);
    expect(() => doc.deleteRange("i3")).toThrow(OutOfScopeError);
    expect(() => doc.deleteRange("i2", "i3")).toThrow(OutOfScopeError);
    const events: WireOperations[] = [];
    doc.onChange((e) => events.push(e.operations));
    doc.deleteRange("s1"); // the anchor itself, under a stub parent
    expect(doc.getNode("s1")).toBeUndefined();
    expect(doc.getNode("i3")).toBeUndefined();
    expect(events[0].ordered).toEqual([[1, "s1", 0]]);
  });

  it("inserting a stub locally is refused", () => {
    const doc = makeDoc();
    expect(() =>
      doc.insertIntoSlot(doc.getNode("s1")!, "items", "append", [createDocNode("i4", "Item", [], true)]),
    ).toThrow(OutOfScopeError);
  });

  it("moving a stub, or into a stub's slot, is refused", () => {
    const doc = makeDoc();
    expect(() => doc.moveRange("s2", undefined, "s1", "items")).toThrow(OutOfScopeError);
    expect(() => doc.moveRangeRelative("i1", undefined, "s2", "before")).toThrow(OutOfScopeError);
    expect(() => doc.moveRange("i1", undefined, ROOT, "sections")).toThrow(OutOfScopeError);
  });

  it("a held node moves within a held parent even next to a stub sibling", () => {
    const doc = makeDoc();
    doc.moveRangeRelative("i1", undefined, "i3", "after");
    expect(getSlotChildren(doc.getNode("s1")!, "items").map((n) => n.id)).toEqual(["i2", "i3", "i1"]);
  });

  it("remote operations may delete a stub and place nodes beside one", () => {
    const doc = makeDoc();
    const events: WireOperations[] = [];
    doc.onChange((e) => events.push(e.operations));
    doc.applyOperations({ ordered: [[1, "s2", 0]], state: {} }, undefined, true);
    expect(doc.getNode("s2")).toBeUndefined();
    expect(events[0].ordered).toEqual([[1, "s2", 0]]);
    // A projected insert whose prev is a boundary stub.
    doc.applyOperations(
      { ordered: [[0, [["i4", "Item"]], "s1", "items", "i3", 0]], state: { i4: { label: "D" } } },
      undefined,
      true,
    );
    expect(getSlotChildren(doc.getNode("s1")!, "items").map((n) => n.id)).toEqual(["i1", "i2", "i3", "i4"]);
    expect(doc.getNode("i4")!.state.label).toBe("D");
  });

  it("a state patch for a stub is a protocol error and rolls the patch back", () => {
    const doc = makeDoc();
    expect(() =>
      doc.applyOperations({ ordered: [], state: { s2: { heading: "x" } } }, undefined, true),
    ).toThrow(OutOfScopeError);
    expect(doc.getNode("s2")!.stub).toBe(true);
  });

  it("a deleted stub revived by a rollback is still a stub", () => {
    const doc = makeDoc();
    const stub = doc.getNode("i3")!;
    // The delete lands, then a duplicate insert fails the patch.
    expect(() =>
      doc.applyOperations(
        { ordered: [[1, "i3", 0], [0, [["i1", "Item"]], "s1", "items", 0, 0]], state: {} },
        undefined,
        true,
      ),
    ).toThrow(/already exists/);
    expect(doc.getNode("i3")).toBe(stub); // revived in place
    expect(stub.stub).toBe(true);
    expect(() => stub.state.label).toThrow(OutOfScopeError);
    expect(getSlotChildren(doc.getNode("s1")!, "items").map((n) => n.id)).toEqual(["i1", "i2", "i3"]);
  });

  it("deleting a held node records no state for its stub children, and marks them", () => {
    const doc = makeDoc();
    let inverse: WireOperations | null = null;
    doc.onChange((e) => (inverse = e.inverseOperations));
    doc.deleteRange("s1");
    expect(inverse!.state.s1).toEqual({ heading: "One", related: "s9" });
    expect(inverse!.state.i3).toBeUndefined();
    expect(inverse!.ordered).toContainEqual([
      0,
      [["i1", "Item"], ["i2", "Item"], ["i3", "Item", null]],
      "s1",
      "items",
      0,
      0,
    ]);
  });

  it("an insert pair marked null creates a stub, even with no handle to revive", () => {
    const doc = new LocalDoc(schema, [ROOT, "Page", { title: "T" }, { sections: [] }]);
    doc.applyOperations(
      { ordered: [[0, [["s5", "Section", null], ["s6", "Section"]], 0, "sections", 0, 0]], state: {} },
      undefined,
      true,
    );
    expect(doc.getNode("s5")!.stub).toBe(true);
    expect(() => doc.getNode("s5")!.state.heading).toThrow(OutOfScopeError);
    expect(doc.getNode("s6")!.stub).toBe(false);
    expect(doc.getNode("s6")!.state.heading).toBe("");
    expect(doc.partial).toBe(true);
  });

  it("a revived node takes the kind the insert pair says", () => {
    const doc = makeDoc();
    const stub = doc.getNode("i3")!;
    const full = doc.getNode("i2")!;
    doc.applyOperations({ ordered: [[1, "i2", "i3"]], state: {} }, undefined, true);
    // The server restores i3 with state (it entered scope) and i2 as a stub.
    doc.applyOperations(
      {
        ordered: [[0, [["i2", "Item", null], ["i3", "Item"]], "s1", "items", "i1", 0]],
        state: { i3: { label: "C" } },
      },
      undefined,
      true,
    );
    expect(doc.getNode("i3")).toBe(stub);
    expect(stub.stub).toBe(false);
    expect(stub.state.label).toBe("C");
    expect(doc.getNode("i2")).toBe(full);
    expect(full.stub).toBe(true);
    expect(() => full.state.label).toThrow(OutOfScopeError);
  });

  it("a remote delete of a detached stub drops it", () => {
    const doc = makeDoc();
    const events: WireOperations[] = [];
    doc.onChange((e) => events.push(e.operations));
    doc.setNodeState("s1", "related", null);
    doc.applyOperations({ ordered: [[1, "s9", 0]], state: {} }, undefined, true);
    expect(doc.getNode("s9")).toBeUndefined();
    expect(doc.detachedStubs()).toEqual([]);
    expect(events[1].ordered).toEqual([[1, "s9", 0]]);
    // A local delete of one is refused like any stub's.
    expect(() => doc.deleteRange("s9")).toThrow(/not found/);
  });

  it("a rolled-back delete of a detached stub brings it back", () => {
    const doc = makeDoc();
    doc.setNodeState("s1", "related", null);
    expect(() =>
      doc.applyOperations(
        { ordered: [[1, "s9", 0], [0, [["i1", "Item"]], "s1", "items", 0, 0]], state: {} },
        undefined,
        true,
      ),
    ).toThrow(/already exists/);
    expect(doc.getNode("s9")!.stub).toBe(true);
    expect(doc.detachedStubs()).toEqual([["s9", "Section"]]);
  });
});

describe("NodeStore with stubs", () => {
  it("loads stubs with the flag and empty state, plus detached referents", () => {
    const store = new NodeStore();
    store.loadSnapshot(partial, referents);
    expect(store.getRoot()!.stub).toBe(true);
    expect(store.getRoot()!.state).toEqual({});
    expect(store.getNode("s1")!.stub).toBeFalsy();
    expect(store.getNode("s2")!.stub).toBe(true);
    expect(store.getNode("s9")).toEqual({
      id: "s9",
      type: "Section",
      state: {},
      slots: {},
      parentId: null,
      slotName: null,
      stub: true,
    });
    expect(store.getChildren(ROOT, "sections")).toEqual(["s1", "s2"]);
    expect(store.getChildren("s1", "items")).toEqual(["i1", "i2", "i3"]);
    expect(store.getNode("i3")!.stub).toBe(true);
  });

  it("a whole-document snapshot has no stubs", () => {
    const store = new NodeStore();
    store.loadSnapshot([ROOT, "Page", { title: "T" }, { sections: [] }]);
    expect(store.getRoot()!.stub).toBeFalsy();
  });

  it("an insert pair marked null creates a stub; a delete removes a detached one", () => {
    const store = new NodeStore();
    store.loadSnapshot(partial, referents);
    applyPatch(store, {
      ordered: [[0, [["i4", "Item", null], ["i5", "Item"]], "s1", "items", "i3", 0]],
      state: {},
    });
    expect(store.getNode("i4")!.stub).toBe(true);
    expect(store.getNode("i5")!.stub).toBeUndefined();
    expect(store.getChildren("s1", "items")).toEqual(["i1", "i2", "i3", "i4", "i5"]);
    applyPatch(store, { ordered: [[1, "s9", 0]], state: {} });
    expect(store.getNode("s9")).toBeUndefined();
  });
});

describe("store bridge with stubs", () => {
  it("loads the document's stubs, detached ones included", () => {
    const doc = makeDoc();
    const store = new NodeStore();
    const bridge = bridgeDocToStore(doc, store, { coalesce: false });
    expect(store.getNode("s9")!.stub).toBe(true);
    expect(store.getNode("s2")!.stub).toBe(true);
    expect(store.getNode("s1")!.state.heading).toBe("One");
    bridge.dispose();
  });
});

describe("thin client with stubs", () => {
  it("getState is undefined for a stub", () => {
    const client = new AtomDocClient({ url: "ws://unused" });
    client._injectMessage({ type: "schema", schema } as SchemaMsg);
    client._injectMessage({
      type: "snapshot",
      doc_id: ROOT,
      version: 0,
      data: partial,
      partial: true,
      stubs: referents,
    } as SnapshotMsg);
    expect(client.getState("s2")).toBeUndefined();
    expect(client.getState("s9")).toBeUndefined();
    expect(client.getState("s1")).toEqual({ heading: "One", related: "s9" });
    expect(client.getStore().getNode("s9")!.stub).toBe(true);
  });
});

describe("thick client with stubs", () => {
  function scopedClient() {
    const client = new ThickAtomDocClient({ url: "ws://unused", coalesce: false });
    client._injectMessage({ type: "schema", schema } as SchemaMsg);
    client._injectMessage({
      type: "snapshot",
      doc_id: ROOT,
      version: 0,
      data: partial,
      partial: true,
      stubs: referents,
      client_id: "me",
    } as SnapshotMsg);
    const sent: Array<{ ref: string; operations: WireOperations }> = [];
    const internals = client as unknown as { online: boolean; ws: unknown };
    internals.online = true;
    internals.ws = { send: (text: string) => sent.push(JSON.parse(text)) };
    return { client, sent };
  }

  it("loads the partial snapshot into the document and the store", () => {
    const { client } = scopedClient();
    expect(client.getDoc()!.partial).toBe(true);
    expect(client.getDoc()!.getNode("s9")!.stub).toBe(true);
    expect(client.getStore().getNode("s9")!.stub).toBe(true);
    expect(client.getStore().getRoot()!.stub).toBe(true);
  });

  it("getState is undefined for a stub and filled for a held node", () => {
    const { client } = scopedClient();
    expect(client.getState(ROOT)).toBeUndefined();
    expect(client.getState("s2")).toBeUndefined();
    expect(client.getState("i1")).toEqual({ label: "A" });
  });

  it("refuses local writes to or under a stub before sending anything", () => {
    const { client, sent } = scopedClient();
    expect(() => client.setField("s2", "heading", "x")).toThrow(OutOfScopeError);
    expect(() => client.createNode("Section", {}, ROOT, "sections")).toThrow(OutOfScopeError);
    expect(() => client.deleteNode("s2")).toThrow(OutOfScopeError);
    expect(() => client.moveNode("s2", "s1", "items")).toThrow(OutOfScopeError);
    expect(() => client.moveNode("i1", ROOT, "sections")).toThrow(OutOfScopeError);
    expect(() => client.moveNodeRelative("i1", "s2", "before")).toThrow(OutOfScopeError);
    expect(sent).toEqual([]);
  });

  it("sends writes on held nodes as usual", () => {
    const { client, sent } = scopedClient();
    client.setField("i1", "label", "AA");
    const id = client.createNode("Item", { label: "C" }, "s1", "items");
    client.moveNodeRelative("i2", "i1", "before");
    expect(sent.length).toBe(3);
    expect(sent[1].operations.ordered).toEqual([[0, [[id, "Item"]], "s1", "items", "i3", 0]]);
    expect(sent[2].operations.ordered).toEqual([[2, "i2", 0, "s1", "items", 0, "i1"]]);
  });

  it("deleting the held anchor sends the delete, stub children included", () => {
    const { client, sent } = scopedClient();
    client.deleteNode("s1");
    expect(sent[0].operations.ordered).toEqual([[1, "s1", 0]]);
  });

  it("moving a held node beside a stub sibling sends the move", () => {
    const { client, sent } = scopedClient();
    client.moveNodeRelative("i1", "i3", "after");
    expect(sent[0].operations.ordered).toEqual([[2, "i1", 0, "s1", "items", "i3", 0]]);
  });

  it("keeps the store's stub flag through a confirmed delete and its server-side undo", () => {
    const { client, sent } = scopedClient();
    let version = 0;
    const echo = (i: number, operations: WireOperations = sent[i].operations) =>
      client._injectMessage({
        type: "patch",
        version: ++version,
        ref: sent[i].ref,
        source_client: null,
        operations,
      } as PatchMsg);
    client.deleteNode("s1");
    echo(0);
    expect(client.getStore().getNode("i3")).toBeUndefined();
    // A scoped client's history lives on the server: undo is a request.
    client.undo();
    expect(sent[1]).toEqual({ type: "undo", ref: sent[1].ref, steps: 1 });
    // The server restores s1 into this client's view, its stub child
    // marked as one.
    echo(1, {
      ordered: [
        [0, [["s1", "Section"]], 0, "sections", 0, "s2"],
        [0, [["i1", "Item"], ["i2", "Item"], ["i3", "Item", null]], "s1", "items", 0, 0],
      ],
      state: { s1: { heading: "One", related: "s9" }, i1: { label: "A" }, i2: { label: "B" } },
    });
    expect(client.getDoc()!.getNode("s1")!.state.heading).toBe("One");
    expect(client.getDoc()!.getNode("i3")!.stub).toBe(true);
    expect(client.getStore().getNode("i3")!.stub).toBe(true);
    expect(client.getState("i3")).toBeUndefined();
    expect(client.getUndoManager()).toBeNull();
  });

  it("a patch carrying state for a stub is reported and the client disconnects", () => {
    const { client } = scopedClient();
    const internals = client as unknown as { ws: { send: unknown; close: () => void } };
    internals.ws.close = vi.fn();
    const errors: ErrorMsg[] = [];
    client.onError((e) => errors.push(e));
    client._injectMessage({
      type: "patch",
      version: 1,
      source_client: null,
      operations: { ordered: [], state: { s2: { heading: "x" } } },
    } as PatchMsg);
    expect(errors.map((e) => e.code)).toEqual(["protocol_error"]);
    expect(client.isOnline()).toBe(false);
    expect(client.getDoc()!.getNode("s2")!.stub).toBe(true);
  });

  it("the resync callback reports a partial document", () => {
    const { client } = scopedClient();
    const cb = vi.fn();
    client.onResync(cb);
    client._injectMessage({
      type: "snapshot",
      doc_id: ROOT,
      version: 1,
      data: partial,
      partial: true,
      stubs: referents,
    } as SnapshotMsg);
    expect(cb).toHaveBeenCalledTimes(1);
    expect(client.getDoc()!.partial).toBe(true);
  });
});
