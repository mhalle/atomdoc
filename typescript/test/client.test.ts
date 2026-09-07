import { describe, it, expect, vi } from "vitest";
import { AtomDocClient } from "../src/client.js";
import type {
  AtomDocSchema,
  ErrorMsg,
  JsonDoc,
  PatchMsg,
  SchemaMsg,
  SnapshotMsg,
} from "../src/types.js";

const testSchema: AtomDocSchema = {
  version: 1,
  root_type: "Page",
  node_types: {
    Page: {
      json_schema: {
        type: "object",
        properties: { title: { type: "string" } },
      },
      field_tiers: { title: "mergeable" },
      slots: { annotations: { allowed_type: "Annotation" } },
      field_defaults: { title: "" },
    },
    Annotation: {
      json_schema: {
        type: "object",
        properties: { label: { type: "string" } },
      },
      field_tiers: { label: "mergeable" },
      slots: {},
      field_defaults: { label: "" },
    },
  },
  value_types: {},
};

const snapshotData: JsonDoc = [
  "doc-1",
  "Page",
  { title: "Test" },
  {
    annotations: [["ann-1", "Annotation", { label: "A" }]],
  },
];

function setupClient(): AtomDocClient {
  const client = new AtomDocClient("ws://unused");

  // Inject schema + snapshot directly (no WebSocket)
  client._injectMessage({ type: "schema", schema: testSchema } as SchemaMsg);
  client._injectMessage({
    type: "snapshot",
    doc_id: "doc-1",
    version: 0,
    data: snapshotData,
  } as SnapshotMsg);

  return client;
}

describe("AtomDocClient", () => {
  it("processes schema message", () => {
    const client = setupClient();
    expect(client.getSchema()).not.toBeNull();
    expect(client.getSchema()!.nodeTypeNames()).toEqual([
      "Page",
      "Annotation",
    ]);
  });

  it("processes snapshot message", () => {
    const client = setupClient();
    const store = client.getStore();
    expect(store.getRootId()).toBe("doc-1");
    expect(store.getRoot()!.state.title).toBe("Test");
    expect(store.getChildren("doc-1", "annotations")).toEqual(["ann-1"]);
  });

  it("fires connected callback after snapshot", () => {
    const client = new AtomDocClient("ws://unused");
    const cb = vi.fn();
    client.onConnected(cb);

    client._injectMessage({ type: "schema", schema: testSchema } as SchemaMsg);
    expect(cb).not.toHaveBeenCalled();

    client._injectMessage({
      type: "snapshot",
      doc_id: "doc-1",
      version: 0,
      data: snapshotData,
    } as SnapshotMsg);
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it("processes patch message", () => {
    const client = setupClient();
    const patchCb = vi.fn();
    client.onPatch(patchCb);

    client._injectMessage({
      type: "patch",
      version: 1,
      operations: {
        ordered: [[0, [["ann-2", "Annotation"]], 0, "annotations", "ann-1", 0]],
        state: { "ann-2": { label: "New" } },
      },
      source_client: "other",
    } as PatchMsg);

    expect(client.getVersion()).toBe(1);
    expect(client.getStore().getNode("ann-2")).toBeDefined();
    expect(client.getStore().getNode("ann-2")!.state.label).toBe("New");
    expect(patchCb).toHaveBeenCalledWith(1);
  });

  it("processes error message", () => {
    const client = setupClient();
    const errCb = vi.fn();
    client.onError(errCb);

    const errorMsg: ErrorMsg = {
      type: "error",
      ref: "x",
      code: "invalid_op",
      message: "Something went wrong",
    };
    client._injectMessage(errorMsg);

    expect(errCb).toHaveBeenCalledWith(errorMsg);
  });

  it("tracks version", () => {
    const client = setupClient();
    expect(client.getVersion()).toBe(0);

    client._injectMessage({
      type: "patch",
      version: 5,
      operations: { ordered: [], state: {} },
      source_client: "other",
    } as PatchMsg);

    expect(client.getVersion()).toBe(5);
  });

  it("unsubscribe works for callbacks", () => {
    const client = setupClient();
    const cb = vi.fn();
    const unsub = client.onPatch(cb);
    unsub();

    client._injectMessage({
      type: "patch",
      version: 1,
      operations: { ordered: [], state: {} },
      source_client: "other",
    } as PatchMsg);

    expect(cb).not.toHaveBeenCalled();
  });
});
