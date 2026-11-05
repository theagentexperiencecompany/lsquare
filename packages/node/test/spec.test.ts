// packages/contract/REDACTION.md lists the exact redaction patterns and secret key names. This keeps it
// identical to the code.
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { PATTERNS, SECRET_KEYS } from '../src/redact.js'

const file = fileURLToPath(new URL('../../contract/REDACTION.md', import.meta.url))

describe('packages/contract/REDACTION.md', () => {
  // Without the file these tests fail, never skip: a skip would pass with nothing checked.
  it('is in packages/contract', () => {
    expect(existsSync(file), file).toBe(true)
  })

  it('lists exactly the patterns in src/redact.ts', () => {
    const spec = readFileSync(file, 'utf8')
    const block = spec.slice(spec.indexOf('const PATTERNS = {'), spec.indexOf('\n}\n', spec.indexOf('const PATTERNS = {')))
    const listed: Record<string, string> = {}
    for (const line of block.split('\n').slice(1)) {
      const match = /^\s+(\w+): \/(.*)\/g,(?:\s*\/\/.*)?$/.exec(line)
      if (match) listed[match[1] as string] = match[2] as string
    }
    const code = Object.fromEntries(Object.entries(PATTERNS).map(([name, re]) => [name, re.source]))
    expect(listed).toEqual(code)
  })

  it('lists exactly the secret key names in src/redact.ts', () => {
    const spec = readFileSync(file, 'utf8')
    const start = spec.indexOf('const SECRET_KEYS = [')
    expect(start).toBeGreaterThan(-1)
    const block = spec.slice(start, spec.indexOf('\n]\n', start))
    expect([...block.matchAll(/'([^']*)'/g)].map((match) => match[1])).toEqual(SECRET_KEYS)
  })
})
