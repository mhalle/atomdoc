/**
 * DocNode — linked-list tree node for the local document model.
 *
 * Each node lives in a doubly-linked sibling list within a named slot
 * of its parent. Parent nodes track first/last child per slot.
 */

export interface DocNode {
  readonly id: string;
  readonly type: string;
  state: Record<string, unknown>;
  parent: DocNode | null;
  slotName: string | null;
  prevSibling: DocNode | null;
  nextSibling: DocNode | null;
  /** slot name → first child (null if empty) */
  slotFirst: Map<string, DocNode | null>;
  /** slot name → last child (null if empty) */
  slotLast: Map<string, DocNode | null>;
  /** ordered slot names (from schema) */
  slotOrder: string[];
}

/**
 * Create a new detached DocNode.
 *
 * @param id - Node ID
 * @param type - Node type name
 * @param slotOrder - Ordered slot names from the schema
 */
export function createDocNode(
  id: string,
  type: string,
  slotOrder: string[],
): DocNode {
  const slotFirst = new Map<string, DocNode | null>();
  const slotLast = new Map<string, DocNode | null>();
  for (const name of slotOrder) {
    slotFirst.set(name, null);
    slotLast.set(name, null);
  }

  return {
    id,
    type,
    state: {},
    parent: null,
    slotName: null,
    prevSibling: null,
    nextSibling: null,
    slotFirst,
    slotLast,
    slotOrder,
  };
}

/**
 * Reset a node to the detached, empty state `createDocNode` returns, in
 * place — so a handle to it stays valid when the node is revived.
 */
export function resetDocNode(node: DocNode): void {
  node.state = {};
  node.parent = null;
  node.slotName = null;
  node.prevSibling = null;
  node.nextSibling = null;
  for (const name of node.slotOrder) {
    node.slotFirst.set(name, null);
    node.slotLast.set(name, null);
  }
}

/** Get ordered list of children in a slot. */
export function getSlotChildren(node: DocNode, slotName: string): DocNode[] {
  const result: DocNode[] = [];
  let child = node.slotFirst.get(slotName) ?? null;
  while (child !== null) {
    result.push(child);
    child = child.nextSibling;
  }
  return result;
}
