import { describe, it, expect } from "vitest";
import {
  setField,
  deleteNode,
  moveNode,
  createNode,
  undo,
  redo,
} from "../src/operations.js";

describe("setField", () => {
  it("builds a state-only op message", () => {
    const msg = setField("n1", "title", "Hello");
    expect(msg.type).toBe("op");
    expect(msg.operations.ordered).toEqual([]);
    expect(msg.operations.state).toEqual({
      n1: { title: "Hello" },
    });
  });

  it("passes numbers through as native JSON", () => {
    const msg = setField("n1", "count", 42);
    expect(msg.operations.state.n1.count).toBe(42);
  });

  it("passes objects through as native JSON", () => {
    const msg = setField("n1", "color", { r: 255, g: 0, b: 0 });
    expect(msg.operations.state.n1.color).toEqual({ r: 255, g: 0, b: 0 });
  });
});

describe("deleteNode", () => {
  it("builds a delete op", () => {
    const msg = deleteNode("n1");
    expect(msg.type).toBe("op");
    expect(msg.operations.ordered).toEqual([[1, "n1", 0]]);
    expect(msg.operations.state).toEqual({});
  });
});

describe("moveNode", () => {
  it("builds a move op with append (no prev/next)", () => {
    const msg = moveNode("n1", "parent", "children");
    expect(msg.operations.ordered).toEqual([
      [2, "n1", 0, "parent", "children", 0, 0],
    ]);
  });

  it("builds a move op with prev", () => {
    const msg = moveNode("n1", "parent", "children", "prev1");
    expect(msg.operations.ordered[0][5]).toBe("prev1");
    expect(msg.operations.ordered[0][6]).toBe(0);
  });

  it("builds a move op with next", () => {
    const msg = moveNode("n1", "parent", "children", undefined, "next1");
    expect(msg.operations.ordered[0][5]).toBe(0);
    expect(msg.operations.ordered[0][6]).toBe("next1");
  });
});

describe("createNode", () => {
  it("builds a create message", () => {
    const msg = createNode(
      "Annotation",
      { label: "test" },
      "root",
      "annotations",
    );
    expect(msg.type).toBe("create");
    expect(msg.node_type).toBe("Annotation");
    expect(msg.state).toEqual({ label: "test" });
    expect(msg.parent_id).toBe("root");
    expect(msg.slot).toBe("annotations");
    expect(msg.position).toBe("append");
  });

  it("accepts custom position", () => {
    const msg = createNode("Annotation", {}, "root", "annotations", "prepend");
    expect(msg.position).toBe("prepend");
  });
});

describe("undo/redo", () => {
  it("builds undo message (default 1 step)", () => {
    expect(undo()).toEqual({ type: "undo" });
  });

  it("builds undo message with steps", () => {
    expect(undo(3)).toEqual({ type: "undo", steps: 3 });
  });

  it("builds redo message (default 1 step)", () => {
    expect(redo()).toEqual({ type: "redo" });
  });

  it("builds redo message with steps", () => {
    expect(redo(2)).toEqual({ type: "redo", steps: 2 });
  });
});
