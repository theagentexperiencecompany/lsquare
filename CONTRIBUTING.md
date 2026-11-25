# Contributing

Issues are welcome: open one for a bug, or for a change you'd like. For now only the repository's
collaborators can open pull requests. For a vulnerability, follow [SECURITY.md](SECURITY.md)
instead. By contributing, you agree that your contribution is licensed under the
[MIT licence](LICENSE).

## TypeScript

Node 22.18+, 24.11+ or 26+ (CI uses 24), from the repository's root:

```sh
npm ci --ignore-scripts
npm run build
npm test
npm run typecheck
```

`npm run build` comes first: `tests/frameworks` imports the built `lsquare`.
`npm run test:bun -w @lsquare/frameworks-test` runs the Bun checks too, with the Bun that npm
installed.

## Python

[uv](https://docs.astral.sh/uv/), and Node 22.18+, 24.11+ or 26+ with the root's `npm ci` done (some tests
run the TypeScript SDK to compare the two):

```sh
cd packages/python
uv sync --frozen
uv run pytest tests
```

The MCP wrapper's tests run against one MCP SDK line at a time:

```sh
uv run --group mcp1 pytest tests/wrapper
```

The other lines are `mcp2`, `fastmcp3` and `fastmcp4`. Add `--group frameworks` for
`tests/frameworks`.

## Go

Go 1.25 or later, and Node 22.18+, 24.11+ or 26+ with the root's `npm ci` done (the tests run the
contract's validator on the events, and the TypeScript SDK's sources to compare the two SDKs'
events):

```sh
cd packages/go
go vet ./...
go test -race ./...
```

Without Node, the tests that need it are skipped; `LSQUARE_REQUIRE_VALIDATOR=1`, which CI sets,
makes them fail instead. The tests find the contract in `packages/contract`, or where
`LSQUARE_CONTRACT_DIR` says: a checkout's, with its sources (`src/validate.ts`), and with the
TypeScript SDK's sources next to it in `lsquare/src`.

## Pull requests

Pull requests go into `main`. CI runs the checks above, on the Node, Python and Go versions each
package supports. A change to what the SDKs send changes each of them, and the shared cases in
`packages/contract/cases`. A change under `packages/contract`, other than `REDACTION.md` and its
tests, raises its version in `packages/contract/package.json`.
