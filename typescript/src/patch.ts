/**
 * Patch applier — updates the NodeStore from wire operations.
 */

import type { NodeStore } from "./store.js";
import type { WireOperations } from "./types.js";

/**
 * Child lists touched by the ordered operations of one patch, written to
 * the store once at the end. The store keeps child lists as immutable
 * arrays (subscribers compare references), so writing after every
 * operation would copy a slot's array per insert and make a transaction
 * that fills a slot node by node quadratic.
 */
class SlotEdits {
  private lists = new Map<string, string[]>();
  constructor(private store: NodeStore) {}

  get(parentId: string, slotName: string): string[] {
    const key = `${parentId}\u0000${slotName}`;
    let list = this.lists.get(key);
    if (!list) {
      list = [...this.store.getChildren(parentId, slotName)];
      this.lists.set(key, list);
    }
    return list;
  }

  /** Slot names of `parentId` this patch has a working list for. */
  slotsOf(parentId: string): string[] {
    const prefix = parentId + "\u0000";
    const names: string[] = [];
    for (const key of this.lists.keys()) {
      if (key.startsWith(prefix)) names.push(key.slice(prefix.length));
    }
    return names;
  }

  /** The child lists of a node this patch has removed are not written. */
  drop(parentId: string): void {
    for (const key of [...this.lists.keys()]) {
      if (key.startsWith(parentId + "\u0000")) this.lists.delete(key);
    }
  }

  flush(): void {
    for (const [key, list] of this.lists) {
      const sep = key.indexOf("\u0000");
      this.store._setChildren(key.slice(0, sep), key.slice(sep + 1), list);
    }
  }
}

export function applyPatch(
  store: NodeStore,
  operations: WireOperations,
): void {
  store.batch(() => {
    // Apply ordered operations first
    const slots = new SlotEdits(store);
    for (const op of operations.ordered) {
      switch (op[0]) {
        case 0:
          applyInsert(store, slots, op);
          break;
        case 1:
          applyDelete(store, slots, op);
          break;
        case 2:
          applyMove(store, slots, op);
          break;
      }
    }
    slots.flush();

    // Apply state patches (values are native JSON — no parsing needed)
    for (const [nodeId, patches] of Object.entries(operations.state)) {
      for (const [field, value] of Object.entries(patches)) {
        store._updateState(nodeId, field, value);
      }
    }
  });
}

function resolveId(id: string | 0): string | null {
  return id === 0 ? null : id;
}

function applyInsert(
  store: NodeStore,
  slots: SlotEdits,
  op: [0, [string, string][], string | 0, string, string | 0, string | 0],
): void {
  const [, nodePairs, parentIdRaw, slotName, prevIdRaw, nextIdRaw] = op;
  const parentId = resolveId(parentIdRaw) ?? store.getRootId();
  const prevId = resolveId(prevIdRaw);
  const nextId = resolveId(nextIdRaw);

  const parent = store.getNode(parentId);
  if (!parent) return;

  // Create new nodes
  const newIds: string[] = [];
  for (const [id, type] of nodePairs) {
    if (!store.getNode(id)) {
      store._setNode(id, {
        id,
        type,
        state: {},
        slots: {},
        parentId,
        slotName,
      });
    }
    newIds.push(id);
  }

  // Insert into parent's slot at the right position. The common case,
  // appending after the current last child, is O(1); the list is written
  // to the store once per patch.
  const children = slots.get(parentId, slotName);

  if (nextId) {
    const idx = children.indexOf(nextId);
    if (idx >= 0) {
      children.splice(idx, 0, ...newIds);
    } else {
      children.push(...newIds);
    }
  } else if (prevId) {
    if (children[children.length - 1] === prevId) {
      children.push(...newIds);
    } else {
      const idx = children.indexOf(prevId);
      if (idx >= 0) {
        children.splice(idx + 1, 0, ...newIds);
      } else {
        children.push(...newIds);
      }
    }
  } else {
    children.push(...newIds);
  }
}

function applyDelete(
  store: NodeStore,
  slots: SlotEdits,
  op: [1, string, string | 0],
): void {
  const [, startId, endIdRaw] = op;
  const endId = resolveId(endIdRaw) ?? startId;

  const startNode = store.getNode(startId);
  if (!startNode || !startNode.parentId || !startNode.slotName) return;

  const parentId = startNode.parentId;
  const slotName = startNode.slotName;
  const children = slots.get(parentId, slotName);

  // Find the range of IDs to delete
  const startIdx = children.indexOf(startId);
  const endIdx = children.indexOf(endId);
  if (startIdx < 0 || endIdx < 0) return;

  const toRemove = children.splice(startIdx, endIdx - startIdx + 1);

  // Remove nodes and their descendants
  for (const id of toRemove) {
    removeRecursive(store, slots, id);
  }
}

/**
 * Remove a node and its subtree. The subtree is read through the
 * patch's working child lists, not the store's: an earlier operation in
 * the same patch may have moved a child out (it must survive) or in (it
 * must go).
 */
function removeRecursive(store: NodeStore, slots: SlotEdits, nodeId: string): void {
  const stack = [nodeId];
  while (stack.length > 0) {
    const id = stack.pop()!;
    const node = store.getNode(id);
    if (!node) continue;
    // A slot the store never saw children in (an insert earlier in this
    // patch) exists only as a working list.
    const names = new Set([...Object.keys(node.slots), ...slots.slotsOf(id)]);
    for (const slotName of names) stack.push(...slots.get(id, slotName));
    slots.drop(id);
    store._removeNode(id);
  }
}

function applyMove(
  store: NodeStore,
  slots: SlotEdits,
  op: [
    2,
    string,
    string | 0,
    string | 0,
    string,
    string | 0,
    string | 0,
  ],
): void {
  const [, startId, endIdRaw, newParentIdRaw, slotName, prevIdRaw, nextIdRaw] =
    op;
  const endId = resolveId(endIdRaw) ?? startId;
  const newParentId = resolveId(newParentIdRaw) ?? store.getRootId();
  const prevId = resolveId(prevIdRaw);
  const nextId = resolveId(nextIdRaw);

  const startNode = store.getNode(startId);
  if (!startNode || !startNode.parentId || !startNode.slotName) return;

  // Remove from old parent
  const oldParentId = startNode.parentId;
  const oldSlot = startNode.slotName;
  const oldChildren = slots.get(oldParentId, oldSlot);
  const startIdx = oldChildren.indexOf(startId);
  const endIdx = oldChildren.indexOf(endId);
  if (startIdx < 0 || endIdx < 0) return;

  const movedIds = oldChildren.splice(startIdx, endIdx - startIdx + 1);

  // Update parent/slot on moved nodes (immutable update so reactive
  // subscribers see a new object reference — see NodeStore contract).
  for (const id of movedIds) {
    const node = store.getNode(id);
    if (node) {
      store._setNode(id, { ...node, parentId: newParentId, slotName });
    }
  }

  // Insert into new parent
  const newChildren = slots.get(newParentId, slotName);
  if (nextId) {
    const idx = newChildren.indexOf(nextId);
    if (idx >= 0) {
      newChildren.splice(idx, 0, ...movedIds);
    } else {
      newChildren.push(...movedIds);
    }
  } else if (prevId) {
    const idx = newChildren.indexOf(prevId);
    if (idx >= 0) {
      newChildren.splice(idx + 1, 0, ...movedIds);
    } else {
      newChildren.push(...movedIds);
    }
  } else {
    newChildren.push(...movedIds);
  }
}
