/**
 * DocNode — linked-list tree node for the local document model.
 *
 * Each node lives in a doubly-linked sibling list within a named slot
 * of its parent. Parent nodes track first/last child per slot.
 */

/**
 * A node this client holds by identity only: an ancestor of its scope, a
 * reference target outside it, or a child past its depth. Reading or
 * writing the node's state, or editing it or under it, is an error.
 */
export class OutOfScopeError extends Error {
  readonly nodeId: string;
  constructor(nodeId: string, what = "is a stub: outside this client's scope") {
    super(`Node '${nodeId}' ${what}`);
    this.name = "OutOfScopeError";
    this.nodeId = nodeId;
  }
}

/**
 * The state object of a stub: every access throws. A stub must never
 * read as an empty node, so this cannot be a plain `{}`.
 */
function stubState(id: string): Record<string, unknown> {
  const fail = (): never => {
    throw new OutOfScopeError(id);
  };
  return new Proxy(
    {},
    {
      // Symbol probes (inspection, `then` checks, tags) see nothing;
      // any named field, in either direction, is an error.
      get: (_t, key) => (typeof key === "symbol" ? undefined : fail()),
      set: fail,
      has: fail,
      deleteProperty: fail,
      ownKeys: fail,
      getOwnPropertyDescriptor: fail,
      defineProperty: fail,
    },
  );
}

export interface DocNode {
  readonly id: string;
  readonly type: string;
  /**
   * True while the node is a stub (see {@link OutOfScopeError}); then
   * `state` throws on every access.
   */
  stub: boolean;
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
 * @param stub - Create a stub: identity only, no readable state
 */
export function createDocNode(
  id: string,
  type: string,
  slotOrder: string[],
  stub = false,
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
    stub,
    state: stub ? stubState(id) : {},
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
 * place — so a handle to it stays valid when the node is revived. A
 * stub stays a stub.
 */
export function resetDocNode(node: DocNode): void {
  node.state = node.stub ? stubState(node.id) : {};
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

/**
 * Turn a stub into a full node in place, with `state` as its state: the
 * node entered the client's scope. A handle to the stub stays valid.
 */
export function fillStub(node: DocNode, state: Record<string, unknown>): void {
  node.stub = false;
  node.state = { ...state };
}

/**
 * Turn a full node into a stub in place: it left the client's scope but
 * is still referenced. Its state is dropped.
 */
export function makeStub(node: DocNode): void {
  node.stub = true;
  node.state = stubState(node.id);
}
