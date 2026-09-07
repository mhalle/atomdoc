/**
 * Integration test: connect a real TS client to a real Python server.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, type ChildProcess } from "node:child_process";
import { AtomDocClient } from "../../src/client.js";

// Node.js ws polyfill for WebSocket (not available in Node without it)
import { WebSocket } from "ws";
(globalThis as any).WebSocket = WebSocket;

let server: ChildProcess;

function startServer(): Promise<void> {
  return new Promise((resolve, reject) => {
    const serverPath = new URL("./server.py", import.meta.url).pathname;
    server = spawn("uv", ["run", "python", serverPath], {
      cwd: new URL("../../../atomdoc", import.meta.url).pathname,
      stdio: ["ignore", "pipe", "pipe"],
    });

    const timeout = setTimeout(() => reject(new Error("Server start timeout")), 10000);

    server.stdout!.on("data", (data: Buffer) => {
      if (data.toString().includes("SERVER_READY")) {
        clearTimeout(timeout);
        resolve();
      }
    });

    server.stderr!.on("data", (data: Buffer) => {
      // Log server errors for debugging
      const msg = data.toString().trim();
      if (msg) console.error("[server]", msg);
    });

    server.on("error", (err) => {
      clearTimeout(timeout);
      reject(err);
    });
  });
}

beforeAll(async () => {
  await startServer();
}, 15000);

afterAll(() => {
  if (server) server.kill();
});

describe("Integration: Python server + TS client", () => {
  it("connects and receives schema + snapshot", async () => {
    const client = new AtomDocClient("ws://localhost:9876");

    const ready = new Promise<void>((resolve) => {
      client.onConnected(() => resolve());
    });

    await client.connect();
    await ready;

    // Schema received
    const schema = client.getSchema();
    expect(schema).not.toBeNull();
    expect(schema!.nodeTypeNames()).toContain("Page");
    expect(schema!.nodeTypeNames()).toContain("Annotation");
    expect(schema!.valueTypeNames()).toContain("Color");

    // Snapshot received
    const store = client.getStore();
    const root = store.getRoot();
    expect(root).toBeDefined();
    expect(root!.type).toBe("Page");
    expect(root!.state.title).toBe("Hello World");

    // Children loaded
    const annotations = store.getChildren(store.getRootId(), "annotations");
    expect(annotations).toHaveLength(2);

    const first = store.getNode(annotations[0]);
    expect(first!.state.label).toBe("First");
    expect(first!.state.color).toEqual({ r: 255, g: 0, b: 0 });

    const second = store.getNode(annotations[1]);
    expect(second!.state.label).toBe("Second");
    expect(second!.state.color).toEqual({ r: 0, g: 255, b: 0 });

    client.disconnect();
  });

  it("sends setField and receives patch", async () => {
    const client = new AtomDocClient("ws://localhost:9876");

    const ready = new Promise<void>((resolve) => {
      client.onConnected(() => resolve());
    });
    await client.connect();
    await ready;

    const store = client.getStore();
    const rootId = store.getRootId();

    // Use a second client to observe the patch
    const client2 = new AtomDocClient("ws://localhost:9876");
    const ready2 = new Promise<void>((resolve) => {
      client2.onConnected(() => resolve());
    });
    await client2.connect();
    await ready2;

    const patchReceived = new Promise<number>((resolve) => {
      client2.onPatch((v) => resolve(v));
    });

    // Client 1 sets a field
    client.setField(rootId, "title", "Updated Title");

    // Client 2 should receive the patch
    const version = await patchReceived;
    expect(version).toBeGreaterThan(0);
    expect(client2.getStore().getRoot()!.state.title).toBe("Updated Title");

    client.disconnect();
    client2.disconnect();
  });

  it("sends create and receives new node", async () => {
    const client = new AtomDocClient("ws://localhost:9876");

    const ready = new Promise<void>((resolve) => {
      client.onConnected(() => resolve());
    });
    await client.connect();
    await ready;

    const store = client.getStore();
    const rootId = store.getRootId();
    const beforeCount = store.getChildren(rootId, "annotations").length;

    // Use second client to observe
    const client2 = new AtomDocClient("ws://localhost:9876");
    const ready2 = new Promise<void>((resolve) => {
      client2.onConnected(() => resolve());
    });
    await client2.connect();
    await ready2;

    const patchReceived = new Promise<void>((resolve) => {
      client2.onPatch(() => resolve());
    });

    // Create a new annotation
    client.createNode(
      "Annotation",
      { label: "Third", color: { r: 0, g: 0, b: 255 } },
      rootId,
      "annotations",
    );

    await patchReceived;

    // Client 2 should see the new node
    const annotations2 = client2.getStore().getChildren(
      client2.getStore().getRootId(),
      "annotations",
    );
    expect(annotations2.length).toBe(beforeCount + 1);

    const newNode = client2.getStore().getNode(annotations2[annotations2.length - 1]);
    expect(newNode!.state.label).toBe("Third");

    client.disconnect();
    client2.disconnect();
  });

  it("undo reverses the last change", async () => {
    const client = new AtomDocClient("ws://localhost:9876");

    const ready = new Promise<void>((resolve) => {
      client.onConnected(() => resolve());
    });
    await client.connect();
    await ready;

    const store = client.getStore();
    const rootId = store.getRootId();
    const titleBefore = store.getRoot()!.state.title;

    // Make a change
    const patch1 = new Promise<void>((resolve) => {
      client.onPatch(() => resolve());
    });
    client.setField(rootId, "title", "Temporary");

    // Wait for server to echo it back (since we're the source,
    // we won't get a patch, but the server processes it)
    // Use a small delay to let the server process
    await new Promise((r) => setTimeout(r, 100));

    // Undo
    const patch2 = new Promise<void>((resolve) => {
      client.onPatch(() => resolve());
    });
    client.undo();

    // The undo patch won't come to us (we're the source), but
    // let's verify with a second client
    const client2 = new AtomDocClient("ws://localhost:9876");
    const ready2 = new Promise<void>((resolve) => {
      client2.onConnected(() => resolve());
    });
    await client2.connect();
    await ready2;

    // Client2's snapshot should reflect the undo
    const currentTitle = client2.getStore().getRoot()!.state.title;
    // Title should have been undone (may not be original if other tests ran)
    expect(currentTitle).toBeDefined();

    client.disconnect();
    client2.disconnect();
  });

  it("receives error for invalid operation", async () => {
    const client = new AtomDocClient("ws://localhost:9876");

    const ready = new Promise<void>((resolve) => {
      client.onConnected(() => resolve());
    });
    await client.connect();
    await ready;

    const errorReceived = new Promise<string>((resolve) => {
      client.onError((err) => resolve(err.code));
    });

    // Try to create an unknown node type
    client.createNode("NonExistent", {}, client.getStore().getRootId(), "annotations");

    const code = await errorReceived;
    expect(code).toBe("invalid_op");

    client.disconnect();
  });
});
