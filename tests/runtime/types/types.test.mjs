// lsquare's packed declarations, installed with the TypeScript the lockfile pins and compiled as
// consumers would: under node16 and nodenext, each build alone and a client from one passed to the
// other's wrapper, both ways (TypeScript takes the two builds' LSquare classes for different types);
// under node10, which reads typesVersions; under bundler; and with servers of both MCP SDKs. Each
// project has `@ts-expect-error` lines where the types must refuse something.
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { test } from 'node:test'

for (const project of ['node/node16.json', 'node/nodenext.json', 'node10', 'bundler', 'servers']) {
  test(`${project} compiles`, () => {
    try {
      execFileSync(process.execPath, ['node_modules/typescript/bin/tsc', '-p', project], { encoding: 'utf8', stdio: 'pipe' })
    } catch (error) {
      assert.fail(`tsc -p ${project}:\n${error.stdout}${error.stderr}`)
    }
  })
}
