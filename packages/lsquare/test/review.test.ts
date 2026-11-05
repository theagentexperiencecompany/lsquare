// Regression tests for Codex's review of the 0.2 core, 28 October: one per finding, numbered as in
// the review.
import { describe, expect, it, vi } from 'vitest'
import { validateEvent } from '../../contract/src/validate.js'
import { INTERNAL } from '../src/client.js'
import { afterHook, buildCall, buildTurn, type CallRecord, type Context, type Draft } from '../src/event.js'
import { LSquare } from '../src/index.js'
import { resolvePatterns } from '../src/redact.js'
import { binarySize, decodedSize, newState, shapeJson } from '../src/shape.js'
import type { LSquareEvent, ToolCallEvent, TurnEvent } from '../src/types.js'

const context: Context = { sdk: { name: 'lsquare', version: '0.2.0', language: 'js' }, patterns: resolvePatterns(undefined), redactKeys: [], maxEventBytes: 256 * 1024 }
const prose = (chars: number) => 'lorem ipsum '.repeat(Math.ceil(chars / 12)).slice(0, chars)
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

function record(over: Partial<CallRecord> = {}): CallRecord {
  return { callId: 'call-1', capturedAt: '2026-10-28T00:00:00.000Z', toolName: 'search_orders', status: 'completed', durationMs: 1, ids: {}, diagnostics: false, ...over }
}
function draft(built: ReturnType<typeof buildCall>): Draft {
  if (!built.ok) throw new Error(built.code)
  return built.draft
}
const call = (over: Partial<CallRecord> = {}, hook = false) => draft(buildCall(record(over), context, hook))
const turn = (input: Record<string, unknown>, hook = false) => draft(buildTurn(input, '2026-10-28T00:00:00.000Z', context, hook))
const hooked = (pending: Draft, out: Record<string, unknown>) => draft(afterHook(out, pending, context))
const valid = (event: LSquareEvent) => {
  const result = validateEvent(JSON.parse(JSON.stringify(event)))
  return result.ok ? 'valid' : result.reason
}

describe('1. a secret cut at the limit', () => {
  it.each([
    ['after token=', 'token=', `eyJhbGciOiJIUzI1NiJ9.${'a'.repeat(30_000)}.sig`],
    ['after a slash', 'https://hooks.example/', `sk-${'k'.repeat(30_000)}`],
    ['after a dot', 'value.', `ghp_${'g'.repeat(30_000)}`],
  ])('is masked %s', (_name, before, secret) => {
    const answer = (turn({ answer: `${prose(15_899)} ${before}${secret}` }).event as TurnEvent).answer as string
    expect(answer).toContain(`${before}[SECRET_1]…`)
    expect(answer).not.toContain(secret.slice(0, 12))
  })

  it('but not after a letter or digit, where no pattern lets one start', () => {
    const answer = (turn({ answer: `${prose(15_899)} risk-${'a'.repeat(30_000)}` }).event as TurnEvent).answer as string
    expect(answer).toContain('risk-aaa')
  })
})

describe('2. fields that are content, not identifiers', () => {
  it("redacts a task's ID and status, and a block's type and MIME type", () => {
    const event = call({
      task: { id: 'task for jane@acme.io', status: 'waiting on sk-abcdefghijklmnopqrstuvwxyz' },
      result: { value: { content: [{ type: 'image', mimeType: 'image/png; jane@acme.io', data: 'QUJD' }, { type: 'x jane@acme.io' }] } },
    }).event as ToolCallEvent
    expect(event.task).toEqual({ id: 'task for [EMAIL_1]', status: 'waiting on [SECRET_1]' })
    expect(event.result).toEqual({ content: [{ type: 'image', mimeType: 'image/png; [EMAIL_1]', bytes: 3 }, { type: 'x [EMAIL_1]' }] })
  })

  it('lets beforeSend change or remove a task', () => {
    const pending = call({ task: { id: 'task_1', status: 'working' } }, true)
    const { task, ...rest } = pending.event as ToolCallEvent
    expect(hooked(pending, rest).event).not.toHaveProperty('task')
  })
})

describe("3. promises in a call's values", () => {
  it('never become unhandled rejections', async () => {
    const seen: unknown[] = []
    const listener = (reason: unknown) => seen.push(reason)
    process.on('unhandledRejection', listener)
    try {
      const rejected = () => Promise.reject(new Error('from the app'))
      // An ID that is a promise is invalid: the event is dropped, and the promise silenced.
      expect(buildCall(record({ ids: { orgId: rejected() as unknown as string } }), context, false)).toMatchObject({ code: 'invalid_id' })
      call({
        server: rejected(),
        client: rejected(),
        intent: rejected(),
        error: { message: rejected(), type: rejected(), category: 'thrown' },
        agent: { question: rejected(), error: rejected(), toolCalls: rejected(), latency: rejected() },
        metadata: rejected(),
        task: rejected() as unknown as NonNullable<CallRecord['task']>,
      })
      await sleep(20)
    } finally {
      process.off('unhandledRejection', listener)
    }
    expect(seen).toEqual([])
  })
})

describe('4. work outside the budget', () => {
  it("is bounded: base64 padding, a data: URI's header", () => {
    const equals = '='.repeat(2_000_000)
    const started = performance.now()
    expect(decodedSize(equals.length, equals)).toBe(Math.floor(((2_000_000 - 2) * 3) / 4))
    expect(binarySize(`data:${'x'.repeat(2_000_000)},AAAA`)).toBeUndefined()
    expect(performance.now() - started).toBeLessThan(50)
  })

  it('a turnId too long to fit in an event is dropped before it is hashed', () => {
    const built = turn({ conversationId: 'c', turnId: 'x'.repeat(300_000) })
    expect(built.notes.dropped).toEqual({ turnId: 'too_large' })
    expect(built.event.eventId).toMatch(/^[0-9a-f-]{36}$/)
  })
})

describe('5. an event waiting for beforeSend', () => {
  const emails = (i: number) => Array.from({ length: 1_400 }, (_, j) => `u${i}_${j}@acme.io`).join(' ')

  it('keeps nothing of what redaction matched in content that fitting left out', () => {
    const pending = call({ input: { value: { texts: Array.from({ length: 40 }, (_, i) => emails(i)) } } }, true)
    expect(pending.event).not.toHaveProperty('input')
    expect(pending.redactor.retainedBytes()).toBeLessThan(1_000)
  })

  it('counts what it keeps for what it still holds against the queue', async () => {
    // Fifteen cut strings of distinct emails fit in the event: their numbering is kept, and counted.
    const pending = call({ input: { value: { texts: Array.from({ length: 15 }, (_, i) => emails(i)) } } }, true)
    expect(pending.event).toHaveProperty('input')
    const retained = pending.redactor.retainedBytes()
    expect(retained).toBeGreaterThan(JSON.stringify(pending.event).length)
    const errors: string[] = []
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://ingest.test',
      fetch: (async () => new Response('{}')) as typeof fetch,
      beforeSend: (event) => event,
      maxQueueBytes: JSON.stringify(pending.event).length + 1_000,
      onError: (error) => errors.push(error.code),
      logger: { info() {}, warn() {} },
    })
    // Room for the event alone, not for what its numbering keeps: refused.
    client[INTERNAL].record(record({ input: { value: { texts: Array.from({ length: 15 }, (_, i) => emails(i)) } } }))
    await sleep(20)
    expect(errors).toEqual(['queue_full'])
  })
})

describe('7. beforeSend output for a call', () => {
  it("can't add a file's data back, and keeps the sizes this SDK worked out", () => {
    const pending = call({ result: { value: { content: [{ type: 'image', data: 'QUJD', mimeType: 'image/png' }] } } }, true)
    const event = pending.event as ToolCallEvent
    const content = [...(event.result!.content as object[]), { type: 'audio', data: 'QUJDREVG', mimeType: 'audio/wav' }]
    const out = hooked(pending, { ...event, result: { ...event.result, content } }).event as ToolCallEvent
    expect(out.result).toEqual({ content: [{ type: 'image', mimeType: 'image/png', bytes: 3 }, { type: 'audio', mimeType: 'audio/wav', bytes: 6 }] })
  })

  it('keeps the error valid: its category stays when the hook empties or removes it', () => {
    const pending = call({ status: 'error', error: { message: 'boom', category: 'thrown' } }, true)
    const { error, ...rest } = pending.event as ToolCallEvent
    for (const out of [{ ...rest, error: {} }, rest, { ...rest, error: { message: 'm', category: 'protocol' } }]) {
      const event = hooked(call({ status: 'error', error: { message: 'boom', category: 'thrown' } }, true), out).event as ToolCallEvent
      expect(event.error?.category).toBe('thrown')
      expect(valid(event)).toBe('valid')
    }
    expect(error).toEqual({ message: 'boom', category: 'thrown' })
  })
})

describe('10. a string this SDK cut, after beforeSend', () => {
  it('keeps its exact count when the hook left it alone, though redaction made it longer', () => {
    const pending = turn({ answer: 'a@b.co '.repeat(2_400) }, true)
    const before = (pending.event as TurnEvent).answer as string
    expect(before.endsWith('…[+800 chars]')).toBe(true)
    expect((hooked(pending, { ...pending.event }).event as TurnEvent).answer).toBe(before)
  })

  it("is cut again when the hook moves it to a field with a tighter limit, as Codex's check of the fixes found", () => {
    const pending = call({ input: { value: { q: prose(17_000) } } }, true)
    const cut = ((pending.event as ToolCallEvent).input as { q: string }).q
    const out = hooked(pending, { ...pending.event, intent: cut, result: { content: [{ type: 'text', text: cut }] } }).event as ToolCallEvent
    expect(out.intent).toBe(`${prose(500)}…[+16500 chars]`)
    expect((out.result as { content: { text: string }[] }).content[0]!.text).toBe(`${prose(4_000)}…[+13000 chars]`)
    expect((out.input as { q: string }).q).toBe(cut)
  })

  it('is recognised only while it is in the event the hook got: one left out for size is not kept', () => {
    const rows = Array.from({ length: 30 }, (_, i) => `${i} ${prose(16_500)}`)
    const pending = call({ input: { value: { rows } } }, true)
    expect(pending.event).not.toHaveProperty('input')
    const cut = `${rows[0]!.slice(0, 16_000)}…[+502 chars]`
    const out = hooked(pending, { ...pending.event, intent: cut }).event as ToolCallEvent
    expect(out.intent).toBe(`${cut.slice(0, 500)}…[+16002 chars]`)
  })

  it('adds what was cut before to the count when the hook made it longer', () => {
    const pending = turn({ answer: prose(17_000) }, true)
    const before = (pending.event as TurnEvent).answer as string
    const longer = `${before.slice(0, -'…[+1000 chars]'.length)}${prose(500)}…[+1000 chars]`
    expect((hooked(pending, { ...pending.event, answer: longer }).event as TurnEvent).answer).toBe(`${prose(16_000)}…[+1500 chars]`)
  })
})

describe('11. values under secret key names', () => {
  it('are replaced before beforeSend sees them, and a short one is numbered like the same secret elsewhere', () => {
    const key = `sk-${'k'.repeat(40)}`
    const pending = call({ input: { value: { password: prose(20_000), apiKey: key, note: `uses ${key}` } } }, true)
    expect((pending.event as ToolCallEvent).input).toEqual({ password: '[SECRET_1]', apiKey: '[SECRET_2]', note: `uses ${key}` })
    expect((hooked(pending, { ...pending.event }).event as ToolCallEvent).input).toEqual({ password: '[SECRET_1]', apiKey: '[SECRET_2]', note: 'uses [SECRET_2]' })
  })

  it('a long one, which can\'t be read whole, gets a number of its own, never shared', () => {
    const long = prose(20_000)
    const event = call({ input: { value: { password: long, token: long, note: long } } }).event as ToolCallEvent
    const input = event.input as Record<string, string>
    expect(input.password).toBe('[SECRET_1]')
    expect(input.token).toBe('[SECRET_2]')
  })

  it('in metadata too', () => {
    const pending = turn({ metadata: { sessionToken: 'abc', plan: 'pro' } }, true)
    expect((pending.event as TurnEvent).metadata).toEqual({ sessionToken: '[SECRET_1]', plan: 'pro' })
  })
})

describe("12. a call's IDs", () => {
  it('are checked as the ingestion checks them', () => {
    for (const [over, path] of [
      [{ ids: { userId: 'a\u0000b' } }, 'ids.userId'],
      [{ ids: { orgId: 7 as unknown as string } }, 'ids.orgId'],
      [{ sessionId: 'x'.repeat(513) }, 'sessionId'],
      [{ turnRef: 'k/short' }, 'turnRef'],
    ] as const) {
      expect(buildCall(record(over), context, false)).toMatchObject({ ok: false, code: 'invalid_id', paths: [path] })
    }
  })

  it('never let ids replace the callId', () => {
    const event = call({ ids: { callId: 'other', orgId: 'org_1' } as CallRecord['ids'] }).event as ToolCallEvent
    expect(event).toMatchObject({ eventId: 'call-1', callId: 'call-1', orgId: 'org_1' })
  })

  it("keep the agent part's turnId, which the turnRef was worked out from, across beforeSend", () => {
    const pending = call({ turnRef: `k/${'a'.repeat(64)}`, agent: { question: 'q', turnId: 3 } }, true)
    const event = pending.event as ToolCallEvent
    const out = hooked(pending, { ...event, agent: { ...event.agent, turnId: '99' } }).event as ToolCallEvent
    expect(out.agent?.turnId).toBe('3')
  })
})

describe('13. limited diagnostics', () => {
  it('are reduced before any size check, so oversized content never drops them', () => {
    const big = { tenant: 'x'.repeat(262_000) }
    const direct = call({ diagnostics: true, metadata: big }).event
    expect(valid(direct)).toBe('valid')
    const pending = call({ diagnostics: true, input: { value: { rows: Array.from({ length: 30 }, () => prose(15_000)) } } }, true)
    const out = hooked(pending, { ...pending.event, metadata: big }).event
    expect(out).toHaveProperty('identity', 'failed')
    expect(valid(out)).toBe('valid')
  })
})

describe('14. oversized metadata on a call', () => {
  it('drops the event, as on a turn, before and after beforeSend', () => {
    const big = { tenant: 'x'.repeat(300_000) }
    expect(buildCall(record({ metadata: big }), context, false)).toMatchObject({ ok: false, code: 'too_large' })
    const pending = call({}, true)
    expect(afterHook({ ...pending.event, metadata: big }, pending, context)).toMatchObject({ ok: false, code: 'too_large' })
  })
})

describe('15. lengths in code points', () => {
  it('for short fields and for the length of a result that is not a string', () => {
    expect((turn({ client: '😀'.repeat(8_001) }).event as TurnEvent).client).toHaveLength(16_002)
    expect(turn({ client: '😀'.repeat(16_001) }).notes.dropped).toEqual({ client: 'too_large' })
    const event = turn({ toolCalls: [{ name: 'x', result: { x: '😀' } }] }).event as TurnEvent
    expect(event.toolCalls).toEqual([{ name: 'x', resultChars: 9 }])
  })
})

describe('16. coverage', () => {
  it('a call loses its whole agent last, after its toolCalls', () => {
    const agent = { question: prose(15_000), answer: prose(15_000), verification: { rows: Array.from({ length: 16 }, () => prose(15_000)) } }
    const event = call({ input: { value: { q: 'x' } }, agent, intent: 'kept' }).event as ToolCallEvent
    // The input goes before the agent part, however small.
    expect(event.truncated).toEqual(['input', 'agent'])
    expect(event).not.toHaveProperty('agent')
    expect(event).not.toHaveProperty('input')
    expect(event.intent).toBe('kept')
    expect(valid(event)).toBe('valid')
  })

  it('a turn loses its answer, then its question, once its toolCalls are gone', () => {
    const toolCalls = [{ name: 't', input: { rows: Array.from({ length: 16 }, () => prose(15_000)) } }]
    const event = turn({ question: prose(15_000), answer: prose(15_000), verification: { rows: Array.from({ length: 18 }, () => prose(15_000)) }, toolCalls }).event as TurnEvent
    expect(event.truncated).toEqual(['toolCalls', 'answer', 'question', 'verification'])
    expect(valid(event)).toBe('valid')
  })

  it('every array slot costs the budget, holes included', () => {
    const state = newState({ budget: 20 })
    const started = performance.now()
    expect(shapeJson(new Array(1_000_000), 16_000, state)).toHaveLength(20)
    expect(performance.now() - started).toBeLessThan(20)
  })

  it("objects are read in JavaScript's order, integer-like keys first, which numbering follows", () => {
    const event = call({ input: { value: { b: 'b@acme.io', 10: 'ten@acme.io', 2: 'two@acme.io' } } }).event as ToolCallEvent
    expect(event.input).toEqual({ 2: '[EMAIL_1]', 10: '[EMAIL_2]', b: '[EMAIL_3]' })
  })
})

// Codex's check of the fixes, numbered as in that review.
describe('check 1. two different secrets', () => {
  it('never share a number because shortening made them look alike', () => {
    const event = call({ input: { value: { password: 'a'.repeat(2_000), token: 'b'.repeat(2_000), secret: 'a'.repeat(2_000) } } }).event as ToolCallEvent
    expect(event.input).toEqual({ password: '[SECRET_1]', token: '[SECRET_2]', secret: '[SECRET_1]' })
  })

  it('in objects, count only when nothing in them was cut or turned into a size', () => {
    const event = call({ input: { value: { auth: { file: 'QUJD'.repeat(500) }, credentials: { file: 'QUJE'.repeat(500) } } } }).event as ToolCallEvent
    expect(event.input).toEqual({ auth: '[SECRET_1]', credentials: '[SECRET_2]' })
  })
})

describe('check 2. secrets that differ only in a lone surrogate', () => {
  it('get different numbers', () => {
    const z = 'z'.repeat(65)
    const event = call({ input: { value: { password: `${z}\ud800`, token: `${z}\ud801`, secret: `${z}\ud800` } } }).event as ToolCallEvent
    expect(event.input).toEqual({ password: '[SECRET_1]', token: '[SECRET_2]', secret: '[SECRET_1]' })
  })
})

describe('check 4. error.category', () => {
  it('is read once, and a promise in it never becomes an unhandled rejection', async () => {
    const seen: unknown[] = []
    const listener = (reason: unknown) => seen.push(reason)
    process.on('unhandledRejection', listener)
    let reads = 0
    try {
      const error = {
        message: 'm',
        get category() {
          reads++
          return Promise.reject(new Error('from the app'))
        },
      } as unknown as NonNullable<CallRecord['error']>
      const event = call({ status: 'error', error }).event as ToolCallEvent
      expect(event.error).toEqual({ message: 'm' })
      await sleep(20)
    } finally {
      process.off('unhandledRejection', listener)
    }
    expect(seen).toEqual([])
    expect(reads).toBe(1)
  })
})

describe('check 5. redactKeys in result blocks', () => {
  it("replace a block's name, uri, MIME type or text, before the hook too", () => {
    const masked = buildCall(
      record({ result: { value: { content: [{ type: 'resource_link', uri: 'file:///a', name: 'Jane Doe' }] } } }),
      { ...context, redactKeys: ['name'] },
      true,
    )
    expect(draft(masked).event).toMatchObject({ result: { content: [{ type: 'resource_link', uri: 'file:///a', name: '[SECRET_1]' }] } })
  })
})

describe('check 6. a string marked as cut', () => {
  it("is only recognised if the SDK cut it, not from a field it never cuts such as an error's type", () => {
    const forged = `${prose(15_000)}…[cut]`
    const pending = call({ status: 'error', error: { message: 'm', type: forged, category: 'thrown' } }, true)
    const out = hooked(pending, { ...pending.event, result: { content: [{ type: 'text', text: forged }] } }).event as ToolCallEvent
    expect((out.result as { content: { text: string }[] }).content[0]!.text).toBe(`${prose(4_000)}…[cut]`)
  })
})

describe('check 7. a marked string after the hook', () => {
  it('is read once, and the budget charged for it', () => {
    const pending = turn({ question: 'q' }, true)
    const answer = `${'x '.repeat(2_000_000)}…[cut]`
    // Characters read, counted where shortening reads them; the old check read up to a million more.
    const spy = vi.spyOn(String.prototype, 'charCodeAt')
    let out: TurnEvent
    let reads: number
    try {
      out = hooked(pending, { ...pending.event, answer }).event as TurnEvent
    } finally {
      // Counted before restoring, which clears the count.
      reads = spy.mock.calls.length
      spy.mockRestore()
    }
    expect(reads).toBeGreaterThan(900_000)
    expect(reads).toBeLessThan(1_050_000)
    expect(out.answer).toBe(`${'x '.repeat(8_000)}…[cut]`)
  })
})

// Codex's second check of the fixes.
describe('second check 1. an event waiting for its hook', () => {
  it("counts the agent part's turnId it keeps, though fitting left the agent out", async () => {
    const errors: string[] = []
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://ingest.test',
      fetch: (async () => new Response('{}')) as typeof fetch,
      beforeSend: (event) => event,
      maxQueueBytes: 1_000,
      onError: (error) => errors.push(error.code),
      logger: { info() {}, warn() {} },
    })
    client[INTERNAL].record(record({ agent: { turnId: 'x'.repeat(240_000), question: prose(16_000), answer: prose(16_000) } }))
    await sleep(20)
    expect(errors).toEqual(['queue_full'])
  })
})

describe('second check 2. redactKeys in result blocks', () => {
  it('number each value by its raw value, before it could look like a file or be cut', () => {
    const blocks = [{ type: 'text', text: 'a'.repeat(2_000) }, { type: 'text', text: 'b'.repeat(2_000) }, { type: 'text', text: 'a'.repeat(2_000) }, { type: 'text', text: prose(5_000) }, { type: 'text', text: prose(5_000) }]
    const pending = draft(buildCall(record({ result: { value: { content: blocks } } }), { ...context, redactKeys: ['text'] }, true))
    const texts = (event: LSquareEvent) => ((event as ToolCallEvent).result!.content as { text: string }[]).map((block) => block.text)
    // Too long to read whole: a number each, never shared.
    expect(texts(pending.event)).toEqual(['[SECRET_1]', '[SECRET_2]', '[SECRET_1]', '[SECRET_3]', '[SECRET_4]'])
    expect(texts(hooked(pending, { ...pending.event }).event)).toEqual(['[SECRET_1]', '[SECRET_2]', '[SECRET_1]', '[SECRET_3]', '[SECRET_4]'])
  })
})
