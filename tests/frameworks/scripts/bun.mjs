// Runs Bun from npm's packages, so the tests need no global install. The bun package's install
// script, where npm runs it, moves the binary from the platform package into bun/bin/bun.exe;
// where it doesn't, the binary stays in the platform package. Either is used, whichever holds it.
import { spawnSync } from 'node:child_process'
import { statSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'

const require = createRequire(import.meta.url)
const arch = process.arch === 'arm64' ? 'aarch64' : process.arch
const binary = process.platform === 'win32' ? 'bun.exe' : 'bun'
const candidates = [
  ['bun', join('bin', 'bun.exe')],
  [`@oven/bun-${process.platform}-${arch}`, join('bin', binary)],
  [`@oven/bun-${process.platform}-${arch}-musl`, join('bin', binary)],
]

/** A file that is the binary, not the bun package's placeholder (a short script) or nothing. */
function found(name, file) {
  try {
    const path = join(dirname(require.resolve(`${name}/package.json`)), file)
    return statSync(path).size > 1_000_000 ? path : undefined
  } catch {
    return undefined
  }
}

const path = candidates.map(([name, file]) => found(name, file)).find(Boolean)
if (!path) {
  console.error(`[bun] No Bun binary for ${process.platform}-${process.arch}: looked in ${candidates.map(([name, file]) => join(name, file)).join(', ')}.`)
  process.exit(1)
}
const result = spawnSync(path, process.argv.slice(2), { stdio: 'inherit' })
if (result.error) console.error(`[bun] Couldn't run ${path}: ${result.error.message}`)
process.exit(result.status ?? 1)
