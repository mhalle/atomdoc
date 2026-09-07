/**
 * Client-side transaction — buffers operations and sends as one batch.
 *
 * Protocol-agnostic: produces a single WireOperations that any
 * transport can send. Disposable if abandoned.
 */

import type { OpMsg, WireOperations } from "./types.js";

export class Transaction {
  private ordered: WireOperations["ordered"] = [];
  private state: WireOperations["state"] = {};
  private committed = false;
  private aborted = false;

  /** Set a field value on a node. */
  setField(nodeId: string, field: string, value: unknown): this {
    this._checkOpen();
    if (!this.state[nodeId]) this.state[nodeId] = {};
    this.state[nodeId][field] = value;
    return this;
  }

  /** Delete a node. */
  deleteNode(nodeId: string): this {
    this._checkOpen();
    this.ordered.push([1, nodeId, 0]);
    return this;
  }

  /** Move a node to a new parent/slot. */
  moveNode(
    nodeId: string,
    parentId: string,
    slot: string,
    prevId?: string,
    nextId?: string,
  ): this {
    this._checkOpen();
    this.ordered.push([2, nodeId, 0, parentId, slot, prevId ?? 0, nextId ?? 0]);
    return this;
  }

  /** Whether this transaction has pending operations. */
  get dirty(): boolean {
    return this.ordered.length > 0 || Object.keys(this.state).length > 0;
  }

  /** Whether this transaction is still open. */
  get open(): boolean {
    return !this.committed && !this.aborted;
  }

  /** Build the wire message. Call commit() on the client instead of using this directly. */
  toMessage(): OpMsg {
    return {
      type: "op",
      operations: {
        ordered: this.ordered,
        state: this.state,
      },
    };
  }

  /** Mark as committed (called by client.commit). */
  _commit(): void {
    this._checkOpen();
    this.committed = true;
  }

  /** Discard all buffered operations. */
  abort(): void {
    this._checkOpen();
    this.aborted = true;
    this.ordered = [];
    this.state = {};
  }

  private _checkOpen(): void {
    if (this.committed) throw new Error("Transaction already committed");
    if (this.aborted) throw new Error("Transaction already aborted");
  }
}
