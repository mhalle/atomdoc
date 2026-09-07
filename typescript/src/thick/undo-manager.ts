/**
 * Stack-based undo/redo manager — port of _undo.py.
 *
 * Supports a merge interval (consecutive transactions collapse into one
 * step), `skipUndo` transaction flags, and history export/import so undo
 * state survives replacing the document with a newer snapshot.
 */

import type { OrderedOp, WireOperations } from "../types.js";
import { ListenerError, type LocalDoc, type ChangeEvent } from "./local-doc.js";
import { mergeOperations } from "./local-ops.js";

export interface UndoManagerOptions {
  /** Window in ms within which consecutive transactions merge. 0 disables. */
  mergeInterval?: number;
  /** Clock used for the merge interval; defaults to `Date.now`. */
  clock?: () => number;
}

interface UndoStackItem {
  operations: WireOperations;
  meta: Record<string, unknown>;
  /**
   * The inverse objects (as delivered by change events) this item was
   * built from: one, or several when transactions merged. Lets a caller
   * that holds an event's inverse find the item it landed in.
   */
  sources?: WireOperations[];
}

export interface UndoHistoryItem {
  operations: WireOperations;
  meta: Record<string, unknown>;
}

export interface UndoHistory {
  docId: string;
  docType: string;
  undoStack: UndoHistoryItem[];
  redoStack: UndoHistoryItem[];
  lastUpdate?: number;
}

export class UndoManager {
  private doc: LocalDoc;
  private maxSteps: number;
  private mergeInterval: number;
  private clock: () => number;
  private undoStack: UndoStackItem[] = [];
  private redoStack: UndoStackItem[] = [];
  private txType: "update" | "undo" | "redo" = "update";
  private lastUpdate: number | undefined;
  private unsub: () => void;

  constructor(doc: LocalDoc, maxSteps = 100, options: UndoManagerOptions = {}) {
    this.doc = doc;
    this.maxSteps = maxSteps;
    this.mergeInterval = options.mergeInterval ?? 0;
    this.clock = options.clock ?? Date.now;
    this.unsub = this.isEnabled
      ? doc.onChange((event) => this._onChange(event))
      : () => {};
  }

  get isEnabled(): boolean {
    return this.maxSteps > 0;
  }

  private _onChange(event: ChangeEvent): void {
    if (event.flags?.skipUndo) return;
    const item: UndoStackItem = {
      operations: event.inverseOperations,
      meta: {},
      sources: [event.inverseOperations],
    };
    if (this.txType === "update") {
      const now = this.clock();
      const last = this.undoStack[this.undoStack.length - 1];
      if (
        last !== undefined &&
        this.lastUpdate !== undefined &&
        now - this.lastUpdate < this.mergeInterval
      ) {
        // Newest inverse first: undoing replays it before the older one.
        last.operations = mergeOperations(item.operations, last.operations);
        (last.sources ??= []).push(item.operations);
      } else {
        if (this.undoStack.length >= this.maxSteps) {
          this.undoStack.shift();
        }
        this.undoStack.push(item);
      }
      this.redoStack.length = 0;
      this.lastUpdate = now;
    } else if (this.txType === "undo") {
      this.redoStack.push(item);
      this.txType = "update";
    } else if (this.txType === "redo") {
      this.undoStack.push(item);
      this.txType = "update";
    }
  }

  undo(): void {
    this.doc.forceCommit();
    const item = this.undoStack.pop();
    if (!item) return;
    this.txType = "undo";
    this.lastUpdate = undefined;
    try {
      this.doc.applyOperations(item.operations, undefined, true);
    } catch (e) {
      // A ListenerError means the step applied and committed; only an
      // observer failed. Otherwise the step could not be applied (a node
      // it re-creates exists again, say): keep it so the user can retry
      // after the cause is gone, instead of silently losing it.
      if (!(e instanceof ListenerError)) this.undoStack.push(item);
      throw e;
    } finally {
      this.txType = "update";
    }
  }

  redo(): void {
    this.doc.forceCommit();
    const item = this.redoStack.pop();
    if (!item) return;
    this.txType = "redo";
    this.lastUpdate = undefined;
    try {
      this.doc.applyOperations(item.operations, undefined, true);
    } catch (e) {
      if (!(e instanceof ListenerError)) this.redoStack.push(item);
      throw e;
    } finally {
      this.txType = "update";
    }
  }

  /**
   * Replace the recorded original of one field in the entry built from
   * `source` (an inverse a change event delivered). Used when a remote
   * write to that field was masked under a pending local edit: undoing
   * the edit should reveal what others last wrote, not what this client
   * saw before editing. Returns whether an entry was found.
   */
  refreshOriginal(source: WireOperations, nodeId: string, key: string, value: unknown): boolean {
    for (const stack of [this.undoStack, this.redoStack]) {
      for (const item of stack) {
        if (item.operations !== source && !item.sources?.includes(source)) continue;
        const patch = item.operations.state[nodeId];
        if (patch && key in patch) patch[key] = value;
        return true;
      }
    }
    return false;
  }

  get canUndo(): boolean {
    return this.undoStack.length > 0;
  }

  get canRedo(): boolean {
    return this.redoStack.length > 0;
  }

  /** Drop all undo and redo history. */
  clear(): void {
    this.undoStack = [];
    this.redoStack = [];
    this.lastUpdate = undefined;
  }

  dispose(): void {
    this.unsub();
  }

  // --- History transfer ---

  /**
   * Export undo and redo state for transfer to a matching document.
   * Pending edits are committed first so they are part of the history.
   */
  exportHistory(): UndoHistory {
    this.doc.forceCommit();
    const history: UndoHistory = {
      docId: this.doc.id,
      docType: this.doc.root.type,
      undoStack: this.undoStack.map(exportItem),
      redoStack: this.redoStack.map(exportItem),
    };
    if (this.lastUpdate !== undefined) history.lastUpdate = this.lastUpdate;
    return history;
  }

  /**
   * Replace this manager's history with a previously exported one.
   * The document ID and root type must match, because operations
   * reference node IDs. Stacks are truncated to `maxSteps`.
   */
  importHistory(history: unknown): void {
    if (!isUndoHistory(history)) {
      throw new TypeError("Invalid undo history");
    }
    if (history.docId !== this.doc.id || history.docType !== this.doc.root.type) {
      throw new Error("Undo history belongs to a different document");
    }
    this.undoStack = importStack(history.undoStack, this.maxSteps);
    this.redoStack = importStack(history.redoStack, this.maxSteps);
    this.lastUpdate = history.lastUpdate;
    this.txType = "update";
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function cloneOperations(ops: WireOperations): WireOperations {
  return {
    ordered: ops.ordered.map((op) => structuredClone(op)),
    state: Object.fromEntries(
      Object.entries(ops.state).map(([id, patch]) => [id, { ...patch }]),
    ),
  };
}

function exportItem(item: UndoStackItem): UndoHistoryItem {
  return { operations: cloneOperations(item.operations), meta: { ...item.meta } };
}

function importStack(items: UndoHistoryItem[], maxSteps: number): UndoStackItem[] {
  const retained = maxSteps === 0 ? [] : items.slice(-maxSteps);
  return retained.map((item) => ({
    operations: cloneOperations(item.operations),
    meta: { ...item.meta },
  }));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isRef(value: unknown): boolean {
  return value === 0 || typeof value === "string";
}

function isOrderedOp(value: unknown): value is OrderedOp {
  if (!Array.isArray(value)) return false;
  if (value[0] === 0) {
    return (
      value.length === 6 &&
      Array.isArray(value[1]) &&
      value[1].every(
        (pair: unknown) =>
          Array.isArray(pair) &&
          pair.length === 2 &&
          pair.every((p) => typeof p === "string"),
      ) &&
      isRef(value[2]) &&
      typeof value[3] === "string" &&
      isRef(value[4]) &&
      isRef(value[5])
    );
  }
  if (value[0] === 1) {
    return value.length === 3 && typeof value[1] === "string" && isRef(value[2]);
  }
  if (value[0] === 2) {
    return (
      value.length === 7 &&
      typeof value[1] === "string" &&
      isRef(value[2]) &&
      isRef(value[3]) &&
      typeof value[4] === "string" &&
      isRef(value[5]) &&
      isRef(value[6])
    );
  }
  return false;
}

function isOperations(value: unknown): value is WireOperations {
  return (
    isRecord(value) &&
    Array.isArray(value.ordered) &&
    value.ordered.every(isOrderedOp) &&
    isRecord(value.state) &&
    Object.values(value.state).every(isRecord)
  );
}

function isHistoryItem(value: unknown): value is UndoHistoryItem {
  return isRecord(value) && isOperations(value.operations) && isRecord(value.meta);
}

function isUndoHistory(value: unknown): value is UndoHistory {
  return (
    isRecord(value) &&
    typeof value.docId === "string" &&
    typeof value.docType === "string" &&
    Array.isArray(value.undoStack) &&
    value.undoStack.every(isHistoryItem) &&
    Array.isArray(value.redoStack) &&
    value.redoStack.every(isHistoryItem) &&
    (value.lastUpdate === undefined ||
      (typeof value.lastUpdate === "number" && Number.isFinite(value.lastUpdate)))
  );
}
