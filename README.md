# LSquare SDKs

Record every tool call your MCP server handles, and the agent turns behind them, in
[LSquare](https://lsquare.io). LSquare is an invite-only beta: [request access](https://lsquare.io)
to get an API key.

| Package | Language | Install | Docs |
| --- | --- | --- | --- |
| [`lsquare`](packages/lsquare) | TypeScript and JavaScript: Node 18+, Cloudflare Workers, Bun | `npm install lsquare` | [README](packages/lsquare/README.md), [reference](packages/lsquare/REFERENCE.md) |
| [`lsquare`](packages/python) | Python 3.10+ | `pip install lsquare` | [README](packages/python/README.md), [reference](packages/python/REFERENCE.md) |
| [`@lsquare/node`](packages/node) | Node, 0.1 | `npm install @lsquare/node` | [README](packages/node/README.md) |

Both `lsquare` packages send the same events, and `@lsquare/node` is the 0.1 SDK, kept for the
code already using it: new servers use `lsquare`.

## What leaves your process

Each event holds the tool's name, status and duration, and by default its inputs and results,
after the SDK has shortened long values and redacted common formats of emails, phone numbers, card
numbers, API keys and tokens, credentials in URLs and IP addresses. You choose what else it holds,
and what it never sends: each README says what leaves your process and has a privacy checklist
([TypeScript](packages/lsquare/README.md#privacy-checklist),
[Python](packages/python/README.md#privacy-checklist)). Events are stored in the United States.

## In this repository

- `packages/lsquare`, `packages/python`, `packages/node`: the SDKs.
- `packages/contract`: the event format both `lsquare` packages share, with its validator and the
  test cases every SDK runs.
- `tests/frameworks`: the TypeScript SDK on Workers, Node frameworks and Bun.

[CONTRIBUTING.md](CONTRIBUTING.md) says how to build and test, and [SECURITY.md](SECURITY.md) how to
report a vulnerability.

## Licence

MIT, in [LICENSE](LICENSE).
