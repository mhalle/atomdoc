/**
 * Behavior ported from DocNode v0.4: move replay keeping position,
 * undo eviction order, transaction flags (skipUndo), merge interval,
 * history export/import, mergeOperations, and the thick client keeping
 * remote patches out of local undo.
 */
import { describe, it, expect } from "vitest";
import { LocalDoc, type ChangeEvent } from "../../src/thick/local-doc.js";
import { UndoManager } from "../../src/thick/undo-manager.js";
import { mergeOperations } from "../../src/thick/local-ops.js";
import { getSlotChildren } from "../../src/thick/doc-node.js";
import { ThickAtomDocClient } from "../../src/thick/thick-client.js";
import type {
  AtomDocSchema,
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
      slots: { items: { allowed_type: "Item" } },
      field_defaults: { title: "" },
    },
    Item: {
      json_schema: {},
      field_tiers: { value: "mergeable" },
      slots: { items: { allowed_type: "Item" } },
      field_defaults: { value: "" },
    },
  },
  value_types: {},
};

const DOC_ID = "01jqp00000000000000000000";

function snapshot(...values: string[]): JsonDoc {
  return [
    DOC_ID,
    "Page",
    { title: "Hello" },
    { items: values.map((v) => [v, "Item", { value: v }, { items: [] }] as JsonDoc) },
  ];
}

function ids(doc: LocalDoc, parent = doc.root): string[] {
  return getSlotChildren(parent, "items").map((n) => n.id);
}

function record(doc: LocalDoc): ChangeEvent[] {
  const events: ChangeEvent[] = [];
  doc.onChange((e) => events.push(e));
  return events;
}

// ---------------------------------------------------------------------------
// Move replay keeps position
// ---------------------------------------------------------------------------

describe("move replay", () => {
  it("undo of a mid-slot move restores the original position", () => {
    const doc = new LocalDoc(schema, snapshot("a", "b", "c", "d"));
    const undo = new UndoManager(doc);
    doc.moveRangeRelative("b", undefined, "c", "after");
    expect(ids(doc)).toEqual(["a", "c", "b", "d"]);

    undo.undo();
    expect(ids(doc)).toEqual(["a", "b", "c", "d"]);
    undo.redo();
    expect(ids(doc)).toEqual(["a", "c", "b", "d"]);
  });

  it("undo of a move before the first child", () => {
    const doc = new LocalDoc(schema, snapshot("a", "b", "c"));
    const undo = new UndoManager(doc);
    doc.moveRangeRelative("c", undefined, "a", "before");
    expect(ids(doc)).toEqual(["c", "a", "b"]);
    undo.undo();
    expect(ids(doc)).toEqual(["a", "b", "c"]);
  });

  it("undo of a move into another parent restores position", () => {
    const doc = new LocalDoc(schema, snapshot("a", "b", "c"));
    const undo = new UndoManager(doc);
    doc.moveRange("b", undefined, "a", "items");
    expect(ids(doc)).toEqual(["a", "c"]);
    expect(ids(doc, doc.getNode("a")!)).toEqual(["b"]);
    undo.undo();
    expect(ids(doc)).toEqual(["a", "b", "c"]);
  });

  it("remote move operation replays with prev/next", () => {
    const source = new LocalDoc(schema, snapshot("a", "b", "c"));
    const replica = new LocalDoc(schema, snapshot("a", "b", "c"));
    const events = record(source);
    source.moveRangeRelative("a", undefined, "b", "after");
    expect(ids(source)).toEqual(["b", "a", "c"]);

    replica.applyOperations(events[0].operations);
    expect(ids(replica)).toEqual(["b", "a", "c"]);
  });

  it("move op with only a parent still appends", () => {
    const doc = new LocalDoc(schema, snapshot("a", "b", "c"));
    const ops: WireOperations = {
      ordered: [[2, "a", 0, 0, "items", 0, 0]],
      state: {},
    };
    doc.applyOperations(ops);
    expect(ids(doc)).toEqual(["b", "c", "a"]);
  });

  it("range move after a sibling", () => {
    const doc = new LocalDoc(schema, snapshot("a", "b", "c", "d"));
    doc.moveRangeRelative("a", "b", "d", "after");
    expect(ids(doc)).toEqual(["c", "d", "a", "b"]);
  });

  it("moving next to a node already adjacent is a no-op", () => {
    const doc = new LocalDoc(schema, snapshot("a", "b"));
    const events = record(doc);
    doc.moveRangeRelative("b", undefined, "a", "after");
    expect(events).toEqual([]);
  });

  it("moving a range relative to a node inside it throws", () => {
    const doc = new LocalDoc(schema, snapshot("a", "b", "c"));
    expect(() => doc.moveRangeRelative("a", "b", "b", "before")).toThrow(
      /in the range/,
    );
  });

  it("moving next to the root throws", () => {
    const doc = new LocalDoc(schema, snapshot("a"));
    expect(() => doc.moveRangeRelative("a", undefined, DOC_ID, "after")).toThrow(
      /root/,
    );
  });

  it("moving a node into its own descendant throws", () => {
    const doc = new LocalDoc(schema, snapshot("a"));
    const child = doc.createNode("Item", { value: "child" });
    doc.insertIntoSlot(doc.getNode("a")!, "items", "append", [child]);
    expect(() => doc.moveRange("a", undefined, child.id, "items")).toThrow(
      /descendant/,
    );
  });

  it("abort rolls a positional move back", () => {
    const doc = new LocalDoc(schema, snapshot("a", "b", "c", "d"));
    // Hold the transaction open so the move is pending, then abort it
    doc._lifecycleStage = "update";
    doc.moveRangeRelative("d", undefined, "a", "before");
    expect(ids(doc)).toEqual(["d", "a", "b", "c"]);
    doc.abort();
    expect(ids(doc)).toEqual(["a", "b", "c", "d"]);
  });

  it("nested skipUndo isolates the outer transaction's edits", () => {
    const doc = new LocalDoc(schema, snapshot());
    const undo = new UndoManager(doc);
    const events = record(doc);
    doc._lifecycleStage = "update";
    doc.setNodeState(DOC_ID, "title", "a");
    doc.applyOperations(
      { ordered: [], state: { [DOC_ID]: { title: "remote" } } },
      { skipUndo: true },
    );
    doc.setNodeState(DOC_ID, "title", "b");
    doc.forceCommit();

    expect(events.map((e) => e.flags.skipUndo ?? false)).toEqual([false, true, false]);
    undo.undo();
    expect(doc.root.state.title).toBe("remote");
    undo.undo();
    expect(doc.root.state.title).toBe("Hello");
    expect(undo.canUndo).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Transaction flags
// ---------------------------------------------------------------------------

describe("transaction flags", () => {
  it("change events carry empty flags by default", () => {
    const doc = new LocalDoc(schema, snapshot());
    const events = record(doc);
    doc.setNodeState(DOC_ID, "title", "x");
    expect(events[0].flags).toEqual({});
  });

  it("applyOperations with skipUndo is not undoable", () => {
    const doc = new LocalDoc(schema, snapshot());
    const undo = new UndoManager(doc);
    const events = record(doc);
    doc.applyOperations(
      { ordered: [], state: { [DOC_ID]: { title: "remote" } } },
      { skipUndo: true },
    );
    expect(doc.root.state.title).toBe("remote");
    expect(events[0].flags.skipUndo).toBe(true);
    expect(undo.canUndo).toBe(false);

    doc.setNodeState(DOC_ID, "title", "local");
    expect(undo.canUndo).toBe(true);
    undo.undo();
    expect(doc.root.state.title).toBe("remote");
  });

  it("flags reset after commit", () => {
    const doc = new LocalDoc(schema, snapshot());
    const events = record(doc);
    doc.applyOperations(
      { ordered: [], state: { [DOC_ID]: { title: "remote" } } },
      { skipUndo: true },
    );
    doc.setNodeState(DOC_ID, "title", "local");
    expect(events.map((e) => e.flags.skipUndo ?? false)).toEqual([true, false]);
  });

  it("skipUndo isolates an open transaction", () => {
    const doc = new LocalDoc(schema, snapshot());
    const undo = new UndoManager(doc);
    const events = record(doc);
    doc._lifecycleStage = "update";
    doc.setNodeState(DOC_ID, "title", "local");
    doc.applyOperations(
      { ordered: [[0, [["r", "Item"]], 0, "items", 0, 0]], state: {} },
      { skipUndo: true },
    );
    doc.setNodeState(DOC_ID, "title", "local2");
    doc.forceCommit();

    expect(events.map((e) => e.flags.skipUndo ?? false)).toEqual([false, true, false]);
    expect(ids(doc)).toEqual(["r"]);
    undo.undo();
    expect(doc.root.state.title).toBe("local");
    undo.undo();
    expect(doc.root.state.title).toBe("Hello");
    expect(ids(doc)).toEqual(["r"]);
    expect(undo.canUndo).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Undo manager: eviction, merge interval, history
// ---------------------------------------------------------------------------

describe("UndoManager parity", () => {
  it("a full stack drops the oldest step", () => {
    const doc = new LocalDoc(schema, snapshot());
    const undo = new UndoManager(doc, 2);
    for (const v of ["v0", "v1", "v2", "v3", "v4"]) {
      doc.setNodeState(DOC_ID, "title", v);
    }
    undo.undo();
    expect(doc.root.state.title).toBe("v3");
    undo.undo();
    expect(doc.root.state.title).toBe("v2");
    expect(undo.canUndo).toBe(false);
  });

  it("maxSteps 0 disables undo", () => {
    const doc = new LocalDoc(schema, snapshot());
    const undo = new UndoManager(doc, 0);
    expect(undo.isEnabled).toBe(false);
    doc.setNodeState(DOC_ID, "title", "x");
    expect(undo.canUndo).toBe(false);
  });

  it("transactions within the merge interval collapse", () => {
    const doc = new LocalDoc(schema, snapshot());
    let now = 0;
    const undo = new UndoManager(doc, 10, { mergeInterval: 500, clock: () => now });
    doc.setNodeState(DOC_ID, "title", "a");
    now = 100;
    doc.setNodeState(DOC_ID, "title", "ab");
    now = 200;
    const item = doc.createNode("Item", { value: "c" });
    doc.insertIntoSlot(doc.root, "items", "append", [item]);

    undo.undo();
    expect(doc.root.state.title).toBe("Hello");
    expect(ids(doc)).toEqual([]);
    expect(undo.canUndo).toBe(false);

    undo.redo();
    expect(doc.root.state.title).toBe("ab");
    expect(ids(doc)).toEqual([item.id]);
  });

  it("transactions outside the merge interval stay separate", () => {
    const doc = new LocalDoc(schema, snapshot());
    let now = 0;
    const undo = new UndoManager(doc, 10, { mergeInterval: 500, clock: () => now });
    doc.setNodeState(DOC_ID, "title", "a");
    now = 1000;
    doc.setNodeState(DOC_ID, "title", "b");
    undo.undo();
    expect(doc.root.state.title).toBe("a");
    undo.undo();
    expect(doc.root.state.title).toBe("Hello");
  });

  it("does not merge across an undo", () => {
    const doc = new LocalDoc(schema, snapshot());
    const undo = new UndoManager(doc, 10, { mergeInterval: 10_000, clock: () => 0 });
    doc.setNodeState(DOC_ID, "title", "a");
    undo.undo();
    doc.setNodeState(DOC_ID, "title", "b");
    undo.undo();
    expect(doc.root.state.title).toBe("Hello");
    expect(undo.canUndo).toBe(false);
  });

  it("merging is off by default", () => {
    const doc = new LocalDoc(schema, snapshot());
    const undo = new UndoManager(doc);
    doc.setNodeState(DOC_ID, "title", "a");
    doc.setNodeState(DOC_ID, "title", "b");
    undo.undo();
    expect(doc.root.state.title).toBe("a");
  });

  function edited() {
    const doc = new LocalDoc(schema, snapshot("a", "b"));
    const undo = new UndoManager(doc);
    doc.setNodeState(DOC_ID, "title", "title");
    doc.deleteRange("a");
    undo.undo(); // leaves one redo entry
    return { doc, undo };
  }

  it("exports history and imports it into a replacement document", () => {
    const { doc, undo } = edited();
    const history = undo.exportHistory();
    expect(history.docId).toBe(DOC_ID);
    expect(history.docType).toBe("Page");
    expect(history.undoStack.length).toBe(1);
    expect(history.redoStack.length).toBe(1);

    const replacement = new LocalDoc(schema, doc.toSnapshot());
    const undo2 = new UndoManager(replacement);
    undo2.importHistory(history);
    expect(undo2.canUndo).toBe(true);
    expect(undo2.canRedo).toBe(true);

    undo2.redo();
    expect(ids(replacement)).toEqual(["b"]);
    undo2.undo();
    expect(ids(replacement)).toEqual(["a", "b"]);
    undo2.undo();
    expect(replacement.root.state.title).toBe("Hello");
  });

  it("exported history survives JSON round-trip", () => {
    const { doc, undo } = edited();
    const payload = JSON.parse(JSON.stringify(undo.exportHistory()));
    const replacement = new LocalDoc(schema, doc.toSnapshot());
    const undo2 = new UndoManager(replacement);
    undo2.importHistory(payload);
    undo2.undo();
    expect(replacement.root.state.title).toBe("Hello");
  });

  it("export is a copy", () => {
    const { doc, undo } = edited();
    const history = undo.exportHistory();
    history.undoStack[0].operations.state = {};
    undo.undo();
    expect(doc.root.state.title).toBe("Hello");
  });

  it("export commits a pending transaction", () => {
    const doc = new LocalDoc(schema, snapshot());
    const undo = new UndoManager(doc);
    doc._lifecycleStage = "update";
    doc.setNodeState(DOC_ID, "title", "pending");
    const history = undo.exportHistory();
    expect(history.undoStack.length).toBe(1);
    expect(doc._lifecycleStage).toBe("idle");
  });

  it("import rejects history from another document", () => {
    const { undo } = edited();
    const other = new LocalDoc(schema, ["01jqp11111111111111111111", "Page", {}, { items: [] }]);
    const undoOther = new UndoManager(other);
    expect(() => undoOther.importHistory(undo.exportHistory())).toThrow(
      /different document/,
    );
  });

  it.each([
    null,
    {},
    { docId: DOC_ID, docType: "Page", undoStack: [], redoStack: "no" },
    {
      docId: DOC_ID,
      docType: "Page",
      undoStack: [{ operations: { ordered: [[9, "a"]], state: {} }, meta: {} }],
      redoStack: [],
    },
  ])("import rejects invalid history %#", (bad) => {
    const doc = new LocalDoc(schema, snapshot());
    const undo = new UndoManager(doc);
    expect(() => undo.importHistory(bad)).toThrow(/Invalid undo history/);
  });

  it("import truncates to maxSteps", () => {
    const doc = new LocalDoc(schema, snapshot());
    const undo = new UndoManager(doc);
    for (const v of ["v0", "v1", "v2", "v3", "v4"]) {
      doc.setNodeState(DOC_ID, "title", v);
    }
    const small = new LocalDoc(schema, doc.toSnapshot());
    const undoSmall = new UndoManager(small, 2);
    undoSmall.importHistory(undo.exportHistory());
    undoSmall.undo();
    expect(small.root.state.title).toBe("v3");
    undoSmall.undo();
    expect(small.root.state.title).toBe("v2");
    expect(undoSmall.canUndo).toBe(false);
  });

  it("clear drops history", () => {
    const doc = new LocalDoc(schema, snapshot());
    const undo = new UndoManager(doc);
    doc.setNodeState(DOC_ID, "title", "x");
    undo.clear();
    expect(undo.canUndo).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// mergeOperations
// ---------------------------------------------------------------------------

describe("mergeOperations", () => {
  it("concatenates ordered ops and merges state with later values winning", () => {
    const a: WireOperations = {
      ordered: [[1, "x", 0]],
      state: { n1: { value: "a", other: 1 } },
    };
    const b: WireOperations = {
      ordered: [[1, "y", 0]],
      state: { n1: { value: "b" }, n2: { value: "c" } },
    };
    const merged = mergeOperations(a, b);
    expect(merged.ordered).toEqual([[1, "x", 0], [1, "y", 0]]);
    expect(merged.state).toEqual({ n1: { value: "b", other: 1 }, n2: { value: "c" } });
    expect(a.state).toEqual({ n1: { value: "a", other: 1 } });
  });
});

// ---------------------------------------------------------------------------
// Thick client
// ---------------------------------------------------------------------------

describe("ThickAtomDocClient parity", () => {
  function setup(options: { mergeInterval?: number } = {}) {
    const client = new ThickAtomDocClient({ url: "ws://unused", ...options });
    client._injectMessage({ type: "schema", schema } as SchemaMsg);
    client._injectMessage({
      type: "snapshot",
      doc_id: DOC_ID,
      version: 0,
      data: snapshot("a", "b", "c"),
      client_id: "me",
    } as SnapshotMsg);
    return client;
  }

  it("remote patches do not enter local undo history", () => {
    const client = setup();
    client._injectMessage({
      type: "patch",
      version: 1,
      operations: { ordered: [], state: { [DOC_ID]: { title: "remote" } } },
      source_client: "someone-else",
    } as PatchMsg);
    expect(client.getDoc()!.root.state.title).toBe("remote");
    expect(client.getUndoManager()!.canUndo).toBe(false);

    client.setField(DOC_ID, "title", "mine");
    client.undo();
    expect(client.getDoc()!.root.state.title).toBe("remote");
  });

  it("remote moves land at the recorded position", () => {
    const client = setup();
    client._injectMessage({
      type: "patch",
      version: 1,
      operations: { ordered: [[2, "c", 0, 0, "items", 0, "a"]], state: {} },
      source_client: "someone-else",
    } as PatchMsg);
    expect(ids(client.getDoc()!)).toEqual(["c", "a", "b"]);
  });

  it("moveNodeRelative and undo", () => {
    const client = setup();
    client.moveNodeRelative("a", "b", "after");
    expect(ids(client.getDoc()!)).toEqual(["b", "a", "c"]);
    client.undo();
    expect(ids(client.getDoc()!)).toEqual(["a", "b", "c"]);
  });

  it("mergeInterval option collapses quick local edits", () => {
    const client = setup({ mergeInterval: 60_000 });
    client.setField(DOC_ID, "title", "a");
    client.setField(DOC_ID, "title", "ab");
    client.undo();
    expect(client.getDoc()!.root.state.title).toBe("Hello");
  });
});
