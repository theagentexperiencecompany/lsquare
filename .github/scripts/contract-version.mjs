// Fails a pull request that changes packages/contract without raising its version: LSquare's
// ingestion takes the contract by its version, as the tarball this repository's CI packed, so a
// change under the same version would be a second contract of that version, which the ingestion
// could take for the one it holds. Run from the checkout, given the pull request's base commit,
// which the checkout must have. No dependencies.
//   node .github/scripts/contract-version.mjs <base commit>
import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'

const DIR = 'packages/contract'
const MANIFEST = `${DIR}/package.json`
// Its errors stay in the exception: each failure below explains itself.
const git = (...args) => execFileSync('git', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })

const fail = (message) => {
  console.log(`::error file=${MANIFEST}::${message}`)
  process.exit(1)
}

/** Its major, minor and patch, as integers of any size: the contract is versioned with nothing else. */
const parse = (version) => /^(\d+)\.(\d+)\.(\d+)$/.exec(String(version))?.slice(1).map((part) => BigInt(part))

/** Whether `after` is above `before`, by major, then minor, then patch. */
function above(after, before) {
  for (let index = 0; index < 3; index++) if (after[index] !== before[index]) return after[index] > before[index]
  return false
}

const base = process.argv[2]
if (!base) {
  console.error('Usage: contract-version.mjs <base commit>')
  process.exit(2)
}
// Without the base, nothing can be compared: fail rather than pass unchecked.
try {
  git('cat-file', '-e', `${base}^{commit}`)
} catch {
  fail(`The base commit ${base} isn't in the checkout: the contract's version can't be checked.`)
}

// Its redaction reference and its own tests aren't in the tarball the ingestion takes: they change
// freely.
const changed = git('diff', '--name-only', '--no-renames', base, 'HEAD', '--', DIR, `:(exclude)${DIR}/REDACTION.md`, `:(exclude)${DIR}/test`)
  .split('\n')
  .filter(Boolean)
if (changed.length === 0) {
  console.log(`${DIR} unchanged.`)
  process.exit(0)
}
let before
try {
  before = JSON.parse(git('show', `${base}:${MANIFEST}`)).version
} catch {
  console.log(`${DIR} is new: the base has no ${MANIFEST}.`)
  process.exit(0)
}
if (!existsSync(MANIFEST)) {
  console.log(`${DIR} was removed.`)
  process.exit(0)
}
const after = JSON.parse(readFileSync(MANIFEST, 'utf8')).version
console.log(`${DIR} changed (${changed.length} files): version ${before} -> ${after}.`)
const [from, to] = [parse(before), parse(after)]
if (to === undefined) fail(`Its version ${JSON.stringify(after)} isn't major.minor.patch.`)
if (from !== undefined && !above(to, from)) fail(`${DIR} changed, but its version is ${after}, not above ${before}: raise it, as LSquare's ingestion takes it by its version.`)
