/**
 * Transaction lifecycle for LocalDoc — port of _transaction.py (simplified).
 *
 * No normalize or strict mode stages.
 */

import { ListenerError, type LocalDoc } from "./local-doc.js";

export type LifecycleStage = "idle" | "update" | "change";

/**
 * Per-transaction flags, delivered to change listeners.
 * `skipUndo` marks a transaction that must not enter undo history —
 * typically one that applies operations received from the server.
 */
export interface TransactionFlags {
  skipUndo?: boolean;
}

/**
 * Open a transaction if the doc is idle; returns whether one was opened.
 * A `skipUndo` transaction is always isolated: an already-open transaction
 * is committed first, so the caller's own pending edits keep their undo
 * entry and only the flagged work is excluded.
 */
function begin(doc: LocalDoc, flags?: TransactionFlags): boolean {
  if (flags?.skipUndo && doc._lifecycleStage === "update") {
    doc.forceCommit();
  }
  const isNewTx = doc._lifecycleStage === "idle";
  if (isNewTx) {
    doc._lifecycleStage = "update";
  }
  if (flags?.skipUndo) {
    doc._transactionFlags = { skipUndo: true };
  }
  return isNewTx;
}

export function withTransaction(
  doc: LocalDoc,
  fn: () => void,
  isApplyOperations = false,
  flags?: TransactionFlags,
): void {
  const stage = doc._lifecycleStage;

  if (stage === "change") {
    throw new Error("Cannot trigger an update during the 'change' stage");
  }

  const isNewTx = begin(doc, flags);

  try {
    fn();
  } catch (e) {
    // A failure inside a joined transaction propagates untouched: there
    // are no savepoints, so only the outermost boundary rolls back, and
    // it rolls back everything. `isApplyOperations` swallows a failure
    // only for a transaction this call opened.
    if (!isNewTx) throw e;
    try {
      doc.abort();
    } catch {
      // Abort failed, suppress
    }
    if (!isApplyOperations) throw e;
    return;
  }

  if (isNewTx) {
    try {
      doc.forceCommit();
    } catch (e) {
      // A ListenerError means the commit is final and only an observer
      // failed: never swallow that, whatever mode we are in.
      if (e instanceof ListenerError) throw e;
      try {
        doc.abort();
      } catch {
        // Abort failed, suppress
      }
      if (!isApplyOperations) throw e;
    }
  }
}
