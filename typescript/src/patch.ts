/**
 * Patch applier — updates the NodeStore from wire operations.
 */

import type { NodeStore } from "./store.js";
import type { StoreNode } from "./types.js";
import type { InsertPair, WireOperations } from "./types.js";

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
        case 3: {
          // Becomes a stub: its state goes, it stays where it is.
          const node = store.getNode(op[1]);
          if (node) store._setNode(op[1], { ...node, state: {}, stub: true });
          break;
        }
        case 4:
          // Leaves the view with its subtree (it still exists).
          exitNode(store, slots, op[1]);
          break;
        case 5: {
          // Is now a detached stub.
          const [, id, type] = op;
          const node = store.getNode(id);
          if (node && node.parentId === null && id !== store.getRootId()) break;
          if (node) exitNode(store, slots, id);
          store._setNode(id, { id, type, state: {}, slots: {}, parentId: null, slotName: null, stub: true });
          break;
        }
        case 6: {
          // A stub in the tree fills; its state follows in this patch.
          const node = store.getNode(op[1]);
          if (node) {
            const { stub: _stub, ...rest } = node;
            store._setNode(op[1], rest);
          }
          break;
        }
        default:
          throw new Error(`Unknown operation code: ${String((op as unknown[])[0])}`);
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
  op: [0, InsertPair[], string | 0, string, string | 0, string | 0],
): void {
  const [, nodePairs, parentIdRaw, slotName, prevIdRaw, nextIdRaw] = op;
  const parentId = resolveId(parentIdRaw) ?? store.getRootId();
  const prevId = resolveId(prevIdRaw);
  const nextId = resolveId(nextIdRaw);

  const parent = store.getNode(parentId);
  if (!parent) return;

  // Create new nodes
  const newIds: string[] = [];
  for (const pair of nodePairs) {
    const [id, type] = pair;
    const existing = store.getNode(id);
    if (!existing) {
      const node: StoreNode = { id, type, state: {}, slots: {}, parentId, slotName };
      if (pair.length === 3) node.stub = true;
      store._setNode(id, node);
    } else if (existing.parentId === null && id !== store.getRootId()) {
      // A detached stub taking its place in the tree, filled if the
      // pair is full (its state follows in the same patch).
      const { stub, ...rest } = existing;
      const node: StoreNode = { ...rest, parentId, slotName };
      if (pair.length === 3 && stub) node.stub = true;
      store._setNode(id, node);
    } else {
      continue; // already in the tree: nothing to place
    }
    newIds.push(id);
  }

  // Insert into parent's slot at the right position. The common case,
  // appending after the current last child, is O(1); the list is written
  // to the store once per patch.
  const children = slots.get(parentId, slotName);

  placeAfter(children, newIds, prevId, nextId);
}

/**
 * Put `ids` after `prevId` if it is in the list, else before `nextId` if
 * it is, else at the end: the same rule the document models apply.
 */
function placeAfter(list: string[], ids: string[], prevId: string | null, nextId: string | null): void {
  if (prevId) {
    if (list[list.length - 1] === prevId) {
      list.push(...ids);
      return;
    }
    const idx = list.indexOf(prevId);
    if (idx >= 0) {
      list.splice(idx + 1, 0, ...ids);
      return;
    }
  }
  if (nextId) {
    const idx = list.indexOf(nextId);
    if (idx >= 0) {
      list.splice(idx, 0, ...ids);
      return;
    }
  }
  list.push(...ids);
}

function applyDelete(
  store: NodeStore,
  slots: SlotEdits,
  op: [1, string, string | 0],
): void {
  const [, startId, endIdRaw] = op;
  const endId = resolveId(endIdRaw) ?? startId;

  const startNode = store.getNode(startId);
  if (!startNode) return;
  if (!startNode.parentId || !startNode.slotName) {
    // A detached stub (a reference target outside every held chain) has
    // no slot to leave.
    if (startNode.stub && startNode.id !== store.getRootId()) store._removeNode(startId);
    return;
  }

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

/** Take a node out of its slot (if it is in one) and drop its subtree. */
function exitNode(store: NodeStore, slots: SlotEdits, id: string): void {
  const node = store.getNode(id);
  if (!node) return;
  if (node.parentId && node.slotName) {
    const children = slots.get(node.parentId, node.slotName);
    const idx = children.indexOf(id);
    if (idx >= 0) children.splice(idx, 1);
  }
  removeRecursive(store, slots, id);
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
  placeAfter(newChildren, movedIds, prevId, nextId);
}
