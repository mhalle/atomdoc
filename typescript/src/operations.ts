/**
 * Operation constructors — pure functions that build wire messages.
 */

import type { CreateMsg, OpMsg, RedoMsg, UndoMsg } from "./types.js";

/** Set a field on an existing node. */
export function setField(
  nodeId: string,
  field: string,
  value: unknown,
): OpMsg {
  return {
    type: "op",
    operations: {
      ordered: [],
      state: { [nodeId]: { [field]: value } },
    },
  };
}

/** Delete a node (single node, not a range). */
export function deleteNode(nodeId: string): OpMsg {
  return {
    type: "op",
    operations: {
      ordered: [[1, nodeId, 0]],
      state: {},
    },
  };
}

/**
 * Move a node to a new parent/slot.
 * prevId/nextId position the node relative to existing children.
 * If neither is given, appends.
 */
export function moveNode(
  nodeId: string,
  parentId: string,
  slot: string,
  prevId?: string,
  nextId?: string,
): OpMsg {
  return {
    type: "op",
    operations: {
      ordered: [
        [2, nodeId, 0, parentId, slot, prevId ?? 0, nextId ?? 0],
      ],
      state: {},
    },
  };
}

/** Request the server to create a new node and insert it. */
export function createNode(
  nodeType: string,
  state: Record<string, unknown>,
  parentId: string,
  slot: string,
  position: string = "append",
): CreateMsg {
  return {
    type: "create",
    node_type: nodeType,
    state,
    parent_id: parentId,
    slot,
    position,
  };
}

/** Request undo (1 or more steps). */
export function undo(steps: number = 1): UndoMsg {
  return steps === 1 ? { type: "undo" } : { type: "undo", steps };
}

/** Request redo (1 or more steps). */
export function redo(steps: number = 1): RedoMsg {
  return steps === 1 ? { type: "redo" } : { type: "redo", steps };
}
