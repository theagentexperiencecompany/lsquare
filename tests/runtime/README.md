Tests that CI copies into a fresh directory, next to the packed `lsquare` installed alone (and, for
the wrapper, the MCP SDK at its pinned version and zod), and runs with `node --test` on each Node
version it supports: Node's own test runner, since Vitest doesn't run on Node 18 or 20.

`types/` runs once, on Node 24: the packed declarations, installed with the TypeScript and
`@types/node` versions the lockfile has and both MCP SDKs, compiled by consumer projects under
node10, node16, nodenext and bundler, a client from the CommonJS build passed to the ESM wrapper
and the other way round included.
