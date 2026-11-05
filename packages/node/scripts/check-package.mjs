// Runs before every `npm publish` (prepublishOnly). Fails if the package would contain anything
// other than the SDK's build, or matches the denylist below: infrastructure IDs, personal paths,
// email addresses and credential formats. A denylist only finds what it lists; it complements
// reading the package, it doesn't replace it.
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')

const EXPECTED = [
  'LICENSE',
  'README.md',
  'dist/index.cjs',
  'dist/index.d.cts',
  'dist/index.d.ts',
  'dist/index.js',
  'package.json',
]

const FORBIDDEN = [
  // Infrastructure IDs, such as Cloudflare's.
  /\b[0-9a-f]{32}\b/,
  // Personal details.
  /\/Users\/|\/home\//,
  /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/,
  // Credentials. The redaction patterns in the build are regexes, so a real value never matches
  // them by accident: after each prefix they contain a character class, not the token's characters.
  /lsq_[A-Za-z0-9_-]{20,}/,
  /\bsk[-_](?:live_)?[A-Za-z0-9]{16,}/,
  /xox[abprs]-[A-Za-z0-9-]{10,}/,
  /hooks\.slack\.com\/services\//,
  /gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}/,
  /AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}/,
  /eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\./,
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/,
  /Bearer\s+[A-Za-z0-9._~+/-]{16,}/,
]

const [pack] = JSON.parse(
  execFileSync('npm', ['pack', '--dry-run', '--json', '--ignore-scripts'], { cwd: root, encoding: 'utf8' }),
)
const files = pack.files.map((file) => file.path).sort()
const problems = []

const unexpected = files.filter((file) => !EXPECTED.includes(file))
const missing = EXPECTED.filter((file) => !files.includes(file))
if (unexpected.length) problems.push(`unexpected files: ${unexpected.join(', ')}`)
if (missing.length) problems.push(`missing files (run the build first?): ${missing.join(', ')}`)

for (const file of files) {
  const text = readFileSync(join(root, file), 'utf8')
  for (const pattern of FORBIDDEN) {
    if (pattern.test(text)) problems.push(`${file} matches ${pattern}`)
  }
}

if (problems.length) {
  console.error(`[check-package] ${pack.name}@${pack.version} must not be published:\n- ${problems.join('\n- ')}`)
  process.exit(1)
}
console.log(
  `[check-package] ${pack.name}@${pack.version}: exactly the ${files.length} expected files, none matching the ${FORBIDDEN.length} denylist patterns`,
)
