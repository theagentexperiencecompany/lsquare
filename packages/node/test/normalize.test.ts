import { describe, expect, it } from 'vitest'
import { eventIdFor, normalize } from '../src/normalize.js'

const base = { conversationId: 'conv1', orgId: 'org_1', toolName: 'ask_docs', question: 'How many orders?' }

function ok(input: unknown) {
  const result = normalize(input)
  if (!result.ok) throw new Error(`expected ok, got ${result.code}`)
  return result.value
}

describe('required fields', () => {
  it('rejects non-objects and missing fields', () => {
    expect(normalize(null)).toMatchObject({ ok: false, code: 'missing_required' })
    expect(normalize({ ...base, orgId: '' })).toMatchObject({ ok: false, paths: ['orgId'] })
    expect(normalize({ ...base, question: undefined })).toMatchObject({ ok: false, paths: ['question'] })
  })

  it('rejects a conversationId containing "/"', () => {
    expect(normalize({ ...base, conversationId: 'a/b' })).toMatchObject({ ok: false, code: 'invalid_conversation_id' })
  })

  it('defaults status to completed', () => {
    expect(ok(base).body.status).toBe('completed')
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

describe('eventIdFor', () => {
  it('joins conversationId and turnId, or is random without a turnId', () => {
    expect(eventIdFor('conv1', 't_1')).toBe('conv1/t/t_1')
    expect(eventIdFor('conv1', undefined)).toMatch(/^[0-9a-f-]{36}$/)
  })

  it('hashes IDs over 512 UTF-8 bytes, deterministically', () => {
    // 'conv1/t/' is 8 bytes.
    expect(eventIdFor('conv1', 'x'.repeat(504))).toBe(`conv1/t/${'x'.repeat(504)}`)
    const long = eventIdFor('conv1', 'x'.repeat(505))
    expect(long).toMatch(/^h\/[0-9a-f]{64}$/)
    expect(eventIdFor('conv1', 'x'.repeat(505))).toBe(long)
    expect(eventIdFor('conv2', 'x'.repeat(505))).not.toBe(long)
    // Counted in bytes: 'é' is 2.
    expect(eventIdFor('conv1', 'é'.repeat(252))).toBe(`conv1/t/${'é'.repeat(252)}`)
    expect(eventIdFor('conv1', 'é'.repeat(253))).toMatch(/^h\//)
  })
})

describe('toolCalls', () => {
  it('keeps allowed fields and reduces a result to its length', () => {
    const { body, ignoredToolCallFields } = ok({
      ...base,
      toolCalls: [
        { name: 'search_docs', input: { q: 'x' }, result: 'abcdef', resultIsError: false, extra: 1 },
        { name: 'lookup', input: {}, result: { rows: [1, 2] }, resultChars: 3 },
      ],
    })
    expect(body.toolCalls).toEqual([
      { name: 'search_docs', input: { q: 'x' }, resultChars: 6, resultIsError: false },
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

  it('stops as too_large once the content cannot fit the budget', () => {
    expect(normalize({ ...base, answer: 'x'.repeat(200) }, { budget: 100 })).toMatchObject({ ok: false, code: 'too_large' })
    expect(normalize({ ...base, latency: { a: 'x'.repeat(200) } }, { budget: 100 })).toMatchObject({ ok: false, code: 'too_large' })
  })

  // Otherwise only the final size check stops them, after serializing the whole event.
  it.each([
    ['turnId', { turnId: 'x'.repeat(200) }],
    ['apiKeyId', { apiKeyId: 'x'.repeat(200) }],
    ['surface', { surface: 'x'.repeat(200) }],
    ['client', { client: 'x'.repeat(200) }],
    ['client.version', { client: { name: 'c', version: 'x'.repeat(200) } }],
    ['error.type', { error: { message: 'm', type: 'x'.repeat(200) } }],
  ])('charges %s to the budget', (_name, extra) => {
    expect(normalize({ ...base, ...extra }, { budget: 100 })).toMatchObject({ ok: false, code: 'too_large' })
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

  it('keeps primitive metadata only', () => {
    const { body, dropped } = ok({ ...base, metadata: { a: 1, b: 'x', c: { nested: true } } })
    expect(body.metadata).toEqual({ a: 1, b: 'x' })
    expect(dropped.metadata).toBe('not_json')
  })

  it('names unknown fields only when they are known aliases', () => {
    expect(ok({ ...base, foo: 1, tool: 2, 'jane@acme.io': 3 }).ignoredFields).toEqual({ tool: 1, '(other)': 2 })
  })
})
