import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

// The published bundle is whatever src/index.ts reaches. These checks keep it to the SDK's own
// source: code elsewhere in the repo (tests, scripts) can import the SDK, never the other way
// round.
const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const src = join(root, 'src')
const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'))

describe('package boundary', () => {
  it('src imports only its own files and node: builtins', () => {
    const outside: string[] = []
    const files = readdirSync(src, { recursive: true, encoding: 'utf8' }).filter((name) => name.endsWith('.ts'))
    for (const file of files) {
      const code = readFileSync(join(src, file), 'utf8')
      for (const [, specifier] of code.matchAll(/(?:from|import|require)\s*\(?\s*['"]([^'"]+)['"]/g)) {
        if (specifier!.startsWith('node:')) continue
        const target = resolve(dirname(join(src, file)), specifier!)
        if (!specifier!.startsWith('.') || relative(src, target).startsWith('..')) outside.push(`${file}: ${specifier}`)
      }
    }
    expect(outside).toEqual([])
  })

  it('has no runtime dependencies and publishes only dist and the reference, with the notice for the code adapted from AgentCat', () => {
    expect(pkg.dependencies).toBeUndefined()
    expect(pkg.peerDependencies).toBeUndefined()
    expect(pkg.files).toEqual(['dist', 'REFERENCE.md', 'THIRD_PARTY_NOTICES'])
  })
})

/** The specifiers of a file's imports that bring in code, not only types. */
const valueImports = (code: string) => [...code.matchAll(/^import\s+(?!type\b)[^'"]*['"]([^'"]+)['"]/gm)].map(([statement, specifier]) => ({ statement, specifier: specifier! }))

describe('lsquare/mcp', () => {
  it("imports only types from the core, and json.ts's reading helpers: it reaches the client through its object, never its code", () => {
    const files = readdirSync(join(src, 'mcp'), { recursive: true, encoding: 'utf8' }).filter((name) => name.endsWith('.ts'))
    const values: string[] = []
    for (const file of files) {
      const code = readFileSync(join(src, 'mcp', file), 'utf8')
      for (const { statement, specifier } of valueImports(code)) {
        // Its own files, inside src/mcp, are part of it. json.ts keeps no state (below): the
        // bundle's copy of its helpers shares nothing with the client's.
        if (!specifier.startsWith('node:') && !specifier.startsWith('./') && specifier !== '../json.js') values.push(`${file}: ${statement}`)
      }
    }
    expect(values).toEqual([])
  })

  it('takes code only from a core file that imports nothing but types and node: builtins, and keeps no state', () => {
    const code = readFileSync(join(src, 'json.ts'), 'utf8')
    expect(valueImports(code).map(({ specifier }) => specifier)).toEqual(['node:util'])
    expect(code.match(/^(?:export\s+)?(?:let|var)\s/gm)).toBeNull()
  })
})

