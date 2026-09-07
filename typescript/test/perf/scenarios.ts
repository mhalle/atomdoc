/**
 * Benchmark scenarios shared by the sweep (bench.test.ts) and the
 * scaling assertions (scaling.test.ts).
 */

import { LocalDoc } from "../../src/thick/local-doc.js";
import { UndoManager } from "../../src/thick/undo-manager.js";
import { bridgeDocToStore } from "../../src/thick/store-bridge.js";
import { NodeStore } from "../../src/store.js";
import { applyPatch } from "../../src/patch.js";
import { SchemaRegistry } from "../../src/schema.js";
import { getSlotChildren } from "../../src/thick/doc-node.js";
import { schema, makeSnapshot, type Scenario } from "./scene.js";

export function timed(fn: () => void): number {
  const t0 = performance.now();
  fn();
  return (performance.now() - t0) / 1000;
}

export const scenarios: Record<string, Scenario> = {
  load_snapshot: {
    desc: "new LocalDoc from a 2n+1 node snapshot",
    run: (n) => {
      const snap = makeSnapshot(n);
      return timed(() => new LocalDoc(schema, snap));
    },
  },
  build: {
    desc: "n createNode + insert, one commit each",
    run: (n) => {
      const doc = new LocalDoc(schema, makeSnapshot(0));
      return timed(() => {
        for (let i = 0; i < n; i++) {
          doc.insertIntoSlot(doc.root, "volumes", "append", [doc.createNode("Volume", { name: `v${i}` })]);
        }
      });
    },
  },
  build_with_store: {
    desc: "same, bridged to a NodeStore (the UI path)",
    run: (n) => {
      const doc = new LocalDoc(schema, makeSnapshot(0));
      const store = new NodeStore();
      bridgeDocToStore(doc, store);
      return timed(() => {
        for (let i = 0; i < n; i++) {
          doc.insertIntoSlot(doc.root, "volumes", "append", [doc.createNode("Volume", { name: `v${i}` })]);
        }
      });
    },
  },
  write_autocommit: {
    desc: "n field writes, one commit each",
    run: (n) => {
      const doc = new LocalDoc(schema, makeSnapshot(n));
      const ids = getSlotChildren(doc.root, "volumes").map((v) => v.id);
      return timed(() => {
        for (let i = 0; i < ids.length; i++) doc.setNodeState(ids[i], "window", i);
      });
    },
  },
  write_with_store_and_undo: {
    desc: "same, with NodeStore bridge and UndoManager",
    run: (n) => {
      const doc = new LocalDoc(schema, makeSnapshot(n));
      bridgeDocToStore(doc, new NodeStore());
      new UndoManager(doc, 100);
      const ids = getSlotChildren(doc.root, "volumes").map((v) => v.id);
      return timed(() => {
        for (let i = 0; i < ids.length; i++) doc.setNodeState(ids[i], "window", i);
      });
    },
  },
  write_big_list: {
    desc: "20 rewrites of a k-point list field",
    scale: 2,
    run: (k) => {
      const doc = new LocalDoc(schema, makeSnapshot(0));
      const m = doc.createNode("Markup", { points: Array.from({ length: k }, (_, i) => [i, 0, 0]) });
      doc.insertIntoSlot(doc.root, "markups", "append", [m]);
      return timed(() => {
        for (let j = 0; j < 20; j++) {
          doc.setNodeState(m.id, "points", [...(m.state.points as number[][]), [j, 1, 1]]);
        }
      });
    },
  },
  apply_patch_state: {
    desc: "applyOperations, n state entries",
    run: (n) => {
      const doc = new LocalDoc(schema, makeSnapshot(n));
      const state: Record<string, Record<string, unknown>> = {};
      for (const v of getSlotChildren(doc.root, "volumes")) state[v.id] = { window: 1 };
      return timed(() => doc.applyOperations({ ordered: [], state }, { skipUndo: true }));
    },
  },
  undo_redo: {
    desc: "undo + redo of an n-write transaction",
    run: (n) => {
      const doc = new LocalDoc(schema, makeSnapshot(n));
      const um = new UndoManager(doc, 100);
      const state: Record<string, Record<string, unknown>> = {};
      for (const v of getSlotChildren(doc.root, "volumes")) state[v.id] = { window: 1 };
      doc.applyOperations({ ordered: [], state });
      return timed(() => {
        um.undo();
        um.redo();
      });
    },
  },
  delete_range: {
    desc: "delete a range of n siblings",
    run: (n) => {
      const doc = new LocalDoc(schema, makeSnapshot(n));
      const vols = getSlotChildren(doc.root, "volumes");
      return timed(() => doc.deleteRange(vols[0].id, vols[vols.length - 1].id));
    },
  },
  delete_range_with_store: {
    desc: "same, bridged to a NodeStore",
    run: (n) => {
      const doc = new LocalDoc(schema, makeSnapshot(n));
      bridgeDocToStore(doc, new NodeStore());
      const vols = getSlotChildren(doc.root, "volumes");
      return timed(() => doc.deleteRange(vols[0].id, vols[vols.length - 1].id));
    },
  },
  to_snapshot: {
    desc: "toSnapshot of 2n+1 nodes",
    run: (n) => {
      const doc = new LocalDoc(schema, makeSnapshot(n));
      return timed(() => doc.toSnapshot());
    },
  },
  handles: {
    desc: "handles(strong) over 2n+1 nodes",
    run: (n) => {
      const doc = new LocalDoc(schema, makeSnapshot(n));
      return timed(() => doc.handles("strong"));
    },
  },
  referrers: {
    desc: "referrers() of a node with n referrers",
    run: (n) => {
      const doc = new LocalDoc(schema, makeSnapshot(n));
      const hub = getSlotChildren(doc.root, "transforms")[0].id;
      return timed(() => doc.referrers(hub));
    },
  },
  store_load_snapshot: {
    desc: "NodeStore.loadSnapshot of 2n+1 nodes",
    run: (n) => {
      const snap = makeSnapshot(n);
      const store = new NodeStore();
      return timed(() => store.loadSnapshot(snap));
    },
  },
  store_insert_patches: {
    desc: "thin store: n single-node insert patches into one slot",
    run: (n) => {
      const store = new NodeStore();
      store.loadSnapshot(makeSnapshot(0));
      const root = store.getRootId();
      return timed(() => {
        for (let i = 0; i < n; i++) {
          applyPatch(store, {
            ordered: [[0, [[`x${i}`, "Volume"]], 0, "volumes", i ? `x${i - 1}` : 0, 0]],
            state: { [`x${i}`]: { name: `x${i}` } },
          });
        }
        void root;
      });
    },
  },
  store_subscribers: {
    desc: "n subscribers, one state patch each (UI fan-out)",
    run: (n) => {
      const store = new NodeStore();
      store.loadSnapshot(makeSnapshot(n));
      const ids = store.getChildren(store.getRootId(), "volumes");
      let hits = 0;
      for (const id of ids) store.subscribe(id, () => hits++);
      return timed(() => {
        for (const id of ids) applyPatch(store, { ordered: [], state: { [id]: { window: 2 } } });
        void hits;
      });
    },
  },
  validate: {
    desc: "zod validate n Volume states",
    run: (n) => {
      const reg = new SchemaRegistry(schema);
      const doc = new LocalDoc(schema, makeSnapshot(n));
      const states = getSlotChildren(doc.root, "volumes").map((v) => v.state);
      reg.validate("Volume", states[0]);
      return timed(() => {
        for (const s of states) reg.validate("Volume", s);
      });
    },
  },
  merge_undo: {
    desc: "n inserts merged into one undo step",
    scale: 0.5,
    run: (k) => {
      const doc = new LocalDoc(schema, makeSnapshot(0));
      new UndoManager(doc, 100, { mergeInterval: 1e12 });
      return timed(() => {
        for (let i = 0; i < k; i++) {
          doc.insertIntoSlot(doc.root, "volumes", "append", [doc.createNode("Volume")]);
        }
      });
    },
  },
  deep_tree: {
    desc: "build + toSnapshot a chain n folders deep",
    scale: 0.25,
    run: (depth) => {
      const doc = new LocalDoc(schema, makeSnapshot(0));
      return timed(() => {
        let parent = doc.root;
        let slot = "folders";
        for (let i = 0; i < depth; i++) {
          const f = doc.createNode("Folder", { name: `f${i}` });
          doc.insertIntoSlot(parent, slot, "append", [f]);
          parent = f;
          slot = "items";
        }
        doc.toSnapshot();
        doc.deleteRange(getSlotChildren(doc.root, "folders")[0].id);
      });
    },
  },
};

