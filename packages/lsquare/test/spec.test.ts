// packages/contract/REDACTION.md lists 0.1's exact redaction patterns and secret key names, which
// 0.2 keeps: the patterns with JavaScript's \s written out (spec section 2, same in every language),
// and the phone and ip patterns rewritten to match the same (src/redact.ts says why).
import { existsSync, readFileSync } from 'node:fs'
import { relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { PATTERNS, SECRET_KEYS } from '../src/redact.js'
import { firstDifference } from './v1-patterns.js'

const file = fileURLToPath(new URL('../../contract/REDACTION.md', import.meta.url))

describe('packages/contract/REDACTION.md', () => {
  // Without the file these tests fail, never skip: a skip would pass with nothing checked.
  it('is in packages/contract', () => {
    expect(existsSync(file), file).toBe(true)
  })

  it('lists exactly the patterns in src/redact.ts, the phone and ip patterns by what they match', () => {
    const spec = readFileSync(file, 'utf8')
    const block = spec.slice(spec.indexOf('const PATTERNS = {'), spec.indexOf('\n}\n', spec.indexOf('const PATTERNS = {')))
    const listed: Record<string, string> = {}
    for (const line of block.split('\n').slice(1)) {
      const match = /^\s+(\w+): \/(.*)\/g,(?:\s*\/\/.*)?$/.exec(line)
      if (match) listed[match[1] as string] = match[2] as string
    }
    const ws = '\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff'
    const expanded = Object.fromEntries(
      Object.entries(listed).map(([name, source]) => [name, source.replaceAll('[^\\s', `[^${ws}`).replaceAll('\\s', `[${ws}]`)]),
    )
    expect(Object.keys(expanded).sort()).toEqual(Object.keys(PATTERNS).sort())
    for (const [name, pattern] of Object.entries(PATTERNS)) {
      const source = expanded[name] as string
      if (name === 'phone' || name === 'ip') expect(firstDifference(pattern, new RegExp(source, 'g')), name).toBeUndefined()
      else expect(pattern.source, name).toBe(source)
    }
  })

  it('lists exactly the secret key names in src/redact.ts', () => {
    const spec = readFileSync(file, 'utf8')
    const start = spec.indexOf('const SECRET_KEYS = [')
    expect(start).toBeGreaterThan(-1)
    const block = spec.slice(start, spec.indexOf('\n]\n', start))
    expect([...block.matchAll(/'([^']*)'/g)].map((match) => match[1])).toEqual(SECRET_KEYS)
  })

  it("is code to CI's path filter: a pull request changing only it still runs the tests", () => {
    const root = fileURLToPath(new URL('../../../', import.meta.url))
    const ci = readFileSync(fileURLToPath(new URL('../../../.github/workflows/ci.yml', import.meta.url)), 'utf8')
    expect(ci).toContain(`grep -qxF '${relative(root, file)}'`)
  })
})
