import { describe, expect, it } from 'vitest'
import { validateEvent } from '../../contract/src/validate.js'
import { afterHook, buildCall, buildTurn, type CallRecord, type Context } from '../src/event.js'
import { turnKey } from '../src/normalize.js'
import { resolvePatterns } from '../src/redact.js'
import type { LSquareEvent, ToolCallEvent, TurnEvent } from '../src/types.js'

const context: Context = {
  sdk: { name: 'lsquare', version: '0.2.0', language: 'js', runtime: 'node/22' },
  patterns: resolvePatterns(undefined),
  redactKeys: [],
  maxEventBytes: 256 * 1024,
}
const prose = (chars: number) => 'lorem ipsum '.repeat(Math.ceil(chars / 12)).slice(0, chars)

function record(over: Partial<CallRecord> = {}): CallRecord {
  return {
    callId: 'b3c2f0a4-1111-4111-8111-000000000001',
    capturedAt: '2026-10-28T00:00:00.000Z',
    toolName: 'search_orders',
    status: 'completed',
    durationMs: 12,
    ids: {},
    diagnostics: false,
    ...over,
  }
}

function call(over: Partial<CallRecord> = {}, hook = false) {
  const built = buildCall(record(over), context, hook)
  if (!built.ok) throw new Error(built.code)
  return built.draft
}

/** What the ingestion says about an event: stored, or its rejection reason. */
function ingestion(event: LSquareEvent): string {
  const result = validateEvent(JSON.parse(JSON.stringify(event)))
  return result.ok ? 'valid' : result.reason
}

describe('a tool_call event', () => {
  it('carries the call, its IDs and its outline, with the callId as its event ID', () => {
    const event = call({
      ids: { orgId: 'org_1', userId: 'u_1', conversationId: 'conv/1' },
      turnRef: turnKey('org_1', 'conv/1', '3'),
      server: { name: 'orders', version: '1.0.0', other: 'x' },
      client: { name: 'claude-code', version: '2.1' },
      protocolVersion: '2026-07-28',
      sessionId: 'ses_abcdefghijklmnopqrstuv',
      sessionSource: 'issued',
      input: { value: { q: 'late orders' } },
      result: { value: { content: [{ type: 'text', text: '3 orders' }], isError: false } },
      round: 'continuation',
      task: { id: 'task_1', status: 'working' },
      intent: 'Find the late orders of this customer',
      metadata: { plan: 'pro' },
    }).event as ToolCallEvent
    expect(event).toEqual({
      eventId: 'b3c2f0a4-1111-4111-8111-000000000001',
      schemaVersion: 2,
      kind: 'tool_call',
      capturedAt: '2026-10-28T00:00:00.000Z',
      source: 'live',
      sdk: context.sdk,
      callId: 'b3c2f0a4-1111-4111-8111-000000000001',
      toolName: 'search_orders',
      orgId: 'org_1',
      userId: 'u_1',
      conversationId: 'conv/1',
      turnRef: turnKey('org_1', 'conv/1', '3'),
      server: { name: 'orders', version: '1.0.0' },
      client: { name: 'claude-code', version: '2.1' },
      protocolVersion: '2026-07-28',
      sessionId: 'ses_abcdefghijklmnopqrstuv',
      sessionSource: 'issued',
      status: 'completed',
      durationMs: 12,
      round: 'continuation',
      task: { id: 'task_1', status: 'working' },
      input: { q: 'late orders' },
      result: { content: [{ type: 'text', text: '3 orders' }], isError: false },
      intent: 'Find the late orders of this customer',
      metadata: { plan: 'pro' },
    })
    expect(ingestion(event)).toBe('valid')
  })

  it('records errors with their category, and a code only when given one', () => {
    const thrown = call({ status: 'error', error: { message: 'boom for jane@acme.io', type: 'TypeError', category: 'thrown' } }).event as ToolCallEvent
    expect(thrown.error).toEqual({ message: 'boom for [EMAIL_1]', type: 'TypeError', category: 'thrown' })
    const protocol = call({ status: 'error', error: { message: 'Unknown tool', code: -32602, category: 'protocol' } }).event as ToolCallEvent
    expect(protocol.error).toEqual({ message: 'Unknown tool', code: -32602, category: 'protocol' })
    // An isError result's text is left out with the results: the category still says what happened.
    const hidden = call({ status: 'error', error: { category: 'tool_error' } }).event as ToolCallEvent
    expect(hidden.error).toEqual({ category: 'tool_error' })
    for (const event of [thrown, protocol, hidden]) expect(ingestion(event)).toBe('valid')
  })

  it("keeps every 0.1 capture field in agent, and the turn's IDs out of it", () => {
    const agent = {
      question: 'How many?', answer: 'Two.', turnId: 3, status: 'completed', error: { message: 'none' },
      toolCalls: [{ name: 'find', input: { q: 'x' }, resultChars: 3 }], usage: { inputTokens: 1, outputTokens: 2, costUsd: 0.1 },
      latency: { totalMs: 9 }, verification: { passed: true }, client: 'claude-code', apiKeyId: 'key_1', surface: 'mcp',
      orgId: 'org_1', conversationId: 'c', toolName: 'ask_docs',
    }
    const event = call({ agent }).event as ToolCallEvent
    const { orgId, conversationId, toolName, ...fields } = agent
    expect(event.agent).toEqual({ ...fields, turnId: '3' })
    expect(ingestion(event)).toBe('valid')
  })

  it('refuses a tool name the ingestion would', () => {
    expect(buildCall(record({ toolName: 'x'.repeat(513) }), context, false)).toMatchObject({ ok: false, code: 'invalid_id' })
  })
})

describe('events over 256 KB', () => {
  const big = () => Array.from({ length: 30 }, (_, i) => `${i} ${prose(15_000)}`)

  it('a tool_call loses its result, then its input, then its agent.toolCalls, then its agent, and stays valid', () => {
    const agent = { question: prose(15_000), answer: prose(15_000), toolCalls: Array.from({ length: 20 }, (_, i) => ({ name: `t${i}`, input: { q: prose(15_000) } })) }
    const event = call({
      input: { value: { rows: big() } },
      result: { value: { structuredContent: { rows: Array.from({ length: 80 }, (_, i) => `${i} ${prose(3_900)}`) } } },
      agent,
    }).event as ToolCallEvent
    // The result was also shortened, where the scan budget ran out, so it's named once.
    expect(event.truncated).toEqual(['result', 'input', 'agent.toolCalls'])
    expect(event).not.toHaveProperty('result')
    expect(event).not.toHaveProperty('input')
    expect(event.agent).not.toHaveProperty('toolCalls')
    expect(event.agent?.question).toBe(agent.question)
    expect(JSON.stringify(event).length).toBeLessThanOrEqual(256 * 1024)
    expect(ingestion(event)).toBe('valid')
  })

  it('a turn loses its toolCalls, then its answer, then its question', () => {
    const built = buildTurn(
      { question: prose(15_000), answer: prose(15_000), toolCalls: Array.from({ length: 20 }, (_, i) => ({ name: `t${i}`, input: { rows: [prose(15_000)] } })) },
      '2026-10-28T00:00:00.000Z',
      context,
      false,
    )
    if (!built.ok) throw new Error(built.code)
    const event = built.draft.event as TurnEvent
    expect(event.truncated).toEqual(['toolCalls'])
    expect(event).not.toHaveProperty('toolCalls')
    expect(event.answer).toBe(prose(15_000))
    expect(ingestion(event)).toBe('valid')
  })
})

describe('limited diagnostics', () => {
  const failed = (over: Partial<CallRecord> = {}) =>
    call({
      diagnostics: true,
      status: 'error',
      ids: { orgId: 'org_1' },
      input: { value: { email: 'jane@acme.io' } },
      result: { value: { content: [{ type: 'text', text: 'no' }] } },
      error: { message: 'Not found: jane@acme.io', code: 404, category: 'tool_error' },
      client: { name: "jane's-laptop" },
      server: { name: 'acme-orders' },
      protocolVersion: '2026-07-28',
      sessionId: 'ses_abcdefghijklmnopqrstuv',
      intent: 'look someone up',
      agent: { question: 'who?' },
      metadata: { tenant: 'acme' },
      ...over,
    })

  it('keep only what every event needs, the outline of the call and the error category', () => {
    const event = failed().event
    expect(event).toEqual({
      eventId: 'b3c2f0a4-1111-4111-8111-000000000001',
      kind: 'tool_call',
      schemaVersion: 2,
      capturedAt: '2026-10-28T00:00:00.000Z',
      source: 'live',
      sdk: context.sdk,
      callId: 'b3c2f0a4-1111-4111-8111-000000000001',
      toolName: 'search_orders',
      status: 'error',
      durationMs: 12,
      identity: 'failed',
      error: { category: 'tool_error' },
    })
    expect(ingestion(event)).toBe('valid')
  })

  it('are reduced after beforeSend, so a hook cannot add anything back', () => {
    const draft = failed({}, )
    const pending = buildCall(record({ diagnostics: true, ids: { orgId: 'org_1' }, input: { value: { q: 1 } } }), context, true)
    if (!pending.ok) throw new Error(pending.code)
    // Before the hook, the event is whole: the hook sees it as a call.
    expect(pending.draft.event).toHaveProperty('input')
    const out = { ...pending.draft.event, input: { added: true }, metadata: { added: true }, orgId: 'other' } as Record<string, unknown>
    const finished = afterHook(out, pending.draft, context)
    if (!finished.ok) throw new Error(finished.code)
    expect(Object.keys(finished.draft.event).sort()).toEqual(
      ['callId', 'capturedAt', 'durationMs', 'eventId', 'identity', 'kind', 'schemaVersion', 'sdk', 'source', 'status', 'toolName'],
    )
    expect(ingestion(finished.draft.event)).toBe('valid')
    expect(draft.event).toHaveProperty('identity', 'failed')
  })
})

describe('beforeSend', () => {
  it("can't change a call's identity or outline, but can change or remove its content and the client's name", () => {
    const pending = buildCall(
      record({ ids: { orgId: 'org_1', userId: 'u_1' }, client: { name: 'laptop' }, input: { value: { q: 'jane@acme.io' } }, intent: 'x' }),
      context,
      true,
    )
    if (!pending.ok) throw new Error(pending.code)
    // Not redacted yet: the hook sees the data, as in 0.1.
    expect((pending.draft.event as ToolCallEvent).input).toEqual({ q: 'jane@acme.io' })
    const { client, intent, ...rest } = pending.draft.event as ToolCallEvent
    const out = { ...rest, userId: 'someone_else', status: 'error', toolName: 'other', input: { q: 'bob@acme.io' } }
    const finished = afterHook(out, pending.draft, context)
    if (!finished.ok) throw new Error(finished.code)
    const event = finished.draft.event as ToolCallEvent
    expect(event).toMatchObject({ userId: 'u_1', status: 'completed', toolName: 'search_orders', input: { q: '[EMAIL_1]' } })
    expect(event).not.toHaveProperty('client')
    expect(event).not.toHaveProperty('intent')
    expect(event.redactions).toEqual({ email: 1 })
  })

  it("keeps a turn's IDs, keeps the markers of what was cut, and cuts again only what the hook made longer", () => {
    const pending = buildTurn({ orgId: 'org_1', conversationId: 'c', turnId: 1, question: 'q', answer: prose(17_000) }, '2026-10-28T00:00:00.000Z', context, true)
    if (!pending.ok) throw new Error(pending.code)
    const before = pending.draft.event as TurnEvent
    expect(before.answer).toBe(`${prose(16_000)}…[+1000 chars]`)
    expect(before.truncated).toEqual(['answer'])
    const out = { ...before, orgId: 'org_2', question: prose(20_000), truncated: [] }
    const finished = afterHook(out, pending.draft, context)
    if (!finished.ok) throw new Error(finished.code)
    const event = finished.draft.event as TurnEvent
    expect(event.eventId).toBe(turnKey('org_1', 'c', '1'))
    expect(event.orgId).toBe('org_1')
    expect(event.answer).toBe(`${prose(16_000)}…[+1000 chars]`)
    expect(event.question).toBe(`${prose(16_000)}…[+4000 chars]`)
    expect(event.truncated).toEqual(['answer', 'question'])
  })
})

describe("a call's tool description", () => {
  const event = (over: Partial<CallRecord>) => call(over).event as ToolCallEvent

  it('is kept as given, and the ingestion takes it', () => {
    const built = event({ toolDescription: 'Search orders by customer, newest first.' })
    expect(built.toolDescription).toBe('Search orders by customer, newest first.')
    expect(built).not.toHaveProperty('truncated')
    expect(ingestion(built)).toBe('valid')
  })

  it('is cut at 1,000 characters, counted as code points, and marked', () => {
    const built = event({ toolDescription: prose(1_500) })
    expect(built.toolDescription).toBe(`${prose(1_000)}…[+500 chars]`)
    expect(built.truncated).toEqual(['toolDescription'])
    expect(event({ toolDescription: prose(1_000) }).toolDescription).toBe(prose(1_000))
    // A pair outside the Basic Multilingual Plane is one character.
    expect(event({ toolDescription: '😀'.repeat(1_001) }).toolDescription).toBe(`${'😀'.repeat(1_000)}…[+1 chars]`)
  })

  it('is redacted as any text', () => {
    const built = event({ toolDescription: 'Mail billing@acme.io, or call +1 415 555 0134.' })
    expect(built.toolDescription).toBe('Mail [EMAIL_1], or call [PHONE_1].')
    expect(built.redactions).toEqual({ email: 1, phone: 1 })
  })

  it.each([[42], [{ text: 'Search orders' }], [['Search orders']], [true]])('is left out, and recorded as dropped, when it is %j', (value) => {
    const built = event({ toolDescription: value })
    expect(built).not.toHaveProperty('toolDescription')
    expect(built.dropped).toEqual({ toolDescription: 'wrong_type' })
    expect(ingestion(built)).toBe('valid')
  })

  it('is left out, without a word, when there is none', () => {
    for (const none of [undefined, null]) {
      const built = event({ toolDescription: none })
      expect(built).not.toHaveProperty('toolDescription')
      expect(built).not.toHaveProperty('dropped')
    }
  })

  it('is never in a limited-diagnostics event, before the hook or after it', () => {
    const built = event({ diagnostics: true, toolDescription: 'Look a person up by email.' })
    expect(built).toMatchObject({ identity: 'failed' })
    expect(built).not.toHaveProperty('toolDescription')
    expect(ingestion(built)).toBe('valid')
    const pending = call({ diagnostics: true, toolDescription: 'Look a person up by email.' }, true)
    const finished = afterHook({ ...pending.event, toolDescription: 'Put back by the hook.' }, pending, context)
    if (!finished.ok) throw new Error(finished.code)
    expect(finished.draft.event).not.toHaveProperty('toolDescription')
    expect(ingestion(finished.draft.event)).toBe('valid')
  })

  it('reaches beforeSend, which may change it, take it out, or put one in', () => {
    const hooked = (given: Partial<CallRecord>, change: (event: ToolCallEvent) => Record<string, unknown>) => {
      const pending = call(given, true)
      const finished = afterHook(change(pending.event as ToolCallEvent), pending, context)
      if (!finished.ok) throw new Error(finished.code)
      return { seen: pending.event as ToolCallEvent, event: finished.draft.event as ToolCallEvent }
    }
    // Not redacted yet: the hook sees the text, as for any field.
    const changed = hooked({ toolDescription: 'Mail billing@acme.io.' }, (seen) => ({ ...seen, toolDescription: `${seen.toolDescription} Or ops@acme.io.` }))
    expect(changed.seen.toolDescription).toBe('Mail billing@acme.io.')
    expect(changed.event.toolDescription).toBe('Mail [EMAIL_1]. Or [EMAIL_2].')
    const removed = hooked({ toolDescription: 'Internal.' }, ({ toolDescription, ...rest }) => rest)
    expect(removed.event).not.toHaveProperty('toolDescription')
    expect(removed.event).not.toHaveProperty('dropped')
    const added = hooked({}, (seen) => ({ ...seen, toolDescription: prose(1_200) }))
    expect(added.event.toolDescription).toBe(`${prose(1_000)}…[+200 chars]`)
    expect(added.event.truncated).toEqual(['toolDescription'])
    const wrong = hooked({ toolDescription: 'Search orders.' }, (seen) => ({ ...seen, toolDescription: 7 }))
    expect(wrong.event).not.toHaveProperty('toolDescription')
    expect(wrong.event.dropped).toEqual({ 'beforeSend.toolDescription': 'wrong_type' })
    // One this SDK cut keeps its marker when the hook hands it back.
    const cut = hooked({ toolDescription: prose(1_500) }, (seen) => ({ ...seen }))
    expect(cut.seen.toolDescription).toBe(`${prose(1_000)}…[+500 chars]`)
    expect(cut.event.toolDescription).toBe(`${prose(1_000)}…[+500 chars]`)
  })

  it('is read last: a result that uses the scan budget up is recorded as without one, and the description is what is cut', () => {
    const big = { input: { value: { q: prose(20_000) } }, result: { value: { content: [{ type: 'text', text: prose(1_200_000) }] } } }
    const without = event(big)
    const { toolDescription, truncated, ...rest } = event({ ...big, toolDescription: 'Search orders.' })
    expect(toolDescription).toBe('…[cut]')
    expect(truncated).toEqual([...(without.truncated ?? []), 'toolDescription'])
    const { truncated: _, ...plain } = without
    expect(rest).toEqual(plain)
  })

  it('is the first part left out to make an event fit: nothing only this call has goes to make room for it', () => {
    const base = record({
      ids: { orgId: 'org_1' },
      input: { value: { q: 'late orders' } },
      result: { value: { content: [{ type: 'text', text: '3 orders' }] } },
      agent: { question: 'Which orders are late?' },
    })
    const small = buildCall(base, context, false)
    if (!small.ok) throw new Error(small.code)
    // Room for the event without a description, and for saying one was left out; none for the description.
    const tight = { ...context, maxEventBytes: new TextEncoder().encode(JSON.stringify(small.draft.event)).length + 40 }
    const built = buildCall({ ...base, toolDescription: prose(900) }, tight, false)
    if (!built.ok) throw new Error(built.code)
    const { truncated, ...fitted } = built.draft.event as ToolCallEvent
    expect(truncated).toEqual(['toolDescription'])
    expect(fitted).toEqual(small.draft.event)
    expect(ingestion(built.draft.event)).toBe('valid')
    // An event too large without it too loses it, then its result, as before.
    const { result: _, ...noResult } = base
    const smaller = buildCall(noResult, context, false)
    if (!smaller.ok) throw new Error(smaller.code)
    const tighter = { ...context, maxEventBytes: new TextEncoder().encode(JSON.stringify(smaller.draft.event)).length + 50 }
    const less = buildCall({ ...base, result: { value: { content: [{ type: 'text', text: prose(2_000) }] } }, toolDescription: 'Search orders.' }, tighter, false)
    if (!less.ok) throw new Error(less.code)
    const { truncated: left, ...kept } = less.draft.event as ToolCallEvent
    expect(left).toEqual(['toolDescription', 'result'])
    expect(kept).toEqual(smaller.draft.event)
  })
})
