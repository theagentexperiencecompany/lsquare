import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import { MAX_EVENT_DEPTH } from '../../contract/src/limits.js'
import { validateEvent } from '../../contract/src/validate.js'
import { LSquare } from '../src/index.js'

// Everything the SDK can send must pass the service's validation, as the contract has it: leniency
// is the SDK's job, so a mismatch means the service would reject what a customer's SDK produced.

const anything = fc.anything({
  withBigInt: true,
  withBoxedValues: true,
  withDate: true,
  withMap: true,
  withNullPrototype: true,
  withObjectString: true,
  withSet: true,
  withSparseArray: true,
  withTypedArray: true,
  stringUnit: 'binary',
})
const text = fc.oneof(
  fc.string(),
  fc.string({ unit: 'binary' }),
  fc.constantFrom('', 'a/b', 'alice@example.com', 'call +1 415 555 0100', '4111 1111 1111 1111', `sk-${'a'.repeat(30)}`, '10.0.0.1'),
)
const mostly = <T>(likely: fc.Arbitrary<T>) => fc.oneof({ weight: 8, arbitrary: likely }, { weight: 1, arbitrary: anything })
// The service limits these IDs to 512 bytes without NUL or a lone surrogate; the 0.1 SDK doesn't,
// so only IDs within the limit must pass. The others are rejected, as the last test checks.
const withinIdLimit = (value: unknown) =>
  typeof value !== 'string' || (Buffer.byteLength(value, 'utf8') <= 512 && !value.includes('\u0000') && !/\p{Surrogate}/u.test(value))
const id = mostly(fc.stringMatching(/^[A-Za-z0-9_-]{1,24}$/)).filter(withinIdLimit)
const record = <T extends Record<string, fc.Arbitrary<unknown>>>(fields: T) => fc.record(fields, { requiredKeys: [] })

const toolCall = mostly(
  record({
    name: mostly(text),
    input: anything,
    result: anything,
    resultChars: anything,
    resultIsError: anything,
    errorMessage: anything,
    durationMs: anything,
    extra: anything,
  }),
)

const captureInput = fc.record(
  {
    conversationId: id,
    orgId: id,
    toolName: id,
    question: mostly(text),
    turnId: fc.oneof(id, fc.integer(), fc.double(), fc.string({ minLength: 500, maxLength: 600 })),
    answer: fc.oneof(text, fc.constant(null), anything),
    status: fc.oneof(fc.constantFrom('completed', 'error'), anything),
    error: fc.oneof(text, record({ message: anything, type: anything, name: anything }), anything),
    toolCalls: fc.oneof(fc.array(toolCall, { maxLength: 6 }), anything),
    usage: fc.oneof(record({ inputTokens: anything, outputTokens: anything, costUsd: anything }), anything),
    latency: anything,
    verification: anything,
    client: fc.oneof(text, record({ name: mostly(text), version: anything }), anything),
    apiKeyId: fc.oneof(text, fc.constant(null), anything),
    surface: fc.oneof(text, anything),
    metadata: fc.oneof(fc.dictionary(fc.string(), anything), anything),
    unknownField: anything,
  },
  { requiredKeys: ['conversationId', 'orgId', 'toolName', 'question'] },
)

async function send(input: unknown): Promise<unknown[]> {
  const sent: unknown[] = []
  const client = new LSquare({
    apiKey: 'test',
    endpoint: 'https://ingest.test',
    fetch: async (_url, init) => {
      const { events } = JSON.parse(String(init?.body)) as { events: { eventId: string }[] }
      sent.push(...events)
      return Response.json({ accepted: events.map((event) => event.eventId), duplicates: [], rejected: [] })
    },
    onError: () => {},
    logger: { info: () => {}, warn: () => {} },
  })
  client.capture(input as never)
  await client.flush()
  await client.shutdown()
  return sent
}

describe("the SDK's output", () => {
  it('always passes validation', async () => {
    let checked = 0
    await fc.assert(
      fc.asyncProperty(captureInput, async (input) => {
        for (const event of await send(input)) {
          checked++
          const result = validateEvent(JSON.parse(JSON.stringify(event)))
          expect(result.ok ? 'ok' : result.reason, JSON.stringify(event).slice(0, 2000)).toBe('ok')
        }
      }),
      { numRuns: 1_000 },
    )
    // Most generated inputs must actually reach the wire, or the property proves little.
    expect(checked).toBeGreaterThan(500)
  })

  it('passes validation however deep its values are nested', async () => {
    const depth = (value: unknown): number => (typeof value === 'object' && value !== null ? 1 + Math.max(0, ...Object.values(value).map(depth)) : 0)
    for (const levels of [64, 200]) {
      let deep: unknown = 1
      for (let i = 0; i < levels; i++) deep = [deep]
      const [event] = await send({ conversationId: 'c', orgId: 'o', toolName: 't', question: 'q', toolCalls: [{ name: 'x', input: deep }], latency: deep })
      // 67 today: a call's input, kept 64 levels deep, under the event, its list and the call.
      expect(depth(event)).toBeLessThanOrEqual(MAX_EVENT_DEPTH)
      expect(validateEvent(JSON.parse(JSON.stringify(event)))).toMatchObject({ ok: true })
    }
  })

  it('is rejected as invalid_field, a reason 0.1 knows, when an ID passes the service’s limit', async () => {
    for (const input of [
      { conversationId: 'c', orgId: 'o', toolName: 't'.repeat(513), question: 'q' },
      { conversationId: 'c', orgId: 'o\u0000', toolName: 't', question: 'q' },
      { conversationId: 'c', orgId: 'o\uD800', toolName: 't', question: 'q' },
    ]) {
      const [event] = await send(input)
      expect(validateEvent(JSON.parse(JSON.stringify(event)))).toMatchObject({ ok: false, reason: 'invalid_field' })
    }
    // In the conversation, the 0.1 event ID has it too: rejected by position, as for NUL, which the
    // 0.1 SDK reports as an event the service didn't acknowledge.
    const [event] = await send({ conversationId: 'c\uD800', turnId: '1', orgId: 'o', toolName: 't', question: 'q' })
    expect(validateEvent(JSON.parse(JSON.stringify(event)))).toEqual({ ok: false, reason: 'invalid_field' })
  })
})
