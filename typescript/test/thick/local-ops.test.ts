import { describe, it, expect } from "vitest";
import { createDocNode, getSlotChildren } from "../../src/thick/doc-node.js";
import type { DocNode } from "../../src/thick/doc-node.js";
import {
  createOpsAccumulator,
  createDiff,
  onSetStateInverse,
  onSetStateForward,
  applyOperations,
} from "../../src/thick/local-ops.js";
import type { WireOperations } from "../../src/types.js";

/** Link children into a parent's slot and register in map. */
function linkAndRegister(
  nodeMap: Map<string, DocNode>,
  parent: DocNode,
  slot: string,
  children: DocNode[],
): void {
  for (let i = 0; i < children.length; i++) {
    const c = children[i];
    c.parent = parent;
    c.slotName = slot;
    c.prevSibling = i > 0 ? children[i - 1] : null;
    c.nextSibling = i < children.length - 1 ? children[i + 1] : null;
    nodeMap.set(c.id, c);
  }
  parent.slotFirst.set(slot, children[0] ?? null);
  parent.slotLast.set(slot, children[children.length - 1] ?? null);
}

describe("state tracking", () => {
  it("records inverse on first set", () => {
    const node = createDocNode("n1", "T", []);
    node.state.title = "old";
    const diff = createDiff();
    const inv = createOpsAccumulator();

    onSetStateInverse(diff, inv, node, "title");
    expect(inv.state.n1.title).toBe("old");
  });

  it("does not overwrite inverse on second set", () => {
    const node = createDocNode("n1", "T", []);
    node.state.title = "original";
    const diff = createDiff();
    const inv = createOpsAccumulator();

    onSetStateInverse(diff, inv, node, "title");
    node.state.title = "changed";
    onSetStateInverse(diff, inv, node, "title");

    // Should still have the original value
    expect(inv.state.n1.title).toBe("original");
  });

  it("skips inverse for inserted nodes", () => {
    const node = createDocNode("n1", "T", []);
    const diff = createDiff();
    diff.inserted.add("n1");
    const inv = createOpsAccumulator();

    onSetStateInverse(diff, inv, node, "title");
    expect(inv.state.n1).toBeUndefined();
  });

  it("records forward on set", () => {
    const node = createDocNode("n1", "T", []);
    node.state.title = "new";
    const diff = createDiff();
    const fwd = createOpsAccumulator();
    const inv = createOpsAccumulator();
    inv.state.n1 = { title: "old" };

    onSetStateForward(diff, fwd, inv, node, "title");
    expect(fwd.state.n1.title).toBe("new");
    expect(diff.updated.has("n1")).toBe(true);
  });

  it("cleans up when reverted to original", () => {
    const node = createDocNode("n1", "T", []);
    node.state.title = "old"; // reverted back
    const diff = createDiff();
    const fwd = createOpsAccumulator();
    fwd.state.n1 = { title: "changed" };
    const inv = createOpsAccumulator();
    inv.state.n1 = { title: "old" };

    onSetStateForward(diff, fwd, inv, node, "title");
    expect(fwd.state.n1).toBeUndefined();
    expect(diff.updated.has("n1")).toBe(false);
  });
});

describe("applyOperations", () => {
  function makeTree() {
    const root = createDocNode("root", "Root", ["items"]);
    const a = createDocNode("a", "Item", []);
    a.state.label = "A";
    const b = createDocNode("b", "Item", []);
    b.state.label = "B";

    const nodeMap = new Map<string, DocNode>();
    nodeMap.set("root", root);
    linkAndRegister(nodeMap, root, "items", [a, b]);

    const createNode = (id: string, type: string) => {
      const n = createDocNode(id, type, []);
      nodeMap.set(id, n);
      return n;
    };

    const insertIntoSlot = (
      parent: DocNode,
      slotName: string,
      position: string,
      nodes: DocNode[],
      target?: DocNode,
    ) => {
      // Simplified insert for testing
      for (const n of nodes) {
        n.parent = parent;
        n.slotName = slotName;
      }

      if (position === "append") {
        const last = parent.slotLast.get(slotName) ?? null;
        for (const n of nodes) {
          if (last) {
            last.nextSibling = nodes[0];
            nodes[0].prevSibling = last;
          } else {
            parent.slotFirst.set(slotName, nodes[0]);
          }
        }
        // Link nodes among themselves
        for (let i = 0; i < nodes.length - 1; i++) {
          nodes[i].nextSibling = nodes[i + 1];
          nodes[i + 1].prevSibling = nodes[i];
        }
        parent.slotLast.set(slotName, nodes[nodes.length - 1]);
        if (!parent.slotFirst.get(slotName)) {
          parent.slotFirst.set(slotName, nodes[0]);
        }
      } else if (position === "before" && target) {
        const prev = target.prevSibling;
        nodes[0].prevSibling = prev;
        if (prev) prev.nextSibling = nodes[0];
        else parent.slotFirst.set(slotName, nodes[0]);
        for (let i = 0; i < nodes.length - 1; i++) {
          nodes[i].nextSibling = nodes[i + 1];
          nodes[i + 1].prevSibling = nodes[i];
        }
        nodes[nodes.length - 1].nextSibling = target;
        target.prevSibling = nodes[nodes.length - 1];
      } else if (position === "after" && target) {
        const next = target.nextSibling;
        target.nextSibling = nodes[0];
        nodes[0].prevSibling = target;
        for (let i = 0; i < nodes.length - 1; i++) {
          nodes[i].nextSibling = nodes[i + 1];
          nodes[i + 1].prevSibling = nodes[i];
        }
        nodes[nodes.length - 1].nextSibling = next;
        if (next) next.prevSibling = nodes[nodes.length - 1];
        else parent.slotLast.set(slotName, nodes[nodes.length - 1]);
      }
    };

    return { root, nodeMap, createNode, insertIntoSlot };
  }

  it("applies insert operation", () => {
    const { root, nodeMap, createNode, insertIntoSlot } = makeTree();

    const ops: WireOperations = {
      ordered: [[0, [["c", "Item"]], 0, "items", "b", 0]],
      state: { c: { label: "C" } },
    };

    applyOperations(nodeMap, root, ops, createNode, insertIntoSlot);

    expect(nodeMap.has("c")).toBe(true);
    expect(nodeMap.get("c")!.state.label).toBe("C");
    const children = getSlotChildren(root, "items");
    expect(children.map((n) => n.id)).toEqual(["a", "b", "c"]);
  });

  it("applies delete operation", () => {
    const { root, nodeMap, createNode, insertIntoSlot } = makeTree();

    const ops: WireOperations = {
      ordered: [[1, "a", 0]],
      state: {},
    };

    applyOperations(nodeMap, root, ops, createNode, insertIntoSlot);

    expect(nodeMap.has("a")).toBe(false);
    const children = getSlotChildren(root, "items");
    expect(children.map((n) => n.id)).toEqual(["b"]);
  });

  it("applies state patch", () => {
    const { root, nodeMap, createNode, insertIntoSlot } = makeTree();

    const ops: WireOperations = {
      ordered: [],
      state: { a: { label: "Updated" } },
    };

    applyOperations(nodeMap, root, ops, createNode, insertIntoSlot);
    expect(nodeMap.get("a")!.state.label).toBe("Updated");
  });

  it("applies move operation", () => {
    // Build a tree with two parents
    const root = createDocNode("root", "Root", ["groups"]);
    const g1 = createDocNode("g1", "Group", ["items"]);
    const g2 = createDocNode("g2", "Group", ["items"]);
    const item = createDocNode("item", "Item", []);

    const nodeMap = new Map<string, DocNode>();
    nodeMap.set("root", root);
    linkAndRegister(nodeMap, root, "groups", [g1, g2]);
    linkAndRegister(nodeMap, g1, "items", [item]);

    const createNode = (id: string, type: string) => {
      const n = createDocNode(id, type, []);
      nodeMap.set(id, n);
      return n;
    };

    const insertIntoSlot = (
      parent: DocNode,
      slotName: string,
      _position: string,
      nodes: DocNode[],
    ) => {
      for (const n of nodes) {
        n.parent = parent;
        n.slotName = slotName;
        const last = parent.slotLast.get(slotName) ?? null;
        n.prevSibling = last;
        if (last) last.nextSibling = n;
        else parent.slotFirst.set(slotName, n);
        parent.slotLast.set(slotName, n);
      }
    };

    const ops: WireOperations = {
      ordered: [[2, "item", 0, "g2", "items", 0, 0]],
      state: {},
    };

    applyOperations(nodeMap, root, ops, createNode, insertIntoSlot);

    expect(getSlotChildren(g1, "items")).toEqual([]);
    expect(getSlotChildren(g2, "items").map((n) => n.id)).toEqual(["item"]);
    expect(item.parent).toBe(g2);
  });

  it("handles empty operations", () => {
    const { root, nodeMap, createNode, insertIntoSlot } = makeTree();
    const ops: WireOperations = { ordered: [], state: {} };
    applyOperations(nodeMap, root, ops, createNode, insertIntoSlot);
    expect(getSlotChildren(root, "items").map((n) => n.id)).toEqual(["a", "b"]);
  });
});
