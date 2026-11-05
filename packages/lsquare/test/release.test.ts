// An npm release publishes the tarball CI built and tested on sdk-v0 (RELEASING.md), never this
// folder: `npm publish` here stops at prepublishOnly, which npm runs for a folder, not a tarball.
import { spawnSync } from 'node:child_process'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, it } from 'vitest'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
// Set for the publish below: a prepublishOnly that ran the tests again would run this one again.
const NESTED = 'LSQUARE_REFUSAL_TEST'

it.skipIf(process.env[NESTED])('refuses to publish the folder', () => {
  // A dry run, to a registry nothing listens on, with none of this run's npm settings: past the
  // refusal it would still publish nothing.
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.toLowerCase().startsWith('npm_')))
  const done = spawnSync('npm', ['publish', '--dry-run', '--registry', 'http://127.0.0.1:9/'], {
    cwd: root,
    env: { ...env, [NESTED]: '1' },
    encoding: 'utf8',
    timeout: 120_000,
  })
  expect(done.status, done.stdout + done.stderr).toBe(1)
  expect(done.stderr).toContain('lsquare is published from the tarball CI built and tested, never from a folder')
})
