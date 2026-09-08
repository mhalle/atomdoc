/**
 * Stack-based undo/redo manager — port of _undo.py.
 *
 * Supports a merge interval (consecutive transactions collapse into one
 * step), `skipUndo` transaction flags, and history export/import so undo
 * state survives replacing the document with a newer snapshot.
 *
 * Two hooks serve the thick client, whose structural edits commit only
 * when the server confirms them: `reserve()` holds a step's place in
 * history from the moment the user acts until its commit arrives
 * (`commitInto`), and `dispatch` lets the client take an undo or redo
 * step over and commit it later (`commitAs`).
 */

import type { OrderedOp, WireOperations } from "../types.js";
import { ListenerError, type LocalDoc, type ChangeEvent } from "./local-doc.js";
import { mergeOperations } from "./local-ops.js";

export interface UndoManagerOptions {
  /** Window in ms within which consecutive transactions merge. 0 disables. */
  mergeInterval?: number;
  /** Clock used for the merge interval; defaults to `Date.now`. */
  clock?: () => number;
  /**
   * Offered every undo or redo step before it is applied. Returning true
   * takes the step over: it is off both stacks until the caller commits
   * it inside {@link UndoManager.commitAs} with the given `token`, which
   * files the resulting commit on the opposite stack. A step the caller
   * never commits (the server had nothing to apply, or refused it) is
   * consumed. Returning false applies the step to the document now.
   */
  dispatch?: (operations: WireOperations, kind: "undo" | "redo", token: number) => boolean;
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
  /** A reservation (see `reserve`) whose commit has not arrived yet. */
  pending?: number;
  /** A reservation made within the merge window of the step before it. */
  merge?: boolean;
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
  private txType: "update" | "undo" | "redo" | "discard" = "update";
  private lastUpdate: number | undefined;
  private unsub: () => void;
  private dispatch: UndoManagerOptions["dispatch"];
  private reservations = 0;
  private fillTarget: number | null = null;
  private tokens = 0;
  /** Dispatched steps awaiting `commitAs`, with the history epoch they left. */
  private inFlight = new Map<number, { kind: "undo" | "redo"; epoch: number }>();
  /** Bumped by every new local step; a dispatched undo older than the newest step is stale. */
  private epoch = 0;

  constructor(doc: LocalDoc, maxSteps = 100, options: UndoManagerOptions = {}) {
    this.doc = doc;
    this.maxSteps = maxSteps;
    this.mergeInterval = options.mergeInterval ?? 0;
    this.clock = options.clock ?? Date.now;
    this.dispatch = options.dispatch;
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
    if (this.fillTarget !== null) {
      this._fill(this.fillTarget, item);
      return;
    }
    if (this.txType === "update") {
      const now = this.clock();
      const last = this.undoStack[this.undoStack.length - 1];
      if (
        last !== undefined &&
        !last.pending &&
        this.lastUpdate !== undefined &&
        now - this.lastUpdate < this.mergeInterval
      ) {
        // Newest inverse first: undoing replays it before the older one.
        last.operations = mergeOperations(item.operations, last.operations);
        (last.sources ??= []).push(item.operations);
      } else {
        this._push(item);
      }
      this.redoStack.length = 0;
      this.lastUpdate = now;
      this.epoch++;
    } else if (this.txType === "undo") {
      this.redoStack.push(item);
      this.txType = "update";
    } else if (this.txType === "redo") {
      this.undoStack.push(item);
      this.txType = "update";
    }
    // "discard": a dispatched step whose commit no longer belongs in
    // history (see commitAs). Nothing is filed.
  }

  private _push(item: UndoStackItem): void {
    if (this.undoStack.length >= this.maxSteps) {
      this.undoStack.shift();
    }
    this.undoStack.push(item);
  }

  /**
   * Hold a place in history for a local step whose commit arrives later
   * (a structural edit the server has yet to confirm). The step is
   * ordered by when the user acted, not by when its commit lands, and
   * the merge window is measured from now. Redo is cleared as for any
   * new step. Until the commit fills it, the placeholder blocks undo:
   * `canUndo` is false while the newest step is one.
   *
   * @returns An id for {@link commitInto} or {@link cancel}.
   */
  reserve(): number {
    const id = ++this.reservations;
    const now = this.clock();
    const merge =
      this.undoStack.length > 0 &&
      this.lastUpdate !== undefined &&
      now - this.lastUpdate < this.mergeInterval;
    this._push({ operations: { ordered: [], state: {} }, meta: {}, pending: id, merge });
    this.redoStack.length = 0;
    this.lastUpdate = now;
    this.epoch++;
    return id;
  }

  /**
   * Run `fn`, whose commit fills the reservation `id`. If `fn` commits
   * nothing, or the reservation is gone, the placeholder is removed.
   */
  commitInto(id: number, fn: () => void): void {
    this.doc.forceCommit();
    this.fillTarget = id;
    try {
      fn();
    } finally {
      this.fillTarget = null;
      this.cancel(id);
    }
  }

  /** Drop the reservation `id` if its commit never arrived. */
  cancel(id: number): void {
    const idx = this.undoStack.findIndex((item) => item.pending === id);
    if (idx >= 0) this.undoStack.splice(idx, 1);
  }

  private _fill(id: number, item: UndoStackItem): void {
    const idx = this.undoStack.findIndex((entry) => entry.pending === id);
    if (idx < 0) return; // cancelled, or pushed out by maxSteps: too old to keep
    const placeholder = this.undoStack[idx];
    const previous = idx > 0 ? this.undoStack[idx - 1] : undefined;
    if (placeholder.merge && previous && !previous.pending) {
      previous.operations = mergeOperations(item.operations, previous.operations);
      (previous.sources ??= []).push(item.operations);
      this.undoStack.splice(idx, 1);
      return;
    }
    placeholder.operations = item.operations;
    placeholder.sources = item.sources;
    delete placeholder.pending;
    delete placeholder.merge;
  }

  undo(): void {
    this.doc.forceCommit();
    const top = this.undoStack[this.undoStack.length - 1];
    if (!top || top.pending) return;
    const item = this.undoStack.pop()!;
    this.lastUpdate = undefined;
    if (this.dispatch) {
      const token = ++this.tokens;
      this.inFlight.set(token, { kind: "undo", epoch: this.epoch });
      if (this.dispatch(item.operations, "undo", token)) return;
      this.inFlight.delete(token);
    }
    this.txType = "undo";
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
    this.lastUpdate = undefined;
    if (this.dispatch) {
      const token = ++this.tokens;
      this.inFlight.set(token, { kind: "redo", epoch: this.epoch });
      if (this.dispatch(item.operations, "redo", token)) return;
      this.inFlight.delete(token);
    }
    this.txType = "redo";
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
   * Run `fn`, whose commit is the dispatched step `token`: an undo commit
   * is filed on the redo stack and a redo commit on the undo stack, as
   * if the manager had applied the step itself. An undo step overtaken
   * by a newer local step is applied but not filed: the edit made since
   * already invalidated its redo.
   */
  commitAs(token: number, fn: () => void): void {
    const flight = this.inFlight.get(token);
    this.inFlight.delete(token);
    this.doc.forceCommit();
    this.txType =
      !flight || (flight.kind === "undo" && flight.epoch !== this.epoch) ? "discard" : flight.kind;
    this.lastUpdate = undefined;
    try {
      fn();
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

  /** True when there is a step to undo and it is not still awaiting confirmation. */
  get canUndo(): boolean {
    const top = this.undoStack[this.undoStack.length - 1];
    return top !== undefined && !top.pending;
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
   * Steps still awaiting confirmation are left out.
   */
  exportHistory(): UndoHistory {
    this.doc.forceCommit();
    const history: UndoHistory = {
      docId: this.doc.id,
      docType: this.doc.root.type,
      undoStack: this.undoStack.filter((item) => !item.pending).map(exportItem),
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
