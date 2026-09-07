/**
 * Lamport-timestamp node ID generation — port of _id.py.
 *
 * IDs have the format: {sessionId}.{clock}
 * - sessionId: base64(elapsed_ms) + random(5)
 * - clock: monotonically incrementing base64 counter
 */

// RFC 4648 §5 alphabet, lexicographically sorted
export const BASE64_ALPHABET =
  "-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz";
const ALPH_LEN = 64;
const FIRST_CHAR = BASE64_ALPHABET[0];

// O(1) char → index map
const IDX = new Map<string, number>();
for (let i = 0; i < BASE64_ALPHABET.length; i++) {
  IDX.set(BASE64_ALPHABET[i], i);
}

export function numberToBase64(num: number): string {
  if (num === 0) return FIRST_CHAR;
  const result: string[] = [];
  while (num > 0) {
    result.push(BASE64_ALPHABET[num % 64]);
    num = Math.floor(num / 64);
  }
  result.reverse();
  return result.join("");
}

export function randomBase64(length: number): string {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => BASE64_ALPHABET[b % 64]).join("");
}

export function incrementBase64(s: string): string {
  const chars = s.split("");
  for (let i = chars.length - 1; i >= 0; i--) {
    const idx = IDX.get(chars[i])!;
    if (idx !== ALPH_LEN - 1) {
      chars[i] = BASE64_ALPHABET[idx + 1];
      for (let j = i + 1; j < chars.length; j++) {
        chars[j] = FIRST_CHAR;
      }
      return chars.join("");
    }
  }
  // All digits maxed — prepend next digit
  return BASE64_ALPHABET[1] + FIRST_CHAR.repeat(s.length);
}

/**
 * Decode a ULID string to its millisecond timestamp.
 * ULID uses Crockford base32; the first 10 chars encode 48-bit ms timestamp.
 */
function ulidToMs(ulid: string): number {
  const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  const upper = ulid.toUpperCase();
  let ms = 0;
  for (let i = 0; i < 10; i++) {
    const idx = CROCKFORD.indexOf(upper[i]);
    if (idx < 0) throw new Error(`Invalid ULID character: ${upper[i]}`);
    ms = ms * 32 + idx;
  }
  return ms;
}

/**
 * Create a node ID factory for a given document.
 * Returns a function that produces monotonically increasing IDs.
 */
export function createNodeIdFactory(docId: string): () => string {
  const createdAtMs = ulidToMs(docId);
  const msPassed = Math.max(0, Date.now() - createdAtMs);
  const msBase64 = numberToBase64(msPassed);
  const randomPart = randomBase64(5);
  const sessionId = msBase64 + randomPart;

  let clock = FIRST_CHAR;

  return () => {
    const nodeId = `${sessionId}.${clock}`;
    clock = incrementBase64(clock);
    return nodeId;
  };
}
