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
    for (const file of readdirSync(src).filter((name) => name.endsWith('.ts'))) {
      const code = readFileSync(join(src, file), 'utf8')
      for (const [, specifier] of code.matchAll(/(?:from|import|require)\s*\(?\s*['"]([^'"]+)['"]/g)) {
        if (specifier!.startsWith('node:')) continue
        const target = resolve(src, specifier!)
        if (!specifier!.startsWith('.') || relative(src, target).startsWith('..')) outside.push(`${file}: ${specifier}`)
      }
    }
    expect(outside).toEqual([])
  })

  it('has no runtime dependencies and publishes only dist', () => {
    expect(pkg.dependencies).toBeUndefined()
    expect(pkg.peerDependencies).toBeUndefined()
    expect(pkg.files).toEqual(['dist'])
  })
})
