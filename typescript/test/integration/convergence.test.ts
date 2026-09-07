/**
 * Convergence harness: one thick client edits while a fake device commits
 * host-side changes into the real Python session. At the end the client's
 * local document must equal the server's.
 *
 * "disjoint" is the recommended deployment rule (device-owned and
 * user-owned fields never overlap). "overlap" makes them interfere, which
 * the echo re-apply must still converge.
 */

import { describe, it, expect, afterEach } from "vitest";
import { spawn, type ChildProcess } from "node:child_process";
import { WebSocket } from "ws";
import { ThickAtomDocClient } from "../../src/thick/thick-client.js";
import type { JsonDoc, ServerMsg } from "../../src/types.js";

(globalThis as any).WebSocket = WebSocket;

let server: ChildProcess | undefined;

function startServer(env: Record<string, string>): Promise<void> {
  return new Promise((resolve, reject) => {
    const serverPath = new URL("./device_server.py", import.meta.url).pathname;
    server = spawn("uv", ["run", "python", serverPath], {
      cwd: new URL("../../../atomdoc", import.meta.url).pathname,
      env: { ...process.env, ...env },
      stdio: ["ignore", "pipe", "pipe"],
    });
    const timeout = setTimeout(() => reject(new Error("Server start timeout")), 15000);
    server.stdout!.on("data", (data: Buffer) => {
      if (data.toString().includes("SERVER_READY")) {
        clearTimeout(timeout);
        resolve();
      }
    });
    server.stderr!.on("data", (data: Buffer) => {
      const msg = data.toString().trim();
      if (msg) console.error("[device-server]", msg);
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

/** Deterministic PRNG so a failure reproduces from its seed. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** The server's current snapshot, read through a fresh connection. */
function serverSnapshot(url: string): Promise<{ version: number; data: JsonDoc }> {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    const timeout = setTimeout(() => reject(new Error("snapshot timeout")), 5000);
    ws.on("message", (raw) => {
      const msg = JSON.parse(raw.toString()) as ServerMsg;
      if (msg.type === "snapshot") {
        clearTimeout(timeout);
        ws.close();
        resolve({ version: msg.version, data: msg.data });
      }
    });
    ws.on("error", reject);
  });
}

async function runScenario(mode: "disjoint" | "overlap", port: number, seed: number) {
  const url = `ws://localhost:${port}`;
  await startServer({
    PORT: String(port),
    SEED: String(seed),
    COMMITS: "80",
    INTERVAL_MS: "4",
    MODE: mode,
  });
  const rng = mulberry32(seed);
  const client = new ThickAtomDocClient({ url, maxUndoSteps: 0 });
  const errors: string[] = [];
  client.onError((e) => errors.push(e.code));
  let resyncs = 0;
  client.onResync(() => resyncs++);
  const connected = new Promise<void>((resolve) => client.onConnected(() => resolve()));
  await client.connect();
  await connected;

  // Invariant, checked after every patch: a field with a pending local
  // write shows the newest pending value. A remote write underneath it
  // must never surface (the server's final value is ours).
  const violations: string[] = [];
  const pending = client as unknown as {
    pendingOps: Array<{ ops: { state: Record<string, Record<string, unknown>> } }>;
  };
  client.onPatch((version) => {
    const expected = new Map<string, unknown>();
    for (const entry of pending.pendingOps) {
      for (const [nodeId, patch] of Object.entries(entry.ops.state)) {
        for (const [key, value] of Object.entries(patch)) expected.set(`${nodeId}/${key}`, value);
      }
    }
    for (const [path, value] of expected) {
      const [nodeId, key] = path.split("/");
      const node = client.getDoc()!.getNode(nodeId);
      if (!node) continue; // deleted remotely: our op will be rejected
      if (JSON.stringify(node.state[key]) !== JSON.stringify(value)) {
        violations.push(`v${version} ${path}: ${JSON.stringify(node.state[key])} != ${JSON.stringify(value)}`);
      }
    }
  });

  // The user edits while the device runs. In disjoint mode the user owns
  // `label` and its own nodes; in overlap mode it also writes `shared`
  // and deletes device nodes.
  let userOps = 0;
  const doc = () => client.getDoc()!;
  const rootId = doc().root.id;
  const deviceIds = () => client.getStore().getChildren(rootId, "devices");
  for (let i = 0; i < 80; i++) {
    await sleep(Math.floor(rng() * 8));
    const ids = deviceIds();
    const roll = rng();
    try {
      if (roll < 0.15) {
        client.createNode("Device", { label: `user-${i}` }, rootId, "devices");
      } else if (mode === "overlap" && roll < 0.25 && ids.length > 0) {
        client.deleteNode(ids[Math.floor(rng() * ids.length)]);
      } else if (ids.length > 0) {
        const id = ids[Math.floor(rng() * ids.length)];
        client.setField(id, "label", `u${i}`);
        if (mode === "overlap" && rng() < 0.5) {
          client.setField(id, "shared", Math.floor(rng() * 1000));
        }
      }
      userOps++;
    } catch {
      // A node the device deleted under us: fine, move on.
    }
  }

  // Quiesce: device finished, nothing pending, version stable.
  const internals = client as unknown as { pendingOps: unknown[] };
  const deadline = Date.now() + 10000;
  let stableSince = 0;
  let lastVersion = -1;
  while (Date.now() < deadline) {
    const done = doc().root.state.device_done === true && internals.pendingOps.length === 0;
    if (client.getVersion() !== lastVersion) {
      lastVersion = client.getVersion();
      stableSince = Date.now();
    }
    if (done && Date.now() - stableSince > 200) break;
    await sleep(20);
  }

  const server = await serverSnapshot(url);
  const local = doc().toSnapshot();
  const context = `mode=${mode} seed=${seed} userOps=${userOps} resyncs=${resyncs} errors=${errors.join(",")}`;
  expect(doc().root.state.device_done, context).toBe(true);
  expect(client.getVersion(), context).toBe(server.version);
  expect(local, context).toEqual(server.data);
  expect(violations, context).toEqual([]);
  expect(userOps).toBeGreaterThan(0);
  expect((server.data[2] as { device_commits: number }).device_commits).toBe(80);
  client.disconnect();
  return { resyncs, errors };
}

describe("Integration: convergence with a host-side device", () => {
  // Several seeds each: the interleavings differ per seed, and single
  // seeds passed while others diverged during development.
  for (const [i, seed] of [7, 22, 23].entries()) {
    it(`converges when device and user own disjoint fields (seed ${seed})`, async () => {
      const { resyncs, errors } = await runScenario("disjoint", 9878 + i, seed);
      // Disjoint ownership never needs a resync.
      expect(errors).toEqual([]);
      expect(resyncs).toBe(0);
    }, 30000);
  }

  for (const [i, seed] of [11, 4, 5].entries()) {
    it(`converges when device and user edit the same fields and nodes (seed ${seed})`, async () => {
      await runScenario("overlap", 9888 + i, seed);
    }, 30000);
  }
});
