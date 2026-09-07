/**
 * Scaling tests: work that should be linear must not turn quadratic.
 *
 * Each case times a scenario at n and at 4n and asserts the ratio stays
 * well under 16 (quadratic). Linear work shows about 4; the bound is
 * loose so machine speed and noise do not matter. Absolute timings come
 * from bench.test.ts (BENCH=1).
 */

import { describe, it, expect } from "vitest";
import { LocalDoc } from "../../src/thick/local-doc.js";
import { NodeStore } from "../../src/store.js";
import { applyPatch } from "../../src/patch.js";
import { getSlotChildren } from "../../src/thick/doc-node.js";
import { scenarios } from "./scenarios.js";
import { schema, makeSnapshot } from "./scene.js";

const N = 500;
const QUADRATIC_BOUND = 10;

/**
 * Time ratio between 4n and n, best of five. If the small run is under
 * a millisecond the timer and the GC dominate, so n is raised until it
 * is not; a quadratic path stays far above the bound either way.
 */
function ratio(run: (n: number) => number, scale = 1): number {
  let small = Math.round(N * scale);
  const best = (n: number) => {
    let t = Infinity;
    for (let i = 0; i < 5; i++) t = Math.min(t, run(n));
    return t;
  };
  run(small); // warm up
  let t1 = best(small);
  while (t1 < 1e-3 && small < 64 * N) {
    small *= 4;
    t1 = best(small);
  }
  const t2 = best(4 * small);
  return t2 / Math.max(t1, 1e-6);
}

describe("scaling", () => {
  for (const name of [
    "load_snapshot",
    "build",
    "write_autocommit",
    "write_with_store_and_undo",
    "apply_patch_state",
    "undo_redo",
    "delete_range",
    "delete_range_with_store",
    "to_snapshot",
    "handles",
    "referrers",
    "store_load_snapshot",
    "store_subscribers",
    "validate",
    "merge_undo",
  ]) {
    it(`${name} scales linearly`, () => {
      const sc = scenarios[name];
      expect(ratio(sc.run, sc.scale), name).toBeLessThan(QUADRATIC_BOUND);
    });
  }

  it("filling a slot in one transaction stays linear through the store", () => {
    const run = (n: number) => {
      const doc = new LocalDoc(schema, makeSnapshot(0));
      const store = new NodeStore();
      store.loadSnapshot(doc.toSnapshot());
      const nodes = Array.from({ length: n }, (_, i) => doc.createNode("Volume", { name: `v${i}` }));
      let ops: import("../../src/types.js").WireOperations | undefined;
      const unsub = doc.onChange((e) => (ops = e.operations));
      // One transaction appending n nodes one by one.
      const t0 = performance.now();
      doc.applyOperations({
        ordered: nodes.map((nd, i) => [0, [[nd.id, "Volume"]], 0, "volumes", i ? nodes[i - 1].id : 0, 0]),
        state: {},
      });
      applyPatch(store, ops!);
      const secs = (performance.now() - t0) / 1000;
      unsub();
      expect(store.getChildren(store.getRootId(), "volumes").length).toBe(n);
      return secs;
    };
    expect(ratio(run)).toBeLessThan(QUADRATIC_BOUND);
  });

  it("a chain thousands of nodes deep builds, serializes, and deletes", () => {
    const depth = 5000;
    const doc = new LocalDoc(schema, makeSnapshot(0));
    let parent = doc.root;
    let slot = "folders";
    for (let i = 0; i < depth; i++) {
      const f = doc.createNode("Folder", { name: `f${i}` });
      doc.insertIntoSlot(parent, slot, "append", [f]);
      parent = f;
      slot = "items";
    }
    const snap = doc.toSnapshot();
    const reloaded = new LocalDoc(schema, snap);
    expect(reloaded.nodeMap.size).toBe(depth + 2); // root, hub, chain
    const store = new NodeStore();
    store.loadSnapshot(snap);
    expect(store.getAllNodeIds().length).toBe(depth + 2);
    doc.deleteRange(getSlotChildren(doc.root, "folders")[0].id);
    expect(doc.nodeMap.size).toBe(2);
    applyPatch(store, { ordered: [[1, "x", 0]], state: {} }); // no-op on a missing node
  });
});
