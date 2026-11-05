// The weekly job's view of the MCP SDKs (spec section 10): the newest versions, pre-releases
// included, set as packages/lsquare's own in the job's disposable checkout, and checked to be the
// ones its tests load. No dependencies.
//   pick   writes sdk, server and client to $GITHUB_OUTPUT, and a table to $GITHUB_STEP_SUMMARY
//   apply  sets SDK, SERVER and CLIENT as packages/lsquare's devDependencies, and drops
//          tests/frameworks from the workspaces: agents there pins the SDKs exactly
//   check  resolves the entry points the wrapper tests import, from packages/lsquare, and fails
//          unless each package's version is the one picked
import { execFileSync } from 'node:child_process'
import { appendFileSync, existsSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const lsquare = join(root, 'packages', 'lsquare')
const PACKAGES = { sdk: '@modelcontextprotocol/sdk', server: '@modelcontextprotocol/server', client: '@modelcontextprotocol/client' }
const ENTRIES = {
  sdk: ['@modelcontextprotocol/sdk/server/mcp.js', '@modelcontextprotocol/sdk/server/index.js', '@modelcontextprotocol/sdk/client/index.js'],
  server: ['@modelcontextprotocol/server'],
  client: ['@modelcontextprotocol/client'],
}

/** Semver precedence: major, minor and patch as numbers, then a release above its pre-releases. */
function compare(a, b) {
  const parse = (version) => {
    const [core, pre] = version.split('+')[0].split(/-(.*)/s)
    return { numbers: core.split('.').map(Number), pre: pre ? pre.split('.') : [] }
  }
  const x = parse(a)
  const y = parse(b)
  for (let index = 0; index < 3; index++) if (x.numbers[index] !== y.numbers[index]) return x.numbers[index] - y.numbers[index]
  if (x.pre.length === 0 || y.pre.length === 0) return y.pre.length - x.pre.length
  for (let index = 0; index < Math.max(x.pre.length, y.pre.length); index++) {
    const p = x.pre[index]
    const q = y.pre[index]
    if (p === undefined) return -1
    if (q === undefined) return 1
    if (p === q) continue
    const numeric = (value) => /^\d+$/.test(value)
    // As integers of any size: Number would round large ones.
    if (numeric(p) && numeric(q)) return BigInt(p) < BigInt(q) ? -1 : 1
    if (numeric(p)) return -1
    if (numeric(q)) return 1
    return p < q ? -1 : 1
  }
  return 0
}

function newest(name) {
  const versions = JSON.parse(execFileSync('npm', ['view', name, 'versions', '--json'], { encoding: 'utf8' }))
  const valid = (Array.isArray(versions) ? versions : [versions]).filter((version) => /^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$/.test(version))
  if (valid.length === 0) throw new Error(`No versions of ${name} on npm.`)
  return valid.sort(compare).at(-1)
}

/**
 * The version of `name` whose `entry` the tests load: the entry resolved as an import from
 * packages/lsquare (its exports included, so a removed entry fails here), the file checked to
 * exist, and the nearest manifest of that name above it.
 */
function loaded(entry, name) {
  const url = execFileSync(process.execPath, ['--input-type=module', '-e', 'process.stdout.write(import.meta.resolve(process.argv[1]))', entry], { cwd: lsquare, encoding: 'utf8' })
  const file = fileURLToPath(url)
  if (!existsSync(file)) throw new Error(`${entry} resolves to ${file}, which doesn't exist.`)
  for (let dir = dirname(file); ; dir = dirname(dir)) {
    const manifest = join(dir, 'package.json')
    if (existsSync(manifest)) {
      const json = JSON.parse(readFileSync(manifest, 'utf8'))
      if (json.name === name) return json.version
    }
    if (dirname(dir) === dir) throw new Error(`${entry} doesn't resolve inside ${name}.`)
  }
}

const picked = () => ({ sdk: process.env.SDK, server: process.env.SERVER, client: process.env.CLIENT })

const command = process.argv[2]
if (command === 'order') {
  // For checking the comparison by hand: prints the versions given, oldest first.
  console.log(process.argv.slice(3).sort(compare).join(' '))
} else if (command === 'pick') {
  const versions = Object.fromEntries(Object.entries(PACKAGES).map(([key, name]) => [key, newest(name)]))
  for (const [key, version] of Object.entries(versions)) {
    console.log(`${PACKAGES[key]} ${version}`)
    if (process.env.GITHUB_OUTPUT) appendFileSync(process.env.GITHUB_OUTPUT, `${key}=${version}\n`)
  }
  if (process.env.GITHUB_STEP_SUMMARY) {
    const rows = Object.entries(versions).map(([key, version]) => `| \`${PACKAGES[key]}\` | ${version} |`)
    appendFileSync(process.env.GITHUB_STEP_SUMMARY, ['| Package | Newest version |', '| --- | --- |', ...rows, ''].join('\n'))
  }
} else if (command === 'apply') {
  const versions = picked()
  const file = join(lsquare, 'package.json')
  const manifest = JSON.parse(readFileSync(file, 'utf8'))
  for (const [key, name] of Object.entries(PACKAGES)) {
    if (!versions[key]) throw new Error(`No ${key.toUpperCase()} version given.`)
    manifest.devDependencies[name] = versions[key]
  }
  writeFileSync(file, `${JSON.stringify(manifest, null, 2)}\n`)
  const rootFile = join(root, 'package.json')
  const rootManifest = JSON.parse(readFileSync(rootFile, 'utf8'))
  rootManifest.workspaces = rootManifest.workspaces.filter((workspace) => workspace !== 'tests/frameworks')
  writeFileSync(rootFile, `${JSON.stringify(rootManifest, null, 2)}\n`)
  console.log(`packages/lsquare now uses ${Object.entries(versions).map(([key, version]) => `${PACKAGES[key]}@${version}`).join(', ')}.`)
} else if (command === 'check') {
  const versions = picked()
  const wrong = []
  for (const [key, name] of Object.entries(PACKAGES)) {
    for (const entry of ENTRIES[key]) {
      const version = loaded(entry, name)
      console.log(`${entry}: ${name} ${version}`)
      if (version !== versions[key]) wrong.push(`${entry} loads ${name} ${version}, not ${versions[key]}`)
    }
  }
  if (wrong.length > 0) {
    console.error(`The tests wouldn't run on the versions picked:\n- ${wrong.join('\n- ')}`)
    process.exit(1)
  }
} else {
  console.error('Usage: newest-mcp-sdks.mjs pick|apply|check')
  process.exit(2)
}
