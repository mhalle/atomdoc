/**
 * LocalDoc — client-side document model, port of _doc.py (simplified).
 *
 * No Pydantic validation, no normalize hooks, no strict mode.
 * Provides: tree structure, operations, inverse tracking, transactions.
 */

import type {
  AtomDocSchema,
  HandleDef,
  JsonDoc,
  RefDef,
  WireOperations,
} from "../types.js";
import { createDocNode, resetDocNode, type DocNode } from "./doc-node.js";
import {
  createOpsAccumulator,
  createDiff,
  onSetStateInverse,
  onSetStateForward,
  onInsertRange,
  onInsertRangeBefore,
  onDeleteRange,
  onMoveRange,
  type OpsAccumulator,
  type Diff,
} from "./local-ops.js";
import { iterRange, detachRange, descendantsInclusive } from "./local-range.js";
import {
  withTransaction,
  type LifecycleStage,
  type TransactionFlags,
} from "./local-transaction.js";
import { createNodeIdFactory } from "./node-id.js";

export interface ChangeEvent {
  operations: WireOperations;
  inverseOperations: WireOperations;
  diff: Diff;
  /** Flags of the committed transaction (e.g. `skipUndo`). */
  flags: TransactionFlags;
}

function opsToWire(acc: OpsAccumulator): WireOperations {
  return { ordered: acc.ordered, state: acc.state };
}

/**
 * A reference does not resolve, has the wrong target type, or a referenced
 * node would be deleted. Thrown at commit; the transaction is rolled back.
 */
export class RefIntegrityError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RefIntegrityError";
  }
}

/**
 * One or more change listeners threw after a transaction committed.
 *
 * Change listeners are observers: they run once the commit is final, so
 * a failing listener cannot undo what other listeners (the store bridge,
 * the network) have already seen. The document keeps the change; this
 * error reports the failures to the caller afterwards. `errors` holds
 * every value thrown, in listener order; the first is also `cause`.
 */
export class ListenerError extends Error {
  readonly errors: unknown[];
  constructor(errors: unknown[]) {
    const first = errors[0];
    const text = first instanceof Error ? `${first.name}: ${first.message}` : String(first);
    const more = errors.length > 1 ? ` (+${errors.length - 1} more)` : "";
    super(`${errors.length} change listener(s) failed after commit: ${text}${more}`, {
      cause: first,
    });
    this.name = "ListenerError";
    this.errors = errors;
  }
}

/** IDs held by a reference field value (a string or an array of strings). */
function refIds(value: unknown): string[] {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.filter((v): v is string => typeof v === "string");
  return [];
}

const REF_SEP = "\u0000";

function copyState(
  state: Record<string, Record<string, unknown>>,
): Record<string, Record<string, unknown>> {
  return Object.fromEntries(
    Object.entries(state).map(([id, patch]) => [id, { ...patch }]),
  );
}

/** Whether `field` is declared as a mapping (`dict[str, T]`) by the schema. */
function isMapField(jsonSchema: Record<string, unknown> | undefined, field: string): boolean {
  const props = jsonSchema?.properties as Record<string, Record<string, unknown>> | undefined;
  const prop = props?.[field];
  if (!prop) return false;
  const candidates = [prop, ...((prop.anyOf as Record<string, unknown>[] | undefined) ?? [])];
  return candidates.some(
    (c) => c.type === "object" && !c.properties && typeof c.additionalProperties === "object",
  );
}

export class LocalDoc {
  readonly id: string;
  readonly root: DocNode;
  readonly nodeMap: Map<string, DocNode>;

  _lifecycleStage: LifecycleStage = "idle";
  _forwardOps: OpsAccumulator = createOpsAccumulator();
  _inverseOps: OpsAccumulator = createOpsAccumulator();
  _diff: Diff = createDiff();
  _transactionFlags: TransactionFlags = {};

  private schema: AtomDocSchema;
  private idGen: () => string;
  private changeListeners: Array<(e: ChangeEvent) => void> = [];
  /**
   * Nodes removed from the document, by ID, for as long as someone still
   * holds them. A rollback or undo that re-inserts the same ID revives
   * the object, so a handle survives the round trip.
   */
  private graveyard = new Map<string, WeakRef<DocNode>>();
  /**
   * Reverse reference index: target id → set of `${referrerId}\0${field}`.
   * Derived state — never serialized, rebuilt from a snapshot.
   */
  private refIndex = new Map<string, Set<string>>();

  constructor(schema: AtomDocSchema, snapshot: JsonDoc) {
    this.schema = schema;
    this.id = snapshot[0];
    this.nodeMap = new Map();
    this.idGen = createNodeIdFactory(this.id);

    // Build root
    const rootType = snapshot[1];
    this.root = this._createNodeFromJson(snapshot);
    this.nodeMap.set(this.root.id, this.root);

    // Recursively load children
    if (snapshot[3]) {
      this._loadSlots(this.root, snapshot[3]);
    }
    this._rebuildRefIndex();
  }

  // --- Read ---

  getNode(id: string): DocNode | undefined {
    return this.nodeMap.get(id);
  }

  // --- References ---

  /** Nodes holding a reference to `nodeId`, optionally only via `field`. */
  referrers(nodeId: string, field?: string): DocNode[] {
    const result: DocNode[] = [];
    const seen = new Set<string>();
    for (const entry of this.refIndex.get(nodeId) ?? []) {
      const [referrerId, fieldName] = entry.split(REF_SEP);
      if (field !== undefined && fieldName !== field) continue;
      if (seen.has(referrerId)) continue;
      const referrer = this.nodeMap.get(referrerId);
      if (referrer) {
        seen.add(referrerId);
        result.push(referrer);
      }
    }
    return result;
  }

  // --- Handles ---

  /**
   * Every handle held by the document. With `strength: "strong"` this is
   * the document's hard dependency list — what must resolve for it to be
   * usable. Nothing is fetched; values are read off the tree.
   */
  handles(strength?: "weak" | "strong"): Array<{
    node: DocNode;
    field: string;
    handle: Record<string, unknown>;
    strength: "weak" | "strong";
  }> {
    const result: Array<{
      node: DocNode;
      field: string;
      handle: Record<string, unknown>;
      strength: "weak" | "strong";
    }> = [];
    for (const node of this.nodeMap.values()) {
      const defs: Record<string, HandleDef> = this.schema.node_types[node.type]?.handles ?? {};
      for (const [field, def] of Object.entries(defs)) {
        if (strength !== undefined && def.strength !== strength) continue;
        const walk = (value: unknown): void => {
          if (Array.isArray(value)) {
            for (const item of value) walk(item);
          } else if (value && typeof value === "object") {
            result.push({
              node,
              field,
              handle: value as Record<string, unknown>,
              strength: def.strength,
            });
          }
        };
        const value = node.state[field];
        // A handle field holds one handle, a list of them, or a map of
        // them; the values of a map are handles, the map itself is not.
        const tier = this.schema.node_types[node.type]?.json_schema;
        const isMap = isMapField(tier, field);
        if (isMap && value && typeof value === "object" && !Array.isArray(value)) {
          for (const item of Object.values(value as Record<string, unknown>)) walk(item);
        } else {
          walk(value);
        }
      }
    }
    return result;
  }

  private _refDefsFor(type: string): Record<string, RefDef> {
    return this.schema.node_types[type]?.refs ?? {};
  }

  private _refsAdd(node: DocNode): void {
    for (const field of Object.keys(this._refDefsFor(node.type))) {
      for (const targetId of refIds(node.state[field])) {
        this._refIndexAdd(targetId, node.id, field);
      }
    }
  }

  private _refsRemove(node: DocNode): void {
    for (const field of Object.keys(this._refDefsFor(node.type))) {
      for (const targetId of refIds(node.state[field])) {
        this._refIndexDiscard(targetId, node.id, field);
      }
    }
  }

  private _refsUpdate(node: DocNode, field: string, oldValue: unknown, newValue: unknown): void {
    const oldIds = refIds(oldValue);
    const newIds = refIds(newValue);
    for (const id of oldIds) {
      if (!newIds.includes(id)) this._refIndexDiscard(id, node.id, field);
    }
    for (const id of newIds) this._refIndexAdd(id, node.id, field);
  }

  private _refIndexAdd(targetId: string, referrerId: string, field: string): void {
    let set = this.refIndex.get(targetId);
    if (!set) {
      set = new Set();
      this.refIndex.set(targetId, set);
    }
    set.add(referrerId + REF_SEP + field);
  }

  private _refIndexDiscard(targetId: string, referrerId: string, field: string): void {
    const set = this.refIndex.get(targetId);
    if (!set) return;
    set.delete(referrerId + REF_SEP + field);
    if (set.size === 0) this.refIndex.delete(targetId);
  }

  private _rebuildRefIndex(): void {
    this.refIndex.clear();
    for (const node of this.nodeMap.values()) this._refsAdd(node);
  }

  /**
   * Commit-time referential integrity: every reference written by an
   * inserted or updated node must resolve to a node of the declared type,
   * and no deleted node may still be referenced by a live one (policy
   * "restrict").
   */
  private _checkRefIntegrity(): void {
    const diff = this._diff;
    for (const nodeId of [...diff.inserted, ...diff.updated]) {
      const node = this.nodeMap.get(nodeId);
      if (!node) continue;
      for (const [field, rdef] of Object.entries(this._refDefsFor(node.type))) {
        for (const targetId of refIds(node.state[field])) {
          const target = this.nodeMap.get(targetId);
          if (!target) {
            throw new RefIntegrityError(
              `${node.type}.${field} on node '${nodeId}' references '${targetId}', which is not in the document`,
            );
          }
          if (rdef.target_type !== null && target.type !== rdef.target_type) {
            throw new RefIntegrityError(
              `${node.type}.${field} on node '${nodeId}' references ${target.type} '${targetId}', expected ${rdef.target_type}`,
            );
          }
        }
      }
    }
    for (const deletedId of diff.deleted.keys()) {
      for (const entry of this.refIndex.get(deletedId) ?? []) {
        const [referrerId, field] = entry.split(REF_SEP);
        const referrer = this.nodeMap.get(referrerId);
        if (referrer) {
          throw new RefIntegrityError(
            `Cannot delete node '${deletedId}': still referenced by ${referrer.type}.${field} on node '${referrerId}'`,
          );
        }
      }
    }
  }

  // --- Node creation ---

  createNode(type: string, state?: Record<string, unknown>): DocNode {
    if (!this.schema.node_types[type]) throw new Error(`Unknown node type: ${type}`);
    const id = this.idGen();
    const slotOrder = this._slotOrderFor(type);
    const node = createDocNode(id, type, slotOrder);
    if (state) {
      for (const [k, v] of Object.entries(state)) {
        this._checkField(type, k);
        node.state[k] = v;
      }
    }
    // Apply defaults from schema
    const defaults = this.schema.node_types[type]?.field_defaults;
    if (defaults) {
      for (const [k, v] of Object.entries(defaults)) {
        if (!(k in node.state)) {
          node.state[k] = structuredClone(v);
        }
      }
    }
    return node;
  }

  // --- Mutations (open transaction if idle) ---

  setNodeState(nodeId: string, key: string, value: unknown): void {
    const node = this.nodeMap.get(nodeId);
    if (!node) throw new Error(`Node not found: ${nodeId}`);
    this._checkField(node.type, key);

    withTransaction(this, () => {
      const current = node.state[key];
      if (current === value || JSON.stringify(current) === JSON.stringify(value)) return;

      onSetStateInverse(this._diff, this._inverseOps, node, key);
      node.state[key] = value;
      onSetStateForward(this._diff, this._forwardOps, this._inverseOps, node, key);
      if (key in this._refDefsFor(node.type)) {
        this._refsUpdate(node, key, current, value);
      }
    });
  }

  insertIntoSlot(
    parent: DocNode,
    slotName: string,
    position: string,
    nodes: DocNode[],
    target?: DocNode,
  ): void {
    if (nodes.length === 0) return;

    withTransaction(this, () => {
      if (!parent.slotFirst.has(slotName)) {
        throw new Error(`Slot '${slotName}' does not exist on ${parent.type}`);
      }
      // The parent (and the sibling target) must be the live objects for
      // their IDs: a stale handle to a deleted-and-restored node would
      // link the new nodes into a tree nobody can see.
      if (this.nodeMap.has(parent.id)) this._checkLive(parent);
      if (target) {
        if (this.nodeMap.has(parent.id)) this._checkLive(target);
        if (target.parent !== parent || target.slotName !== slotName) {
          throw new Error(`Node '${target.id}' is not in slot '${slotName}' of '${parent.id}'`);
        }
      }
      // A node (or ID) may enter the document once: the same node twice in
      // one batch would link it to itself, an existing ID would shadow a
      // live node.
      const seen = new Set<string>();
      for (const top of nodes) {
        for (const desc of descendantsInclusive(top)) {
          if (this.nodeMap.has(desc.id) || seen.has(desc.id)) {
            throw new Error(`Node '${desc.id}' already exists in the document`);
          }
          seen.add(desc.id);
        }
      }

      // Handle position redirects
      if (position === "prepend") {
        const first = parent.slotFirst.get(slotName) ?? null;
        if (first) {
          this.insertIntoSlot(parent, slotName, "before", nodes, first);
        } else {
          this.insertIntoSlot(parent, slotName, "append", nodes);
        }
        return;
      }
      if (position === "after" && target) {
        const nxt = target.nextSibling;
        if (nxt) {
          this.insertIntoSlot(parent, slotName, "before", nodes, nxt);
        } else {
          this.insertIntoSlot(parent, slotName, "append", nodes);
        }
        return;
      }

      // Record ops
      if (position === "append") {
        if (this.nodeMap.has(parent.id)) {
          onInsertRange(
            this._diff, this._forwardOps, this._inverseOps,
            this.root, parent, slotName, "append", nodes,
          );
        }
      } else if (position === "before" && target) {
        onInsertRangeBefore(
          this._diff, this._forwardOps, this._inverseOps,
          this.root, target, slotName, nodes,
        );
      }

      // Perform tree linking
      if (position === "append") {
        let current = parent.slotLast.get(slotName) ?? null;
        for (const nd of nodes) {
          this._attachNode(nd, parent, slotName, current);
          if (current) {
            current.nextSibling = nd;
          } else {
            parent.slotFirst.set(slotName, nd);
          }
          current = nd;
        }
        parent.slotLast.set(slotName, current);
      } else if (position === "before" && target) {
        let currentTarget = target;
        for (let i = nodes.length - 1; i >= 0; i--) {
          const nd = nodes[i];
          const prevOfTarget = currentTarget.prevSibling;
          this._attachNode(nd, parent, slotName, prevOfTarget, currentTarget);
          if (prevOfTarget) {
            prevOfTarget.nextSibling = nd;
          }
          currentTarget.prevSibling = nd;
          currentTarget = nd;
        }
        if (parent.slotFirst.get(slotName) === target) {
          parent.slotFirst.set(slotName, nodes[0]);
        }
      }
    });
  }

  deleteRange(startId: string, endId?: string): void {
    const start = this.nodeMap.get(startId);
    if (!start) throw new Error(`Node not found: ${startId}`);
    const end = endId ? this.nodeMap.get(endId) : start;
    if (!end) throw new Error(`Node not found: ${endId}`);

    withTransaction(this, () => {
      if (start === this.root) throw new Error("Root node cannot be deleted");
      // Validate before recording anything, so a bad range inside an
      // enclosing transaction leaves it untouched.
      this._checkLive(start);
      this._checkLive(end);
      const range = iterRange(start, end);
      if (range[range.length - 1] !== end) {
        throw new Error(`Node '${end.id}' is not a later sibling of '${start.id}'`);
      }

      onDeleteRange(
        this._diff, this._forwardOps, this._inverseOps,
        this.root, start, end,
      );

      // Remove from node map and drop the deleted nodes' own references
      for (const node of range) {
        for (const desc of descendantsInclusive(node)) {
          this.nodeMap.delete(desc.id);
          this.graveyard.set(desc.id, new WeakRef(desc));
          this._refsRemove(desc);
        }
      }
      if (this.graveyard.size > 4096) this._sweepGraveyard();
    });
  }

  /** Move a range to the end of `slotName` on `parentId` ("0" or "" = root). */
  moveRange(
    startId: string,
    endId: string | undefined,
    parentId: string,
    slotName: string,
  ): void {
    const { start, end } = this._resolveRange(startId, endId);
    const newParent = parentId === "0" || !parentId ? this.root : this.nodeMap.get(parentId);
    if (!newParent) throw new Error(`Parent not found: ${parentId}`);
    if (!newParent.slotFirst.has(slotName)) {
      throw new Error(`Slot '${slotName}' does not exist on ${newParent.type}`);
    }

    withTransaction(this, () => {
      const newPrev = newParent.slotLast.get(slotName) ?? null;
      if (newPrev === end) return; // already there
      this._moveRange(start, end, newParent, slotName, newPrev, null);
    });
  }

  /** Move a range so it sits immediately before or after sibling `targetId`. */
  moveRangeRelative(
    startId: string,
    endId: string | undefined,
    targetId: string,
    position: "before" | "after",
  ): void {
    const { start, end } = this._resolveRange(startId, endId);
    const target = this.nodeMap.get(targetId);
    if (!target) throw new Error(`Node not found: ${targetId}`);
    const newParent = target.parent;
    const slotName = target.slotName;
    if (!newParent || !slotName) {
      throw new Error("Cannot move before or after the root");
    }

    withTransaction(this, () => {
      if (iterRange(start, end).includes(target)) {
        throw new Error("Target is in the range");
      }
      if (position === "before") {
        if (target.prevSibling === end) return;
        this._moveRange(start, end, newParent, slotName, target.prevSibling, target);
      } else {
        if (target.nextSibling === start) return;
        this._moveRange(start, end, newParent, slotName, target, target.nextSibling);
      }
    });
  }

  private _resolveRange(startId: string, endId: string | undefined) {
    const start = this.nodeMap.get(startId);
    if (!start) throw new Error(`Node not found: ${startId}`);
    const end = endId ? this.nodeMap.get(endId) : start;
    if (!end) throw new Error(`Node not found: ${endId ?? startId}`);
    return { start, end };
  }

  private _moveRange(
    start: DocNode,
    end: DocNode,
    newParent: DocNode,
    slotName: string,
    newPrev: DocNode | null,
    newNext: DocNode | null,
  ): void {
    this._checkLive(start);
    this._checkLive(end);
    this._checkLive(newParent);
    if (newPrev) this._checkLive(newPrev);
    if (newNext) this._checkLive(newNext);
    const range = iterRange(start, end);
    if (range[range.length - 1] !== end) {
      throw new Error(`Node '${end.id}' is not a later sibling of '${start.id}'`);
    }
    if (range.includes(newParent)) throw new Error("Target is in the range");
    for (let anc = newParent.parent; anc; anc = anc.parent) {
      if (range.includes(anc)) throw new Error("Target is descendant of the range");
    }

    onMoveRange(
      this._diff, this._forwardOps, this._inverseOps,
      this.root, start, end, newParent, slotName, newPrev, newNext,
    );

    detachRange(start, end);

    start.prevSibling = newPrev;
    if (newPrev) {
      newPrev.nextSibling = start;
    } else {
      newParent.slotFirst.set(slotName, start);
    }
    end.nextSibling = newNext;
    if (newNext) {
      newNext.prevSibling = end;
    } else {
      newParent.slotLast.set(slotName, end);
    }
    for (const nd of range) {
      nd.parent = newParent;
      nd.slotName = slotName;
    }
  }

  // --- Apply remote/inverse operations ---

  /**
   * Apply operations (remote patches or inverse ops). With
   * `flags.skipUndo` the operations run in their own transaction that the
   * undo manager ignores; any open transaction is committed first.
   *
   * A failure rolls the operations back. By default it is then swallowed
   * (best effort); with `raiseOnError` it propagates. Called inside an
   * open transaction, the operations join it and a failure always
   * propagates: the enclosing transaction rolls back as a whole.
   */
  applyOperations(ops: WireOperations, flags?: TransactionFlags, raiseOnError = false): void {
    withTransaction(this, () => this._applyOps(ops), !raiseOnError, flags);
  }

  /**
   * Apply operations through the tracked mutators, inside whatever
   * transaction is open. An operation whose target is missing is skipped
   * (best effort: undo and rollback may legitimately meet a node that is
   * gone), as is a delete or move that fails; an insert that fails (a
   * duplicate ID) or a malformed operation throws, and the transaction
   * that called `applyOperations` rolls back as a whole. Shared by
   * `applyOperations` and `abort`, so the reverse reference index is
   * maintained on every path.
   */
  private _applyOps(ops: WireOperations): void {
    for (const op of ops.ordered) {
      {
        if (op[0] === 0) {
          // Insert
          const nodePairs = op[1] as [string, string][];
          const parentIdRaw = op[2];
          const slotName = op[3] as string;
          const prevId = op[4];
          const nextId = op[5];

          const parent = parentIdRaw === 0
            ? this.root
            : this.nodeMap.get(String(parentIdRaw));
          if (!parent) continue;

          const nodes = nodePairs.map(([id, type]) => this._nodeForInsert(id, type));

          if (prevId) {
            const prev = this.nodeMap.get(String(prevId));
            if (prev) {
              this.insertIntoSlot(parent, slotName, "after", nodes, prev);
              continue;
            }
          }
          if (nextId) {
            const next = this.nodeMap.get(String(nextId));
            if (next) {
              this.insertIntoSlot(parent, slotName, "before", nodes, next);
              continue;
            }
          }
          this.insertIntoSlot(parent, slotName, "append", nodes);

        } else if (op[0] === 1) {
          // Delete
          const startId = op[1] as string;
          const endIdRaw = op[2];
          const endId = endIdRaw === 0 ? undefined : String(endIdRaw);
          if (!this.nodeMap.has(startId)) continue;
          try {
            this.deleteRange(startId, endId);
          } catch {
            // Skipped: the range is gone or no longer contiguous.
          }

        } else if (op[0] === 2) {
          // Move
          const startId = op[1] as string;
          const endIdRaw = op[2];
          const parentIdRaw = op[3];
          const slotName = op[4] as string;
          const prevIdRaw = op[5];
          const nextIdRaw = op[6];
          const endId = endIdRaw === 0 ? undefined : String(endIdRaw);
          const parentId = parentIdRaw === 0 ? "" : String(parentIdRaw);
          if (!this.nodeMap.has(startId)) continue;
          try {
            if (prevIdRaw && this.nodeMap.has(String(prevIdRaw))) {
              this.moveRangeRelative(startId, endId, String(prevIdRaw), "after");
            } else if (nextIdRaw && this.nodeMap.has(String(nextIdRaw))) {
              this.moveRangeRelative(startId, endId, String(nextIdRaw), "before");
            } else {
              this.moveRange(startId, endId, parentId, slotName);
            }
          } catch {
            // Skipped: the move no longer applies.
          }
        } else {
          throw new Error(`Unknown operation code: ${String((op as unknown[])[0])}`);
        }
      }
    }

    // Apply state patches through tracking
    for (const [nodeId, patches] of Object.entries(ops.state)) {
      const node = this.nodeMap.get(nodeId);
      if (!node) continue;
      const defaults = this.schema.node_types[node.type]?.field_defaults ?? {};
      for (const [key, value] of Object.entries(patches)) {
        this._checkField(node.type, key);
        onSetStateInverse(this._diff, this._inverseOps, node, key);
        const oldValue = node.state[key];
        if (value === null && !(key in defaults)) {
          // null for a field without a default means "unset" (that is how
          // an unset field serializes), so restore it to unset.
          delete node.state[key];
        } else {
          node.state[key] = value;
        }
        onSetStateForward(this._diff, this._forwardOps, this._inverseOps, node, key);
        if (key in this._refDefsFor(node.type)) {
          this._refsUpdate(node, key, oldValue, value);
        }
      }
    }
  }

  // --- Transaction lifecycle ---

  forceCommit(): void {
    if (this._lifecycleStage === "change") {
      throw new Error("Cannot trigger an update inside a change event");
    }

    this._lifecycleStage = "idle";

    // Fire listeners if there are changes
    const hasChanges =
      this._diff.inserted.size > 0 ||
      this._diff.deleted.size > 0 ||
      this._diff.moved.size > 0 ||
      Object.keys(this._forwardOps.state).length > 0;

    if (hasChanges) {
      // Referential integrity is a document-level check; a failure reopens
      // the transaction so the caller (withTransaction) can roll it back.
      try {
        this._checkRefIntegrity();
      } catch (e) {
        this._lifecycleStage = "update";
        throw e;
      }

      this._lifecycleStage = "change";
      // The event owns copies: listeners may keep it (the undo manager
      // does) and the document reuses nothing they received. Inverse ops
      // are recorded in forward order; hand them out in application order.
      const event: ChangeEvent = {
        operations: {
          ordered: [...this._forwardOps.ordered],
          state: copyState(this._forwardOps.state),
        },
        inverseOperations: {
          ordered: [...this._inverseOps.ordered].reverse(),
          state: copyState(this._inverseOps.state),
        },
        diff: {
          inserted: new Set(this._diff.inserted),
          deleted: new Map(this._diff.deleted),
          moved: new Set(this._diff.moved),
          updated: new Set(this._diff.updated),
        },
        flags: { ...this._transactionFlags },
      };
      // Listeners are observers of a commit that is already final: every
      // one of them runs, whatever the others do, and failures are
      // reported together afterwards (see ListenerError).
      const errors: unknown[] = [];
      for (const cb of [...this.changeListeners]) {
        try {
          cb(event);
        } catch (e) {
          errors.push(e);
        }
      }
      this._reset();
      if (errors.length > 0) throw new ListenerError(errors);
      return;
    }

    this._reset();
  }

  private _reset(): void {
    this._forwardOps = createOpsAccumulator();
    this._inverseOps = createOpsAccumulator();
    this._diff = createDiff();
    this._transactionFlags = {};
    this._lifecycleStage = "idle";
  }

  abort(): void {
    if (this._lifecycleStage === "idle") return; // nothing open
    // Inverse ops are recorded in forward order; roll back in reverse,
    // through the tracked mutators so the reverse reference index follows.
    const inverse: WireOperations = {
      ordered: [...this._inverseOps.ordered].reverse(),
      state: { ...this._inverseOps.state },
    };
    try {
      this._applyOps(inverse);
    } finally {
      // Whatever happens, the document must not stay in the update stage.
      this._reset();
      this._rebuildRefIndex();
    }
  }

  // --- Events ---

  onChange(cb: (e: ChangeEvent) => void): () => void {
    this.changeListeners.push(cb);
    return () => {
      const idx = this.changeListeners.indexOf(cb);
      if (idx >= 0) this.changeListeners.splice(idx, 1);
    };
  }


  // --- Serialization ---

  toSnapshot(): JsonDoc {
    return this._nodeToWire(this.root);
  }

  // --- Internal ---

  /** Throw unless `node` is the object the document holds for its ID. */
  private _checkLive(node: DocNode): void {
    const live = this.nodeMap.get(node.id);
    if (!live) throw new Error(`Node '${node.id}' is not in the document`);
    if (live !== node) {
      throw new Error(
        `Node '${node.id}' is a stale handle: the document holds a different object for this ID`,
      );
    }
  }

  private _checkField(type: string, key: string): void {
    const def = this.schema.node_types[type];
    if (def && !(key in def.field_tiers)) {
      throw new Error(`${type} has no field '${key}'`);
    }
  }

  /**
   * A bare node for an insert operation. If the ID belongs to a node this
   * document removed earlier (an undone delete, a rolled-back
   * transaction) and someone still holds that object, it is revived:
   * reset to a fresh node and handed back, so the holder's handle is
   * live again. Otherwise a new object is created.
   */
  private _nodeForInsert(id: string, type: string): DocNode {
    const old = this.graveyard.get(id)?.deref();
    this.graveyard.delete(id);
    if (old && old.type === type) {
      resetDocNode(old);
      this._applyDefaults(old);
      return old;
    }
    const node = this.createNode(type);
    (node as { id: string }).id = id;
    return node;
  }

  private _applyDefaults(node: DocNode): void {
    const defaults = this.schema.node_types[node.type]?.field_defaults;
    if (!defaults) return;
    for (const [k, v] of Object.entries(defaults)) {
      if (!(k in node.state)) node.state[k] = structuredClone(v);
    }
  }

  private _sweepGraveyard(): void {
    for (const [id, ref] of this.graveyard) {
      if (ref.deref() === undefined) this.graveyard.delete(id);
    }
  }

  private _attachNode(
    node: DocNode,
    parent: DocNode,
    slotName: string,
    prev: DocNode | null = null,
    next: DocNode | null = null,
  ): void {
    node.parent = parent;
    node.slotName = slotName;
    node.prevSibling = prev;
    node.nextSibling = next;
    if (this.nodeMap.has(parent.id)) {
      for (const desc of descendantsInclusive(node)) {
        this.nodeMap.set(desc.id, desc);
        this._refsAdd(desc);
      }
    }
  }

  private _slotOrderFor(type: string): string[] {
    const typeDef = this.schema.node_types[type];
    return typeDef ? Object.keys(typeDef.slots) : [];
  }

  private _createNodeFromJson(data: JsonDoc): DocNode {
    const [id, type, state] = data;
    const slotOrder = this._slotOrderFor(type);
    const node = createDocNode(id, type, slotOrder);
    for (const [k, v] of Object.entries(state)) {
      node.state[k] = v;
    }
    // Apply defaults
    const defaults = this.schema.node_types[type]?.field_defaults;
    if (defaults) {
      for (const [k, v] of Object.entries(defaults)) {
        if (!(k in node.state)) {
          node.state[k] = structuredClone(v);
        }
      }
    }
    return node;
  }

  /** Load slot children from a snapshot, iteratively (any nesting depth). */
  private _loadSlots(
    root: DocNode,
    rootSlots: Record<string, JsonDoc[]>,
  ): void {
    const work: Array<[DocNode, Record<string, JsonDoc[]>]> = [[root, rootSlots]];
    while (work.length > 0) {
      const [parent, slotsData] = work.pop()!;
      for (const [slotName, children] of Object.entries(slotsData)) {
        if (!parent.slotFirst.has(slotName)) continue;

        let prev: DocNode | null = null;
        for (const childJson of children) {
          const child = this._createNodeFromJson(childJson);
          child.parent = parent;
          child.slotName = slotName;
          child.prevSibling = prev;
          if (prev) {
            prev.nextSibling = child;
          } else {
            parent.slotFirst.set(slotName, child);
          }
          this.nodeMap.set(child.id, child);
          prev = child;
          if (childJson[3]) work.push([child, childJson[3]]);
        }
        if (prev) {
          parent.slotLast.set(slotName, prev);
        }
      }
    }
  }

  /**
   * Serialize a node tree to wire format, iteratively: each node's entry
   * is created when it is visited and its children are appended to it in
   * document order as they are visited.
   */
  private _nodeToWire(root: DocNode): JsonDoc {
    const entryFor = (node: DocNode): JsonDoc => {
      const state: Record<string, unknown> = {};
      const defaults = this.schema.node_types[node.type]?.field_defaults ?? {};
      for (const [k, v] of Object.entries(node.state)) {
        if (JSON.stringify(v) !== JSON.stringify(defaults[k])) {
          state[k] = v;
        }
      }
      const result: JsonDoc = [node.id, node.type, state];
      if (node.slotOrder.length > 0) {
        const slots: Record<string, JsonDoc[]> = {};
        for (const slotName of node.slotOrder) slots[slotName] = [];
        result.push(slots);
      }
      return result;
    };

    const rootEntry = entryFor(root);
    const stack: Array<[DocNode, JsonDoc[] | null, JsonDoc]> = [[root, null, rootEntry]];
    while (stack.length > 0) {
      const [node, into, entry] = stack.pop()!;
      if (into) into.push(entry);
      if (node.slotOrder.length === 0) continue;
      const slots = entry[3]!;
      const pending: Array<[DocNode, JsonDoc[] | null, JsonDoc]> = [];
      for (const slotName of node.slotOrder) {
        let child = node.slotFirst.get(slotName) ?? null;
        while (child !== null) {
          pending.push([child, slots[slotName], entryFor(child)]);
          child = child.nextSibling;
        }
      }
      for (let i = pending.length - 1; i >= 0; i--) stack.push(pending[i]);
    }
    return rootEntry;
  }
}
