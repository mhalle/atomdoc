# AtomDoc

A schema-driven document model with a server-authoritative sync protocol:
a Python document and session (`python/`) and a TypeScript client library
with thin and thick clients (`typescript/`). The wire protocol both sides
implement is specified once, in [PROTOCOL.md](PROTOCOL.md).

| Directory | Package | Docs |
|---|---|---|
| `python/` | `atomdoc` on PyPI | [README](python/README.md), [CHANGELOG](python/CHANGELOG.md) |
| `typescript/` | `atomdoc-ts` on npm | [README](typescript/README.md), [CHANGELOG](typescript/CHANGELOG.md) |

The two packages are versioned together: a release tags the repository
once (`vX.Y.Z`) and publishes both. Tags from before the repositories were
joined are kept as `vX.Y.Z` (Python) and `ts-vX.Y.Z` (TypeScript).

## Development

```bash
cd python && uv run pytest -q && uv run ruff check src tests benchmarks
cd typescript && npm ci && npx tsc --noEmit && npx vitest run
```

The TypeScript suite includes end-to-end tests that start the Python
server from `python/` with `uv`, so both toolchains are needed to run it
in full. Performance sweeps: `python/benchmarks/bench.py` and
`BENCH=1 npx vitest run test/perf/bench.test.ts` under `typescript/`.
