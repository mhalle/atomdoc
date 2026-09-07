import { describe, it, expect } from "vitest";
import { createDocNode } from "../../src/thick/doc-node.js";
import {
  iterRange,
  detachRange,
  descendants,
  descendantsInclusive,
} from "../../src/thick/local-range.js";
import type { DocNode } from "../../src/thick/doc-node.js";

/** Helper: link children into a parent's slot. */
function linkChildren(parent: DocNode, slot: string, children: DocNode[]): void {
  for (let i = 0; i < children.length; i++) {
    const c = children[i];
    c.parent = parent;
    c.slotName = slot;
    c.prevSibling = i > 0 ? children[i - 1] : null;
    c.nextSibling = i < children.length - 1 ? children[i + 1] : null;
  }
  parent.slotFirst.set(slot, children[0] ?? null);
  parent.slotLast.set(slot, children[children.length - 1] ?? null);
}

describe("iterRange", () => {
  it("iterates single node", () => {
    const a = createDocNode("a", "N", []);
    expect(iterRange(a, a).map((n) => n.id)).toEqual(["a"]);
  });

  it("iterates multiple siblings", () => {
    const parent = createDocNode("p", "P", ["items"]);
    const a = createDocNode("a", "N", []);
    const b = createDocNode("b", "N", []);
    const c = createDocNode("c", "N", []);
    linkChildren(parent, "items", [a, b, c]);

    expect(iterRange(a, c).map((n) => n.id)).toEqual(["a", "b", "c"]);
    expect(iterRange(a, b).map((n) => n.id)).toEqual(["a", "b"]);
    expect(iterRange(b, c).map((n) => n.id)).toEqual(["b", "c"]);
  });
});

describe("detachRange", () => {
  it("detaches middle node", () => {
    const parent = createDocNode("p", "P", ["items"]);
    const a = createDocNode("a", "N", []);
    const b = createDocNode("b", "N", []);
    const c = createDocNode("c", "N", []);
    linkChildren(parent, "items", [a, b, c]);

    detachRange(b, b);

    expect(a.nextSibling).toBe(c);
    expect(c.prevSibling).toBe(a);
    expect(b.prevSibling).toBeNull();
    expect(b.nextSibling).toBeNull();
  });

  it("detaches first node", () => {
    const parent = createDocNode("p", "P", ["items"]);
    const a = createDocNode("a", "N", []);
    const b = createDocNode("b", "N", []);
    linkChildren(parent, "items", [a, b]);

    detachRange(a, a);

    expect(parent.slotFirst.get("items")).toBe(b);
    expect(b.prevSibling).toBeNull();
  });

  it("detaches last node", () => {
    const parent = createDocNode("p", "P", ["items"]);
    const a = createDocNode("a", "N", []);
    const b = createDocNode("b", "N", []);
    linkChildren(parent, "items", [a, b]);

    detachRange(b, b);

    expect(parent.slotLast.get("items")).toBe(a);
    expect(a.nextSibling).toBeNull();
  });

  it("detaches a range", () => {
    const parent = createDocNode("p", "P", ["items"]);
    const a = createDocNode("a", "N", []);
    const b = createDocNode("b", "N", []);
    const c = createDocNode("c", "N", []);
    const d = createDocNode("d", "N", []);
    linkChildren(parent, "items", [a, b, c, d]);

    detachRange(b, c);

    expect(a.nextSibling).toBe(d);
    expect(d.prevSibling).toBe(a);
  });

  it("detaches all children", () => {
    const parent = createDocNode("p", "P", ["items"]);
    const a = createDocNode("a", "N", []);
    const b = createDocNode("b", "N", []);
    linkChildren(parent, "items", [a, b]);

    detachRange(a, b);

    expect(parent.slotFirst.get("items")).toBeNull();
    expect(parent.slotLast.get("items")).toBeNull();
  });
});

describe("descendants", () => {
  it("returns empty for leaf", () => {
    const leaf = createDocNode("l", "N", []);
    expect(descendants(leaf)).toEqual([]);
  });

  it("returns children depth-first", () => {
    const root = createDocNode("r", "R", ["items"]);
    const a = createDocNode("a", "N", ["sub"]);
    const b = createDocNode("b", "N", []);
    const a1 = createDocNode("a1", "N", []);
    linkChildren(root, "items", [a, b]);
    linkChildren(a, "sub", [a1]);

    const ids = descendants(root).map((n) => n.id);
    expect(ids).toEqual(["a", "a1", "b"]);
  });
});

describe("descendantsInclusive", () => {
  it("includes the node itself", () => {
    const root = createDocNode("r", "R", ["items"]);
    const a = createDocNode("a", "N", []);
    linkChildren(root, "items", [a]);

    const ids = descendantsInclusive(root).map((n) => n.id);
    expect(ids).toEqual(["r", "a"]);
  });
});
