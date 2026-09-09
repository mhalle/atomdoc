/**
 * Join cost is proportional to the scope, not the document: a scoped
 * client holding a 7-node section joins a document of ~20k nodes in the
 * time a whole-document client needs for a small one, and scope churn
 * across the document leaves the client at working-set size.
 */

import { describe, it, expect, afterEach } from "vitest";
import { spawn, type ChildProcess } from "node:child_process";
import { WebSocket } from "ws";
import { ThickAtomDocClient } from "../../src/thick/thick-client.js";
import type { ScopeAnchor } from "../../src/types.js";

(globalThis as any).WebSocket = WebSocket;

let server: ChildProcess | undefined;
let ids: Record<string, string> = {};

function startServer(port: number, size: number): Promise<void> {
  return new Promise((resolve, reject) => {
    const serverPath = new URL("./scope_server.py", import.meta.url).pathname;
    server = spawn("uv", ["run", "python", serverPath], {
      cwd: new URL("../../../python", import.meta.url).pathname,
      env: { ...process.env, PORT: String(port), SIZE: String(size) },
      stdio: ["ignore", "pipe", "pipe"],
    });
    const timeout = setTimeout(() => reject(new Error("Server start timeout")), 60000);
    server.stdout!.on("data", (data: Buffer) => {
      for (const line of data.toString().split("\n")) {
        if (line.startsWith("IDS ")) ids = JSON.parse(line.slice(4));
        if (line.includes("SERVER_READY")) {
          clearTimeout(timeout);
          resolve();
        }
      }
    });
    server.on("error", (err) => {
      clearTimeout(timeout);
      reject(err);
    });
  });
}

afterEach(() => {
  server?.kill();
  server = undefined;
});

/** Milliseconds from connect() to the document being loaded. */
async function joinTime(url: string, scope?: ScopeAnchor[]): Promise<{ ms: number; client: ThickAtomDocClient }> {
  const client = new ThickAtomDocClient({ url, scope, coalesce: false });
  const t0 = performance.now();
  await client.connect();
  await client.ready();
  return { ms: performance.now() - t0, client };
}

describe("Integration: scoped join cost", () => {
  it("a scoped join of a large document costs the working set", async () => {
    const port = 9885;
    const url = `ws://localhost:${port}`;
    await startServer(port, 3000); // 3 + 3000 sections, ~21k nodes
    // Warm up both paths once.
    (await joinTime(url)).client.disconnect();
    (await joinTime(url, [{ id: ids.s1 }])).client.disconnect();
    const whole = await joinTime(url);
    const scoped = await joinTime(url, [{ id: ids.s1 }]);
    const doc = scoped.client.getDoc()!;
    expect(doc.nodeMap.size).toBe(1 + 7); // the root stub and s1's subtree
    console.log(`join: whole ${whole.ms.toFixed(1)} ms, scoped ${scoped.ms.toFixed(1)} ms`);
    expect(scoped.ms * 5).toBeLessThan(whole.ms);

    // Churn: move the scope across the document; the client stays at
    // working-set size and its graveyard does not grow without bound.
    const sections = whole.client.getStore().getChildren(ids.root, "sections");
    for (let i = 0; i < 40; i++) {
      await scoped.client.setScope([{ id: sections[(i * 37) % sections.length] }]);
      // The root stub, a 7-node section, and at most one referent stub.
      expect(scoped.client.getDoc()!.nodeMap.size).toBeLessThanOrEqual(1 + 7 + 1);
    }
    const internals = scoped.client.getDoc() as unknown as { graveyard: Map<string, unknown> };
    expect(internals.graveyard.size).toBeLessThan(4096 + 8);
    whole.client.disconnect();
    scoped.client.disconnect();
  }, 120000);
});
