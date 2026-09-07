import { describe, it, expect } from "vitest";
import { createDocNode, getSlotChildren } from "../../src/thick/doc-node.js";

describe("createDocNode", () => {
  it("creates a detached node", () => {
    const node = createDocNode("n1", "Page", ["children"]);
    expect(node.id).toBe("n1");
    expect(node.type).toBe("Page");
    expect(node.parent).toBeNull();
    expect(node.slotName).toBeNull();
    expect(node.prevSibling).toBeNull();
    expect(node.nextSibling).toBeNull();
  });

  it("initializes slots from slotOrder", () => {
    const node = createDocNode("n1", "Page", ["annotations", "notes"]);
    expect(node.slotOrder).toEqual(["annotations", "notes"]);
    expect(node.slotFirst.get("annotations")).toBeNull();
    expect(node.slotLast.get("annotations")).toBeNull();
    expect(node.slotFirst.get("notes")).toBeNull();
  });

  it("initializes empty state", () => {
    const node = createDocNode("n1", "Page", []);
    expect(node.state).toEqual({});
  });

  it("handles no slots", () => {
    const node = createDocNode("n1", "Leaf", []);
    expect(node.slotOrder).toEqual([]);
    expect(node.slotFirst.size).toBe(0);
  });
});

describe("getSlotChildren", () => {
  it("returns empty for no children", () => {
    const parent = createDocNode("p", "Parent", ["items"]);
    expect(getSlotChildren(parent, "items")).toEqual([]);
  });

  it("returns children in order", () => {
    const parent = createDocNode("p", "Parent", ["items"]);
    const a = createDocNode("a", "Item", []);
    const b = createDocNode("b", "Item", []);
    const c = createDocNode("c", "Item", []);

    // Manually link (normally done by LocalDoc)
    a.parent = parent;
    a.slotName = "items";
    b.parent = parent;
    b.slotName = "items";
    c.parent = parent;
    c.slotName = "items";
    a.nextSibling = b;
    b.prevSibling = a;
    b.nextSibling = c;
    c.prevSibling = b;
    parent.slotFirst.set("items", a);
    parent.slotLast.set("items", c);

    const children = getSlotChildren(parent, "items");
    expect(children.map((n) => n.id)).toEqual(["a", "b", "c"]);
  });

  it("returns empty for unknown slot", () => {
    const parent = createDocNode("p", "Parent", ["items"]);
    expect(getSlotChildren(parent, "bogus")).toEqual([]);
  });
});
