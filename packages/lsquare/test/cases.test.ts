// The shared cases in packages/contract/cases (spec section 9): the Python SDK must pass the same
// files, and every event they give passes the contract's checks.
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { validateEvent } from '../../contract/src/validate.js'
import { afterHook, buildCall, buildTurn, type Built, type CallRecord, type Context } from '../src/event.js'
import { resolveKeys, resolvePatterns } from '../src/redact.js'

const dir = fileURLToPath(new URL('../../contract/cases/', import.meta.url))
const CAPTURED_AT = '2026-10-28T00:00:00.000Z'

interface Case {
  name: string
  turn?: Record<string, unknown>
  call?: Record<string, unknown>
  options?: { redact?: false | Record<string, boolean>; redactKeys?: unknown }
  hook?: { seen?: Record<string, unknown> }
  event?: Record<string, unknown>
  error?: string
}

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

function build(test: Case): Built {
  const context: Context = {
    sdk: { name: 'lsquare', version: '0.2.0', language: 'js' },
    patterns: resolvePatterns(test.options?.redact),
    redactKeys: resolveKeys(test.options?.redactKeys),
    maxEventBytes: 256 * 1024,
  }
  const withHook = (built: Built): Built => {
    if (test.hook === undefined || !built.ok) return built
    const seen = expand(test.hook.seen ?? {}) as Record<string, unknown>
    const given = structuredClone(built.draft.event) as unknown as Record<string, unknown>
    for (const [field, value] of Object.entries(seen)) expect(given[field]).toEqual(value)
    return afterHook(given, built.draft, context)
  }
  const hook = test.hook !== undefined
  if (test.turn) return withHook(buildTurn(expand(test.turn), CAPTURED_AT, context, hook))
  const { input, result, ...rest } = expand(test.call) as Record<string, unknown>
  const record = {
    callId: 'case-call',
    capturedAt: CAPTURED_AT,
    toolName: 'search_orders',
    status: 'completed',
    durationMs: 1,
    ids: {},
    diagnostics: false,
    ...rest,
    ...(input === undefined ? {} : { input: { value: input } }),
    ...(result === undefined ? {} : { result: { value: result } }),
  } as CallRecord
  return withHook(buildCall(record, context, hook))
}

const files = readdirSync(dir).filter((name) => name.endsWith('.json')).sort()

it('reads every shared case file, none of them empty', () => {
  expect(files).toEqual(['calls.json', 'ids.json', 'numbers.json', 'redaction.json', 'shortening.json'])
  for (const file of files) expect((JSON.parse(readFileSync(`${dir}${file}`, 'utf8')) as Case[]).length, file).toBeGreaterThan(0)
})

for (const file of files) {
  const cases = JSON.parse(readFileSync(`${dir}${file}`, 'utf8')) as Case[]
  describe(`packages/contract/cases/${file}`, () => {
    it.each(cases.map((test) => [test.name, test] as const))('%s', (_name, test) => {
      const built = build(test)
      if (test.error !== undefined) {
        expect(built).toMatchObject({ ok: false, code: test.error })
        return
      }
      if (!built.ok) throw new Error(`dropped as ${built.code}`)
      const { sdk, capturedAt, ...event } = built.draft.event as unknown as Record<string, unknown>
      expect(capturedAt).toBe(CAPTURED_AT)
      const expected = expand(test.event) as Record<string, unknown>
      if (expected.eventId === '$uuid') {
        expect(event.eventId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
        expected.eventId = event.eventId
      }
      expect(event).toEqual(expected)
      expect(validateEvent(JSON.parse(JSON.stringify(built.draft.event)))).toMatchObject({ ok: true })
    })
  })
}
