/**
 * Bridge: projects LocalDoc changes into the existing NodeStore.
 *
 * Reuses the thin client's applyPatch() so the NodeStore subscription
 * model and all UI code works unchanged.
 */

import type { NodeStore } from "../store.js";
import { applyPatch } from "../patch.js";
import type { LocalDoc } from "./local-doc.js";

/**
 * Connect a LocalDoc to a NodeStore.
 *
 * 1. Loads the current LocalDoc state as a snapshot into the store.
 * 2. Subscribes to doc changes and applies patches to the store.
 *
 * @returns Unsubscribe function.
 */
export function bridgeDocToStore(
  doc: LocalDoc,
  store: NodeStore,
): () => void {
  // Initial load
  store.loadSnapshot(doc.toSnapshot());

  // Subscribe to changes
  return doc.onChange((event) => {
    applyPatch(store, event.operations);
  });
}
