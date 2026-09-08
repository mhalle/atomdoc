/**
 * AtomDoc WebSocket client.
 */

import { applyPatch } from "./patch.js";
import { SchemaRegistry } from "./schema.js";
import { NodeStore } from "./store.js";
import { Transaction } from "./transaction.js";
import type {
  ClientMsg,
  ErrorMsg,
  ServerMsg,
} from "./types.js";

export class AtomDocClient {
  private ws: WebSocket | null = null;
  private store = new NodeStore();
  private schema: SchemaRegistry | null = null;
  private version = 0;
  private url: string;

  private connectedCallbacks = new Set<() => void>();
  private errorCallbacks = new Set<(err: ErrorMsg) => void>();
  private patchCallbacks = new Set<(version: number) => void>();

  private webSocket: new (url: string) => WebSocket;

  /**
   * @param options.webSocket The WebSocket constructor to use; defaults
   *   to the global `WebSocket` (browsers, Node 22 and later).
   */
  constructor(url: string, options: { webSocket?: new (url: string) => WebSocket } = {}) {
    this.url = url;
    this.webSocket = options.webSocket ?? WebSocket;
  }

  // --- Lifecycle ---

  connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      const ws = new this.webSocket(this.url);
      this.ws = ws;

      ws.onopen = () => {
        resolve();
      };

      ws.onerror = (event) => {
        reject(event);
      };

      ws.onmessage = (event) => {
        const msg = JSON.parse(
          typeof event.data === "string" ? event.data : event.data.toString(),
        ) as ServerMsg;
        this._handleMessage(msg);
      };

      ws.onclose = () => {
        this.ws = null;
      };
    });
  }

  disconnect(): void {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  // --- State access ---

  getStore(): NodeStore {
    return this.store;
  }

  getSchema(): SchemaRegistry | null {
    return this.schema;
  }

  getVersion(): number {
    return this.version;
  }

  /**
   * A node's state with the schema defaults filled in (a snapshot and
   * a patch omit fields at their default), or undefined if the node is
   * absent or a stub (held by identity only; see `StoreNode.stub`).
   */
  getState(nodeId: string): Record<string, unknown> | undefined {
    const node = this.store.getNode(nodeId);
    if (!node || node.stub) return undefined;
    return { ...(this.schema?.getDefaults(node.type) ?? {}), ...node.state };
  }

  // --- Send operations ---

  send(msg: ClientMsg): void {
    if (!this.ws) throw new Error("Not connected");
    this.ws.send(JSON.stringify(msg));
  }

  setField(nodeId: string, field: string, value: unknown): void {
    this.send({
      type: "op",
      operations: {
        ordered: [],
        state: { [nodeId]: { [field]: value } },
      },
    });
  }

  createNode(
    nodeType: string,
    state: Record<string, unknown>,
    parentId: string,
    slot: string,
    position: string = "append",
  ): void {
    this.send({
      type: "create",
      node_type: nodeType,
      state,
      parent_id: parentId,
      slot,
      position,
    });
  }

  deleteNode(nodeId: string): void {
    this.send({
      type: "op",
      operations: {
        ordered: [[1, nodeId, 0]],
        state: {},
      },
    });
  }

  /**
   * Move a node into `slot` of `parentId` (`"0"` or `""` for the root):
   * after `prevId` if given, else before `nextId` if given, else at the
   * end.
   */
  moveNode(nodeId: string, parentId: string, slot: string, prevId?: string, nextId?: string): void {
    this.send({
      type: "op",
      operations: {
        ordered: [[2, nodeId, 0, parentId === "" || parentId === "0" ? 0 : parentId, slot, prevId ?? 0, nextId ?? 0]],
        state: {},
      },
    });
  }

  /** Start a client-side transaction. Buffer operations, then commit or abort. */
  begin(): Transaction {
    return new Transaction();
  }

  /** Commit a transaction — sends all buffered operations as one batch. */
  commit(tx: Transaction): void {
    if (!tx.dirty) {
      tx._commit();
      return;
    }
    tx._commit();
    this.send(tx.toMessage());
  }

  undo(steps: number = 1): void {
    this.send(steps === 1 ? { type: "undo" } : { type: "undo", steps });
  }

  redo(steps: number = 1): void {
    this.send(steps === 1 ? { type: "redo" } : { type: "redo", steps });
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

  onPatch(cb: (version: number) => void): () => void {
    this.patchCallbacks.add(cb);
    return () => this.patchCallbacks.delete(cb);
  }

  // --- Internal ---

  private _handleMessage(msg: ServerMsg): void {
    switch (msg.type) {
      case "schema":
        this.schema = new SchemaRegistry(msg.schema);
        break;

      case "snapshot":
        this.version = msg.version;
        this.store.loadSnapshot(msg.data, msg.stubs);
        for (const cb of this.connectedCallbacks) cb();
        break;

      case "patch":
        this.version = msg.version;
        applyPatch(this.store, msg.operations);
        for (const cb of this.patchCallbacks) cb(msg.version);
        break;

      case "error":
        for (const cb of this.errorCallbacks) cb(msg);
        break;
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
