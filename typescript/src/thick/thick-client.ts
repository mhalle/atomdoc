/**
 * ThickAtomDocClient — local document model + WebSocket sync.
 *
 * Field writes apply locally first and are sent; the server's echo
 * confirms them. Structural edits (create, delete, move) are built from
 * the local tree, sent, and applied when the server echoes them, so the
 * local tree only ever holds the server's order: nothing is reconciled
 * after the fact. The client is a replica of one server document, not
 * a peer in a collaborative session.
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
import type { DocNode } from "./doc-node.js";
import { LocalDoc } from "./local-doc.js";
import { bridgeDocToStore, type StoreBridge } from "./store-bridge.js";
import { UndoManager } from "./undo-manager.js";

export interface ThickClientOptions {
  url: string;
  /** Undo steps to keep; 0 disables undo. Default 100. */
  maxUndoSteps?: number;
  /** Merge consecutive local transactions within this many ms into one undo step. Default 0. */
  mergeInterval?: number;
  /**
   * Update the store once per animation frame (default) rather than
   * after every document change. The document itself is always current;
   * `flushStore()` brings the store up to date on demand.
   */
  coalesce?: boolean;
}

/**
 * An operation sent to the server and not yet answered (echoed or
 * rejected), matched by `ref`.
 */
interface PendingOp {
  ref: string;
  ops: WireOperations;
  /**
   * Already applied to the local document (a field write, or an edit
   * made on the LocalDoc directly): the echo only confirms it. Otherwise
   * the op was built from the tree and waits for its echo to apply.
   */
  applied: boolean;
  /**
   * For an applied op, the event's inverse (the object the undo manager
   * holds too), refreshed when a remote write underneath is masked.
   */
  inverse?: WireOperations;
  /** An undo or redo step sent for confirmation: its echo commits as that step. */
  history?: "undo" | "redo";
  /** What a built op does to the tree, for edits made before its echo. */
  structure?: PendingStructure;
}

/** The structural intent of one built op. */
interface PendingStructure {
  inserted: Array<{ id: string; type: string; key?: string }>;
  deleted: string[];
  moved: string[];
  /** Nodes appended to a slot, in order. */
  tail?: { key: string; ids: string[] };
  /** Nodes prepended to a slot, in order (first is the slot's first). */
  head?: { key: string; ids: string[] };
}

/**
 * What the pending built ops, together, will have done to the tree once
 * the server confirms them. A new structural edit is anchored against
 * this rather than against the local tree alone, so that two appends in
 * a row keep their order and a child can be created under a parent that
 * is itself still pending.
 */
class PendingModel {
  inserted = new Map<string, { type: string; key?: string }>();
  deleted = new Set<string>();
  moved = new Set<string>();
  private chains = new Map<string, { head: string[]; tail: string[] }>();

  constructor(entries: Iterable<PendingStructure | undefined>) {
    for (const s of entries) {
      if (!s) continue;
      for (const { id, type, key } of s.inserted) {
        this.inserted.set(id, { type, key });
        this.deleted.delete(id);
      }
      for (const id of s.deleted) {
        this.deleted.add(id);
        this.inserted.delete(id);
        this._unchain(id);
      }
      for (const id of s.moved) {
        this.moved.add(id);
        this._unchain(id);
      }
      if (s.tail) this._chain(s.tail.key).tail.push(...s.tail.ids);
      if (s.head) this._chain(s.head.key).head.unshift(...s.head.ids);
    }
  }

  private _chain(key: string): { head: string[]; tail: string[] } {
    let c = this.chains.get(key);
    if (!c) {
      c = { head: [], tail: [] };
      this.chains.set(key, c);
    }
    return c;
  }

  private _unchain(id: string): void {
    for (const c of this.chains.values()) {
      for (const list of [c.head, c.tail]) {
        const i = list.indexOf(id);
        if (i >= 0) list.splice(i, 1);
      }
    }
  }

  /** True if `id` is gone or about to leave its place in the tree. */
  private _unstable(id: string): boolean {
    return this.deleted.has(id) || this.moved.has(id);
  }

  /** The node a new last child of the slot should follow. */
  lastOf(parent: DocNode | undefined, slot: string, key: string): string | 0 {
    const c = this.chains.get(key);
    const tail = c?.tail[c.tail.length - 1];
    if (tail) return tail;
    for (let n = parent?.slotLast.get(slot) ?? null; n; n = n.prevSibling) {
      if (!this._unstable(n.id)) return n.id;
    }
    const head = c?.head[c.head.length - 1];
    return head ?? 0;
  }

  /** The node a new first child of the slot should precede. */
  firstOf(parent: DocNode | undefined, slot: string, key: string): string | 0 {
    const c = this.chains.get(key);
    if (c?.head[0]) return c.head[0];
    for (let n = parent?.slotFirst.get(slot) ?? null; n; n = n.nextSibling) {
      if (!this._unstable(n.id)) return n.id;
    }
    return c?.tail[0] ?? 0;
  }

  /** The nearest stable sibling before `node`, if any. */
  prevOf(node: DocNode): string | 0 {
    for (let n = node.prevSibling; n; n = n.prevSibling) {
      if (!this._unstable(n.id)) return n.id;
    }
    return 0;
  }

  /** The nearest stable sibling after `node`, if any. */
  nextOf(node: DocNode): string | 0 {
    for (let n = node.nextSibling; n; n = n.nextSibling) {
      if (!this._unstable(n.id)) return n.id;
    }
    return 0;
  }
}

/** Ids of the nodes an op inserts, deletes, and moves. */
function structureOf(ops: WireOperations): PendingStructure | undefined {
  const s: PendingStructure = { inserted: [], deleted: [], moved: [] };
  for (const op of ops.ordered) {
    if (op[0] === 0) {
      for (const [id, type] of op[1]) s.inserted.push({ id, type });
    } else if (op[0] === 1) {
      s.deleted.push(op[1]);
    } else {
      s.moved.push(op[1]);
    }
  }
  return s.inserted.length + s.deleted.length + s.moved.length > 0 ? s : undefined;
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
  private coalesce: boolean;
  private clientId: string = crypto.randomUUID();

  private bridge: StoreBridge | null = null;
  private docUnsub: (() => void) | null = null;
  private online = false;
  private pendingOps: PendingOp[] = [];
  /**
   * Waiting for the connection: sent, in order, once the (re)connect
   * snapshot is in. Every one is applied when its echo arrives.
   */
  private bufferedOps: Array<{ ops: WireOperations; structure?: PendingStructure }> = [];
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
    this.coalesce = options.coalesce ?? true;
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
    // reconnect sends them in order. Whether or not they were applied
    // to this document, the reconnect snapshot replaces it and their
    // echoes apply them afresh.
    if (this.pendingOps.length > 0) {
      this.bufferedOps = [
        ...this.pendingOps.map((p) => ({ ops: p.ops, structure: p.structure })),
        ...this.bufferedOps,
      ];
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

  /**
   * Apply to the store every document change still queued for the next
   * frame. The store is otherwise updated once per frame; read it after
   * this to see an edit made a moment ago.
   */
  flushStore(): void {
    this.bridge?.flush();
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

  /**
   * True while a structural edit (create, delete, move, or an undo or
   * redo step containing one) is waiting for the server's confirmation.
   * The local tree shows it once the echo arrives; `onPatch` fires then.
   */
  pendingStructure(): boolean {
    return (
      this.pendingOps.some((p) => !p.applied && p.ops.ordered.length > 0) ||
      this.bufferedOps.some((b) => b.ops.ordered.length > 0)
    );
  }

  // --- Mutations ---

  /**
   * Write a field. Applied locally at once and sent; the echo confirms
   * it. A node that is still pending creation accepts writes too: they
   * are sent and show up with the node.
   */
  setField(nodeId: string, field: string, value: unknown): void {
    const doc = this._doc();
    if (doc.getNode(nodeId)) {
      doc.setNodeState(nodeId, field, value);
      return;
    }
    const pending = this._pendingModel().inserted.get(nodeId);
    if (!pending) throw new Error(`Node not found: ${nodeId}`);
    this._checkField(pending.type, field);
    this._sendBuilt({ ordered: [], state: { [nodeId]: { [field]: value } } });
  }

  /**
   * Create a node in `slot` of `parentId` (`"append"` or `"prepend"`).
   * Returns the new node's ID at once; the node itself appears in the
   * local tree when the server confirms the insert. The parent may be a
   * node that is itself still pending.
   */
  createNode(
    type: string,
    state: Record<string, unknown>,
    parentId: string,
    slot: string,
    position: string = "append",
  ): string {
    const doc = this._doc();
    const node = doc.createNode(type, state); // validates type and fields
    const model = this._pendingModel();
    const { parent, parentRef, key } = this._parentFor(model, parentId, slot);
    let prev: string | 0 = 0;
    let next: string | 0 = 0;
    const structure: PendingStructure = {
      inserted: [{ id: node.id, type, key }],
      deleted: [],
      moved: [],
    };
    if (position === "append") {
      prev = model.lastOf(parent, slot, key);
      structure.tail = { key, ids: [node.id] };
    } else if (position === "prepend") {
      next = model.firstOf(parent, slot, key);
      structure.head = { key, ids: [node.id] };
    } else {
      throw new Error(`Unsupported position: ${position}`);
    }
    const ops: WireOperations = {
      ordered: [[0, [[node.id, type]], parentRef, slot, prev, next]],
      state: Object.keys(node.state).length > 0 ? { [node.id]: { ...node.state } } : {},
    };
    this._sendBuilt(ops, structure);
    return node.id;
  }

  /** Delete a node (and its subtree) once the server confirms. */
  deleteNode(nodeId: string): void {
    const doc = this._doc();
    const model = this._pendingModel();
    const node = doc.getNode(nodeId);
    if (node === doc.root) throw new Error("Root node cannot be deleted");
    if ((!node && !model.inserted.has(nodeId)) || model.deleted.has(nodeId)) {
      throw new Error(`Node not found: ${nodeId}`);
    }
    this._sendBuilt(
      { ordered: [[1, nodeId, 0]], state: {} },
      { inserted: [], deleted: [nodeId], moved: [] },
    );
  }

  /** Move a node to the end of `slot` on `parentId` (`""` or `"0"` = root). */
  moveNode(nodeId: string, parentId: string, slot: string): void {
    const doc = this._doc();
    const model = this._pendingModel();
    const node = this._movable(doc, model, nodeId);
    const { parent, parentRef, key } = this._parentFor(model, parentId, slot);
    if (node && parent) {
      for (let anc: DocNode | null = parent; anc; anc = anc.parent) {
        if (anc === node) throw new Error("Target is in the range");
      }
    }
    const prev = model.lastOf(parent, slot, key);
    if (prev === nodeId) return; // already there
    this._sendBuilt(
      { ordered: [[2, nodeId, 0, parentRef, slot, prev, 0]], state: {} },
      { inserted: [], deleted: [], moved: [nodeId], tail: { key, ids: [nodeId] } },
    );
  }

  /** Move a node so it sits immediately before or after sibling `targetId`. */
  moveNodeRelative(
    nodeId: string,
    targetId: string,
    position: "before" | "after",
  ): void {
    const doc = this._doc();
    const model = this._pendingModel();
    const node = this._movable(doc, model, nodeId);
    if (targetId === nodeId) throw new Error("Target is in the range");
    const target = doc.getNode(targetId);
    let parentRef: string | 0;
    let slot: string;
    let prev: string | 0;
    let next: string | 0;
    if (target) {
      if (model.deleted.has(targetId)) throw new Error(`Node not found: ${targetId}`);
      if (!target.parent || !target.slotName) {
        throw new Error("Cannot move before or after the root");
      }
      if (node) {
        for (let anc: DocNode | null = target.parent; anc; anc = anc.parent) {
          if (anc === node) throw new Error("Target is descendant of the range");
        }
      }
      parentRef = target.parent === doc.root ? 0 : target.parent.id;
      slot = target.slotName;
      if (position === "before") {
        if (model.prevOf(target) === nodeId) return;
        prev = model.prevOf(target);
        next = targetId;
      } else {
        if (model.nextOf(target) === nodeId) return;
        prev = targetId;
        next = model.nextOf(target);
      }
    } else {
      // A pending sibling: anchor on it alone; the server resolves it.
      const pending = model.inserted.get(targetId);
      if (!pending?.key) throw new Error(`Node not found: ${targetId}`);
      const sep = pending.key.indexOf(" ");
      const parentId = pending.key.slice(0, sep);
      parentRef = parentId === doc.root.id ? 0 : parentId;
      slot = pending.key.slice(sep + 1);
      prev = position === "before" ? 0 : targetId;
      next = position === "before" ? targetId : 0;
    }
    this._sendBuilt(
      { ordered: [[2, nodeId, 0, parentRef, slot, prev, next]], state: {} },
      { inserted: [], deleted: [], moved: [nodeId] },
    );
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

  private _doc(): LocalDoc {
    if (!this.doc) throw new Error("Not connected");
    return this.doc;
  }

  private _checkField(type: string, field: string): void {
    const def = this.rawSchema?.node_types[type];
    if (!def) throw new Error(`Unknown node type: ${type}`);
    if (!(field in def.field_tiers) && !(field in (def.refs ?? {}))) {
      throw new Error(`Unknown field '${field}' on ${type}`);
    }
  }

  private _pendingModel(): PendingModel {
    return new PendingModel([
      ...this.pendingOps.map((p) => (p.applied ? undefined : p.structure)),
      ...this.bufferedOps.map((b) => b.structure),
    ]);
  }

  /** Resolve the parent of a structural edit: a live node, or a pending one. */
  private _parentFor(
    model: PendingModel,
    parentId: string,
    slot: string,
  ): { parent: DocNode | undefined; parentRef: string | 0; key: string } {
    const doc = this._doc();
    const isRoot = parentId === "" || parentId === "0" || parentId === doc.root.id;
    const parent = isRoot ? doc.root : doc.getNode(parentId);
    let parentType: string;
    if (parent) {
      if (model.deleted.has(parent.id)) throw new Error(`Node not found: ${parentId}`);
      parentType = parent.type;
    } else {
      const pending = model.inserted.get(parentId);
      if (!pending) throw new Error(`Parent not found: ${parentId}`);
      parentType = pending.type;
    }
    if (!(slot in (this.rawSchema?.node_types[parentType]?.slots ?? {}))) {
      throw new Error(`Slot '${slot}' does not exist on ${parentType}`);
    }
    const id = parent ? parent.id : parentId;
    return { parent, parentRef: parent === doc.root ? 0 : id, key: `${id} ${slot}` };
  }

  /** The node a move may move: live and not pending deletion, or pending creation. */
  private _movable(doc: LocalDoc, model: PendingModel, nodeId: string): DocNode | undefined {
    const node = doc.getNode(nodeId);
    if (node === doc.root) throw new Error("Cannot move the root");
    if ((!node && !model.inserted.has(nodeId)) || model.deleted.has(nodeId)) {
      throw new Error(`Node not found: ${nodeId}`);
    }
    return node;
  }

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
        if (msg.ref) this._takePending(msg.ref);
        for (const cb of this.errorCallbacks) cb(msg);
        break;
    }
  }

  /**
   * Retire the pending op `ref` names, and every pending op before it
   * (the server answers in order); returns it, or null if none. Only a
   * ref this client minted can match: refs carry the client's ID, so
   * another client's `ref` can never retire our pending work.
   */
  private _takePending(ref: string): PendingOp | null {
    if (!ref.startsWith(this.clientId + ":")) return null;
    const idx = this.pendingOps.findIndex((p) => p.ref === ref);
    if (idx < 0) return null;
    const [entry] = this.pendingOps.splice(0, idx + 1).slice(-1);
    return entry;
  }

  private _initDoc(snapshot: JsonDoc, version: number): void {
    if (!this.rawSchema) return;

    const isResync = this.doc !== null;

    // Clean up previous doc. Anything in flight was either acknowledged
    // (and is in the snapshot) or rejected (and is not): the server
    // answered every request it received before taking this snapshot.
    if (this.bridge) this.bridge.dispose();
    if (this.docUnsub) this.docUnsub();
    if (this.undoMgr) this.undoMgr.dispose();
    this.pendingOps = [];
    this.applyingRemote = false;

    this.version = version;
    this.doc = new LocalDoc(this.rawSchema, snapshot);
    this.undoMgr = new UndoManager(this.doc, this.maxUndoSteps, {
      mergeInterval: this.mergeInterval,
      dispatch: (ops, kind) => this._dispatchHistory(ops, kind),
    });
    this.bridge = bridgeDocToStore(this.doc, this.store, { coalesce: this.coalesce });

    // Forward local changes to server (skip if we're applying a remote patch)
    this.docUnsub = this.doc.onChange((event) => {
      if (!this.applyingRemote) {
        this._sendApplied(event.operations, event.inverseOperations);
      }
    });

    // Edits made while offline are not in the snapshot: send them now,
    // in order, and let their echoes apply them. One the server rejects
    // comes back as a resync.
    const cameBackOnline = this.onlinePending;
    this.onlinePending = false;
    const replay = this.bufferedOps;
    this.bufferedOps = [];
    for (const { ops, structure } of replay) {
      this._sendBuilt(ops, structure);
    }

    for (const cb of this.connectedCallbacks) cb();
    if (isResync) {
      for (const cb of this.resyncCallbacks) cb();
    }
    if (cameBackOnline) {
      for (const cb of this.onlineCallbacks) cb();
    }
  }

  /**
   * An undo or redo step. One that only writes fields applies locally
   * like any edit. One that touches structure is sent like any
   * structural edit and commits, as that step, when its echo arrives.
   */
  private _dispatchHistory(ops: WireOperations, kind: "undo" | "redo"): boolean {
    if (ops.ordered.length === 0) return false;
    this._sendBuilt(ops, structureOf(ops), kind);
    return true;
  }

  private _handlePatch(msg: PatchMsg): void {
    this.version = msg.version;

    // A patch carrying one of our refs answers that request (and any
    // earlier one still pending: the server handles requests in order).
    // Only refs we minted match, so this is ownership enough;
    // `source_client` is informational.
    const entry = typeof msg.ref === "string" ? this._takePending(msg.ref) : null;

    if (entry && this.doc) {
      if (entry.applied) {
        // Confirmation of an edit already in the local document. Its
        // own structure is in place; a node the server added alongside
        // (a normalizer's) is not, and is inserted. Each field is set to
        // the value the server holds (a normalizer may have changed it),
        // unless a later pending write of ours covers it.
        const ordered = this._missingInserts(msg.operations.ordered);
        const state = this._maskState(msg.operations.state, false);
        if (ordered.length > 0 || Object.keys(state).length > 0) {
          this._applyRemote({ ordered, state }, { skipUndo: true });
        }
      } else {
        // A built edit, as the server recorded it: apply it now. It is
        // this client's own work, so it enters undo history (as the undo
        // or redo step it was sent as, if it was one), but it is not
        // sent again.
        // Only a write the local document already holds can mask one of
        // its fields: a later pending write to a node that is itself
        // still pending has nothing local to protect.
        const ops: WireOperations = {
          ordered: msg.operations.ordered,
          state: this._maskState(msg.operations.state, false, true),
        };
        const commit = () => this._applyRemote(ops, {});
        if (entry.history && this.undoMgr) {
          this.undoMgr.commitAs(entry.history, commit);
        } else {
          this.doc.forceCommit();
          commit();
        }
      }
    } else if (this.doc) {
      // Another client's change — or our own op echoed after a resync
      // dropped the pending list (the snapshot predates it). Applied
      // outside undo history: it is not this user's work. Field writes
      // that a pending local edit covers are masked: see _maskState.
      this._applyRemote(
        { ordered: msg.operations.ordered, state: this._maskState(msg.operations.state) },
        { skipUndo: true },
      );
    }

    for (const cb of this.patchCallbacks) cb(msg.version);
  }

  /**
   * The inserts among `ordered` of nodes the local document does not
   * have, each anchored after the node before it in the echo, so a node
   * the server created alongside an edit of ours lands where it put it.
   * Moves and deletes are kept: replaying them against a document that
   * already applied them changes nothing.
   */
  private _missingInserts(ordered: OrderedOp[]): OrderedOp[] {
    const doc = this.doc!;
    const out: OrderedOp[] = [];
    for (const op of ordered) {
      if (op[0] !== 0) {
        out.push(op);
        continue;
      }
      const [, pairs, parentRef, slot, prevRef, nextRef] = op;
      let prev: string | 0 = prevRef;
      pairs.forEach(([id, type], i) => {
        if (!doc.getNode(id)) {
          out.push([0, [[id, type]], parentRef, slot, prev, i === pairs.length - 1 ? nextRef : 0]);
        }
        prev = id;
      });
    }
    return out;
  }

  private _applyRemote(ops: WireOperations, flags: { skipUndo?: boolean }): void {
    this.applyingRemote = true;
    try {
      this.doc!.applyOperations(ops, flags);
    } finally {
      this.applyingRemote = false;
    }
  }

  /**
   * Drop from a patch the field writes a pending local edit will
   * overwrite.
   *
   * The server orders everything, and a remote patch that reaches us
   * before our own echo was committed before our pending op. So for a
   * field both touched, the server's final value is ours; the remote
   * value is an intermediate the server itself passed through. Applying
   * it would show the wrong value until our echo arrived. Masking keeps
   * the local document at what the server will hold. If the server
   * rejects our op instead, the resync snapshot brings the remote value
   * in.
   *
   * A masked write still matters to undo: undoing our edit should leave
   * the field at what others last wrote, not at what we saw before
   * editing. With `refreshUndo`, the oldest pending edit of that field
   * gets its inverse refreshed to the masked value, and the undo
   * manager's entry for it along with it. An echo of our own older write
   * masked under a newer one is not "what others wrote": no refresh.
   */
  private _maskState(
    remote: WireOperations["state"],
    refreshUndo = true,
    appliedOnly = false,
  ): WireOperations["state"] {
    if (this.pendingOps.length === 0) return remote;
    const state: WireOperations["state"] = {};
    for (const [nodeId, patch] of Object.entries(remote)) {
      const kept: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(patch)) {
        const oldest = this.pendingOps.find(
          (e) => (e.applied || !appliedOnly) && nodeId in e.ops.state && key in e.ops.state[nodeId],
        );
        if (!oldest) {
          kept[key] = value;
          continue;
        }
        if (!refreshUndo || !oldest.inverse) continue;
        if (!oldest.inverse.state[nodeId]) oldest.inverse.state[nodeId] = {};
        oldest.inverse.state[nodeId][key] = value;
        this.undoMgr?.refreshOriginal(oldest.inverse, nodeId, key, value);
      }
      if (Object.keys(kept).length > 0) state[nodeId] = kept;
    }
    return state;
  }

  /** Send an edit the local document already applied. */
  private _sendApplied(ops: WireOperations, inverse: WireOperations): void {
    this._queue({ ops, inverse, applied: true });
  }

  /** Send an edit built from the tree; its echo applies it. */
  private _sendBuilt(
    ops: WireOperations,
    structure?: PendingStructure,
    history?: "undo" | "redo",
  ): void {
    this._queue({ ops, applied: false, structure, history });
  }

  private _queue(entry: Omit<PendingOp, "ref">): void {
    if (this.online && this.ws && !this.onlinePending) {
      // Unique across clients: the server-assigned client ID plus a
      // counter. A ref is what ties a patch back to a pending op.
      const ref = `${this.clientId}:${this.nextRef++}`;
      this.pendingOps.push({ ref, ...entry });
      this._send({ type: "op", ref, operations: entry.ops });
    } else {
      // Until the connection (and its snapshot) is in, every edit waits
      // its turn: it is sent from the snapshot, whether or not this
      // document applied it, and its echo applies it to the new one.
      this.bufferedOps.push({ ops: entry.ops, structure: entry.structure });
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
