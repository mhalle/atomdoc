/**
 * Range operations on the linked-list tree — port of _range.py.
 */

import type { DocNode } from "./doc-node.js";

/** Iterate siblings from start to end (inclusive). */
export function iterRange(start: DocNode, end: DocNode): DocNode[] {
  const result: DocNode[] = [];
  let current: DocNode | null = start;
  while (current !== null) {
    result.push(current);
    if (current === end) break;
    current = current.nextSibling;
  }
  return result;
}

/**
 * Detach a contiguous range of siblings from their parent.
 * Fixes the parent's slotFirst/slotLast and surrounding sibling pointers.
 */
export function detachRange(start: DocNode, end: DocNode): void {
  const parent = start.parent;
  const slotName = start.slotName;
  if (!parent || !slotName) return;

  const oldPrev = start.prevSibling;
  const oldNext = end.nextSibling;

  // Fix surrounding siblings
  if (oldPrev) {
    oldPrev.nextSibling = oldNext;
  } else {
    parent.slotFirst.set(slotName, oldNext);
  }

  if (oldNext) {
    oldNext.prevSibling = oldPrev;
  } else {
    parent.slotLast.set(slotName, oldPrev);
  }

  // Detach the range endpoints
  start.prevSibling = null;
  end.nextSibling = null;
}

/**
 * All descendants of a node, depth-first in document order (excludes the
 * node itself). Iterative: a chain thousands of nodes deep must not
 * overflow the call stack.
 */
export function descendants(node: DocNode): DocNode[] {
  const result: DocNode[] = [];
  const stack: DocNode[] = [];
  pushChildren(stack, node);
  while (stack.length > 0) {
    const current = stack.pop()!;
    result.push(current);
    pushChildren(stack, current);
  }
  return result;
}

/** Node and all its descendants (depth-first, document order). */
export function descendantsInclusive(node: DocNode): DocNode[] {
  const result: DocNode[] = [];
  const stack: DocNode[] = [node];
  while (stack.length > 0) {
    const current = stack.pop()!;
    result.push(current);
    pushChildren(stack, current);
  }
  return result;
}

/** Push a node's children so they pop in document order. */
function pushChildren(stack: DocNode[], node: DocNode): void {
  for (let i = node.slotOrder.length - 1; i >= 0; i--) {
    let child = node.slotLast.get(node.slotOrder[i]) ?? null;
    while (child !== null) {
      stack.push(child);
      child = child.prevSibling;
    }
  }
}
