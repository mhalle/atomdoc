/**
 * Partial replication against the real Python session: a scoped thick
 * client beside a whole-document one. The scoped client must see exactly
 * what its scope holds as the other client edits, and at quiescence its
 * document must equal what a fresh client with the same scope receives
 * from the server.
 */

import { describe, it, expect, afterEach } from "vitest";
import { spawn, type ChildProcess } from "node:child_process";
import { WebSocket } from "ws";
import { ThickAtomDocClient } from "../../src/thick/thick-client.js";
import { OutOfScopeError } from "../../src/thick/doc-node.js";
import { getSlotChildren } from "../../src/thick/doc-node.js";
import type { JsonDoc, ScopeAnchor, ServerMsg } from "../../src/types.js";

(globalThis as any).WebSocket = WebSocket;

let server: ChildProcess | undefined;
let ids: Record<string, string> = {};

function startServer(port: number): Promise<void> {
  return new Promise((resolve, reject) => {
    const serverPath = new URL("./scope_server.py", import.meta.url).pathname;
    server = spawn("uv", ["run", "python", serverPath], {
      cwd: new URL("../../../python", import.meta.url).pathname,
      env: { ...process.env, PORT: String(port) },
      stdio: ["ignore", "pipe", "pipe"],
    });
    const timeout = setTimeout(() => reject(new Error("Server start timeout")), 15000);
    server.stdout!.on("data", (data: Buffer) => {
      for (const line of data.toString().split("\n")) {
        if (line.startsWith("IDS ")) ids = JSON.parse(line.slice(4));
        if (line.includes("SERVER_READY")) {
          clearTimeout(timeout);
          resolve();
        }
      }
    });
    server.stderr!.on("data", (data: Buffer) => {
      const msg = data.toString().trim();
      if (msg) console.error("[scope-server]", msg);
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

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** What a fresh client with this scope receives: the ground truth. */
function scopedSnapshot(
  url: string,
  anchors: ScopeAnchor[],
): Promise<{ version: number; data: JsonDoc; stubs: [string, string][] }> {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url + "?partial=1");
    const timeout = setTimeout(() => reject(new Error("snapshot timeout")), 5000);
    ws.on("message", (raw) => {
      const msg = JSON.parse(raw.toString()) as ServerMsg;
      if (msg.type === "schema") ws.send(JSON.stringify({ type: "scope", ref: "t", anchors }));
      if (msg.type === "snapshot") {
        clearTimeout(timeout);
        ws.close();
        resolve({ version: msg.version, data: msg.data, stubs: msg.stubs ?? [] });
      }
    });
    ws.on("error", reject);
  });
}

async function connect(url: string, scope?: ScopeAnchor[]): Promise<ThickAtomDocClient> {
  const client = new ThickAtomDocClient({ url, scope, coalesce: false });
  const connected = new Promise<void>((resolve) => client.onConnected(() => resolve()));
  await client.connect();
  await connected;
  return client;
}

/** Wait until the scoped client has seen version `version` (or later). */
async function caughtUp(client: ThickAtomDocClient, version: number): Promise<void> {
  for (let i = 0; i < 200; i++) {
    if (client.getVersion() >= version) return;
    await sleep(10);
  }
  throw new Error(`client stuck at version ${client.getVersion()}, wanted ${version}`);
}

/** The scoped client's document must equal a fresh scoped snapshot. */
async function expectAgrees(url: string, scoped: ThickAtomDocClient): Promise<void> {
  const truth = await scopedSnapshot(url, scoped.getScope()!);
  expect(scoped.getDoc()!.toSnapshot()).toEqual(truth.data);
  expect(new Set(scoped.getDoc()!.detachedStubs().map(([id]) => id))).toEqual(
    new Set(truth.stubs.map(([id]) => id)),
  );
}

const items = (c: ThickAtomDocClient, parentId: string, slot = "items") =>
  getSlotChildren(c.getDoc()!.getNode(parentId)!, slot).map((n) => n.id);

describe("Integration: a scoped client beside a whole-document client", () => {
  it("holds its subtree, follows edits in it, and ignores the rest", async () => {
    const port = 9880;
    const url = `ws://localhost:${port}`;
    await startServer(port);
    const full = await connect(url);
    const scoped = await connect(url, [{ id: ids.s1 }]);
    const doc = scoped.getDoc()!;
    expect(doc.partial).toBe(true);
    expect(doc.root.stub).toBe(true);
    expect(items(scoped, ids.root, "sections")).toEqual([ids.s1]);
    expect(scoped.getState(ids.i10)).toEqual({ label: "Item 1.0" });
    expect(scoped.getState(ids.s0)).toBeUndefined();
    expect(scoped.getUndoManager()).toBeNull();

    // An edit inside the scope arrives; one outside does not.
    full.setField(ids.i10, "label", "changed");
    full.setField(ids.i00, "label", "unseen");
    await full.settled();
    await caughtUp(scoped, full.getVersion() - 1);
    await sleep(30);
    expect(scoped.getState(ids.i10)).toEqual({ label: "changed" });
    expect(scoped.getDoc()!.getNode(ids.i00)).toBeUndefined();
    await expectAgrees(url, scoped);

    // A node moved out of the scope leaves; moved back, it re-enters
    // with its subtree.
    full.moveNode(ids.i11, ids.s0, "items");
    await full.settled();
    await caughtUp(scoped, full.getVersion());
    expect(scoped.getDoc()!.getNode(ids.i11)).toBeUndefined();
    expect(scoped.getDoc()!.getNode(ids.n11)).toBeUndefined();
    full.moveNodeRelative(ids.i11, ids.i10, "before");
    await full.settled();
    await caughtUp(scoped, full.getVersion());
    expect(items(scoped, ids.s1)).toEqual([ids.i11, ids.i10, ids.i12]);
    expect(scoped.getState(ids.n11)).toEqual({ text: "Note 1.1" });
    await expectAgrees(url, scoped);

    // A reference from inside the scope to a node outside it brings a
    // detached stub; clearing it drops the stub.
    full.setField(ids.s1, "related", ids.i21);
    await full.settled();
    await caughtUp(scoped, full.getVersion());
    expect(scoped.getDoc()!.getNode(ids.i21)!.stub).toBe(true);
    expect(scoped.getDoc()!.detachedStubs()).toEqual([[ids.i21, "Item"]]);
    await expectAgrees(url, scoped);
    full.setField(ids.s1, "related", null);
    await full.settled();
    await caughtUp(scoped, full.getVersion());
    expect(scoped.getDoc()!.getNode(ids.i21)).toBeUndefined();

    // The scoped client's own edits: a write, a create, a delete, an undo.
    scoped.setField(ids.i12, "label", "mine");
    const created = scoped.createNode("Item", { label: "new" }, ids.s1, "items", "prepend");
    await scoped.settled();
    expect(items(scoped, ids.s1)).toEqual([created, ids.i11, ids.i10, ids.i12]);
    await full.settled();
    await caughtUp(full, scoped.getVersion());
    expect(full.getState(ids.i12)).toEqual({ label: "mine" });
    expect(full.getState(created)).toEqual({ label: "new" });
    scoped.deleteNode(ids.i10);
    await scoped.settled();
    expect(scoped.getDoc()!.getNode(ids.i10)).toBeUndefined();
    scoped.undo();
    await scoped.settled();
    expect(items(scoped, ids.s1)).toEqual([created, ids.i11, ids.i10, ids.i12]);
    expect(scoped.getState(ids.n10)).toEqual({ text: "Note 1.0" });
    await expectAgrees(url, scoped);

    // Writes to stubs are refused locally, before anything is sent.
    expect(() => scoped.setField(ids.root, "title", "x")).toThrow(OutOfScopeError);
    expect(() => scoped.createNode("Section", {}, ids.root, "sections")).toThrow(OutOfScopeError);

    scoped.disconnect();
    full.disconnect();
  });

  it("changes scope in place: deepen, widen, narrow", async () => {
    const port = 9881;
    const url = `ws://localhost:${port}`;
    await startServer(port);
    const full = await connect(url);
    const scoped = await connect(url, [{ id: ids.s1, depth: 0 }]);
    expect(scoped.getDoc()!.getNode(ids.i10)!.stub).toBe(true);
    expect(items(scoped, ids.s1)).toEqual([ids.i10, ids.i11, ids.i12]);
    expect(() => scoped.createNode("Item", {}, ids.s1, "items")).toThrow(OutOfScopeError);

    // Deepen by one: the items fill in place, their notes arrive as stubs.
    const i10 = scoped.getDoc()!.getNode(ids.i10)!;
    const resolved = await scoped.setScope([{ id: ids.s1, depth: 1 }]);
    expect(resolved).toEqual([{ id: ids.s1, depth: 1 }]);
    expect(scoped.getDoc()!.getNode(ids.i10)).toBe(i10);
    expect(i10.stub).toBe(false);
    expect(scoped.getState(ids.i10)).toEqual({ label: "Item 1.0" });
    expect(scoped.getDoc()!.getNode(ids.n10)!.stub).toBe(true);
    await expectAgrees(url, scoped);

    // Widen: another section enters with its subtree.
    await scoped.setScope([{ id: ids.s1, depth: 1 }, { id: ids.s2 }]);
    expect(items(scoped, ids.root, "sections")).toEqual([ids.s1, ids.s2]);
    expect(scoped.getState(ids.n21)).toEqual({ text: "Note 2.1" });
    await expectAgrees(url, scoped);

    // Narrow: s1 leaves; s2 stays, and edits to it keep flowing.
    await scoped.setScope([{ id: ids.s2 }]);
    expect(scoped.getDoc()!.getNode(ids.s1)).toBeUndefined();
    full.setField(ids.i20, "label", "still seen");
    full.setField(ids.i10, "label", "not seen");
    await full.settled();
    await caughtUp(scoped, full.getVersion() - 1);
    await sleep(30);
    expect(scoped.getState(ids.i20)).toEqual({ label: "still seen" });
    expect(scoped.getDoc()!.getNode(ids.i10)).toBeUndefined();
    await expectAgrees(url, scoped);

    scoped.disconnect();
    full.disconnect();
  });

  it("converges with a whole-document client under random edits", async () => {
    const port = 9882;
    const url = `ws://localhost:${port}`;
    await startServer(port);
    const full = await connect(url);
    const scoped = await connect(url, [{ id: ids.s1 }, { id: ids.s2, depth: 0 }]);
    let seed = 12345;
    const rng = () => {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      return seed / 4294967296;
    };
    const sections = [ids.s0, ids.s1, ids.s2];
    const fullDoc = full.getDoc()!;
    const allItems = () => sections.flatMap((s) => getSlotChildren(fullDoc.getNode(s)!, "items").map((n) => n.id));
    for (let k = 0; k < 60; k++) {
      const roll = rng();
      const pool = allItems();
      const pick = () => pool[Math.floor(rng() * pool.length)];
      try {
        if (roll < 0.25) {
          full.setField(pick(), "label", `l${k}`);
        } else if (roll < 0.45) {
          full.moveNode(pick(), sections[Math.floor(rng() * 3)], "items");
        } else if (roll < 0.6 && pool.length > 4) {
          const victim = pick();
          if (fullDoc.referrers(victim).length === 0) full.deleteNode(victim);
        } else if (roll < 0.75) {
          full.createNode("Item", { label: `c${k}` }, sections[Math.floor(rng() * 3)], "items");
        } else if (roll < 0.85) {
          full.setField(sections[Math.floor(rng() * 3)], "related", rng() < 0.7 ? pick() : null);
        } else if (roll < 0.95) {
          const target = pick();
          const notes = getSlotChildren(fullDoc.getNode(target)!, "notes");
          if (notes.length > 0) full.setField(notes[0].id, "text", `t${k}`);
          else full.createNode("Note", { text: `n${k}` }, target, "notes");
        } else {
          full.undo();
        }
      } catch {
        // A move into its own subtree, say: the harness is not careful.
      }
      if (k % 10 === 9) {
        await full.settled();
        await caughtUp(scoped, full.getVersion() - 2);
        await sleep(30);
      }
    }
    await full.settled();
    await sleep(100);
    await expectAgrees(url, scoped);
    scoped.disconnect();
    full.disconnect();
  }, 30000);
});
