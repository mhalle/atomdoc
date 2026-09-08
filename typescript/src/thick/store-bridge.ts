/**
 * Bridge: projects LocalDoc changes into the existing NodeStore.
 *
 * Reuses the thin client's applyPatch() so the NodeStore subscription
 * model and all UI code works unchanged.
 *
 * The document is the source of truth and is always current; the store
 * is the UI's view of it. By default the bridge flushes store updates
 * once per animation frame: a burst of patches (a device streaming
 * measurements, a transaction touching many nodes) reaches subscribers
 * as one notification per node per frame instead of one per patch.
 * Outside a browser the frame is a macrotask. `coalesce: false` applies
 * every change to the store synchronously.
 */

import type { NodeStore } from "../store.js";
import { applyPatch } from "../patch.js";
import type { WireOperations } from "../types.js";
import type { LocalDoc } from "./local-doc.js";

export interface StoreBridgeOptions {
  /**
   * Batch store updates per animation frame (default) rather than
   * applying each document change to the store as it commits.
   */
  coalesce?: boolean;
}

/**
 * Handle for a bridge. Calling it disconnects the bridge (the original
 * API); `flush()` applies queued changes to the store now.
 */
export interface StoreBridge {
  (): void;
  /** Apply every change queued since the last frame to the store now. */
  flush(): void;
  /** Stop syncing; queued changes are discarded. */
  dispose(): void;
  /** True while changes are queued for the next frame. */
  readonly pending: boolean;
}

/**
 * Longest a queued change waits when animation frames do not fire (a
 * hidden tab): subscribers that are not painting still hear about it.
 */
const FALLBACK_MS = 100;

/** Run `cb` on the next frame; returns a cancel function. */
function scheduleFrame(cb: () => void): () => void {
  const g = globalThis as {
    requestAnimationFrame?: (cb: () => void) => number;
    cancelAnimationFrame?: (handle: number) => void;
  };
  let done = false;
  let frame: number | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const clear = () => {
    done = true;
    if (frame !== undefined) g.cancelAnimationFrame?.(frame);
    if (timer !== undefined) clearTimeout(timer);
  };
  const fire = () => {
    if (done) return;
    clear();
    cb();
  };
  if (typeof g.requestAnimationFrame === "function") {
    frame = g.requestAnimationFrame(fire);
    timer = setTimeout(fire, FALLBACK_MS);
  } else {
    timer = setTimeout(fire, 0);
  }
  return clear;
}

/**
 * Connect a LocalDoc to a NodeStore.
 *
 * 1. Loads the current LocalDoc state as a snapshot into the store.
 * 2. Subscribes to doc changes and applies patches to the store, once
 *    per frame unless `coalesce` is false.
 *
 * @returns A handle that disconnects when called; see {@link StoreBridge}.
 */
export function bridgeDocToStore(
  doc: LocalDoc,
  store: NodeStore,
  options: StoreBridgeOptions = {},
): StoreBridge {
  const coalesce = options.coalesce ?? true;
  let queue: WireOperations[] = [];
  let cancel: (() => void) | null = null;
  let live = true;

  // Initial load
  store.loadSnapshot(doc.toSnapshot());

  const flush = () => {
    if (cancel) {
      cancel();
      cancel = null;
    }
    if (queue.length === 0) return;
    const batch = queue;
    queue = [];
    // One store batch for the whole frame: a node touched by several
    // patches notifies once.
    store.batch(() => {
      for (const ops of batch) applyPatch(store, ops);
    });
  };

  const unsubscribe = doc.onChange((event) => {
    if (!coalesce) {
      applyPatch(store, event.operations);
      return;
    }
    queue.push(event.operations);
    if (!cancel) cancel = scheduleFrame(flush);
  });

  const dispose = () => {
    if (!live) return;
    live = false;
    unsubscribe();
    if (cancel) {
      cancel();
      cancel = null;
    }
    queue = [];
  };

  const handle = dispose as StoreBridge;
  handle.flush = flush;
  handle.dispose = dispose;
  Object.defineProperty(handle, "pending", { get: () => queue.length > 0 });
  return handle;
}
