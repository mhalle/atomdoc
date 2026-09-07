/**
 * Performance benchmarks for atomdoc-ts. Skipped unless BENCH=1:
 *
 *   BENCH=1 npx vitest run test/perf/bench.test.ts
 *   BENCH=1 SIZES=1000,4000 npx vitest run test/perf/bench.test.ts
 *
 * Each scenario is timed at every size; the `x` column is the time ratio
 * between consecutive sizes (linear work shows the size ratio, quadratic
 * work shows its square). See test/perf/scaling.test.ts for the
 * assertions that run in the normal suite.
 */

import { describe, it } from "vitest";
import { scenarios } from "./scenarios.js";

const sizes = (process.env.SIZES ?? "1000,4000").split(",").map(Number);

describe.skipIf(!process.env.BENCH)("benchmarks", () => {
  it("runs the sweep", () => {
    const rows: string[] = [];
    rows.push(`${"scenario".padEnd(28)}${"n".padStart(7)}${"ms".padStart(10)}${"us/n".padStart(9)}${"x".padStart(6)}  description`);
    for (const [name, sc] of Object.entries(scenarios)) {
      let prev: number | undefined;
      for (const size of sizes) {
        const n = Math.max(1, Math.round(size * (sc.scale ?? 1)));
        sc.run(n); // warm up
        const secs = Math.min(sc.run(n), sc.run(n));
        const ratio = prev ? (secs / prev).toFixed(1) : "";
        rows.push(`${name.padEnd(28)}${String(n).padStart(7)}${(secs * 1000).toFixed(1).padStart(10)}${((secs / n) * 1e6).toFixed(1).padStart(9)}${ratio.padStart(6)}  ${sc.desc}`);
        prev = secs;
      }
    }
    console.log("\n" + rows.join("\n") + "\n");
  }, 600000);
});
