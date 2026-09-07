/**
 * ThickAtomDocClient — local document model + WebSocket sync + offline buffer.
 *
 * Operations apply locally first for instant UI feedback. Forward ops
 * are sent to the server. Server echoes are skipped (already applied).
 * Remote patches from other clients are applied to the local doc.
 * Offline: ops buffer until reconnect, then rebased.
 */

import { SchemaRegistry } from "../schema.js";
import { NodeStore } from "../store.js";
import type {
  AtomDocSchema,
  ClientMsg,
  ErrorMsg,
  JsonDoc,
  OrderedOp,
  PatchMsg,
  ServerMsg,
  WireOperations,
} from "../types.js";
import { LocalDoc, type ChangeEvent } from "./local-doc.js";
import { bridgeDocToStore } from "./store-bridge.js";
import { UndoManager } from "./undo-manager.js";

export interface ThickClientOptions {
  url: string;
  /** Undo steps to keep; 0 disables undo. Default 100. */
  maxUndoSteps?: number;
  /** Merge consecutive local transactions within this many ms into one undo step. Default 0. */
  mergeInterval?: number;
}

/**
 * The operations that bring a local document, which already applied the
 * request optimistically, to what the server recorded for it (`echo`):
 * a node the echo inserts is moved to the echoed neighbors if the client
 * has it, or inserted there if it does not — unless a still-pending op
 * of ours deleted it, in which case it stays gone (a server-side
 * normalizer adding a node is the only other way for it to be missing);
 * moves replay as they are; deletes need nothing; state patches replay
 * as they are.
 */
function echoReconcileOps(
  doc: LocalDoc,
  echo: WireOperations,
  pendingDeleted: Set<string>,
): WireOperations {
  const ordered: OrderedOp[] = [];
  for (const op of echo.ordered) {
    if (op[0] === 0) {
      const [, pairs, parentId, slotName, prevId, nextId] = op;
      let prev: string | 0 = prevId;
      // The echoed `next` is a hint for the first node only: `prev` is
      // preferred when it resolves, `next` is the fallback when it does
      // not (deleted locally meanwhile). Later nodes follow the previous.
      let next: string | 0 = nextId;
      for (const [id, type] of pairs) {
        if (doc.getNode(id)) {
          ordered.push([2, id, 0, parentId, slotName, prev, next]);
        } else if (!pendingDeleted.has(id)) {
          ordered.push([0, [[id, type]], parentId, slotName, prev, next]);
        }
        prev = id;
        next = 0;
      }
    } else if (op[0] === 2) {
      ordered.push(op);
    }
  }
  return { ordered, state: echo.state };
}

export class ThickAtomDocClient {
  private ws: WebSocket | null = null;
  private store = new NodeStore();
  private schema: SchemaRegistry | null = null;
  private rawSchema: AtomDocSchema | null = null;
  private doc: LocalDoc | null = null;
  private undoMgr: UndoManager | null = null;
  private version = 0;
  private url: string;
  private maxUndoSteps: number;
  private mergeInterval: number;
  private clientId: string = crypto.randomUUID();

  private bridgeUnsub: (() => void) | null = null;
  private docUnsub: (() => void) | null = null;
  private online = false;
  /**
   * Sent, not yet echoed or rejected; matched by `ref`. `inverse` is the
   * event's inverse (the object the undo manager holds too), refreshed
   * when a remote write underneath is masked.
   */
  private pendingOps: Array<{
    ref: string;
    ops: WireOperations;
    inverse: WireOperations;
    /** IDs the op deleted, so an earlier echo does not resurrect them. */
    deleted: Set<string>;
  }> = [];
  private bufferedOps: WireOperations[] = [];
  private applyingRemote = false;
  private nextRef = 1;
  /** Online again, but the reconnect snapshot has not arrived yet. */
  private onlinePending = false;

  private connectedCallbacks = new Set<() => void>();
  private resyncCallbacks = new Set<() => void>();
  private errorCallbacks = new Set<(err: ErrorMsg) => void>();
  private patchCallbacks = new Set<(version: number) => void>();
  private offlineCallbacks = new Set<() => void>();
  private onlineCallbacks = new Set<() => void>();

  constructor(options: ThickClientOptions) {
    this.url = options.url;
    this.maxUndoSteps = options.maxUndoSteps ?? 100;
    this.mergeInterval = options.mergeInterval ?? 0;
  }

  // --- Lifecycle ---

  connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      // One live socket at a time: a previous one (a failed attempt, a
      // connection being replaced) must not report on this client.
      const previous = this.ws;
      if (previous) {
        this._detachSocket(previous);
        previous.close();
        this._wentOffline();
      }
      const ws = new WebSocket(this.url);
      this.ws = ws;

      ws.onopen = () => {
        if (this.ws !== ws) return;
        this.online = true;
        // Online callbacks wait for the reconnect snapshot, so they see
        // the resynced document rather than the stale one.
        this.onlinePending = this.doc !== null;
        resolve();
      };

      ws.onerror = (event) => {
        if (this.ws !== ws) return;
        reject(event);
      };

      ws.onmessage = (event) => {
        if (this.ws !== ws) return;
        const msg = JSON.parse(
          typeof event.data === "string" ? event.data : event.data.toString(),
        ) as ServerMsg;
        this._handleMessage(msg);
      };

      ws.onclose = () => {
        // A late close from a socket that was already replaced says
        // nothing about the live connection.
        if (this.ws !== ws) return;
        this.ws = null;
        this._wentOffline();
      };
    });
  }

  disconnect(): void {
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      this._detachSocket(ws);
      ws.close();
    }
    this._wentOffline();
  }

  private _detachSocket(ws: WebSocket): void {
    ws.onopen = null;
    ws.onerror = null;
    ws.onmessage = null;
    ws.onclose = null;
  }

  private _wentOffline(): void {
    const wasOnline = this.online;
    this.online = false;
    this.onlinePending = false;
    // Operations sent but not yet echoed may never have reached the
    // server: keep them, ahead of anything buffered since, so the
    // reconnect replays them in order.
    if (this.pendingOps.length > 0) {
      this.bufferedOps = [...this.pendingOps.map((p) => p.ops), ...this.bufferedOps];
      this.pendingOps = [];
    }
    if (wasOnline) {
      for (const cb of this.offlineCallbacks) cb();
    }
  }

  // --- State access ---

  getStore(): NodeStore {
    return this.store;
  }

  getSchema(): SchemaRegistry | null {
    return this.schema;
  }

  getDoc(): LocalDoc | null {
    return this.doc;
  }

  getUndoManager(): UndoManager | null {
    return this.undoMgr;
  }

  getVersion(): number {
    return this.version;
  }

  isOnline(): boolean {
    return this.online;
  }

  // --- Mutations (local first, then send) ---

  setField(nodeId: string, field: string, value: unknown): void {
    if (!this.doc) throw new Error("Not connected");
    this.doc.setNodeState(nodeId, field, value);
  }

  createNode(
    type: string,
    state: Record<string, unknown>,
    parentId: string,
    slot: string,
    position: string = "append",
  ): string {
    if (!this.doc) throw new Error("Not connected");
    const node = this.doc.createNode(type, state);
    const parent = this.doc.getNode(parentId) ?? this.doc.root;
    this.doc.insertIntoSlot(parent, slot, position, [node]);
    return node.id;
  }

  deleteNode(nodeId: string): void {
    if (!this.doc) throw new Error("Not connected");
    this.doc.deleteRange(nodeId);
  }

  moveNode(
    nodeId: string,
    parentId: string,
    slot: string,
  ): void {
    if (!this.doc) throw new Error("Not connected");
    this.doc.moveRange(nodeId, undefined, parentId, slot);
  }

  /** Move a node so it sits immediately before or after sibling `targetId`. */
  moveNodeRelative(
    nodeId: string,
    targetId: string,
    position: "before" | "after",
  ): void {
    if (!this.doc) throw new Error("Not connected");
    this.doc.moveRangeRelative(nodeId, undefined, targetId, position);
  }

  undo(steps = 1): void {
    if (!this.undoMgr) return;
    for (let i = 0; i < steps; i++) {
      if (!this.undoMgr.canUndo) break;
      this.undoMgr.undo();
    }
  }

  redo(steps = 1): void {
    if (!this.undoMgr) return;
    for (let i = 0; i < steps; i++) {
      if (!this.undoMgr.canRedo) break;
      this.undoMgr.redo();
    }
  }

  // --- Events ---

  onConnected(cb: () => void): () => void {
    this.connectedCallbacks.add(cb);
    return () => this.connectedCallbacks.delete(cb);
  }

  onError(cb: (err: ErrorMsg) => void): () => void {
    this.errorCallbacks.add(cb);
    return () => this.errorCallbacks.delete(cb);
  }

  /**
   * Fires when the server replaces the local document with a fresh
   * snapshot after connecting — because it rejected one of this client's
   * operations (error code `rejected`) or on reconnect. The local doc, store,
   * and undo history are rebuilt from the snapshot. Operations still in
   * flight are dropped: the server either applied them (they are in the
   * snapshot) or rejected them. UI that caches DocNode references must
   * re-read them.
   */
  onResync(cb: () => void): () => void {
    this.resyncCallbacks.add(cb);
    return () => this.resyncCallbacks.delete(cb);
  }

  onPatch(cb: (version: number) => void): () => void {
    this.patchCallbacks.add(cb);
    return () => this.patchCallbacks.delete(cb);
  }

  onOffline(cb: () => void): () => void {
    this.offlineCallbacks.add(cb);
    return () => this.offlineCallbacks.delete(cb);
  }

  onOnline(cb: () => void): () => void {
    this.onlineCallbacks.add(cb);
    return () => this.onlineCallbacks.delete(cb);
  }

  // --- Internal ---

  private _handleMessage(msg: ServerMsg): void {
    switch (msg.type) {
      case "schema":
        this.rawSchema = msg.schema;
        this.schema = new SchemaRegistry(msg.schema);
        break;

      case "snapshot":
        if (msg.client_id) this.clientId = msg.client_id;
        this._initDoc(msg.data, msg.version);
        break;

      case "patch":
        this._handlePatch(msg);
        break;

      case "error":
        if (msg.ref) this._dropPending(msg.ref);
        for (const cb of this.errorCallbacks) cb(msg);
        break;
    }
  }

  /**
   * Forget pending operations up to and including `ref`; true if found.
   * Only a ref this client minted can match: refs carry the client's ID,
   * so another client's `ref` can never retire our pending work.
   */
  private _dropPending(ref: string): boolean {
    if (!ref.startsWith(this.clientId + ":")) return false;
    const idx = this.pendingOps.findIndex((p) => p.ref === ref);
    if (idx < 0) return false;
    this.pendingOps.splice(0, idx + 1);
    return true;
  }

  private _initDoc(snapshot: JsonDoc, version: number): void {
    if (!this.rawSchema) return;

    const isResync = this.doc !== null;

    // Clean up previous doc. Anything in flight was either acknowledged
    // (and is in the snapshot) or rejected (and is not): the server
    // answered every request it received before taking this snapshot.
    if (this.bridgeUnsub) this.bridgeUnsub();
    if (this.docUnsub) this.docUnsub();
    if (this.undoMgr) this.undoMgr.dispose();
    this.pendingOps = [];
    this.applyingRemote = false;

    this.version = version;
    this.doc = new LocalDoc(this.rawSchema, snapshot);
    this.undoMgr = new UndoManager(this.doc, this.maxUndoSteps, {
      mergeInterval: this.mergeInterval,
    });
    this.bridgeUnsub = bridgeDocToStore(this.doc, this.store);

    // Forward local changes to server (skip if we're applying a remote patch)
    this.docUnsub = this.doc.onChange((event) => {
      if (!this.applyingRemote) {
        this._sendOps(event.operations, event.inverseOperations, new Set(event.diff.deleted.keys()));
      }
    });

    // Edits made while offline were applied to the old local doc and are
    // not in the snapshot: replay them as fresh local transactions, which
    // sends them. One the server rejects comes back as a resync.
    const replay = this.bufferedOps;
    this.bufferedOps = [];
    for (const ops of replay) {
      this.doc.applyOperations(ops);
    }

    for (const cb of this.connectedCallbacks) cb();
    if (isResync) {
      for (const cb of this.resyncCallbacks) cb();
    }
    if (this.onlinePending) {
      this.onlinePending = false;
      for (const cb of this.onlineCallbacks) cb();
    }
  }

  private _handlePatch(msg: PatchMsg): void {
    this.version = msg.version;

    // A patch carrying one of our refs answers that request (and any
    // earlier one still pending: the server handles requests in order).
    // Only refs we minted match, so this is ownership enough;
    // `source_client` is informational (it is null whenever the server
    // recorded the request differently from how we sent it, which is
    // exactly the case reconciliation exists for).
    const answersOurs = typeof msg.ref === "string" && this._dropPending(msg.ref);

    if (answersOurs) {
      // Our own request, as the server recorded it. We applied it
      // optimistically; if another change landed on the server first,
      // the server's order is "theirs, then ours", and the patch
      // describes the result: each state field is set to the value the
      // server holds, and each node we inserted is moved to the
      // neighbors the server placed it between. That converges the
      // local document on the server's order. A later pending edit of
      // ours on the same field must not be overwritten by this older
      // value, so the echo is masked by the ops still pending (all later
      // than it) before it is replayed. Nothing here is sent back or
      // undoable.
      const pendingDeleted = new Set<string>();
      for (const entry of this.pendingOps) for (const id of entry.deleted) pendingDeleted.add(id);
      const reconcile = this._maskPending(
        echoReconcileOps(this.doc!, msg.operations, pendingDeleted),
        false,
      );
      if (this.doc && (reconcile.ordered.length > 0 || Object.keys(reconcile.state).length > 0)) {
        this.applyingRemote = true;
        try {
          this.doc.applyOperations(reconcile, { skipUndo: true });
        } finally {
          this.applyingRemote = false;
        }
      }
    } else {
      // Remote change — or our own op echoed after a resync dropped the
      // pending list (the snapshot predates it), which must be applied.
      // Apply to local doc (flag to prevent re-sending, skipUndo so
      // another user's edits never enter local undo history). Writes
      // that a pending local edit covers are masked: see _maskPending.
      if (this.doc) {
        const ops = this._maskPending(msg.operations);
        this.applyingRemote = true;
        try {
          this.doc.applyOperations(ops, { skipUndo: true });
        } finally {
          this.applyingRemote = false;
        }
      }
    }

    for (const cb of this.patchCallbacks) cb(msg.version);
  }

  /**
   * Drop from a remote patch what a pending local edit will overwrite.
   *
   * The server orders everything, and a remote patch that reaches us
   * before our own echo was committed before our pending op. So for a
   * field (or a moved node) both touched, the server's final state is
   * ours; the remote value is an intermediate the server itself passed
   * through. Applying it would show the wrong value until our echo
   * arrived. Masking keeps the local document at what the server will
   * hold. If the server rejects our op instead, the resync snapshot
   * brings the remote value in.
   *
   * A masked write still matters to undo: undoing our edit should leave
   * the field at what others last wrote, not at what we saw before
   * editing. With `refreshUndo`, the oldest pending edit of that field
   * gets its inverse refreshed to the masked value, and the undo
   * manager's entry for it along with it. An echo of our own older write
   * masked under a newer one is not "what others wrote": no refresh.
   */
  private _maskPending(remote: WireOperations, refreshUndo = true): WireOperations {
    if (this.pendingOps.length === 0) return remote;
    const pendingMoves = new Set<string>();
    for (const entry of this.pendingOps) {
      for (const op of entry.ops.ordered) {
        if (op[0] === 2) pendingMoves.add(op[1]);
      }
    }
    const ordered = remote.ordered.filter((op) => !(op[0] === 2 && pendingMoves.has(op[1])));
    const state: WireOperations["state"] = {};
    for (const [nodeId, patch] of Object.entries(remote.state)) {
      const kept: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(patch)) {
        const oldest = this.pendingOps.find((e) => nodeId in e.ops.state && key in e.ops.state[nodeId]);
        if (!oldest) {
          kept[key] = value;
          continue;
        }
        if (!refreshUndo) continue;
        if (!oldest.inverse.state[nodeId]) oldest.inverse.state[nodeId] = {};
        oldest.inverse.state[nodeId][key] = value;
        this.undoMgr?.refreshOriginal(oldest.inverse, nodeId, key, value);
      }
      if (Object.keys(kept).length > 0) state[nodeId] = kept;
    }
    return { ordered, state };
  }

  private _sendOps(ops: WireOperations, inverse: WireOperations, deleted: Set<string>): void {
    if (this.online && this.ws) {
      // Unique across clients: the server-assigned client ID plus a
      // counter. A ref is what ties a patch back to a pending op.
      const ref = `${this.clientId}:${this.nextRef++}`;
      this.pendingOps.push({ ref, ops, inverse, deleted });
      this._send({
        type: "op",
        ref,
        operations: ops,
      });
    } else {
      this.bufferedOps.push(ops);
    }
  }

  private _send(msg: ClientMsg): void {
    if (this.ws) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  /**
   * Handle a raw message object (for testing without WebSocket).
   * @internal
   */
  _injectMessage(msg: ServerMsg): void {
    this._handleMessage(msg);
  }
}
