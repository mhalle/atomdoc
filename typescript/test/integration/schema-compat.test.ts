/**
 * Schema compatibility test: verify that TS-defined schemas produce
 * the same output as Python-defined schemas for the same document model.
 *
 * Starts a Python server, connects, receives its schema, then compares
 * against the equivalent schema built with defineNode/defineValue in TS.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, type ChildProcess } from "node:child_process";
import { WebSocket } from "ws";
import { defineNode, defineValue, buildSchema } from "../../src/define.js";
import type { AtomDocSchema } from "../../src/types.js";

(globalThis as any).WebSocket = WebSocket;

// --- Python server ---

let server: ChildProcess;
const PORT = 9877;
const WS_URL = `ws://localhost:${PORT}`;

function startServer(): Promise<void> {
  return new Promise((resolve, reject) => {
    const serverPath = new URL("./schema_server.py", import.meta.url).pathname;
    server = spawn("uv", ["run", "python", serverPath], {
      cwd: new URL("../../../atomdoc", import.meta.url).pathname,
      stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env, PORT: String(PORT) },
    });

    const timeout = setTimeout(
      () => reject(new Error("Server start timeout")),
      10000,
    );

    server.stdout!.on("data", (data: Buffer) => {
      if (data.toString().includes("SERVER_READY")) {
        clearTimeout(timeout);
        resolve();
      }
    });

    server.stderr!.on("data", (data: Buffer) => {
      const msg = data.toString().trim();
      if (msg) console.error("[server]", msg);
    });

    server.on("error", (err) => {
      clearTimeout(timeout);
      reject(err);
    });
  });
}

// --- Get schema from Python server ---

function getPythonSchema(): Promise<AtomDocSchema> {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(WS_URL);
    const timeout = setTimeout(() => reject(new Error("Timeout")), 5000);

    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data.toString());
      if (msg.type === "schema") {
        clearTimeout(timeout);
        ws.close();
        resolve(msg.schema);
      }
    };

    ws.onerror = (err) => {
      clearTimeout(timeout);
      reject(err);
    };
  });
}

// --- Define the same schema in TS ---

const Color = defineValue(
  "Color",
  {
    r: { type: "integer", default: 0 },
    g: { type: "integer", default: 0 },
    b: { type: "integer", default: 0 },
  },
  { frozen: true },
);

const Annotation = defineNode("Annotation", {
  label: { type: "string", default: "" },
  color: {
    type: "object",
    schema: Color,
    tier: "atomic",
    default: { r: 0, g: 0, b: 0 },
  },
});

const Page = defineNode(
  "Page",
  {
    title: { type: "string", default: "" },
  },
  { slots: { annotations: "Annotation" } },
);

const tsSchema = buildSchema("Page", [Page, Annotation], [Color]);

// --- Tests ---

beforeAll(async () => {
  await startServer();
}, 15000);

afterAll(() => {
  if (server) server.kill();
});

describe("Schema compatibility: Python vs TypeScript", () => {
  let pySchema: AtomDocSchema;

  beforeAll(async () => {
    pySchema = await getPythonSchema();
  });

  it("same version", () => {
    expect(tsSchema.version).toBe(pySchema.version);
  });

  it("same root type", () => {
    expect(tsSchema.root_type).toBe(pySchema.root_type);
  });

  it("same node type names", () => {
    expect(Object.keys(tsSchema.node_types).sort()).toEqual(
      Object.keys(pySchema.node_types).sort(),
    );
  });

  it("same value type names", () => {
    expect(Object.keys(tsSchema.value_types).sort()).toEqual(
      Object.keys(pySchema.value_types).sort(),
    );
  });

  it("same field tiers for Annotation", () => {
    expect(tsSchema.node_types.Annotation.field_tiers).toEqual(
      pySchema.node_types.Annotation.field_tiers,
    );
  });

  it("same field tiers for Page", () => {
    expect(tsSchema.node_types.Page.field_tiers).toEqual(
      pySchema.node_types.Page.field_tiers,
    );
  });

  it("same slots for Page", () => {
    expect(tsSchema.node_types.Page.slots).toEqual(
      pySchema.node_types.Page.slots,
    );
  });

  it("same slots for Annotation (empty)", () => {
    expect(tsSchema.node_types.Annotation.slots).toEqual(
      pySchema.node_types.Annotation.slots,
    );
  });

  it("same field defaults for Annotation", () => {
    expect(tsSchema.node_types.Annotation.field_defaults).toEqual(
      pySchema.node_types.Annotation.field_defaults,
    );
  });

  it("same field defaults for Page", () => {
    expect(tsSchema.node_types.Page.field_defaults).toEqual(
      pySchema.node_types.Page.field_defaults,
    );
  });

  it("Color value type matches", () => {
    expect(tsSchema.value_types.Color.frozen).toBe(
      pySchema.value_types.Color.frozen,
    );
  });

  it("Color json_schema properties match", () => {
    const tsProps = (tsSchema.value_types.Color.json_schema as any).properties;
    const pyProps = (pySchema.value_types.Color.json_schema as any).properties;

    // Both should have r, g, b as integer fields
    for (const key of ["r", "g", "b"]) {
      expect(tsProps[key].type).toBe(pyProps[key].type);
      expect(tsProps[key].default).toBe(pyProps[key].default);
    }
  });

  it("Annotation json_schema field types match", () => {
    const tsProps = (tsSchema.node_types.Annotation.json_schema as any)
      .properties;
    const pyProps = (pySchema.node_types.Annotation.json_schema as any)
      .properties;

    expect(tsProps.label.type).toBe(pyProps.label.type);
    expect(tsProps.color.type).toBe(pyProps.color.type);
  });

  it("TS schema can be used to load Python snapshot", async () => {
    // Connect and get snapshot from Python
    const snapshot = await new Promise<any>((resolve, reject) => {
      const ws = new WebSocket(WS_URL);
      const timeout = setTimeout(() => reject(new Error("Timeout")), 5000);
      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data.toString());
        if (msg.type === "snapshot") {
          clearTimeout(timeout);
          ws.close();
          resolve(msg.data);
        }
      };
      ws.onerror = (err) => {
        clearTimeout(timeout);
        reject(err);
      };
    });

    // Load the Python snapshot using the TS schema
    const { LocalDoc } = await import("../../src/thick/local-doc.js");
    const { getSlotChildren } = await import("../../src/thick/doc-node.js");

    const doc = new LocalDoc(tsSchema, snapshot);
    expect(doc.root.type).toBe("Page");
    expect(doc.root.state.title).toBe("Hello World");

    const annotations = getSlotChildren(doc.root, "annotations");
    expect(annotations.length).toBe(2);
    expect(annotations[0].state.label).toBe("First");
    expect(annotations[0].state.color).toEqual({ r: 255, g: 0, b: 0 });
  });

  it("TS-created ops work on Python server", async () => {
    // Connect, send an op from TS, verify it works
    const result = await new Promise<boolean>((resolve, reject) => {
      const ws = new WebSocket(WS_URL);
      const timeout = setTimeout(() => reject(new Error("Timeout")), 5000);
      let rootId = "";

      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data.toString());
        if (msg.type === "snapshot") {
          rootId = msg.data[0];
          // Send a setField op
          ws.send(
            JSON.stringify({
              type: "op",
              operations: {
                ordered: [],
                state: {
                  [rootId]: { title: "Updated from TS" },
                },
              },
            }),
          );
        } else if (msg.type === "patch") {
          clearTimeout(timeout);
          ws.close();
          // Verify the patch echoes our change
          const titlePatch = msg.operations.state[rootId]?.title;
          resolve(titlePatch === "Updated from TS");
        }
      };

      ws.onerror = (err) => {
        clearTimeout(timeout);
        reject(err);
      };
    });

    expect(result).toBe(true);
  });
});
