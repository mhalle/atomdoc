import { describe, it, expect } from "vitest";
import { Transaction } from "../src/transaction.js";

describe("Transaction", () => {
  it("starts open and clean", () => {
    const tx = new Transaction();
    expect(tx.open).toBe(true);
    expect(tx.dirty).toBe(false);
  });

  it("buffers setField operations", () => {
    const tx = new Transaction();
    tx.setField("n1", "title", "Hello");
    expect(tx.dirty).toBe(true);

    const msg = tx.toMessage();
    expect(msg.operations.state).toEqual({
      n1: { title: "Hello" },
    });
    expect(msg.operations.ordered).toEqual([]);
  });

  it("merges multiple setField on same node", () => {
    const tx = new Transaction();
    tx.setField("n1", "title", "Hello");
    tx.setField("n1", "color", { r: 255 });

    const msg = tx.toMessage();
    expect(msg.operations.state.n1).toEqual({
      title: "Hello",
      color: { r: 255 },
    });
  });

  it("last setField wins for same field", () => {
    const tx = new Transaction();
    tx.setField("n1", "title", "First");
    tx.setField("n1", "title", "Second");

    const msg = tx.toMessage();
    expect(msg.operations.state.n1.title).toBe("Second");
  });

  it("buffers deleteNode", () => {
    const tx = new Transaction();
    tx.deleteNode("n1");

    const msg = tx.toMessage();
    expect(msg.operations.ordered).toEqual([[1, "n1", 0]]);
  });

  it("buffers moveNode", () => {
    const tx = new Transaction();
    tx.moveNode("n1", "p2", "children", "prev1");

    const msg = tx.toMessage();
    expect(msg.operations.ordered).toEqual([
      [2, "n1", 0, "p2", "children", "prev1", 0],
    ]);
  });

  it("combines ordered and state operations", () => {
    const tx = new Transaction();
    tx.setField("n1", "title", "Updated");
    tx.deleteNode("n2");

    const msg = tx.toMessage();
    expect(msg.operations.ordered).toHaveLength(1);
    expect(Object.keys(msg.operations.state)).toHaveLength(1);
  });

  it("supports chaining", () => {
    const tx = new Transaction();
    tx.setField("n1", "a", 1).setField("n1", "b", 2).deleteNode("n2");

    const msg = tx.toMessage();
    expect(msg.operations.state.n1).toEqual({ a: 1, b: 2 });
    expect(msg.operations.ordered).toHaveLength(1);
  });

  it("abort clears everything", () => {
    const tx = new Transaction();
    tx.setField("n1", "title", "Hello");
    tx.abort();

    expect(tx.open).toBe(false);
    expect(tx.dirty).toBe(false);
  });

  it("throws on use after commit", () => {
    const tx = new Transaction();
    tx._commit();
    expect(() => tx.setField("n1", "x", 1)).toThrow("already committed");
  });

  it("throws on use after abort", () => {
    const tx = new Transaction();
    tx.abort();
    expect(() => tx.setField("n1", "x", 1)).toThrow("already aborted");
  });

  it("throws on double commit", () => {
    const tx = new Transaction();
    tx._commit();
    expect(() => tx._commit()).toThrow("already committed");
  });

  it("throws on double abort", () => {
    const tx = new Transaction();
    tx.abort();
    expect(() => tx.abort()).toThrow("already aborted");
  });
});
