import { describe, it, expect } from "vitest";
import {
  numberToBase64,
  incrementBase64,
  randomBase64,
  createNodeIdFactory,
  BASE64_ALPHABET,
} from "../../src/thick/node-id.js";

describe("numberToBase64", () => {
  it("converts 0", () => {
    expect(numberToBase64(0)).toBe(BASE64_ALPHABET[0]);
  });

  it("converts small numbers", () => {
    expect(numberToBase64(1)).toBe(BASE64_ALPHABET[1]);
    expect(numberToBase64(63)).toBe(BASE64_ALPHABET[63]);
  });

  it("converts multi-digit numbers", () => {
    expect(numberToBase64(64)).toBe(BASE64_ALPHABET[1] + BASE64_ALPHABET[0]);
  });
});

describe("incrementBase64", () => {
  it("increments simple", () => {
    const first = BASE64_ALPHABET[0];
    const second = BASE64_ALPHABET[1];
    expect(incrementBase64(first)).toBe(second);
  });

  it("carries over", () => {
    const last = BASE64_ALPHABET[63];
    const result = incrementBase64(last);
    expect(result).toBe(BASE64_ALPHABET[1] + BASE64_ALPHABET[0]);
    expect(result.length).toBe(2);
  });

  it("increments middle character", () => {
    const a = BASE64_ALPHABET[5];
    const b = BASE64_ALPHABET[6];
    const first = BASE64_ALPHABET[0];
    expect(incrementBase64(a + BASE64_ALPHABET[63])).toBe(b + first);
  });
});

describe("randomBase64", () => {
  it("generates correct length", () => {
    expect(randomBase64(3).length).toBe(3);
    expect(randomBase64(10).length).toBe(10);
  });

  it("only uses valid characters", () => {
    const s = randomBase64(100);
    for (const ch of s) {
      expect(BASE64_ALPHABET).toContain(ch);
    }
  });
});

describe("createNodeIdFactory", () => {
  // Use a valid lowercase ULID as doc ID
  const docId = "01jqp00000000000000000000";

  it("generates IDs in session.clock format", () => {
    const gen = createNodeIdFactory(docId);
    const id = gen();
    expect(id).toContain(".");
    const [session, clock] = id.split(".");
    expect(session.length).toBeGreaterThan(0);
    expect(clock.length).toBeGreaterThan(0);
  });

  it("generates unique IDs", () => {
    const gen = createNodeIdFactory(docId);
    const ids = new Set<string>();
    for (let i = 0; i < 100; i++) {
      ids.add(gen());
    }
    expect(ids.size).toBe(100);
  });

  it("generates monotonically increasing clocks", () => {
    const gen = createNodeIdFactory(docId);
    const id1 = gen();
    const id2 = gen();
    const clock1 = id1.split(".")[1];
    const clock2 = id2.split(".")[1];
    expect(clock2 > clock1).toBe(true);
  });

  it("session ID is consistent within a factory", () => {
    const gen = createNodeIdFactory(docId);
    const session1 = gen().split(".")[0];
    const session2 = gen().split(".")[0];
    expect(session1).toBe(session2);
  });

  it("different factories have different session IDs", () => {
    const gen1 = createNodeIdFactory(docId);
    const gen2 = createNodeIdFactory(docId);
    const session1 = gen1().split(".")[0];
    const session2 = gen2().split(".")[0];
    // Different random parts → different sessions (with very high probability)
    expect(session1).not.toBe(session2);
  });
});
