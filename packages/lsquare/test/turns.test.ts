import { describe, expect, it, vi } from 'vitest'
import { buildTurn } from '../src/event.js'
import { idValue, short, turnKey } from '../src/normalize.js'
import { resolvePatterns } from '../src/redact.js'
import { newState, shapeJson } from '../src/shape.js'
import type { TurnEvent } from '../src/types.js'

const base = { conversationId: 'conv1', orgId: 'org_1', toolName: 'ask_docs', question: 'How many orders?' }
const context = { sdk: { name: 'lsquare', version: '0.2.0', language: 'js' as const }, patterns: resolvePatterns(undefined), redactKeys: [], maxEventBytes: 256 * 1024 }

function build(input: unknown, maxEventBytes = context.maxEventBytes) {
  return buildTurn(input, '2026-10-28T00:00:00.000Z', { ...context, maxEventBytes }, false)
}

function ok(input: unknown) {
  const built = build(input)
  if (!built.ok) throw new Error(`expected ok, got ${built.code}`)
  const { event, notes, ignoredFields } = built.draft
  return { body: event as TurnEvent, dropped: notes.dropped, ignoredFields, ignoredToolCallFields: notes.ignoredToolCallFields }
}

describe('fields', () => {
  it('needs none: an empty turn is still a turn, with a random ID', () => {
    const { body } = ok({})
    expect(body).toMatchObject({ kind: 'turn', schemaVersion: 2, source: 'live', status: 'completed' })
    expect(body.eventId).toMatch(/^[0-9a-f-]{36}$/)
  })

  it('refuses anything but a plain object', () => {
    for (const input of [null, 'question', [base], new (class Turn {})()]) {
      expect(build(input)).toMatchObject({ ok: false, code: 'invalid_input' })
    }
  })

  it('accepts a conversationId containing "/", which 0.1 refused', () => {
    expect(ok({ ...base, conversationId: 'a/b' }).body.conversationId).toBe('a/b')
  })

  it('defaults status to completed, as 0.1 did', () => {
    expect(ok(base).body.status).toBe('completed')
  })

  it('gives a 0.1 input the same turn fields', () => {
    const input = {
      ...base,
      turnId: 3,
      answer: 'Two.',
      toolCalls: [{ name: 'find', input: { q: 'x' }, result: 'abc', durationMs: 4 }],
      usage: { inputTokens: 10, outputTokens: 5, costUsd: 0.01 },
      latency: { totalMs: 900 },
      verification: { passed: true },
      client: { name: 'claude-code', version: '2.1' },
      apiKeyId: 'key_1',
      surface: 'mcp',
      metadata: { plan: 'pro' },
    }
    const { body } = ok(input)
    const { eventId, schemaVersion, kind, capturedAt, source, sdk, ...fields } = body
    expect(fields).toEqual({
      ...input,
      turnId: '3',
      status: 'completed',
      toolCalls: [{ name: 'find', input: { q: 'x' }, resultChars: 3, durationMs: 4 }],
    })
  })
})

describe('IDs', () => {
  it('drops the event for an ID that is not a string, has a NUL or is over 512 bytes', () => {
    for (const [field, value] of [
      ['orgId', 7],
      ['userId', 'a\u0000b'],
      ['conversationId', 'x'.repeat(513)],
      ['toolName', 'é'.repeat(257)],
      ['callId', {}],
    ] as const) {
      expect(build({ ...base, [field]: value })).toMatchObject({ ok: false, code: 'invalid_id', paths: [field] })
    }
    expect(ok({ ...base, conversationId: 'x'.repeat(512), userId: 'é'.repeat(256) }).body.userId).toHaveLength(256)
  })

  it("counts an empty one as absent, and says so", () => {
    const { body, dropped } = ok({ ...base, orgId: '', userId: '' })
    expect(body.orgId).toBeUndefined()
    expect(body.userId).toBeUndefined()
    expect(dropped).toEqual({ orgId: 'empty', userId: 'empty' })
  })

  it('never reads one too long to be valid, nor a short field too long to keep: their length decides', () => {
    // A rope string (built by concatenation) is flattened, its whole length copied, by the first read.
    const read: number[] = []
    const includes = vi.spyOn(String.prototype, 'includes').mockImplementation(function (this: string, ...args: [string, number?]) {
      read.push(this.length)
      return String.prototype.indexOf.call(this, ...args) !== -1
    })
    const charCodeAt = vi.spyOn(String.prototype, 'charCodeAt')
    try {
      expect(idValue('c'.repeat(100_000))).toBe('invalid')
      expect(idValue('c'.repeat(512))).toEqual({ value: 'c'.repeat(512) })
      expect(idValue('c\u0000')).toBe('invalid')
      expect(short('s'.repeat(40_000))).toEqual({ reason: 'too_large' })
      expect(read.filter((length) => length > 512)).toEqual([])
      expect(charCodeAt.mock.contexts.filter((text) => String(text).length > 32_000)).toEqual([])
    } finally {
      includes.mockRestore()
      charCodeAt.mockRestore()
    }
    expect(short('😀'.repeat(16_000))).toEqual({ value: '😀'.repeat(16_000) })
  })

  it('sends userId and callId as given, never redacted', () => {
    const { body } = ok({ ...base, userId: 'jane@acme.io', callId: 'call-1' })
    expect(body).toMatchObject({ userId: 'jane@acme.io', callId: 'call-1' })
  })
})

describe('turnId', () => {
  it.each([
    ['t_1', 't_1', undefined],
    [42, '42', undefined],
    ['', undefined, 'empty'],
    [1.5, undefined, 'unsafe_integer'],
    [2 ** 60, undefined, 'unsafe_integer'],
    [{}, undefined, 'wrong_type'],
  ])('%j', (turnId, kept, reason) => {
    const { body, dropped } = ok({ ...base, turnId })
    expect(body.turnId).toBe(kept)
    expect(dropped.turnId).toBe(reason)
  })
})

describe('event IDs', () => {
  it('are k/ and the SHA-256 of [orgId, conversationId, turnId] as compact JSON', () => {
    // Computed with Python's json.dumps(separators=(",", ":"), ensure_ascii=False) and hashlib.
    expect(turnKey('org_1', 'conv1', '7')).toBe('k/3f8e87b789e2e017772841b8a6b6be39f9ad5b3384435e43ff6be84ac541c1cc')
    expect(turnKey(undefined, 'conv/1', 't_1')).toBe('k/808ca52684483385c951e9c815da6d29511a8743454656ae124ef951942cf6d0')
    expect(turnKey('org_é', 'c😀', 'x')).toBe('k/7667cae4d178cedaf66c9c9afd64b6397aa564fcfc08f6083d0af90633e6783d')
  })

  it('are equal for turnId 7 and "7", and differ for two orgs with the same conversation and turn', () => {
    expect(ok({ ...base, turnId: 7 }).body.eventId).toBe(ok({ ...base, turnId: '7' }).body.eventId)
    expect(ok({ ...base, turnId: 7 }).body.eventId).toBe(turnKey('org_1', 'conv1', '7'))
    expect(ok({ ...base, orgId: 'org_2', turnId: 7 }).body.eventId).not.toBe(ok({ ...base, turnId: 7 }).body.eventId)
  })

  it('are random without a conversation or a turnId, and hash long turnIds whatever their length', () => {
    expect(ok(base).body.eventId).toMatch(/^[0-9a-f-]{36}$/)
    expect(ok({ turnId: 1 }).body.eventId).toMatch(/^[0-9a-f-]{36}$/)
    expect(ok({ ...base, turnId: 'x'.repeat(10_000) }).body.eventId).toMatch(/^k\/[0-9a-f]{64}$/)
  })
})

describe('toolCalls', () => {
  it('keeps allowed fields and reduces a result to its length, in code points', () => {
    const { body, ignoredToolCallFields } = ok({
      ...base,
      toolCalls: [
        { name: 'search_docs', input: { q: 'x' }, result: 'abc😀', resultIsError: false, extra: 1 },
        { name: 'lookup', input: {}, result: { rows: [1, 2] }, resultChars: 3 },
      ],
    })
    expect(body.toolCalls).toEqual([
      { name: 'search_docs', input: { q: 'x' }, resultChars: 4, resultIsError: false },
      { name: 'lookup', input: {}, resultChars: 3 },
    ])
    expect(ignoredToolCallFields).toEqual({ result: 2, '(other)': 1 })
  })

  it('is absent, not [], when every item is invalid', () => {
    const { body, dropped, ignoredToolCallFields } = ok({ ...base, toolCalls: [{ tool: 'search_docs', args: {} }, 'x'] })
    expect(body.toolCalls).toBeUndefined()
    expect(dropped).toEqual({ 'toolCalls[0]': 'missing_name', 'toolCalls[1]': 'wrong_type' })
    expect(ignoredToolCallFields).toEqual({ tool: 1, args: 1 })
  })

  it('keeps an empty array: no tools were called', () => {
    expect(ok({ ...base, toolCalls: [] }).body.toolCalls).toEqual([])
  })

  it('keeps an explicit null input', () => {
    expect(ok({ ...base, toolCalls: [{ name: 'q', input: null }] }).body.toolCalls).toEqual([{ name: 'q', input: null }])
  })

  it('measures non-string results within a work limit, and says why when it cannot', () => {
    const { body, dropped } = ok({
      ...base,
      toolCalls: [
        { name: 'map', result: new Map() },
        { name: 'big', result: Array.from({ length: 1_100_000 }, () => 0) },
        { name: 'small', result: [1] },
      ],
    })
    // The allowance is shared: once a big result uses it up, later results aren't measured either.
    expect(body.toolCalls).toEqual([{ name: 'map' }, { name: 'big' }, { name: 'small' }])
    expect(dropped).toEqual({
      'toolCalls[0].resultChars': 'not_json',
      'toolCalls[1].resultChars': 'too_large',
      'toolCalls[2].resultChars': 'too_large',
    })
  })

  it('keeps the first 100', () => {
    const { body } = ok({ ...base, toolCalls: Array.from({ length: 150 }, (_, i) => ({ name: `t${i}` })) })
    expect(body.toolCalls).toHaveLength(100)
    expect(body.truncated).toEqual(['toolCalls'])
  })

  it('drops a non-array', () => {
    const { body, dropped } = ok({ ...base, toolCalls: { name: 'x' } })
    expect(body.toolCalls).toBeUndefined()
    expect(dropped.toolCalls).toBe('wrong_type')
  })

  it('marks an input with unencodable parts, without exposing keys', () => {
    const { body, dropped } = ok({ ...base, toolCalls: [{ name: 'q', input: { 'jane@acme.io': 1n, ok: 1 } }] })
    expect(body.toolCalls?.[0]?.input).toEqual({ ok: 1 })
    expect(dropped).toEqual({ 'toolCalls[0].input': 'not_json' })
  })
})

describe('free-form JSON fields', () => {
  it('drops a Map instead of turning it into {}', () => {
    const { body, dropped } = ok({ ...base, verification: new Map([['a', 1]]) })
    expect(body.verification).toBeUndefined()
    expect(dropped.verification).toBe('not_json')
  })

  it('keeps the encodable rest of a lossy value', () => {
    const circular: Record<string, unknown> = { totalMs: 10 }
    circular.self = circular
    const { body, dropped } = ok({ ...base, latency: circular })
    expect(body.latency).toEqual({ totalMs: 10 })
    expect(dropped.latency).toBe('not_json')
  })

  it('marks undefined array items as lossy', () => {
    const { body, dropped } = ok({ ...base, verification: [1, undefined] })
    expect(body.verification).toEqual([1, null])
    expect(dropped.verification).toBe('not_json')
  })

  it('drops short fields over 16,000 characters, instead of the event', () => {
    const long = 'x'.repeat(16_001)
    const { body, dropped } = ok({ ...base, apiKeyId: long, surface: long, client: long, error: { message: 'm', type: long } })
    expect(body).not.toHaveProperty('apiKeyId')
    expect(body.error).toEqual({ message: 'm' })
    expect(dropped).toEqual({ apiKeyId: 'too_large', surface: 'too_large', client: 'too_large', 'error.type': 'too_large' })
  })

  it('drops binary data as not_json', () => {
    const { body, dropped } = ok({ ...base, latency: Buffer.from('raw'), verification: { at: 1, bytes: new Uint8Array(4) } })
    expect(body.latency).toBeUndefined()
    expect(body.verification).toEqual({ at: 1 })
    expect(dropped).toEqual({ latency: 'not_json', verification: 'not_json' })
  })

  it('uses toJSON, like JSON.stringify', () => {
    const date = new Date('2026-10-14T10:00:00.000Z')
    expect(ok({ ...base, verification: { at: date } }).body.verification).toEqual({ at: '2026-10-14T10:00:00.000Z' })
  })

  it('reads an object or array whose toJSON returns itself as it is, as JSON.stringify does, without its toJSON', () => {
    const verification = { data: 'hello', toJSON() { return this } }
    const latency = Object.assign([1, 2], { toJSON() { return this } })
    const nested = { outer: { data: 'x', toJSON() { return this } } }
    expect(JSON.parse(JSON.stringify({ verification, latency, nested }))).toEqual({ verification: { data: 'hello' }, latency: [1, 2], nested: { outer: { data: 'x' } } })
    const { body, dropped } = ok({ ...base, verification, latency, toolCalls: [{ name: 'n', input: nested }] })
    expect(body.verification).toEqual({ data: 'hello' })
    expect(body.latency).toEqual([1, 2])
    expect(body.toolCalls).toEqual([{ name: 'n', input: { outer: { data: 'x' } } }])
    expect(dropped).toEqual({})
  })

  it('runs a toJSON getter that returns itself once, and leaves out a class instance that does', () => {
    let reads = 0
    const verification = Object.defineProperty({ data: 'hello' }, 'toJSON', {
      enumerable: true,
      // A fresh function at every read.
      get: () => (reads++, function (this: unknown) { return this }),
    })
    const { body, dropped } = ok({ ...base, verification })
    expect(body.verification).toEqual({ data: 'hello' })
    expect(dropped).toEqual({})
    expect(reads).toBe(1)
    class Point {
      x = 1
      toJSON() {
        return this
      }
    }
    const point = ok({ ...base, verification: new Point() })
    expect(point.body.verification).toBeUndefined()
    expect(point.dropped).toEqual({ verification: 'not_json' })
  })

  it("still reads what any other toJSON returns, also a toJSON's own result's", () => {
    const state = newState()
    expect(shapeJson({ when: { toJSON: () => '2026-10-28' }, nested: { toJSON: () => ({ a: 1 }) }, twice: { toJSON: () => ({ toJSON: () => 'inner' }) } }, 100, state)).toEqual({
      when: '2026-10-28',
      nested: { a: 1 },
      twice: 'inner',
    })
    expect(state.lossy).toBe(false)
  })

  it('never copies a __proto__ key', () => {
    const value = JSON.parse('{"__proto__": {"polluted": true}, "a": 1}')
    const { body, dropped } = ok({ ...base, verification: value })
    expect(body.verification).toEqual({ a: 1 })
    expect(Object.getPrototypeOf(body.verification)).toBe(Object.prototype)
    expect(dropped.verification).toBe('not_json')
  })
})

describe('other fields', () => {
  it('normalises errors', () => {
    expect(ok({ ...base, error: new TypeError('boom') }).body.error).toEqual({ message: 'boom', type: 'TypeError' })
    expect(ok({ ...base, error: 'boom' }).body.error).toEqual({ message: 'boom' })
    expect(ok({ ...base, error: { message: 'boom', type: 'Timeout' } }).body.error).toEqual({ message: 'boom', type: 'Timeout' })
    expect(ok({ ...base, error: 42 }).dropped.error).toBe('wrong_type')
  })

  it('accepts client as a string or { name, version }', () => {
    expect(ok({ ...base, client: 'claude-code' }).body.client).toBe('claude-code')
    expect(ok({ ...base, client: { name: 'cursor', version: '2.1', x: 1 } }).body.client).toEqual({ name: 'cursor', version: '2.1' })
    expect(ok({ ...base, client: 7 }).dropped.client).toBe('wrong_type')
  })

  it('keeps valid usage fields and records invalid ones', () => {
    const { body, dropped } = ok({ ...base, usage: { inputTokens: 10, outputTokens: undefined, costUsd: 'free' } })
    expect(body.usage).toEqual({ inputTokens: 10 })
    expect(dropped).toEqual({ 'usage.costUsd': 'wrong_type' })
  })

  it('keeps primitive metadata only, and drops the event when metadata alone is too big', () => {
    const { body, dropped } = ok({ ...base, metadata: { a: 1, b: 'x', c: { nested: true } } })
    expect(body.metadata).toEqual({ a: 1, b: 'x' })
    expect(dropped.metadata).toBe('not_json')
    expect(build({ ...base, metadata: { a: 'x'.repeat(300_000) } })).toMatchObject({ ok: false, code: 'too_large' })
  })

  it('names unknown fields only when they are known aliases', () => {
    expect(ok({ ...base, foo: 1, tool: 2, 'jane@acme.io': 3 }).ignoredFields).toEqual({ tool: 1, '(other)': 2 })
  })
})
