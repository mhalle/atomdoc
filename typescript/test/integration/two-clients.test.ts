/**
 * Convergence harness, two thick clients: both edit the same fields and
 * slots concurrently (state writes, creates, deletes, moves), with random
 * seeded interleavings, against the real Python session. At quiescence
 * both clients must equal the server's snapshot.
 *
 * Moves are the interesting part: a move that the server finds already
 * satisfied commits nothing, and the server must still answer the
 * requester with the slot's real order (PROTOCOL.md, "A request that
 * changes nothing").
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

async function connect(url: string): Promise<ThickAtomDocClient> {
  const client = new ThickAtomDocClient({ url, maxUndoSteps: 100 });
  const connected = new Promise<void>((resolve) => client.onConnected(() => resolve()));
  await client.connect();
  await connected;
  return client;
}

type Mix = "state" | "moves" | "all";

async function runScenario(mix: Mix, port: number, seed: number, device: boolean) {
  const url = `ws://localhost:${port}`;
  await startServer({
    PORT: String(port),
    SEED: String(seed),
    COMMITS: device ? "40" : "0",
    INTERVAL_MS: "6",
    MODE: "overlap",
  });
  const rng = mulberry32(seed);
  const clients = [await connect(url), await connect(url)];
  const stats = clients.map(() => ({ errors: [] as string[], resyncs: 0, ops: 0, order: [] as string[] }));
  clients.forEach((c, i) => {
    c.onError((e) => stats[i].errors.push(e.code));
    // Every client must see versions in order: a patch never carries a
    // version below the last one seen (a no-op echo repeats it), and a
    // resync snapshot is never followed by a patch it already contains.
    let last = c.getVersion();
    c.onResync(() => {
      stats[i].resyncs++;
      last = c.getVersion();
    });
    c.onPatch((version) => {
      if (version < last) stats[i].order.push(`v${version} after v${last}`);
      last = Math.max(last, version);
    });
  });

  const step = (c: ThickAtomDocClient, i: number, k: number) => {
    const rootId = c.getDoc()!.root.id;
    const ids = c.getStore().getChildren(rootId, "devices");
    const pick = () => ids[Math.floor(rng() * ids.length)];
    const roll = rng();
    try {
      if (mix !== "moves" && roll < 0.12) {
        c.createNode("Device", { label: `c${i}-${k}` }, rootId, "devices");
      } else if (mix === "all" && roll < 0.2 && ids.length > 2) {
        c.deleteNode(pick());
      } else if (mix !== "state" && roll < 0.6 && ids.length > 1) {
        const a = pick();
        let b = pick();
        while (b === a) b = pick();
        c.moveNodeRelative(a, b, rng() < 0.5 ? "before" : "after");
      } else if (ids.length > 0) {
        const id = pick();
        c.setField(id, "label", `c${i}-${k}`);
        if (rng() < 0.5) c.setField(id, "shared", Math.floor(rng() * 1000));
      } else if (mix === "moves") {
        c.createNode("Device", { label: `c${i}-${k}` }, rootId, "devices");
      }
      stats[i].ops++;
    } catch {
      // a node the other side deleted under us
    }
  };

  const drivers = clients.map(async (c, i) => {
    for (let k = 0; k < 60; k++) {
      await sleep(Math.floor(rng() * 6));
      step(c, i, k);
    }
  });
  await Promise.all(drivers);

  // Quiesce: nothing pending anywhere, versions stable.
  const deadline = Date.now() + 15000;
  let stableSince = Date.now();
  let last = "";
  while (Date.now() < deadline) {
    const pending = clients.some(
      (c) => (c as unknown as { pendingOps: unknown[] }).pendingOps.length > 0,
    );
    const key = clients.map((c) => c.getVersion()).join(",");
    if (key !== last) {
      last = key;
      stableSince = Date.now();
    }
    const done = !device || clients[0].getDoc()!.root.state.device_done === true;
    if (!pending && done && Date.now() - stableSince > 300) break;
    await sleep(25);
  }

  const snap = await serverSnapshot(url);
  const context = `mix=${mix} seed=${seed} device=${device} ` + stats
    .map((s, i) => `c${i}:{ops=${s.ops},resyncs=${s.resyncs},errors=${s.errors.join("|")}}`)
    .join(" ");
  for (const [i, c] of clients.entries()) {
    expect(stats[i].order, context).toEqual([]);
    expect(c.getVersion(), context).toBe(snap.version);
    expect(c.getDoc()!.toSnapshot(), context).toEqual(snap.data);
  }
  clients.forEach((c) => c.disconnect());
}

describe("Integration: two thick clients converge", () => {
  const cases: Array<[Mix, boolean, number]> = [
    ["moves", false, 3],
    ["moves", false, 4],
    ["state", false, 5],
    ["all", false, 6],
    ["all", false, 7],
    ["all", true, 8],
  ];
  for (const [i, [mix, device, seed]] of cases.entries()) {
    it(`${mix}${device ? " + device" : ""} (seed ${seed})`, async () => {
      await runScenario(mix, 9930 + i, seed, device);
    }, 40000);
  }
});
