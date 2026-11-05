// The deep review's last data findings, on TypeScript's side: what both SDKs changed, and guards
// for what TypeScript already did as the Python fixes now do. Where both must give the same event,
// the case is in packages/contract/cases.
import { Client as Client1 } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport as Memory1 } from '@modelcontextprotocol/sdk/inMemory.js'
import { McpServer as McpServer1 } from '@modelcontextprotocol/sdk/server/mcp.js'
import { Client as Client2 } from '@modelcontextprotocol/client'
import { McpServer as McpServer2, InMemoryTransport as Memory2 } from '@modelcontextprotocol/server'
import { describe, expect, it } from 'vitest'
import { z } from 'zod'
import { validateEvent } from '../../contract/src/validate.js'
import { afterHook, buildCall, buildTurn, type CallRecord, capturedIds, type Context, type Draft, readAgent } from '../src/event.js'
import { LSquare, type LSquareError, type LSquareEvent, type ToolCallEvent, type TurnEvent } from '../src/index.js'
import { instrument } from '../src/mcp/index.js'
import { resolvePatterns } from '../src/redact.js'
import { Binary, binarySize, CUT_MARK, newState, Secret, shapeJson } from '../src/shape.js'

const context: Context = { sdk: { name: 'lsquare', version: '0.2.0', language: 'js' }, patterns: resolvePatterns(undefined), redactKeys: [], maxEventBytes: 256 * 1024 }
const prose = (chars: number) => 'lorem ipsum '.repeat(Math.ceil(chars / 12)).slice(0, chars)
const AT = '2026-10-28T00:00:00.000Z'

function record(over: Partial<CallRecord> = {}): CallRecord {
  return { callId: 'b3c2f0a4-1111-4111-8111-000000000001', capturedAt: AT, toolName: 'search_orders', status: 'completed', durationMs: 12, ids: {}, diagnostics: false, ...over }
}
function draft(built: ReturnType<typeof buildCall>): Draft {
  if (!built.ok) throw new Error(built.code)
  return built.draft
}
const call = (over: Partial<CallRecord> = {}, hook = false, extra: Partial<Context> = {}) => draft(buildCall(record(over), { ...context, ...extra }, hook))
const turn = (input: Record<string, unknown>, hook = false) => draft(buildTurn(input, AT, context, hook))
const hooked = (pending: Draft, out: Record<string, unknown>) => draft(afterHook(out, pending, context))
const valid = (event: LSquareEvent) => {
  const result = validateEvent(JSON.parse(JSON.stringify(event)))
  return result.ok ? 'valid' : result.reason
}

// py-data-12: a value under a secret key name that came out empty without being empty.
describe('a secret made empty by the budget or by what JSON cannot hold', () => {
  it('gets a number of its own', () => {
    const state = newState({ budget: 'password'.length + 1, secretKeys: () => true })
    state.secretKey = state.secretKeys
    expect(shapeJson({ password: ['hunter2'] }, 16_000, state)).toEqual({ password: new Secret(undefined) })
    const event = call({ input: { value: { password: { k: Buffer.from('hunter2') }, token: { f: () => 1 }, auth: {}, cookies: [] } } }).event as ToolCallEvent
    expect(event.input).toEqual({ password: '[SECRET_1]', token: '[SECRET_2]', auth: {}, cookies: [] })
    expect(event.redactions).toEqual({ secret: 2 })
  })
})

// py-data-13: a key is charged before its value is read.
describe('the scan budget', () => {
  it('reads no value past it, a getter included', () => {
    const reads: string[] = []
    const recorded = (values: Record<string, unknown>) => {
      const out = {}
      for (const [key, value] of Object.entries(values)) {
        Object.defineProperty(out, key, {
          enumerable: true,
          get: () => {
            reads.push(key)
            return value
          },
        })
      }
      return out
    }
    // "first" costs 6 and its value the 2 left: "second", charged before it's read, never is.
    const state = newState({ budget: 8 })
    expect(Object.keys(shapeJson(recorded({ first: 'more than the budget', second: 's' }), 16_000, state) as object)).toEqual(['first'])
    expect(state.scan.remaining).toBe(0)
    expect(reads).toEqual(['first'])
    reads.length = 0
    shapeJson(recorded({ a: recorded({ b: recorded({ c: 'more than the budget', d: 'd' }), e: 'e' }), f: 'f' }), 16_000, newState({ budget: 8 }))
    expect(reads).toEqual(['a', 'b', 'c'])
  })
})

// parity-2: a capture() inside a call reads its data when it is called.
describe("a capture() inside a call, read when it's called", () => {
  function agentCall(agent: Record<string, unknown>, early: boolean, inputChars: number): ToolCallEvent {
    const extra = { maxEventBytes: 10 * 1024 * 1024 }
    let given: Record<string, unknown> = { ...agent }
    if (early) {
      given = capturedIds(agent)
      readAgent(given, agent, { ...context, ...extra })
    }
    const input = { value: { q: Array<string>(inputChars / 16_000).fill(prose(16_000)) } }
    const result = { value: { content: Array(30).fill({ type: 'text', text: prose(5_000) }) } }
    return call({ input, result, agent: given }, false, extra).event as ToolCallEvent
  }

  it("charges what it read to the call's budget, in its place", () => {
    const agent = { question: prose(16_000), answer: prose(20_000), turnId: 1 }
    const late = agentCall(agent, false, 880_000)
    const early = agentCall(agent, true, 880_000)
    const texts = (early.result as { content: Array<{ text: string }> }).content.map((block) => block.text)
    const exact = texts.filter((text) => text.endsWith('…[+1000 chars]')).length
    expect(exact).toBeGreaterThan(0)
    expect(exact).toBeLessThan(30)
    expect(early).toEqual(late)
  })

  it('keeps what it read where less was left for it', () => {
    const agent = { question: prose(16_000), answer: prose(20_000), turnId: 1 }
    const late = agentCall(agent, false, 976_000)
    const early = agentCall(agent, true, 976_000)
    expect(late.agent?.answer?.endsWith(CUT_MARK)).toBe(true)
    expect(early.agent?.answer).toBe(`${prose(16_000)}…[+4000 chars]`)
    // The budget is used up there: no block of the result is taken, as in order.
    expect(early.result).toEqual(late.result)
    expect(early.result).toEqual({ content: [] })
  })

  it('keeps only its IDs of what the app gave', () => {
    const fields = { orgId: 'o', userId: 'u', conversationId: 'c', turnId: 7, question: 'q', toolCalls: [{ name: 'search' }] }
    const ids = capturedIds(fields)
    expect({ ...ids }).toEqual({ orgId: 'o', userId: 'u', conversationId: 'c', turnId: 7 })
    readAgent(ids, fields, context)
    fields.toolCalls.length = 0
    expect((call({ agent: ids }).event as ToolCallEvent).agent).toEqual({ question: 'q', turnId: '7', status: 'completed', toolCalls: [{ name: 'search' }] })
  })

  it('that threw while it was read drops the call, never read again', () => {
    let returned = false
    const toolCalls = [{}]
    Object.defineProperty(toolCalls, 0, {
      get: () => {
        if (!returned) throw new Error('not yet')
        return { name: 'late', durationMs: 999 }
      },
    })
    const fields = { conversationId: 'c', question: 'q', toolCalls }
    const ids = capturedIds(fields)
    readAgent(ids, fields, context)
    returned = true
    expect(() => buildCall(record({ agent: ids }), context, false)).toThrow()
  })
})

/** One SDK's high-level server and an in-memory client of it. */
const sdks = [
  {
    name: 'SDK 1.x',
    async serve(tools: Record<string, () => unknown>, client: LSquare) {
      const server = new McpServer1({ name: 'orders', version: '1.0.0' })
      for (const [name, tool] of Object.entries(tools)) server.registerTool(name, { inputSchema: { q: z.string().optional() } }, tool as never)
      instrument(server, client, { extras: { sessions: false, intent: false, missingTools: false } })
      const [a, b] = Memory1.createLinkedPair()
      await server.connect(a)
      const connection = new Client1({ name: 'claude-code', version: '2.1' })
      await connection.connect(b)
      return { call: (name: string) => connection.callTool({ name, arguments: {} }) }
    },
  },
  {
    name: 'SDK 2.x',
    async serve(tools: Record<string, () => unknown>, client: LSquare) {
      const server = new McpServer2({ name: 'orders', version: '1.0.0' })
      for (const [name, tool] of Object.entries(tools)) server.registerTool(name, { inputSchema: z.object({ q: z.string().optional() }) }, tool as never)
      instrument(server, client, { extras: { sessions: false, intent: false, missingTools: false } })
      const [a, b] = Memory2.createLinkedPair()
      await server.connect(a)
      const connection = new Client2({ name: 'claude-code', version: '2.1' })
      await connection.connect(b)
      return { call: (name: string) => connection.callTool({ name, arguments: {} }) }
    },
  },
]

function setup() {
  const events: LSquareEvent[] = []
  const errors: LSquareError[] = []
  const fetch = (async (_url: unknown, init?: RequestInit) => {
    const batch = JSON.parse(String(init?.body)).events as LSquareEvent[]
    events.push(...batch)
    return new Response(JSON.stringify({ accepted: batch.map((e) => e.eventId), duplicates: [], rejected: [] }))
  }) as typeof globalThis.fetch
  const client = new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test', fetch, onError: (error) => errors.push(error), logger: { info: () => {}, warn: () => {} } })
  const calls = () => events.filter((e): e is ToolCallEvent => e.kind === 'tool_call')
  const turns = () => events.filter((e): e is TurnEvent => e.kind === 'turn')
  return { client, calls, turns, errors }
}
const text = (value: string) => ({ content: [{ type: 'text' as const, text: value }] })

describe.each(sdks)('$name: a capture() inside the tool', (sdk) => {
  it('reads its data before the tool returns: what the tool changes or reuses afterwards changes nothing', async () => {
    const { client, calls, turns } = setup()
    const connection = await sdk.serve(
      {
        ask: async () => {
          const toolCalls = [{ name: 'search', durationMs: 5 }]
          const usage = { inputTokens: 1, outputTokens: 1 }
          const metadata: { turn?: string } = { turn: '1' }
          client.capture({ conversationId: 'c', turnId: 1, question: 'first', toolCalls, usage, metadata })
          toolCalls.splice(0, 1, { name: 'fetch', durationMs: 7 })
          usage.inputTokens = 99
          metadata.turn = '2'
          client.capture({ conversationId: 'c', turnId: 2, question: 'again', toolCalls, usage, metadata })
          usage.inputTokens = 999
          toolCalls.length = 0
          delete metadata.turn
          return text('ok')
        },
      },
      client,
    )
    await connection.call('ask')
    await client.flush()
    const [event] = calls()
    expect(event!.agent).toMatchObject({ question: 'first', toolCalls: [{ name: 'search', durationMs: 5 }], usage: { inputTokens: 1, outputTokens: 1 } })
    expect(event!.metadata).toEqual({ turn: '1' })
    expect(event!.turnRef).toMatch(/^k\//)
    expect(turns()).toMatchObject([{ question: 'again', toolCalls: [{ name: 'fetch', durationMs: 7 }], usage: { inputTokens: 99, outputTokens: 1 }, metadata: { turn: '2' } }])
  })

  it('that threw while it was read drops the call, never reading it later', async () => {
    const { client, calls, errors } = setup()
    let returned = false
    const connection = await sdk.serve(
      {
        ask: async () => {
          const toolCalls = [{}]
          Object.defineProperty(toolCalls, 0, {
            get: () => {
              if (!returned) throw new Error('not yet')
              return { name: 'late', durationMs: 999 }
            },
          })
          client.capture({ conversationId: 'c', question: 'q', toolCalls })
          returned = true
          return text('ok')
        },
      },
      client,
    )
    expect(await connection.call('ask')).toEqual(text('ok'))
    await client.flush()
    expect(calls()).toEqual([])
    expect(errors.map((error) => error.code)).toEqual(['redaction_failed'])
  })

  it('from the app code that shaping what it holds runs, is a turn of its own, linked to the call', async () => {
    const { client, calls, turns } = setup()
    let returned = false
    const ran: boolean[] = []
    const connection = await sdk.serve(
      {
        ask: async () => {
          const usage = {
            get inputTokens() {
              if (ran.length === 0) {
                ran.push(returned)
                client.capture({ question: 'from a getter', conversationId: 'c' })
              }
              return 3
            },
          }
          client.capture({ question: 'agent', conversationId: 'c', usage })
          returned = true
          return text('ok')
        },
      },
      client,
    )
    await connection.call('ask')
    await client.flush()
    expect(ran).toEqual([false])
    const [event] = calls()
    expect(event!.agent?.usage).toEqual({ inputTokens: 3 })
    expect(turns().map((turn) => [turn.question, turn.callId])).toEqual([['from a getter', event!.callId]])
  })

  // Data review 1: its top-level fields are read before the slot is taken, so the promise above is
  // only for what shaping runs.
  it("from a getter of the object it's given, comes first and can be the call's agent part", async () => {
    const { client, calls, turns } = setup()
    let ran = 0
    const connection = await sdk.serve(
      {
        ask: async () => {
          client.capture({
            conversationId: 'c',
            get question() {
              if (ran++ === 0) client.capture({ question: 'from a getter', conversationId: 'c' })
              return 'agent'
            },
          })
          return text('ok')
        },
      },
      client,
    )
    await connection.call('ask')
    await client.flush()
    expect(ran).toBe(1)
    const [event] = calls()
    expect(event!.agent?.question).toBe('from a getter')
    expect(turns().map((turn) => [turn.question, turn.callId])).toEqual([['agent', event!.callId]])
  })

  // parity-3, a guard: TypeScript already drops it, as Python now does.
  it("whose ID can't be read is dropped, never sent without the ID", async () => {
    const { client, calls, errors } = setup()
    const connection = await sdk.serve(
      {
        ask: async () => {
          const input = { conversationId: 'c', question: 'private' }
          Object.defineProperty(input, 'userId', {
            enumerable: true,
            get: () => {
              throw new Error('no')
            },
          })
          client.capture(input)
          return text('ok')
        },
      },
      client,
    )
    await connection.call('ask')
    await client.flush()
    const [event] = calls()
    expect(event).not.toHaveProperty('agent')
    expect(event).not.toHaveProperty('userId')
    expect(event).not.toHaveProperty('conversationId')
    expect(errors.map((error) => error.code)).toEqual(['internal_error'])
  })
})

// parity-3, a guard: outside a call too, and in beforeSend's output.
describe("a field that can't be read", () => {
  const unreadable = (fields: Record<string, unknown>) => {
    Object.defineProperty(fields, 'userId', {
      enumerable: true,
      get: () => {
        throw new Error('no')
      },
    })
    return fields
  }

  it('drops the capture, never sending it without the field', async () => {
    const { client, turns, errors } = setup()
    client.capture(unreadable({ conversationId: 'c', question: 'private' }))
    await client.flush()
    expect(turns()).toEqual([])
    expect(errors.map((error) => error.code)).toEqual(['redaction_failed'])
  })
})

// parity-6, a guard: TypeScript reads only what the budget allows of a huge marked string, as Python now does.
describe("a huge string from beforeSend, marked as one this SDK cut", () => {
  it('is read only as far as the budget allows', () => {
    const pending = turn({ question: 'q' }, true)
    const out = hooked(pending, { ...pending.event, answer: `${'😀 '.repeat(10_000_000)}${CUT_MARK}` })
    expect((out.event as TurnEvent).answer).toBe(`${'😀 '.repeat(8_000)}${CUT_MARK}`)
  })
})

// parity-10, a guard: a file's size is from its exact UTF-16 length, as Python's within its allowance.
describe("a file's size", () => {
  it('is from its exact JavaScript length, however many times the event holds it', () => {
    const file = `${'A'.repeat(4_096)}${'😀'.repeat(10_000)}${'A'.repeat(1_024)}`
    expect(binarySize(file)).toBe((25_120 * 3) / 4)
    expect(shapeJson([[file, file]], 4_000, newState())).toEqual([[new Binary(18_840), new Binary(18_840)]])
  })
})

// parity-17: the error's category stays, whatever the hook puts in its place.
describe('an error the hook replaces with something else', () => {
  it('keeps its category', () => {
    const { error: _, ...rest } = call({ status: 'error', error: { message: 'boom', category: 'thrown' } }, true).event as ToolCallEvent
    for (const other of [false, 'hidden', ['x'], 0]) {
      const event = hooked(call({ status: 'error', error: { message: 'boom', category: 'thrown' } }, true), { ...rest, error: other }).event as ToolCallEvent
      expect(event.error).toEqual({ category: 'thrown' })
      expect(event.dropped).toEqual({ 'beforeSend.error': 'wrong_type' })
      expect(valid(event)).toBe('valid')
    }
  })
})

// parity-18: a short field over 16,000 characters is too_large, wherever it is: here, set by the hook.
describe("one of the call's short fields in beforeSend's output, over 16,000 characters", () => {
  it('is dropped as too_large there, and one of another type as before', () => {
    const long = 'x'.repeat(16_001)
    const pending = () => call({ client: { name: 'c' }, server: { name: 's' }, protocolVersion: '2025-06-18' }, true)
    let base = pending()
    let event = hooked(base, { ...base.event, client: { name: long }, server: { name: 's', version: long }, protocolVersion: long, task: { id: 't', status: long } })
      .event as ToolCallEvent
    expect(event).not.toHaveProperty('client')
    expect(event.server).toEqual({ name: 's' })
    expect(event).not.toHaveProperty('protocolVersion')
    expect(event).not.toHaveProperty('task')
    expect(event.dropped).toEqual({
      'beforeSend.server.version': 'too_large',
      'beforeSend.client': 'too_large',
      'beforeSend.protocolVersion': 'too_large',
      'beforeSend.task': 'too_large',
    })
    expect(valid(event)).toBe('valid')
    base = pending()
    event = hooked(base, { ...base.event, client: 5, server: { version: '1' } }).event as ToolCallEvent
    expect(event).not.toHaveProperty('dropped')
    expect(event).not.toHaveProperty('client')
    expect(event).not.toHaveProperty('server')
  })
})

// parity-20: numbering skips a placeholder anywhere in the event, with a hook as without.
describe('numbering', () => {
  it('skips a placeholder in a field never redacted, with a hook as without', () => {
    const fields = { userId: '[EMAIL_1]', question: 'mail jane@acme.io', surface: '[EMAIL_2]', toolCalls: [{ name: '[EMAIL_3]' }] }
    expect((turn(fields).event as TurnEvent).question).toBe('mail [EMAIL_4]')
    const pending = turn(fields, true)
    expect((hooked(pending, { ...pending.event }).event as TurnEvent).question).toBe('mail [EMAIL_4]')
    const base = call({ toolName: '[EMAIL_1]', input: { value: { email: 'jane@acme.io' } } }, true)
    const event = hooked(base, { ...base.event, client: { name: '[EMAIL_2]' }, server: { name: 's', version: '[EMAIL_3]' } }).event as ToolCallEvent
    expect(event.input).toEqual({ email: '[EMAIL_4]' })
    expect(event.client).toEqual({ name: '[EMAIL_2]' })
  })
})

// parity-7, a guard: a block's fields are strings only, which Python now follows but for a pydantic URL.
describe("a result block's fields", () => {
  it('are strings: a Date or a URL object is left out', () => {
    const when = new Date('2026-10-28T01:02:03.004Z')
    const event = call({
      result: { value: { content: [{ type: 'text', text: when }, { type: 'resource_link', uri: new URL('https://example.com/x'), name: when }] } },
    }).event as ToolCallEvent
    expect(event.result).toEqual({ content: [{ type: 'text', text: '' }, { type: 'resource_link' }] })
  })
})

// parity-13: a turnId's size is its UTF-8 bytes as JSON writes it (packages/contract/cases/ids.json
// has the cases).
describe('a turnId', () => {
  it('just within 256 KB as JSON writes it is kept', () => {
    const big = { ...context, maxEventBytes: 1024 * 1024 }
    const within = '😀'.repeat((256 * 1024) / 4)
    const kept = draft(buildTurn({ conversationId: 'c', turnId: within }, AT, big, false)).event as TurnEvent
    expect(kept.turnId).toBe(within)
    expect(kept.eventId).toMatch(/^k\//)
    const escaped = '"'.repeat((256 * 1024) / 2)
    expect((draft(buildTurn({ conversationId: 'c', turnId: escaped }, AT, big, false)).event as TurnEvent).turnId).toBe(escaped)
    expect((draft(buildTurn({ conversationId: 'c', turnId: `${escaped}"` }, AT, context, false)).event as TurnEvent).dropped).toEqual({ turnId: 'too_large' })
  })
})
