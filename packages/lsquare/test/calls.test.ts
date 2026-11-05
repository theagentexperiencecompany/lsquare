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
