import { describe, it, expect, vi } from "vitest";
import { ThickAtomDocClient } from "../../src/thick/thick-client.js";
import type {
  AtomDocSchema,
  JsonDoc,
  SchemaMsg,
  SnapshotMsg,
  PatchMsg,
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
  { items: [["i1", "Item", { label: "First" }]] },
];

function setupClient(): ThickAtomDocClient {
  const client = new ThickAtomDocClient({ url: "ws://unused" });
  client._injectMessage({ type: "schema", schema } as SchemaMsg);
  client._injectMessage({
    type: "snapshot",
    doc_id: "01jqp00000000000000000000",
    version: 0,
    data: snapshot,
  } as SnapshotMsg);
  return client;
}

describe("ThickAtomDocClient", () => {
  it("initializes from schema + snapshot", () => {
    const client = setupClient();
    expect(client.getDoc()).not.toBeNull();
    expect(client.getSchema()).not.toBeNull();
    expect(client.getStore().getRootId()).toBe("01jqp00000000000000000000");
    expect(client.getStore().getRoot()!.state.title).toBe("Hello");
  });

  it("fires connected callback", () => {
    const client = new ThickAtomDocClient({ url: "ws://unused" });
    const cb = vi.fn();
    client.onConnected(cb);
    client._injectMessage({ type: "schema", schema } as SchemaMsg);
    client._injectMessage({
      type: "snapshot",
      doc_id: "01jqp00000000000000000000",
      version: 0,
      data: snapshot,
    } as SnapshotMsg);
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it("setField applies locally and updates store", () => {
    const client = setupClient();
    client.setField("01jqp00000000000000000000", "title", "Updated");

    // Doc updated
    expect(client.getDoc()!.root.state.title).toBe("Updated");
    // Store updated via bridge
    expect(client.getStore().getRoot()!.state.title).toBe("Updated");
  });

  it("createNode applies locally", () => {
    const client = setupClient();
    const rootId = client.getStore().getRootId();
    const newId = client.createNode("Item", { label: "New" }, rootId, "items");

    expect(newId).toBeTruthy();
    expect(client.getDoc()!.getNode(newId)).toBeDefined();
    expect(client.getStore().getNode(newId)!.state.label).toBe("New");
  });

  it("deleteNode applies locally", () => {
    const client = setupClient();
    client.deleteNode("i1");

    expect(client.getDoc()!.getNode("i1")).toBeUndefined();
    expect(client.getStore().getNode("i1")).toBeUndefined();
  });

  it("undo/redo works locally", () => {
    const client = setupClient();
    const rootId = client.getStore().getRootId();

    client.setField(rootId, "title", "Changed");
    expect(client.getStore().getRoot()!.state.title).toBe("Changed");

    client.undo();
    expect(client.getStore().getRoot()!.state.title).toBe("Hello");

    client.redo();
    expect(client.getStore().getRoot()!.state.title).toBe("Changed");
  });

  it("multi-step undo", () => {
    const client = setupClient();
    const rootId = client.getStore().getRootId();

    client.setField(rootId, "title", "A");
    client.setField(rootId, "title", "B");
    client.setField(rootId, "title", "C");

    client.undo(3);
    expect(client.getStore().getRoot()!.state.title).toBe("Hello");
  });

  it("applies remote patch from another client", () => {
    const client = setupClient();

    client._injectMessage({
      type: "patch",
      version: 1,
      operations: {
        ordered: [],
        state: { i1: { label: "Remote" } },
      },
      source_client: "other-client",
    } as PatchMsg);

    expect(client.getDoc()!.getNode("i1")!.state.label).toBe("Remote");
    expect(client.getStore().getNode("i1")!.state.label).toBe("Remote");
    expect(client.getVersion()).toBe(1);
  });

  it("handles error message", () => {
    const client = setupClient();
    const cb = vi.fn();
    client.onError(cb);
    client._injectMessage({
      type: "error",
      code: "invalid_op",
      message: "bad",
    });
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it("fires patch callback on remote changes", () => {
    const client = setupClient();
    const cb = vi.fn();
    client.onPatch(cb);

    client._injectMessage({
      type: "patch",
      version: 2,
      operations: { ordered: [], state: {} },
      source_client: "other",
    } as PatchMsg);

    expect(cb).toHaveBeenCalledWith(2);
  });
});

describe("ThickAtomDocClient resync", () => {
  it("rebuilds the document from a second snapshot and drops pending ops", () => {
    const client = new ThickAtomDocClient({ url: "ws://test" });
    client._injectMessage({ type: "schema", schema });
    client._injectMessage({
      type: "snapshot",
      doc_id: "01jqp00000000000000000000",
      version: 1,
      data: snapshot,
      client_id: "c1",
    });
    const resynced = vi.fn();
    client.onResync(resynced);
    // Online, with a socket that swallows sends: the edit is sent, not buffered.
    (client as unknown as { online: boolean; ws: unknown }).online = true;
    (client as unknown as { ws: unknown }).ws = { send() {} };

    // A local edit that the server will reject.
    client.setField("i1", "label", "optimistic");
    expect(client.getDoc()!.getNode("i1")!.state.label).toBe("optimistic");
    expect(client.getUndoManager()!.canUndo).toBe(true);

    client._injectMessage({ type: "error", ref: "x", code: "rejected", message: "no" });
    const corrected: JsonDoc = [
      "01jqp00000000000000000000",
      "Page",
      { title: "Hello" },
      { items: [["i1", "Item", { label: "server" }]] },
    ];
    client._injectMessage({ type: "snapshot", doc_id: "01jqp00000000000000000000", version: 7, data: corrected });

    expect(resynced).toHaveBeenCalledTimes(1);
    expect(client.getVersion()).toBe(7);
    expect(client.getDoc()!.getNode("i1")!.state.label).toBe("server");
    expect(client.getStore().getNode("i1")!.state.label).toBe("server");
    expect(client.getUndoManager()!.canUndo).toBe(false);
    // A self-echo after resync must not be mistaken for an older pending op.
    client._injectMessage({
      type: "patch",
      version: 8,
      operations: { ordered: [], state: { i1: { label: "later" } } },
      source_client: "other",
    });
    expect(client.getDoc()!.getNode("i1")!.state.label).toBe("later");
  });
});
