/**
 * Operation tracking and remote applier — port of _operations.py.
 *
 * All functions are stateless: they take accumulators and diff as arguments.
 */

import type { OrderedOp, WireOperations } from "../types.js";
import type { DocNode } from "./doc-node.js";
import { getSlotChildren } from "./doc-node.js";
import {
  iterRange,
  detachRange,
  descendants,
  descendantsInclusive,
} from "./local-range.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface OpsAccumulator {
  ordered: OrderedOp[];
  state: Record<string, Record<string, unknown>>;
}

export interface Diff {
  inserted: Set<string>;
  deleted: Map<string, DocNode>;
  moved: Set<string>;
  updated: Set<string>;
}

export function createOpsAccumulator(): OpsAccumulator {
  return { ordered: [], state: {} };
}

export function createDiff(): Diff {
  return {
    inserted: new Set(),
    deleted: new Map(),
    moved: new Set(),
    updated: new Set(),
  };
}

// ---------------------------------------------------------------------------
// State tracking
// ---------------------------------------------------------------------------

function stateValue(node: DocNode, key: string): unknown {
  return node.state[key] ?? null;
}

function valuesEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  // Structural equality for arrays/objects — cheap JSON compare is fine here
  // because both sides came from the same state dict and are JSON-compatible.
  return JSON.stringify(a) === JSON.stringify(b);
}

export function onSetStateInverse(
  diff: Diff,
  inverseOps: OpsAccumulator,
  node: DocNode,
  key: string,
): void {
  if (diff.inserted.has(node.id)) return;
  if (inverseOps.state[node.id]?.[key] !== undefined) return;
  const original = stateValue(node, key);
  if (!inverseOps.state[node.id]) inverseOps.state[node.id] = {};
  inverseOps.state[node.id][key] = original;
}

export function onSetStateForward(
  diff: Diff,
  forwardOps: OpsAccumulator,
  inverseOps: OpsAccumulator,
  node: DocNode,
  key: string,
): void {
  const newValue = stateValue(node, key);
  const prevValue = inverseOps.state[node.id]?.[key];
  const nodePatch = forwardOps.state[node.id];

  if (valuesEqual(prevValue, newValue) && nodePatch !== undefined) {
    delete nodePatch[key];
    if (Object.keys(nodePatch).length === 0) {
      delete forwardOps.state[node.id];
      diff.updated.delete(node.id);
    }
    const invNode = inverseOps.state[node.id];
    if (invNode) delete invNode[key];
  } else {
    if (!forwardOps.state[node.id]) forwardOps.state[node.id] = {};
    forwardOps.state[node.id][key] = newValue;
    if (!diff.inserted.has(node.id)) {
      diff.updated.add(node.id);
    }
  }
}

// ---------------------------------------------------------------------------
// Insert tracking
// ---------------------------------------------------------------------------

function copyInsertedToDiff(
  diff: Diff,
  forwardOps: OpsAccumulator,
  node: DocNode,
): void {
  const wasDeleted = diff.deleted.has(node.id);
  if (wasDeleted) {
    diff.deleted.delete(node.id);
    diff.moved.add(node.id);
    diff.updated.add(node.id);
  } else {
    diff.inserted.add(node.id);
  }
  // Record non-default state for the insert op (native JSON values)
  const jsonState: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(node.state)) {
    jsonState[k] = v;
  }
  if (Object.keys(jsonState).length > 0) {
    forwardOps.state[node.id] = jsonState;
  }
}

export function onInsertRange(
  diff: Diff,
  forwardOps: OpsAccumulator,
  inverseOps: OpsAccumulator,
  root: DocNode,
  parent: DocNode,
  slotName: string,
  position: string,
  nodes: DocNode[],
): void {
  let newPrev: DocNode | null = null;
  if (position === "append") {
    newPrev = parent.slotLast.get(slotName) ?? null;
  }

  forwardOps.ordered.push([
    0,
    nodes.map((n) => [n.id, n.type]),
    parent === root ? 0 : parent.id,
    slotName,
    newPrev ? newPrev.id : 0,
    0,
  ]);

  if (!diff.inserted.has(parent.id)) {
    inverseOps.ordered.push([
      1,
      nodes[0].id,
      nodes.length > 1 ? nodes[nodes.length - 1].id : 0,
    ]);
  }

  for (const topNode of nodes) {
    copyInsertedToDiff(diff, forwardOps, topNode);
    for (const desc of descendants(topNode)) {
      copyInsertedToDiff(diff, forwardOps, desc);
      if (
        desc.parent &&
        desc.slotName &&
        desc.prevSibling === null
      ) {
        const children = getSlotChildren(desc.parent, desc.slotName);
        forwardOps.ordered.push([
          0,
          children.map((c) => [c.id, c.type]),
          desc.parent.id,
          desc.slotName,
          0,
          0,
        ]);
        if (!diff.inserted.has(desc.parent.id)) {
          const last = desc.parent.slotLast.get(desc.slotName) ?? null;
          inverseOps.ordered.push([
            1,
            desc.id,
            last && last !== desc ? last.id : 0,
          ]);
        }
      }
    }
  }
}

export function onInsertRangeBefore(
  diff: Diff,
  forwardOps: OpsAccumulator,
  inverseOps: OpsAccumulator,
  root: DocNode,
  target: DocNode,
  slotName: string,
  nodes: DocNode[],
): void {
  const parent = target.parent!;

  forwardOps.ordered.push([
    0,
    nodes.map((n) => [n.id, n.type]),
    parent === root ? 0 : parent.id,
    slotName,
    target.prevSibling ? target.prevSibling.id : 0,
    target.id,
  ]);

  if (!diff.inserted.has(parent.id)) {
    inverseOps.ordered.push([
      1,
      nodes[0].id,
      nodes.length > 1 ? nodes[nodes.length - 1].id : 0,
    ]);
  }

  for (const topNode of nodes) {
    copyInsertedToDiff(diff, forwardOps, topNode);
    for (const desc of descendants(topNode)) {
      copyInsertedToDiff(diff, forwardOps, desc);
      if (
        desc.parent &&
        desc.slotName &&
        desc.prevSibling === null
      ) {
        const children = getSlotChildren(desc.parent, desc.slotName);
        forwardOps.ordered.push([
          0,
          children.map((c) => [c.id, c.type]),
          desc.parent.id,
          desc.slotName,
          0,
          0,
        ]);
        if (!diff.inserted.has(desc.parent.id)) {
          const last = desc.parent.slotLast.get(desc.slotName) ?? null;
          inverseOps.ordered.push([
            1,
            desc.id,
            last && last !== desc ? last.id : 0,
          ]);
        }
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Delete tracking
// ---------------------------------------------------------------------------

function copyDeletedToDiff(
  diff: Diff,
  inverseOps: OpsAccumulator,
  node: DocNode,
): void {
  const wasInserted = diff.inserted.has(node.id);
  if (wasInserted) {
    diff.inserted.delete(node.id);
    diff.moved.delete(node.id);
  } else {
    // Capture current state for undo (native JSON values)
    const currentState: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(node.state)) {
      currentState[k] = v;
    }
    const prevInv = inverseOps.state[node.id] ?? {};
    inverseOps.state[node.id] = { ...currentState, ...prevInv };
    diff.deleted.set(node.id, node);
  }
}

export function onDeleteRange(
  diff: Diff,
  forwardOps: OpsAccumulator,
  inverseOps: OpsAccumulator,
  root: DocNode,
  startNode: DocNode,
  endNode: DocNode,
): void {
  const parent = startNode.parent!;
  const slotName = startNode.slotName!;

  forwardOps.ordered.push([
    1,
    startNode.id,
    endNode !== startNode ? endNode.id : 0,
  ]);

  const nodePairs: [string, string][] = [];
  for (const node of iterRange(startNode, endNode)) {
    nodePairs.push([node.id, node.type]);
    copyDeletedToDiff(diff, inverseOps, node);
  }

  const shouldAddInverse = !diff.inserted.has(parent.id);
  const tempInverse: OrderedOp[] = [];

  if (shouldAddInverse) {
    tempInverse.push([
      0,
      nodePairs,
      parent === root ? 0 : parent.id,
      slotName,
      startNode.prevSibling ? startNode.prevSibling.id : 0,
      endNode.nextSibling ? endNode.nextSibling.id : 0,
    ]);
  }

  // Detach from tree
  detachRange(startNode, endNode);

  // Handle descendants and clean up
  for (const node of iterRange(startNode, endNode)) {
    for (const desc of descendantsInclusive(node)) {
      delete forwardOps.state[desc.id];
      diff.updated.delete(desc.id);
      for (const descSlot of desc.slotOrder) {
        if (desc.slotFirst.get(descSlot) !== null) {
          const childPairs: [string, string][] = [];
          let child = desc.slotFirst.get(descSlot) ?? null;
          while (child !== null) {
            copyDeletedToDiff(diff, inverseOps, child);
            childPairs.push([child.id, child.type]);
            child = child.nextSibling;
          }
          if (shouldAddInverse) {
            tempInverse.push([0, childPairs, desc.id, descSlot, 0, 0]);
          }
        }
      }
    }
  }

  tempInverse.reverse();
  inverseOps.ordered.push(...tempInverse);
}

// ---------------------------------------------------------------------------
// Move tracking
// ---------------------------------------------------------------------------

export function onMoveRange(
  diff: Diff,
  forwardOps: OpsAccumulator,
  inverseOps: OpsAccumulator,
  root: DocNode,
  startNode: DocNode,
  endNode: DocNode,
  newParent: DocNode,
  newSlot: string,
  newPrev: DocNode | null,
  newNext: DocNode | null,
): void {
  const endId = endNode === startNode ? 0 : endNode.id;

  forwardOps.ordered.push([
    2,
    startNode.id,
    endId,
    newParent === root ? 0 : newParent.id,
    newSlot,
    newPrev ? newPrev.id : 0,
    newNext ? newNext.id : 0,
  ]);

  const currentParent = startNode.parent!;
  const currentSlot = startNode.slotName ?? "";
  const currentPrev = startNode.prevSibling;
  const currentNext = endNode.nextSibling;

  inverseOps.ordered.push([
    2,
    startNode.id,
    endId,
    currentParent === root ? 0 : currentParent.id,
    currentSlot,
    currentPrev ? currentPrev.id : 0,
    currentNext ? currentNext.id : 0,
  ]);

  for (const node of iterRange(startNode, endNode)) {
    if (!diff.inserted.has(node.id)) {
      diff.moved.add(node.id);
    }
  }
}

// ---------------------------------------------------------------------------
// Remote/inverse operation applier
// ---------------------------------------------------------------------------

export function applyOperations(
  nodeMap: Map<string, DocNode>,
  root: DocNode,
  ops: WireOperations,
  createNodeFn: (id: string, type: string) => DocNode,
  insertIntoSlotFn: (
    parent: DocNode,
    slotName: string,
    position: string,
    nodes: DocNode[],
    target?: DocNode,
  ) => void,
): void {
  for (const op of ops.ordered) {
    if (op[0] === 0) {
      // Insert
      const nodePairs = op[1] as [string, string][];
      const parentId = op[2];
      const slotName = op[3] as string;
      const prevId = op[4];
      const nextId = op[5];

      const nodes = nodePairs.map(([id, type]) => createNodeFn(id, type));
      const parent = parentId === 0 ? root : nodeMap.get(String(parentId));
      if (!parent) continue;

      if (prevId) {
        const prev = nodeMap.get(String(prevId));
        if (prev) {
          insertIntoSlotFn(parent, slotName, "after", nodes, prev);
          continue;
        }
      }
      if (nextId) {
        const next = nodeMap.get(String(nextId));
        if (next) {
          insertIntoSlotFn(parent, slotName, "before", nodes, next);
          continue;
        }
      }
      insertIntoSlotFn(parent, slotName, "append", nodes);
    } else if (op[0] === 1) {
      // Delete
      const startId = op[1] as string;
      const endIdRaw = op[2];
      const endId = endIdRaw === 0 ? startId : String(endIdRaw);

      const start = nodeMap.get(startId);
      const end = nodeMap.get(endId);
      if (!start || !end) continue;

      // Remove from tree
      const toRemove = iterRange(start, end);
      detachRange(start, end);
      for (const node of toRemove) {
        for (const desc of descendantsInclusive(node)) {
          nodeMap.delete(desc.id);
        }
      }
    } else if (op[0] === 2) {
      // Move
      const startId = op[1] as string;
      const endIdRaw = op[2];
      const parentIdRaw = op[3];
      const slotName = op[4] as string;

      const endId = endIdRaw === 0 ? startId : String(endIdRaw);
      const start = nodeMap.get(startId);
      const end = nodeMap.get(endId);
      if (!start || !end) continue;

      const prevIdRaw = op[5];
      const nextIdRaw = op[6];

      const parent =
        parentIdRaw === 0 ? root : nodeMap.get(String(parentIdRaw));
      if (!parent) continue;

      // Capture the range before detaching (detach clears the end pointers)
      const movedNodes = iterRange(start, end);
      const prev = prevIdRaw ? nodeMap.get(String(prevIdRaw)) : undefined;
      const next = nextIdRaw ? nodeMap.get(String(nextIdRaw)) : undefined;

      // Detach from old position
      detachRange(start, end);
      start.prevSibling = null;
      end.nextSibling = null;

      // Re-insert where the operation recorded it: after prev, before
      // next, or appended to the parent's slot.
      if (prev && prev.parent && prev.slotName) {
        insertIntoSlotFn(prev.parent, prev.slotName, "after", movedNodes, prev);
      } else if (next && next.parent && next.slotName) {
        insertIntoSlotFn(next.parent, next.slotName, "before", movedNodes, next);
      } else {
        insertIntoSlotFn(parent, slotName, "append", movedNodes);
      }
    }
  }

  // Apply state patches
  for (const [nodeId, patches] of Object.entries(ops.state)) {
    const node = nodeMap.get(nodeId);
    if (!node) continue;
    for (const [key, value] of Object.entries(patches)) {
      node.state[key] = value;
    }
  }
}

// ---------------------------------------------------------------------------
// Merging
// ---------------------------------------------------------------------------

/**
 * Concatenate several operation sets into one. Ordered operations are
 * appended in order; state patches merge per node with later values
 * winning. Used by the undo manager to collapse consecutive transactions.
 */
export function mergeOperations(...list: WireOperations[]): WireOperations {
  const ordered: OrderedOp[] = [];
  const state: Record<string, Record<string, unknown>> = {};
  for (const ops of list) {
    ordered.push(...ops.ordered);
    for (const [nodeId, patch] of Object.entries(ops.state)) {
      state[nodeId] = { ...(state[nodeId] ?? {}), ...patch };
    }
  }
  return { ordered, state };
}
