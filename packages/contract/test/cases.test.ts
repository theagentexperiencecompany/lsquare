// The shared cases (cases/README.md says what they hold), which both SDKs' tests read: every file is
// there and holds cases, the events they expect pass the checks, and their k/ IDs are turnRef's.
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { turnRef } from '../src/turn-ref.js'
import { validateEvent } from '../src/validate.js'

const dir = fileURLToPath(new URL('../cases/', import.meta.url))
const FILES = readdirSync(dir).filter((name) => name.endsWith('.json')).sort()

interface Case {
  name: string
  turn?: Record<string, unknown>
  call?: Record<string, unknown>
  options?: unknown
  event?: Record<string, unknown>
  error?: string
}

const read = (file: string) => JSON.parse(readFileSync(`${dir}${file}`, 'utf8')) as Case[]

/** Expands the compact forms the README describes. */
function expand(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(expand)
  if (typeof value !== 'object' || value === null) return value
  const object = value as Record<string, unknown>
  if ('$prose' in object) return 'lorem ipsum '.repeat(Math.ceil((object.$prose as number) / 12)).slice(0, object.$prose as number)
  if ('$repeat' in object) {
    const [text, times] = object.$repeat as [string, number]
    return text.repeat(times)
  }
  if ('$concat' in object) return (object.$concat as unknown[]).map(expand).join('')
  if ('$range' in object) return Array.from({ length: object.$range as number }, (_, i) => i)
  if ('$keys' in object) return Object.fromEntries(Array.from({ length: object.$keys as number }, (_, i) => [`k${i}`, i]))
  return Object.fromEntries(Object.entries(object).map(([key, item]) => [key, expand(item)]))
}

describe('the shared cases', () => {
  it('are the files both SDKs read', () => {
    expect(FILES).toEqual(['calls.json', 'ids.json', 'numbers.json', 'redaction.json', 'shortening.json'])
  })

  it.each(FILES)('%s holds named cases, each an input and either the event it gives or an error', (file) => {
    const cases = read(file)
    expect(Array.isArray(cases)).toBe(true)
    expect(cases.length).toBeGreaterThan(0)
    expect(new Set(cases.map((test) => test.name)).size).toBe(cases.length)
    for (const test of cases) {
      expect(typeof test.name).toBe('string')
      expect((test.turn === undefined) !== (test.call === undefined), test.name).toBe(true)
      expect((test.event === undefined) !== (test.error === undefined), test.name).toBe(true)
    }
  })

  // With what the README leaves out of the comparison: its time, its SDK and a random ID.
  it.each(FILES)('%s: every event it expects passes the checks', (file) => {
    const events = read(file).filter((test) => test.event !== undefined)
    expect(events.length).toBeGreaterThan(0)
    for (const test of events) {
      const event = expand(test.event) as Record<string, unknown>
      if (event.eventId === '$uuid') event.eventId = '6f1c2a3b-4d5e-4f60-8a71-829304a5b6c7'
      const sent = { ...event, capturedAt: '2026-10-28T00:00:00.000Z', sdk: { name: 'lsquare', version: '0.2.0', language: 'js' } }
      const result = validateEvent(JSON.parse(JSON.stringify(sent)))
      expect(result.ok ? 'valid' : result.reason, test.name).toBe('valid')
    }
  })
})

describe("the cases' k/ IDs", () => {
  const vectors = FILES.flatMap((file) => read(file).map((test): readonly [string, unknown] => [`${file} ${test.name}`, expand(test.event)]))
    .filter((vector): vector is readonly [string, Record<string, unknown>] => {
      const eventId = (vector[1] as Record<string, unknown> | undefined)?.eventId
      return typeof eventId === 'string' && eventId.startsWith('k/')
    })

  it('are there, ids.json holding some', () => {
    expect(vectors.filter(([name]) => name.startsWith('ids.json ')).length).toBeGreaterThan(0)
  })

  it.each(vectors)("%s: turnRef of the event's org (null when it has none), conversation and turnId", async (_name, event) => {
    expect(typeof event.conversationId).toBe('string')
    expect(typeof event.turnId).toBe('string')
    const key = [(event.orgId as string | undefined) ?? null, event.conversationId as string, event.turnId as string] as const
    expect(await turnRef(key)).toBe(event.eventId)
  })
})

describe('the sources', () => {
  it("import only each other: no dependencies, and nothing of Node's, so they run in Workers too", () => {
    const src = fileURLToPath(new URL('../src/', import.meta.url))
    const files = readdirSync(src).filter((name) => name.endsWith('.ts'))
    expect(files.length).toBeGreaterThan(0)
    for (const file of files) {
      const text = readFileSync(`${src}${file}`, 'utf8')
      const imported = [...text.matchAll(/\b(?:from|import)\s*\(?\s*['"]([^'"]+)['"]/g)].map((match) => match[1])
      expect(imported.filter((specifier) => !specifier?.startsWith('./')), file).toEqual([])
    }
  })
})
