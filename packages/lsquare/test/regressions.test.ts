// Regression tests for bugs found in code review: re-entrancy, sentinels, work budgets, result
// lengths and non-2xx responses.
import { AsyncResource } from 'node:async_hooks'
import { Server as Server1 } from '@modelcontextprotocol/sdk/server/index.js'
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { INTERNAL } from '../src/client.js'
import { LSquare, type BeforeSend, type FlushResult, type LSquareError, type LSquareEvent, type LSquareOptions, type TurnEvent } from '../src/index.js'
import { instrument, type Identity, type InstrumentOptions } from '../src/mcp/index.js'
import { afterHook, buildCall, buildTurn, type CallRecord, type Draft } from '../src/event.js'
import { jsonLength, toJson } from '../src/json.js'
import { turnKey } from '../src/normalize.js'
import { resolvePatterns } from '../src/redact.js'
import { MAX_ENTRIES, newState, shapeJson } from '../src/shape.js'
import type { ToolCallEvent, TurnEvent as Turn } from '../src/types.js'

const base = { conversationId: 'conv1', orgId: 'org_1', toolName: 'ask_docs', question: 'q' }
/** Dropped by capture() with a report: an org that isn't a string. */
const invalid = { ...base, orgId: 7 as unknown as string }
const context = { sdk: { name: 'lsquare', version: '0.2.0', language: 'js' as const }, patterns: [], redactKeys: [], maxEventBytes: 256 * 1024 }
const turn = (input: Record<string, unknown>) => buildTurn(input, '2026-10-28T00:00:00.000Z', context, false)
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const ackFor = (init?: RequestInit) => {
  const events = JSON.parse(String(init?.body)).events as TurnEvent[]
  return new Response(JSON.stringify({ accepted: events.map((e) => e.eventId) }))
}

describe('re-entrant callbacks', () => {
  it('never sends an event after reporting it dropped, even if onError captures', async () => {
    const timeline: string[] = []
    const fetch = (async (_url: unknown, init?: RequestInit) => {
      const event = JSON.parse(String(init?.body)).events[0] as TurnEvent
      timeline.push(`send ${(event as TurnEvent).question}`)
      if ((event as TurnEvent).question === 'B') {
        return new Promise<Response>((_, reject) => init?.signal?.addEventListener('abort', () => reject(new Error('aborted'))))
      }
      return ackFor(init)
    }) as typeof globalThis.fetch
    const grow: BeforeSend = (event) => ((event as TurnEvent).question === 'A' ? { ...event, answer: 'x'.repeat(600) } : event)
    let client: LSquare
    client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      fetch,
      beforeSend: grow,
      maxQueueBytes: 1000,
      onError: (error, event) => {
        timeline.push(`dropped ${(event as TurnEvent | undefined)?.question ?? error.eventId}`)
        if ((event as TurnEvent | undefined)?.question === 'B') client.capture({ ...base, question: 'C' })
      },
    })
    client.capture({ ...base, question: 'B' })
    await sleep(5)
    client.capture({ ...base, question: 'A' })
    await sleep(50)
    await client.flush()
    for (const entry of timeline.filter((line) => line.startsWith('dropped '))) {
      const name = entry.slice('dropped '.length)
      const index = timeline.indexOf(entry)
      expect(timeline.slice(index + 1), `${name} sent after being reported dropped`).not.toContain(`send ${name}`)
    }
  })
})

describe('sentinels', () => {
  it('keeps the string "too_large" as data', () => {
    const result = turn({ ...base, verification: 'too_large', latency: 'too_large', toolCalls: [{ name: 'x', input: 'too_large' }] })
    expect(result.ok).toBe(true)
    if (result.ok) {
      const event = result.draft.event as Turn
      expect(event.verification).toBe('too_large')
      expect(event.toolCalls).toEqual([{ name: 'x', input: 'too_large' }])
    }
  })
})

describe('work budgets', () => {
  it('charges every array slot, including holes', () => {
    const started = performance.now()
    expect(toJson(new Array(1_000_000), { remaining: 20 })).toEqual({ ok: false, tooLarge: true })
    expect(performance.now() - started).toBeLessThan(20)
  })

  it('reads only the first 100 tool calls, valid or not', () => {
    const calls = Array.from({ length: 100_000 }, () => ({ tool: 'x' }))
    const started = performance.now()
    const result = turn({ ...base, toolCalls: calls })
    expect(performance.now() - started).toBeLessThan(50)
    if (!result.ok) throw new Error(result.code)
    expect(Object.keys(result.draft.notes.dropped)).toHaveLength(100)
  })

  it('shares one measuring allowance across all tool results', () => {
    const big = Array.from({ length: 900_000 }, () => 0)
    const started = performance.now()
    const result = turn({ ...base, toolCalls: Array.from({ length: 20 }, () => ({ name: 'x', result: big })) })
    expect(performance.now() - started).toBeLessThan(100)
    expect(result.ok).toBe(true)
  })

  describe("listing an object's keys, which costs its whole key table, outside the budget", () => {
    // JavaScript lists every key of an object, hidden ones too, before its first: the same big
    // object, referred to from every item of a list, would cost that at every item.
    /** `target` behind a Proxy that counts how often its keys are listed (for...in and Object.keys alike). */
    function counted(target: Record<string, unknown>) {
      const lists = { count: 0 }
      const value = new Proxy(target, {
        ownKeys(t) {
          lists.count++
          return Reflect.ownKeys(t)
        },
      })
      return { value, lists }
    }
    const keyed = (size: number) => Object.fromEntries(Array.from({ length: size }, (_, i) => [`k${i}`, i]))

    it('lists each object once per pass, however often the data refers to it, with few keys to keep or many', () => {
      for (const size of [1, MAX_ENTRIES + 100]) {
        const { value, lists } = counted(keyed(size))
        const shaped = shapeJson(Array(MAX_ENTRIES).fill(value), 16_000, newState()) as unknown[]
        expect(shaped).toHaveLength(MAX_ENTRIES)
        expect(shaped[MAX_ENTRIES - 1]).toEqual(keyed(Math.min(size, MAX_ENTRIES)))
        expect(lists.count).toBe(1)
      }
    })

    it("in tool calls too: an input they share, the tool call itself, and a result they share, which is only measured", () => {
      const input = counted(keyed(MAX_ENTRIES + 100))
      turn({ ...base, toolCalls: Array(MAX_ENTRIES).fill({ name: 'lookup', input: input.value }) })
      expect(input.lists.count).toBe(1)
      const item = counted({ ...keyed(MAX_ENTRIES + 100), name: 'lookup' })
      turn({ ...base, toolCalls: Array(MAX_ENTRIES).fill(item.value) })
      expect(item.lists.count).toBe(1)
      // Its first value isn't JSON: each measure stops there, with nearly all its allowance left.
      const result = counted({ 0: 1n, ...keyed(200) })
      const built = turn({ ...base, toolCalls: Array.from({ length: MAX_ENTRIES }, () => ({ name: 'lookup', result: result.value })) })
      expect(built.ok && built.draft.event.dropped).toMatchObject({ 'toolCalls[0].resultChars': 'not_json', 'toolCalls[99].resultChars': 'not_json' })
      expect(result.lists.count).toBe(1)
    })

    it('lists it again in the next event', () => {
      const { value, lists } = counted(keyed(MAX_ENTRIES + 100))
      for (let i = 0; i < 3; i++) turn({ ...base, verification: [value, value] })
      expect(lists.count).toBe(3)
    })
  })

  describe('past the scan budget, lists take no more entries: content blocks and tool calls too', () => {
    const prose = (chars: number) => 'lorem ipsum '.repeat(Math.ceil(chars / 12)).slice(0, chars)
    const record = (over: Partial<CallRecord> = {}): CallRecord => ({
      callId: 'call-1',
      capturedAt: '2026-10-28T00:00:00.000Z',
      toolName: 'search_orders',
      status: 'completed',
      durationMs: 1,
      ids: {},
      diagnostics: false,
      ...over,
    })
    const event = (built: ReturnType<typeof buildCall>) => {
      if (!built.ok) throw new Error(built.code)
      return built.draft.event
    }

    it("a call's result content and agent tool calls", () => {
      const toolCalls = Array.from({ length: MAX_ENTRIES }, (_, i) => ({ name: `tool_${i}`, input: { a: 1 } }))
      const content = Array.from({ length: MAX_ENTRIES }, (_, i) => ({ type: 'text', text: `block ${i}` }))
      const sent = event(buildCall(record({ input: { value: { big: prose(2_400_000) } }, agent: { toolCalls }, result: { value: { content } } }), context, false)) as ToolCallEvent
      expect(sent.agent).toEqual({ status: 'completed' })
      expect(sent.result).toEqual({ content: [] })
      expect(sent.truncated).toEqual(['input', 'agent', 'result'])
    })

    it('keeps the blocks and tool calls read before it ran out', () => {
      const content = [{ type: 'text', text: prose(1_200_000) }, { type: 'text', text: 'after' }]
      const blocks = (event(buildCall(record({ result: { value: { content } } }), context, false)) as ToolCallEvent).result as { content?: unknown[] }
      expect(blocks.content).toHaveLength(1)
      expect(blocks.content?.[0]).toMatchObject({ type: 'text' })
      const built = turn({ ...base, toolCalls: [{ name: 'a', input: { s: prose(2_000_000) } }, { name: 'b' }] })
      expect(built.ok && (built.draft.event as Turn).toolCalls?.map((call) => call.name)).toEqual(['a'])
    })

    it('a turn whose tool calls took none leaves them out: sent empty, they would say none was called', () => {
      const built = turn({ ...base, question: prose(2_000_000), toolCalls: [{ name: 'lookup' }] })
      if (!built.ok) throw new Error(built.code)
      expect(built.draft.event).not.toHaveProperty('toolCalls')
      expect(built.draft.event.truncated).toEqual(['question', 'toolCalls'])
    })
  })
})

describe('a polluted Object.prototype while an event is built', () => {
  // Another dependency's prototype pollution: the objects the SDK reads the app's data into, and
  // builds the event from, have no prototype, so a field one lacks never comes from it.
  const proto = Object.prototype as Record<string, unknown>
  /** Runs `run` with Object.prototype holding `key`, removed however `run` ends. */
  function polluted<T>(key: string, value: unknown, run: () => T): T {
    proto[key] = value
    try {
      return run()
    } finally {
      delete proto[key]
    }
  }
  /** Every field name an event, its parts and the SDK's own bookkeeping read. */
  const FIELD_NAMES = `orgId userId conversationId toolName callId question answer turnId status error toolCalls usage latency verification
client apiKeyId surface metadata message type code category name version input resultChars
resultIsError errorMessage durationMs inputTokens outputTokens costUsd content structuredContent isError text data mimeType uri resource
blob bytes task id intent agent server protocolVersion turnRef sessionId sessionSource round extra diagnostics value result identity
budget afterHook cuts secretKeys email phone card secret ip redactions dropped truncated`.split(/\s+/)
  const hooked = { ...context, patterns: resolvePatterns(undefined) }
  const record = (): CallRecord => ({
    callId: 'call-1',
    capturedAt: '2026-10-28T00:00:00.000Z',
    toolName: 'search_orders',
    status: 'error',
    durationMs: 1,
    ids: { conversationId: 'c1' },
    diagnostics: false,
    input: { value: { a: 1 } },
    agent: { question: 'mail jane@acme.io', toolCalls: [{ name: 'n' }] },
    result: { value: { content: [{ type: 'text', text: 'hi' }, { type: 'resource', resource: { uri: 'u', text: 'x' } }] } },
    error: { message: 'boom', category: 'thrown' },
  })
  const turnInput = () => ({ conversationId: 'c1', turnId: 't1', question: 'mail jane@acme.io', toolCalls: [{ name: 'n', input: { a: 1 } }], usage: { inputTokens: 1 }, error: { message: 'm' }, verification: { checked: true } })
  const draft = (built: ReturnType<typeof buildCall>): Draft => {
    if (!built.ok) throw new Error(built.code)
    return built.draft
  }
  const written = (built: ReturnType<typeof buildCall>) => JSON.stringify(built.ok ? built.draft.event : built)
  const after = (built: ReturnType<typeof buildCall>) => written(afterHook(JSON.parse(written(built)), draft(built), hooked))
  const builds: Array<[string, () => ReturnType<typeof buildCall>, (built: ReturnType<typeof buildCall>) => string]> = [
    ['a turn', () => buildTurn(turnInput(), '2026-10-28T00:00:00.000Z', hooked, false), written],
    ['a turn, after beforeSend', () => buildTurn(turnInput(), '2026-10-28T00:00:00.000Z', hooked, true), after],
    ['a call', () => buildCall(record(), hooked, false), written],
    ['a call, after beforeSend', () => buildCall(record(), hooked, true), after],
  ]

  it.each(builds)('adds or changes nothing in %s, whichever field it supplies', (_name, build, write) => {
    const clean = write(build())
    const changed = FIELD_NAMES.filter((key) => polluted(key, `POLLUTED_${key}`, () => write(build())) !== clean)
    expect(changed).toEqual([])
  })

  it.each(builds)("nor in %s, with a toJSON that would replace each of the app's objects", (_name, build, write) => {
    const clean = write(build())
    // Written out once the prototype is clean again, as the queue sends it later.
    const built = polluted('toJSON', () => 'POLLUTED', build)
    expect(write(built)).toEqual(clean)
  })

  it('leaves two turns their own event IDs: hashing them reads no toJSON', () => {
    const ids = (turnId: string) => (draft(buildTurn({ ...turnInput(), turnId }, '2026-10-28T00:00:00.000Z', hooked, false)).event.eventId)
    const clean = ['t1', 't2'].map(ids)
    expect(polluted('toJSON', () => 'POLLUTED', () => ['t1', 't2'].map(ids))).toEqual(clean)
    expect(polluted('toJSON', () => 'POLLUTED', () => turnKey('o', 'c', 't'))).toBe(turnKey('o', 'c', 't'))
  })

  it("can't put capture()'s turn in another org, or give it an answer the app never supplied", async () => {
    const bodies: string[] = []
    const fetch = (async (_url: unknown, init?: RequestInit) => {
      bodies.push(String(init?.body))
      return ackFor(init)
    }) as typeof globalThis.fetch
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch, logger: { info() {}, warn() {} } })
    proto.orgId = 'wrong_tenant'
    proto.answer = 'unrequested private prose'
    try {
      client.capture({ conversationId: 'c1', turnId: 't1', question: 'actual input' })
    } finally {
      delete proto.orgId
      delete proto.answer
    }
    expect(await client.flush()).toMatchObject({ sent: 1 })
    const [sent] = JSON.parse(bodies[0]!).events as Turn[]
    expect(sent).not.toHaveProperty('orgId')
    expect(sent).not.toHaveProperty('answer')
    // The turn's key without an org, as the ingestion works it out.
    expect(sent!.eventId).toBe(turnKey(undefined, 'c1', 't1'))
  })

  it("names the runtime it runs on, never one the global object inherits from it (Codex's third review)", async () => {
    const bodies: string[] = []
    const fetch = (async (_url: unknown, init?: RequestInit) => {
      bodies.push(String(init?.body))
      return ackFor(init)
    }) as typeof globalThis.fetch
    // The global object inherits from Object.prototype: a Node process has no Bun or Deno of its own.
    proto.Bun = { version: '1.4.4' }
    proto.Deno = { version: { deno: '2.5.0' } }
    let client: LSquare
    try {
      client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch, logger: { info() {}, warn() {} } })
    } finally {
      delete proto.Bun
      delete proto.Deno
    }
    client.capture({ conversationId: 'c1', turnId: 't1', question: 'q' })
    expect(await client.flush()).toMatchObject({ sent: 1 })
    const [sent] = JSON.parse(bodies[0]!).events as Turn[]
    expect(sent!.sdk.runtime).toBe(`node/${process.versions.node.split('.')[0]}`)
  })

  // The check of the third review's sweep: the core's own objects, and the runtime's `process`.
  /** A client made while `pollute` holds, with no endpoint given unless `endpoint`: where it sends a turn, and what it warned of. */
  async function made(pollute: () => () => void, endpoint?: string) {
    const urls: string[] = []
    const warned: string[] = []
    const fetch = (async (url: unknown, init?: RequestInit) => {
      urls.push(String(url))
      return ackFor(init)
    }) as typeof globalThis.fetch
    const restore = pollute()
    let client: LSquare
    try {
      client = new LSquare({ apiKey: 'k', fetch, logger: { info() {}, warn: (line) => void warned.push(line) }, ...(endpoint ? { endpoint } : {}) })
    } finally {
      restore()
    }
    client.capture({ conversationId: 'c1', turnId: 't1', question: 'q' })
    await client.flush()
    // A disabled client's warning has its own later turn.
    await new Promise((resolve) => setImmediate(resolve))
    return { urls, warned }
  }
  const field = (key: string, value: unknown) => () => {
    proto[key] = value
    return () => delete proto[key]
  }

  it('keeps capture on with a polluted problem while the client is made, as an invalid endpoint still turns it off', async () => {
    // `in` finds an inherited field: the endpoint's check reads its own.
    expect(await made(field('problem', 'polluted'), 'https://x.test')).toEqual({ urls: ['https://x.test/v1/turns'], warned: [] })
    expect(await made(field('problem', 'polluted'), 'http://x.test')).toEqual({ urls: [], warned: ['[lsquare] The endpoint must use https (http only for localhost): capture is disabled.'] })
  })

  // A runtime with no `process` of its own (Workers on an older compatibility date), or one whose
  // `process` has no `env`: simulated by replacing Node's while the client is made.
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'process')!
  const RUNTIMES = [
    ['without a process', 'process', (env: Record<string, string>) => ({ env })],
    ['whose process has no env', 'env', (env: Record<string, string>) => env],
  ] as const
  it.each(RUNTIMES)('takes no LSQUARE_ENDPOINT or LSQUARE_DRY_RUN from it on a runtime %s, as a process.env of its own still gives them', async (_name, key, value) => {
    const runtime = (fields: Record<string, string>) => () => {
      if (key === 'process') delete (globalThis as { process?: unknown }).process
      else Object.defineProperty(globalThis, 'process', { value: { versions: {} }, configurable: true, writable: true })
      proto[key] = value(fields)
      return () => {
        delete proto[key]
        Object.defineProperty(globalThis, 'process', descriptor)
      }
    }
    expect(await made(runtime({ LSQUARE_ENDPOINT: 'https://collector.example' }))).toEqual({ urls: ['https://ingest.lsquare.io/v1/turns'], warned: [] })
    expect(await made(runtime({ LSQUARE_DRY_RUN: '1' }))).toEqual({ urls: ['https://ingest.lsquare.io/v1/turns'], warned: [] })
    const own = (name: string, value: string) => () => {
      process.env[name] = value
      return () => delete process.env[name]
    }
    expect(await made(own('LSQUARE_ENDPOINT', 'https://own.test'))).toEqual({ urls: ['https://own.test/v1/turns'], warned: [] })
    expect((await made(own('LSQUARE_DRY_RUN', '1'))).urls).toEqual([])
  })

  // Codex's fourth review: each value the client reads of the runtime as it is made is silenced,
  // the objects along the way and the environment variable too, so a getter that gives a rejected
  // promise leaves none unhandled (one ends a Node process). Each global is replaced while the
  // client is made; the wrapper's own reads of them (instrument()'s serverless check) as a control.
  const node = process
  const rejecting = () => Promise.reject(new Error('a getter of the runtime'))
  /** `fields`, with `key` a getter that gives a rejected promise. */
  const withGetter = (fields: Record<string, unknown>, key: string) => Object.defineProperty({ ...fields }, key, { get: rejecting, enumerable: true, configurable: true })
  const GETTERS: Array<[string, string, () => unknown]> = [
    ['Bun', 'Bun', rejecting],
    ['Bun.version', 'Bun', () => withGetter({}, 'version')],
    ['Deno', 'Deno', rejecting],
    ['Deno.version', 'Deno', () => withGetter({}, 'version')],
    ['Deno.version.deno', 'Deno', () => ({ version: withGetter({}, 'deno') })],
    ['navigator', 'navigator', rejecting],
    ['navigator.userAgent', 'navigator', () => withGetter({}, 'userAgent')],
    ['process', 'process', rejecting],
    ['process.env', 'process', () => withGetter({ versions: node.versions, stderr: node.stderr }, 'env')],
    ['process.env.LSQUARE_ENDPOINT', 'process', () => ({ versions: node.versions, stderr: node.stderr, env: withGetter({}, 'LSQUARE_ENDPOINT') })],
    ['process.env.LSQUARE_DRY_RUN', 'process', () => ({ versions: node.versions, stderr: node.stderr, env: withGetter({}, 'LSQUARE_DRY_RUN') })],
    ['process.versions', 'process', () => withGetter({ env: {}, stderr: node.stderr }, 'versions')],
    ['process.versions.node', 'process', () => ({ env: {}, stderr: node.stderr, versions: withGetter({}, 'node') })],
    ['process.stderr', 'process', () => withGetter({ env: {}, versions: node.versions }, 'stderr')],
    ['process.stderr.write', 'process', () => ({ env: {}, versions: node.versions, stderr: withGetter({}, 'write') })],
    // Codex's fifth review: the runtime's fetch, read though the app gave its own; its console,
    // read to tell whether the app's logger is it; and the clock's performance, in a field
    // initializer, outside the constructor's guard.
    ['fetch', 'fetch', rejecting],
    ['console', 'console', rejecting],
    ['performance', 'performance', rejecting],
    // Codex's sixth review: the clock read performance's `now` to call it, and a getter there
    // gave a rejected promise each of the three times the client is made reads the clock.
    ['performance.now', 'performance', () => withGetter({}, 'now')],
    ['performance.now, read again', 'performance', () => {
      let reads = 0
      return Object.defineProperty({}, 'now', { get: () => (reads++ === 0 ? () => 1 : rejecting()), enumerable: true, configurable: true })
    }],
  ]
  it.each(GETTERS)('leaves no rejected promise unhandled when the runtime’s %s is a getter that gives one, as the client is made', async (_name, key, get) => {
    const seen: unknown[] = []
    const listener = (reason: unknown) => void seen.push(reason)
    node.on('unhandledRejection', listener)
    const replace = () => {
      const before = Object.getOwnPropertyDescriptor(globalThis, key)
      Object.defineProperty(globalThis, key, { get, configurable: true })
      return () => (before ? Object.defineProperty(globalThis, key, before) : delete (globalThis as Record<string, unknown>)[key])
    }
    try {
      // The client is still made, and sends where it would.
      expect(await made(replace)).toEqual({ urls: ['https://ingest.lsquare.io/v1/turns'], warned: [] })
      // Made first: the SDK's own Server reads the runtime's console as it is made (its JSON Schema validator).
      const server = new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
      const restore = replace()
      try {
        instrument(server, new LSquare({ apiKey: 'k', fetch: (async () => new Response('{}')) as typeof fetch, logger: { info() {}, warn() {} } }))
      } finally {
        restore()
      }
      await new Promise((resolve) => setTimeout(resolve, 20))
    } finally {
      node.off('unhandledRejection', listener)
    }
    expect(seen).toEqual([])
  })

  it('leaves no rejected promise unhandled warning, through the runtime’s console, that instrument() was given no client', async () => {
    const seen: unknown[] = []
    const listener = (reason: unknown) => void seen.push(reason)
    const server = new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
    const before = Object.getOwnPropertyDescriptor(globalThis, 'console')!
    node.on('unhandledRejection', listener)
    Object.defineProperty(globalThis, 'console', { get: rejecting, configurable: true })
    try {
      instrument(server, {} as never)
      // What it was handed instead is asked for the client's internals as any of the app's values is.
      instrument(server, new Proxy({}, { get: rejecting }) as never)
    } finally {
      Object.defineProperty(globalThis, 'console', before)
    }
    try {
      await new Promise((resolve) => setTimeout(resolve, 20))
    } finally {
      node.off('unhandledRejection', listener)
    }
    expect(seen).toEqual([])
  })

  it("leaves no rejected promise unhandled when the app's logger has a getter for its method that gives one: read once, the line goes nowhere", async () => {
    const seen: unknown[] = []
    const listener = (reason: unknown) => void seen.push(reason)
    let reads = 0
    node.on('unhandledRejection', listener)
    try {
      void new LSquare({ logger: { info() {}, get warn() { return reads++, rejecting() } } as never })
      for (let turn = 0; turn < 5; turn++) await new Promise((resolve) => setImmediate(resolve))
      await new Promise((resolve) => setTimeout(resolve, 20))
    } finally {
      node.off('unhandledRejection', listener)
    }
    expect(seen).toEqual([])
    expect(reads).toBe(1)
  })

  // Codex's fifth review: a getter of the runtime's that throws, as the client is made, whether
  // read in the constructor (`fetch`, `console`) or in a field initializer outside its guard
  // (`process`, for a default logger made even with the app's own, and `performance`, for the
  // client's clock). The client is still made, and sends where it would.
  const THROWING = ['process', 'performance', 'fetch', 'console', 'navigator', 'Bun', 'Deno']
  it.each(THROWING)('makes the client, which sends where it would, though the runtime’s %s is a getter that throws, the app giving its own logger and fetch', async (key) => {
    const replace = () => {
      const before = Object.getOwnPropertyDescriptor(globalThis, key)
      Object.defineProperty(globalThis, key, {
        get: () => {
          throw new Error(`the runtime's ${key}`)
        },
        configurable: true,
      })
      return () => (before ? Object.defineProperty(globalThis, key, before) : delete (globalThis as Record<string, unknown>)[key])
    }
    expect(await made(replace)).toEqual({ urls: ['https://ingest.lsquare.io/v1/turns'], warned: [] })
  })

  it('logs a report, its clock unread, while the runtime’s performance is a getter that gives a rejected promise all along', async () => {
    const warned: string[] = []
    const seen: unknown[] = []
    const listener = (reason: unknown) => void seen.push(reason)
    const before = Object.getOwnPropertyDescriptor(globalThis, 'performance')!
    node.on('unhandledRejection', listener)
    Object.defineProperty(globalThis, 'performance', { get: rejecting, configurable: true })
    try {
      const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch: (async () => new Response('{}')) as typeof fetch, logger: { info() {}, warn: (line) => void warned.push(line) } })
      // Dropped, and reported to the logger: when it last logged one is read off the clock.
      client.capture(invalid)
      for (let turn = 0; turn < 5; turn++) await new Promise((resolve) => setImmediate(resolve))
    } finally {
      Object.defineProperty(globalThis, 'performance', before)
    }
    try {
      await new Promise((resolve) => setTimeout(resolve, 20))
    } finally {
      node.off('unhandledRejection', listener)
    }
    expect(seen).toEqual([])
    expect(warned).toEqual([expect.stringMatching(/^\[lsquare\] invalid_id: /)])
  })

  it('never reads the runtime’s fetch for a client given its own: it is read only when needed', async () => {
    let reads = 0
    const before = Object.getOwnPropertyDescriptor(globalThis, 'fetch')!
    expect(await made(() => {
      Object.defineProperty(globalThis, 'fetch', { get: () => (reads++, before.value), configurable: true })
      return () => Object.defineProperty(globalThis, 'fetch', before)
    })).toEqual({ urls: ['https://ingest.lsquare.io/v1/turns'], warned: [] })
    expect(reads).toBe(0)
  })

  it("takes no fetch from it on a runtime without one: capture is off, as without any fetch", async () => {
    const before = Object.getOwnPropertyDescriptor(globalThis, 'fetch')!
    const polluting: string[] = []
    const warned: string[] = []
    delete (globalThis as { fetch?: unknown }).fetch
    proto.fetch = async (url: unknown, init?: RequestInit) => (polluting.push(String(url)), ackFor(init))
    let client: LSquare
    try {
      client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', logger: { info() {}, warn: (line) => void warned.push(line) } })
    } finally {
      delete proto.fetch
      Object.defineProperty(globalThis, 'fetch', before)
    }
    client.capture({ conversationId: 'c1', turnId: 't1', question: 'q' })
    await client.flush()
    await new Promise((resolve) => setImmediate(resolve))
    expect(polluting).toEqual([])
    expect(warned).toEqual(['[lsquare] fetch is not available in this runtime: capture is disabled.'])
  })

  // Codex's fifth review: the default logger, made only for a client given none, as it first
  // writes, reads standard error's `write` once, and every other method of the stream as it needs
  // it, each silenced, with what each returns, `write`'s too.
  /** A standard error that writes into `lines`, its own fields replaced by `fields`. */
  const stderr = (fields: PropertyDescriptorMap = {}) => {
    const lines: string[] = []
    const stream: Record<string, unknown> = {
      write: (text: string, done?: (error?: Error | null) => void) => (lines.push(text), done?.(null), true),
      listenerCount: () => 0,
      on: () => stream,
      once: () => stream,
      removeListener: () => stream,
    }
    return { stream: Object.defineProperties(stream, fields), lines }
  }
  /** A getter that gives `first` as it is first read, and a rejected promise after. */
  const firstThenRejecting = (first: unknown): PropertyDescriptor => {
    let reads = 0
    return { get: () => (reads++ === 0 ? first : rejecting()), configurable: true }
  }
  const NO_KEY = '[lsquare] No apiKey: capture is disabled.\n'
  const STREAMS: Array<[string, () => ReturnType<typeof stderr>, string[]]> = [
    ['a write read again giving one', () => {
      const made = stderr()
      return { ...made, stream: Object.defineProperty(made.stream, 'write', firstThenRejecting(made.stream.write)) }
    }, [NO_KEY]],
    ['a listenerCount getter that gives one', () => stderr({ listenerCount: { get: rejecting, configurable: true } }), [NO_KEY]],
    ['a listenerCount that returns one', () => stderr({ listenerCount: { value: rejecting, configurable: true } }), [NO_KEY]],
    ['a write that returns one', () => {
      const made = stderr()
      const write = made.stream.write as (...args: unknown[]) => unknown
      return { ...made, stream: Object.defineProperty(made.stream, 'write', { value: (...args: unknown[]) => (write(...args), rejecting()), configurable: true }) }
    }, [NO_KEY]],
    ['a once getter that gives one', () => stderr({ once: { get: rejecting, configurable: true } }), [NO_KEY]],
    ['a removeListener that returns one', () => stderr({ removeListener: { value: rejecting, configurable: true } }), [NO_KEY]],
    ['an on getter that gives one, as a write fails', () => {
      const made = stderr({ on: { get: rejecting, configurable: true } })
      return { ...made, stream: Object.defineProperty(made.stream, 'write', { value: (text: string, done?: (error?: Error | null) => void) => (made.lines.push(text), done?.(new Error('EPIPE')), true), configurable: true }) }
    }, [NO_KEY]],
  ]
  it.each(STREAMS)('leaves no rejected promise unhandled when the default logger’s standard error has %s, and still writes the line', async (_name, make, expected) => {
    const { stream, lines } = make()
    const seen: unknown[] = []
    const listener = (reason: unknown) => void seen.push(reason)
    const before = Object.getOwnPropertyDescriptor(globalThis, 'process')!
    node.on('unhandledRejection', listener)
    Object.defineProperty(globalThis, 'process', { get: () => ({ env: {}, versions: node.versions, stderr: stream }), configurable: true })
    try {
      void new LSquare({})
      // Its warning is written in a later turn.
      await new Promise((resolve) => setImmediate(() => setImmediate(resolve)))
    } finally {
      Object.defineProperty(globalThis, 'process', before)
    }
    try {
      await new Promise((resolve) => setTimeout(resolve, 20))
    } finally {
      node.off('unhandledRejection', listener)
    }
    expect(seen).toEqual([])
    expect(lines).toEqual(expected)
  })

  it('writes to the console where standard error can’t be read, as on Workers: a process getter that throws, or a console getter that gives a rejected promise', async () => {
    const seen: unknown[] = []
    const listener = (reason: unknown) => void seen.push(reason)
    const replaced = (key: string, descriptor: PropertyDescriptor) => {
      const before = Object.getOwnPropertyDescriptor(globalThis, key)
      Object.defineProperty(globalThis, key, { ...descriptor, configurable: true })
      return () => (before ? Object.defineProperty(globalThis, key, before) : delete (globalThis as Record<string, unknown>)[key])
    }
    const lines: string[] = []
    const console = { info: (line: string) => void lines.push(`info ${line}`), warn: (line: string) => void lines.push(`warn ${line}`) }
    node.on('unhandledRejection', listener)
    try {
      for (const replace of [
        // Node, its process unreadable: the console.
        () => [replaced('process', { get: () => { throw new Error('process') } }), replaced('console', { value: console })],
        // Workers, its console a getter that gives a rejected promise: nowhere to write.
        () => [replaced('navigator', { value: { userAgent: 'Cloudflare-Workers' } }), replaced('console', { get: rejecting })],
      ]) {
        const restore = replace()
        try {
          void new LSquare({})
          for (let turn = 0; turn < 5; turn++) await new Promise((resolve) => setImmediate(resolve))
        } finally {
          for (const undo of restore.reverse()) undo()
        }
      }
      await new Promise((resolve) => setTimeout(resolve, 20))
    } finally {
      node.off('unhandledRejection', listener)
    }
    expect(seen).toEqual([])
    expect(lines).toEqual(['warn [lsquare] No apiKey: capture is disabled.'])
  })

  // Codex's fifth review's sweep: capture()'s input, and its metadata, are asked for their
  // prototype once, to tell a plain object, and their keys listed with Object.keys(), never
  // `for...in`, which asks a Proxy for its prototype again, and consumes the answer itself.
  it("reads capture()'s input and its metadata, Proxies that give a rejected promise when asked for their prototype again, as plain objects asked once", async () => {
    const bodies: string[] = []
    const fetch = (async (_url: unknown, init?: RequestInit) => (bodies.push(String(init?.body)), ackFor(init))) as typeof globalThis.fetch
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch, logger: { info() {}, warn() {} } })
    const askedOnce = <T extends object>(target: T): T => {
      let asked = 0
      return new Proxy(target, { getPrototypeOf: (object) => (asked++ === 0 ? Reflect.getPrototypeOf(object) : rejecting()) })
    }
    const seen: unknown[] = []
    const listener = (reason: unknown) => void seen.push(reason)
    node.on('unhandledRejection', listener)
    try {
      client.capture(askedOnce({ conversationId: 'c1', turnId: 't1', question: 'q', metadata: { plan: 'pro' } }))
      client.capture({ conversationId: 'c1', turnId: 't2', question: 'q', metadata: askedOnce({ plan: 'pro' }) })
      expect(await client.flush()).toMatchObject({ sent: 2 })
      await new Promise((resolve) => setTimeout(resolve, 20))
    } finally {
      node.off('unhandledRejection', listener)
    }
    expect(seen).toEqual([])
    const sent = bodies.flatMap((body) => JSON.parse(body).events as Turn[])
    expect(sent).toMatchObject([{ question: 'q', metadata: { plan: 'pro' } }, { question: 'q', metadata: { plan: 'pro' } }])
  })

  it('reads what a custom fetch answers once each, its body’s reader too: a getter asked again, which would give a rejected promise, never is', async () => {
    const seen: unknown[] = []
    const listener = (reason: unknown) => void seen.push(reason)
    const fetch = (async (_url: unknown, init?: RequestInit) => {
      const real = ackFor(init)
      const body = real.body!
      Object.defineProperty(body, 'getReader', firstThenRejecting(body.getReader.bind(body)))
      const answer = {}
      for (const [key, value] of [['status', real.status], ['type', real.type], ['headers', real.headers], ['body', body]] as const) Object.defineProperty(answer, key, firstThenRejecting(value))
      return answer as Response
    }) as typeof globalThis.fetch
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch, logger: { info() {}, warn() {} } })
    node.on('unhandledRejection', listener)
    try {
      client.capture({ conversationId: 'c1', turnId: 't1', question: 'q' })
      expect(await client.flush()).toMatchObject({ sent: 1, failed: 0, pending: 0 })
      await new Promise((resolve) => setTimeout(resolve, 20))
    } finally {
      node.off('unhandledRejection', listener)
    }
    expect(seen).toEqual([])
  })

  it("still reads an error class's inherited name as its type", () => {
    class LookupError extends Error {}
    LookupError.prototype.name = 'LookupError'
    const built = turn({ question: 'q', error: new LookupError('failed') })
    expect(built.ok && (built.draft.event as Turn).error).toEqual({ message: 'failed', type: 'LookupError' })
  })

  it('gives beforeSend ordinary objects', () => {
    const built = buildTurn({ question: 'q', client: { name: 'c', version: '1' }, usage: { inputTokens: 1 } }, '2026-10-28T00:00:00.000Z', hooked, true)
    const event = draft(built).event as Turn
    expect(Object.getPrototypeOf(event)).toBe(Object.prototype)
    expect(Object.getPrototypeOf(event.client)).toBe(Object.prototype)
    expect(Object.getPrototypeOf(event.usage)).toBe(Object.prototype)
  })

  // Round 5's check, the write side: Object.defineProperty's default makes a field that isn't
  // writable, and a getter alone has no setter, so an assignment adding a field of that name to an
  // ordinary object throws in strict code; with a setter, the setter takes the value instead. The
  // SDK adds each field of what it builds as an object literal does, and defines with descriptors
  // that have no prototype. Each kind reads as absent: what's checked is the writing.
  const KINDS: Array<[string, PropertyDescriptor]> = [
    ["a field that isn't writable", { value: undefined, configurable: true }],
    ['a getter alone', { get: () => undefined, configurable: true }],
    ['a setter that keeps nothing', { get: () => undefined, set() {}, configurable: true }],
  ]
  /** Runs `run` with Object.prototype holding `key` as `descriptor` has it, removed however `run` ends. */
  async function definedAs<T>(key: string, descriptor: PropertyDescriptor, run: () => T | Promise<T>): Promise<T> {
    Object.defineProperty(proto, key, descriptor)
    try {
      return await run()
    } finally {
      delete proto[key]
    }
  }
  /** The event's names, the app's own keys in its data, and those a property descriptor is read for. */
  const NAMES = [...FIELD_NAMES, 'a', 'k', 'q', 'n', 'plan', 'nested', 'checked', 'totalMs', 'paths', 'omitted', 'get', 'set', 'writable', 'enumerable', 'configurable']
  const kinded = builds.flatMap(([name, build, write]) => KINDS.map(([kind, descriptor]) => [name, kind, descriptor, build, write] as const))
  it.each(kinded)('adds, changes or drops nothing in %s, though Object.prototype holds each of its names as %s', async (_name, _kind, descriptor, build, write) => {
    const clean = write(build())
    const changed: string[] = []
    for (const key of NAMES) {
      const out = await definedAs(key, descriptor, () => {
        try {
          return write(build())
        } catch (error) {
          return `threw ${String(error)}`
        }
      })
      if (out !== clean) changed.push(key)
    }
    expect(changed).toEqual([])
  })

  /**
   * A client whose fetch answers 200, or `status`, with a response of its own making (the
   * runtime's Response assigns its status as it is made), recording what it sends, the reports
   * onError gets and the lines it logs.
   */
  function handMade(options: LSquareOptions = {}, status = 200) {
    const sent: unknown[] = []
    const reports: string[] = []
    const lines: string[] = []
    const fetch = (async (_url: unknown, init?: RequestInit) => {
      const batch = JSON.parse(String(init?.body)).events as Array<{ eventId: string; capturedAt?: string }>
      sent.push(...batch.map(({ capturedAt: _at, ...event }) => event))
      const bytes = new TextEncoder().encode(JSON.stringify({ accepted: batch.map((e) => e.eventId), duplicates: [], rejected: [] }))
      let done = false
      const reader = { read: async () => (done ? { done: true, value: undefined } : ((done = true), { done: false, value: bytes })), cancel: async () => {} }
      return { status, type: 'basic', headers: { get: () => null }, body: { getReader: () => reader } }
    }) as unknown as typeof globalThis.fetch
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      fetch,
      maxAttempts: 1,
      onError: (error) => void reports.push(JSON.stringify(error)),
      logger: { info: (line) => void lines.push(line), warn: (line) => void lines.push(line) },
      ...options,
    })
    return { client, sent, reports, lines }
  }
  /** Turns through capture(), with and without beforeSend, in dry run, refused, and failing to send: what each sends, reports and logs. */
  async function captured(): Promise<string> {
    const out: unknown[] = []
    for (const options of [{}, { beforeSend: (event: LSquareEvent) => event }, { dryRun: true }]) {
      const { client, sent, reports, lines } = handMade(options)
      client.capture({ ...turnInput(), answer: 'jane@acme.io', metadata: { plan: 'pro', n: 1, nested: {} }, client: { name: 'c', version: '1' }, latency: { totalMs: 3 }, apiKeyId: 'k', surface: 's', status: 'error', unknownField: 1 } as never)
      client.capture({ conversationId: 'c1', turnId: 't2', question: 'x'.repeat(300_000), usage: 'many' } as never)
      client.capture({ conversationId: 7 } as never)
      await client.flush({ timeoutMs: 2_000 })
      out.push(sent, reports, lines.map((line) => line.replace(/"bytes":\d+/, '')))
    }
    const failing = handMade({}, 503)
    failing.client.capture({ conversationId: 'c1', turnId: 't3', question: 'q' })
    await failing.client.flush({ timeoutMs: 2_000 })
    out.push(failing.reports)
    return JSON.stringify(out)
  }
  // Codex's sixth review: put() assigns a field whose name Object.prototype doesn't hold, and
  // defines one whose name it does. Defined, its descriptor must have no prototype, which shows
  // only when Object.prototype holds a descriptor's key too: two names polluted at once.
  it.each(builds)('adds, changes or drops nothing in %s, though Object.prototype holds each of its names and a descriptor key, get or writable, as fields that are not writable', async (_name, build, write) => {
    const clean = write(build())
    const changed: string[] = []
    // Not writable, as Object.defineProperty() defines a field; the test's own descriptors without a prototype, as the SDK's.
    const notWritable = (): PropertyDescriptor => Object.assign(Object.create(null), { value: undefined, configurable: true })
    for (const extra of ['get', 'writable']) {
      for (const key of NAMES) {
        if (key === extra) continue
        const out = await definedAs(extra, notWritable(), () =>
          definedAs(key, notWritable(), () => {
            try {
              return write(build())
            } catch (error) {
              return `threw ${String(error)}`
            }
          }),
        )
        if (out !== clean) changed.push(`${extra}+${key}`)
      }
    }
    expect(changed).toEqual([])
  })

  it.each(KINDS)("sends, reports and logs each turn whole through the client, though Object.prototype holds each of its names as %s", async (_kind, descriptor) => {
    const clean = await captured()
    const changed: string[] = []
    // Node's own timers assign their list's `id` as they are made: under such a name a timer can't
    // be set, by the app or the SDK, so the runtime itself can't take it.
    const timers = (key: string) =>
      definedAs(key, descriptor, () => {
        try {
          clearTimeout(setTimeout(() => {}, 1))
          return true
        } catch {
          return false
        }
      })
    const runtime: string[] = []
    for (const key of NAMES) {
      if (!(await timers(key))) runtime.push(key)
      else if ((await definedAs(key, descriptor, captured)) !== clean) changed.push(key)
    }
    expect(changed).toEqual([])
    expect(runtime).toEqual(descriptor.set ? [] : ['id'])
  })

  // Round 5's check: what the app's callbacks and a custom fetch answer, read once each, silenced,
  // and adopted through the method read, never by `await`, `for await` or Promise.resolve(), which
  // read a `then`, a `done` or a `return` again themselves and consume what a getter gave.
  /** The rejections nothing handled while `run` ran, and a little after, as Node reports them. */
  async function unhandledIn(run: () => Promise<void>): Promise<unknown[]> {
    const seen: unknown[] = []
    const listener = (reason: unknown) => void seen.push(reason)
    node.on('unhandledRejection', listener)
    try {
      await run()
      for (let turn = 0; turn < 5; turn++) await new Promise((resolve) => setImmediate(resolve))
      await new Promise((resolve) => setTimeout(resolve, 20))
    } finally {
      node.off('unhandledRejection', listener)
    }
    return seen
  }
  /** A thenable whose `then` getter gives a rejected promise. */
  const thenRejecting = () => ({
    get then() {
      return rejecting()
    },
  })
  /** A thenable whose `then` getter gives a method that settles it, then a rejected promise. */
  const thenOnce = () => {
    let reads = 0
    return {
      get then() {
        return reads++ === 0 ? (resolve: (value: unknown) => void) => resolve(undefined) : rejecting()
      },
    }
  }
  const ANSWERS: Array<[string, (answer: () => unknown) => Promise<void>]> = [
    ["the logger's method", async (answer) => void new LSquare({ logger: { info() {}, warn: () => answer() } })],
    ['onError', async (answer) => {
      const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch: (async () => new Response('{}', { status: 500 })) as typeof fetch, maxAttempts: 1, onError: () => answer(), logger: { info() {}, warn() {} } })
      client.capture({ conversationId: 'c1', turnId: 't1', question: 'q' })
      await client.flush({ timeoutMs: 2_000 })
    }],
  ]
  const answers = ANSWERS.flatMap(([name, run]) => [['a then getter that gives one', thenRejecting], ['a then getter read again giving one', thenOnce]].map(([how, answer]) => [name, how, run, answer] as const))
  it.each(answers)('leaves no rejected promise unhandled when %s answers with %s: its then is read once, silenced', async (_name, _how, run, answer) => {
    expect(await unhandledIn(() => run(answer as () => unknown))).toEqual([])
  })

  /** A custom fetch answering with a response whose body `body()` makes from the acknowledgement's bytes. */
  const bodied = (body: (bytes: Uint8Array) => unknown) =>
    (async (_url: unknown, init?: RequestInit) => {
      const ids = (JSON.parse(String(init?.body)).events as Array<{ eventId: string }>).map((event) => event.eventId)
      return { status: 200, headers: new Headers(), body: body(new TextEncoder().encode(JSON.stringify({ accepted: ids, duplicates: [], rejected: [] }))) }
    }) as unknown as typeof globalThis.fetch
  /** One chunk, then the end, from `next` or `read`, the end's result made by `end()`. */
  const chunks = (bytes: Uint8Array, end: () => object = () => ({ done: true, value: undefined })) => {
    let sent = false
    return async () => (sent ? end() : ((sent = true), { done: false, value: bytes }))
  }
  const big = () => async () => ({ done: false, value: new Uint8Array(70 * 1024) })
  const BODIES: Array<[string, (bytes: Uint8Array) => unknown, 'sent' | 'failed']> = [
    ["a reader whose result's done is a getter that gives one", (bytes) => ({ getReader: () => ({ read: chunks(bytes, () => ({ get done() { return rejecting() }, value: undefined })), cancel() {} }) }), 'sent'],
    ['a reader whose read is a getter that gives one read again', (bytes) => {
      const reader = { cancel() {} }
      Object.defineProperty(reader, 'read', firstThenRejecting(chunks(bytes)))
      return { getReader: () => reader }
    }, 'sent'],
    ['a reader whose cancel is a getter that gives one, as a chunk past the limit stops the read', () => ({ getReader: () => ({ read: big(), get cancel() { return rejecting() } }) }), 'failed'],
    ["an async iterator whose result's done is a getter that gives one", (bytes) => ({ [Symbol.asyncIterator]: () => ({ next: chunks(bytes, () => ({ get done() { return rejecting() }, value: undefined })) }) }), 'sent'],
    ['an async iterator whose next is a getter that gives one read again', (bytes) => ({ [Symbol.asyncIterator]: () => Object.defineProperty({}, 'next', firstThenRejecting(chunks(bytes))) }), 'sent'],
    ['an async iterator whose return is a getter that gives one, as a chunk past the limit stops it', () => ({ [Symbol.asyncIterator]: () => ({ next: big(), get return() { return rejecting() } }) }), 'failed'],
  ]
  it.each(BODIES)("leaves no rejected promise unhandled when a custom fetch's body is %s: each member read once, silenced", async (_name, body, outcome) => {
    let result: FlushResult | undefined
    const seen = await unhandledIn(async () => {
      const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch: bodied(body), maxAttempts: 1, onError() {}, logger: { info() {}, warn() {} } })
      client.capture({ conversationId: 'c1', turnId: 't1', question: 'q' })
      result = await client.flush({ timeoutMs: 2_000 })
    })
    expect(seen).toEqual([])
    expect(result).toMatchObject(outcome === 'sent' ? { sent: 1, failed: 0 } : { sent: 0, failed: 1 })
  })

  it("writes no line through a method only Object.prototype has: a logger without info drops info lines, the dry run's too", async () => {
    const leaked: string[] = []
    Object.defineProperty(proto, 'info', { value: (line: string) => void leaked.push(line), configurable: true, writable: true })
    try {
      const client = new LSquare({ dryRun: true, logger: { warn() {} } as never })
      client.capture({ conversationId: 'c1', question: 'my card is 4111 1111 1111 1111' })
      await client.flush({ timeoutMs: 1_000 })
    } finally {
      delete proto.info
    }
    expect(leaked).toEqual([])
  })

  // Codex's sixth review. A transparent Proxy reads as what it wraps: round 5 read a Proxy through
  // get alone, a field counting only where Object.prototype had none of its name, so it lost its
  // own field wherever Object.prototype was polluted with that name. And what the SDK adopts it
  // adopts as `await` would, through Promise.resolve().
  /** Runs `run` with Object.prototype holding `key` as `value`, not enumerable, removed however `run` ends. */
  async function pollutedWith<T>(key: string, value: unknown, run: () => Promise<T>): Promise<T> {
    Object.defineProperty(proto, key, { value, writable: true, configurable: true })
    try {
      return await run()
    } finally {
      delete proto[key]
    }
  }
  it("keeps the orgId capture()'s input has of its own, handed over as a transparent Proxy, though Object.prototype has one too, as for the input itself", async () => {
    const orgs: unknown[] = []
    for (const input of [() => ({ conversationId: 'c1', orgId: 'org_real', question: 'q' }), () => new Proxy({ conversationId: 'c1', orgId: 'org_real', question: 'q' }, {})]) {
      const bodies: string[] = []
      const fetch = (async (_url: unknown, init?: RequestInit) => (bodies.push(String(init?.body)), ackFor(init))) as typeof globalThis.fetch
      await pollutedWith('orgId', 'org_polluted', async () => {
        const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch, logger: { info() {}, warn() {} } })
        client.capture(input())
        expect(await client.flush({ timeoutMs: 2_000 })).toMatchObject({ sent: 1 })
      })
      orgs.push((JSON.parse(bodies[0]!).events as Turn[])[0]?.orgId)
    }
    expect(orgs).toEqual(['org_real', 'org_real'])
  })

  it('takes the endpoint the options have of their own, handed over as a transparent Proxy, though Object.prototype has one too, as for the options themselves', async () => {
    const urls: string[][] = []
    for (const wrap of [<T>(options: T) => options, <T extends object>(options: T) => new Proxy(options, {})]) {
      const sent: string[] = []
      const fetch = (async (url: unknown, init?: RequestInit) => (sent.push(String(url)), ackFor(init))) as typeof globalThis.fetch
      await pollutedWith('endpoint', 'https://polluted.test', async () => {
        const client = new LSquare(wrap({ apiKey: 'k', endpoint: 'https://real.test', fetch, logger: { info() {}, warn() {} } }))
        client.capture({ conversationId: 'c1', question: 'q' })
        await client.flush(wrap({ timeoutMs: 2_000 }))
      })
      urls.push(sent)
    }
    expect(urls).toEqual([['https://real.test/v1/turns'], ['https://real.test/v1/turns']])
  })

  it("reads the destroy it checked, once, to stop a body it won't read: a getter giving no method the second time still closes it", async () => {
    // Codex's sixth review: discard() checked `destroy`, then read it again to call it.
    let reads = 0
    let closed = 0
    const body = {
      get destroy() {
        return reads++ === 0 ? () => void closed++ : undefined
      },
    }
    const fetch = (async () => ({ status: 401, type: 'basic', headers: new Headers(), body })) as unknown as typeof globalThis.fetch
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch, maxAttempts: 1, onError() {}, logger: { info() {}, warn() {} } })
    client.capture({ conversationId: 'c1', question: 'q' })
    expect(await client.flush({ timeoutMs: 2_000 })).toMatchObject({ sent: 0, failed: 1 })
    await client.shutdown()
    expect({ reads, closed }).toEqual({ reads: 1, closed: 1 })
  })

  /** A Promise subclass's instance, settled with `value`, whose own `then` hands on `adapt(value)` and counts its runs. */
  function adapting<T>(value: T, adapt: (value: T) => unknown, runs = { count: 0 }): Promise<unknown> {
    class Adapting extends Promise<unknown> {
      override then<A = unknown, B = never>(onFulfilled?: ((value: unknown) => A | PromiseLike<A>) | null, onRejected?: ((reason: unknown) => B | PromiseLike<B>) | null): Promise<A | B> {
        runs.count++
        return super.then(onFulfilled && ((item) => onFulfilled(adapt(item as T))), onRejected)
      }
    }
    return new Adapting((resolve) => resolve(value))
  }

  it("reads a custom fetch's body whose reader answers with a Promise subclass through that subclass's then, as await does", async () => {
    // Round 5 adopted each answer through Promise.prototype.then, skipping the subclass's own.
    let result: FlushResult | undefined
    const seen = await unhandledIn(async () => {
      const body = (bytes: Uint8Array) => {
        let sent = false
        return {
          getReader: () => ({
            read: () => (sent ? Promise.resolve({ done: true, value: undefined }) : ((sent = true), adapting({ done: false, value: new TextEncoder().encode('not the acknowledgement') }, () => ({ done: false, value: bytes })))),
            cancel() {},
          }),
        }
      }
      const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch: bodied(body), maxAttempts: 1, onError() {}, logger: { info() {}, warn() {} } })
      client.capture({ conversationId: 'c1', turnId: 't1', question: 'q' })
      result = await client.flush({ timeoutMs: 2_000 })
    })
    expect(seen).toEqual([])
    expect(result).toMatchObject({ sent: 1, failed: 0 })
  })

  it.each(ANSWERS)("waits for what %s answers, a Promise subclass, through that subclass's then, as await does", async (_name, run) => {
    const runs = { count: 0 }
    expect(await unhandledIn(() => run(() => adapting(undefined, (value) => value, runs)))).toEqual([])
    expect(runs.count).toBe(1)
  })

  // Round 6's check: round 6 silenced a native promise before adopting it, and Promise.prototype.then
  // made one more instance of a Promise subclass, through its species, than `await` makes. And
  // Promise.resolve() hands a native promise of this realm's Promise back as it is, so a method
  // called on what it gave was the app's own `then` where the promise had one, run at once.

  /** A Promise subclass whose constructor counts the instances made, and whose `then` records how many it saw made when it was called. */
  function counting() {
    const made = { count: 0, seen: [] as number[] }
    class Counting extends Promise<unknown> {
      constructor(executor: (resolve: (value: unknown) => void, reject: (reason: unknown) => void) => void) {
        super(executor)
        made.count++
      }
      override then<A = unknown, B = never>(onFulfilled?: ((value: unknown) => A | PromiseLike<A>) | null, onRejected?: ((reason: unknown) => B | PromiseLike<B>) | null): Promise<A | B> {
        made.seen.push(made.count)
        return super.then(onFulfilled, onRejected)
      }
    }
    return { answer: () => new Counting((resolve) => resolve(undefined)), made }
  }

  /** `promise`, a native promise of this realm's Promise, given a `then` of its own that counts its runs and hands on `adapt(value)`. */
  function ownThen<T>(promise: Promise<T>, adapt: (value: T) => unknown, runs: { count: number }): Promise<T> {
    ;(promise as any).then = function (this: Promise<T>, onFulfilled?: (value: unknown) => unknown, onRejected?: (reason: unknown) => unknown) {
      runs.count++
      return Promise.prototype.then.call(this, onFulfilled && ((value: unknown) => onFulfilled(adapt(value as T))), onRejected)
    }
    return promise
  }

  it.each(ANSWERS)('makes the instances of a Promise subclass %s answers with as often as await does, its then seeing as many made', async (_name, run) => {
    const awaited = counting()
    await awaited.answer()
    // The answer itself, then the one its then makes through the species; its then saw one made.
    expect(awaited.made).toEqual({ count: 2, seen: [1] })
    const answered = counting()
    expect(await unhandledIn(() => run(answered.answer))).toEqual([])
    expect(answered.made).toEqual(awaited.made)
  })

  it('follows what beforeSend answers, a native promise with a then of its own, as await does: that then never runs', async () => {
    const runs = { count: 0 }
    const sent: unknown[] = []
    const fetch = (async (_url: unknown, init?: RequestInit) => {
      sent.push(...(JSON.parse(String(init?.body)).events as TurnEvent[]).map((event) => event.question))
      return ackFor(init)
    }) as typeof globalThis.fetch
    // Its own then would drop the event; `await` gives the event.
    const beforeSend: BeforeSend = (event) => ownThen(Promise.resolve(event), () => null, runs) as never
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch, beforeSend, onError() {}, logger: { info() {}, warn() {} } })
    client.capture({ ...base, turnId: 't1', question: 'kept' })
    expect(await client.flush({ timeoutMs: 2_000 })).toMatchObject({ sent: 1, skipped: 0 })
    expect(runs.count).toBe(0)
    expect(sent).toEqual(['kept'])
  })

  it('follows what a custom fetch answers, a native promise with a then of its own, as await does: that then never runs', async () => {
    const runs = { count: 0 }
    // Its own then would hand on a server error; `await` gives the acknowledgement.
    const fetch = ((_url: unknown, init?: RequestInit) => ownThen(Promise.resolve(ackFor(init)), () => new Response('{}', { status: 500 }), runs)) as typeof globalThis.fetch
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch, maxAttempts: 1, onError() {}, logger: { info() {}, warn() {} } })
    client.capture({ ...base, turnId: 't1', question: 'q' })
    expect(await client.flush({ timeoutMs: 2_000 })).toMatchObject({ sent: 1, failed: 0 })
    expect(runs.count).toBe(0)
  })
})

describe("Codex's seventh review: what a callback answers is adopted at the callback's depth", () => {
  // Round 6 adopted what beforeSend answered once its depth had been left: the `then` a thenable
  // runs, in a job, is the hook's own code, and a capture() there counted as the app's own, at
  // depth 0, so the depth limit never cut a chain of them. onError's and the logger's answers were
  // adopted inside their depth already (swallow()): controls.
  /** `value`, settled by the `then` of a thenable, or of a Promise subclass's instance, that runs `side()` first. */
  const ANSWERS: Array<[string, (value: unknown, side: () => void) => unknown]> = [
    ['itself', (value, side) => (side(), value)],
    ['a thenable', (value, side) => ({ then: (resolve: (value: unknown) => void) => (side(), resolve(value)) })],
    [
      'a Promise subclass',
      (value, side) => {
        class Side extends Promise<unknown> {
          override then<A = unknown, B = never>(onFulfilled?: ((value: unknown) => A | PromiseLike<A>) | null, onRejected?: ((reason: unknown) => B | PromiseLike<B>) | null): Promise<A | B> {
            side()
            return super.then(onFulfilled, onRejected)
          }
        }
        return new Side((resolve) => resolve(value))
      },
    ],
  ]
  /**
   * How often `callback` ran, each run capturing again, at most 20 times, by the code `answer`
   * makes it answer with: beforeSend an event, onError one it reports, the logger's `info` (a
   * dry run's line for each event) an event.
   */
  async function chain(callback: 'beforeSend' | 'onError' | 'logger', answer: (value: unknown, side: () => void) => unknown): Promise<number> {
    let runs = 0
    let client!: LSquare
    const input = callback === 'onError' ? invalid : base
    const again = () => {
      if (runs <= 20) client.capture(input)
    }
    const quiet = { info() {}, warn() {} }
    const options: LSquareOptions =
      callback === 'beforeSend'
        ? { dryRun: true, logger: quiet, beforeSend: (event) => (runs++, answer(event, again)) as never }
        : callback === 'onError'
          ? { dryRun: true, logger: quiet, onError: () => (runs++, answer(undefined, again)) as never }
          : { dryRun: true, logger: { info: () => (runs++, answer(undefined, again)) as never, warn() {} } }
    client = new LSquare(options)
    client.capture(input)
    await sleep(50)
    return runs
  }

  it.each(ANSWERS)('cuts a chain of captures made from the then of what beforeSend answers, %s, at the depth limit, as one made from beforeSend itself', async (_how, answer) => {
    expect(await chain('beforeSend', ANSWERS[0]![1])).toBe(3)
    expect(await chain('beforeSend', answer)).toBe(3)
  })

  it.each((['onError', 'logger'] as const).flatMap((callback) => ANSWERS.slice(1).map(([how, answer]) => [callback, how, answer] as const)))(
    'cuts a chain of captures made from the then of what %s answers, %s, at the depth limit, as one made from it itself (a control)',
    async (callback, _how, answer) => {
      const itself = await chain(callback, ANSWERS[0]![1])
      expect(itself).toBeGreaterThan(1)
      expect(await chain(callback, answer)).toBe(itself)
    },
  )
})

describe('a synchronous beforeSend that changes the queue while hooks start', () => {
  /** A client whose fetch acknowledges every event and records its question. */
  function setup(options: LSquareOptions) {
    const sent: string[] = []
    const errors: LSquareError[] = []
    const fetch = (async (_url: unknown, init?: RequestInit) => {
      sent.push(...(JSON.parse(String(init?.body)).events as TurnEvent[]).map((event) => String(event.question)))
      return ackFor(init)
    }) as typeof globalThis.fetch
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch, onError: (error) => void errors.push(error), logger: { info() {}, warn() {} }, ...options })
    return { client, sent, errors }
  }
  /** Polls until `check()` holds, for up to a second. */
  const until = async (check: () => boolean) => {
    for (let i = 0; i < 200 && !check(); i++) await sleep(5)
  }
  const question = (event: LSquareEvent) => (event === undefined ? undefined : (event as TurnEvent).question)

  it('never starts the hook of an event its capture pushed out of a full queue', async () => {
    const seen: unknown[] = []
    let client!: LSquare
    const beforeSend: BeforeSend = (event) => {
      seen.push(question(event))
      // The queue holds 3: this pushes out B, the oldest event not inside a hook.
      if (seen.length === 1) client.capture({ ...base, turnId: 'h', question: 'from hook' })
      return event
    }
    const made = setup({ maxQueueEvents: 3, beforeSend })
    client = made.client
    for (const q of ['A', 'B', 'C']) client.capture({ ...base, turnId: q, question: q })
    // The hook's own capture came after this flush began, so it isn't counted.
    expect(await client.flush()).toMatchObject({ sent: 2, failed: 1, pending: 0 })
    await client.flush()
    expect(seen).toEqual(['A', 'C', 'from hook'])
    expect(made.sent.sort()).toEqual(['A', 'C', 'from hook'])
    expect(made.errors.map((error) => error.code)).toEqual(['queue_full'])
  })

  it("starts each waiting event's hook once when it calls flush(), whose pump runs inside it, and sends every event", async () => {
    const seen: unknown[] = []
    let client!: LSquare
    const beforeSend: BeforeSend = (event) => {
      seen.push(question(event))
      if (seen.length === 1) void client.flush({ timeoutMs: 0 })
      return event
    }
    const made = setup({ beforeSend })
    client = made.client
    for (const q of ['A', 'B', 'C']) client.capture({ ...base, turnId: q, question: q })
    expect(await client.flush()).toMatchObject({ sent: 3, failed: 0, pending: 0 })
    expect(seen).toEqual(['A', 'B', 'C'])
    expect(made.sent.sort()).toEqual(['A', 'B', 'C'])
    expect(made.errors).toEqual([])
  })

  it('starts no other hook once it calls shutdown()', async () => {
    const seen: unknown[] = []
    let client!: LSquare
    const beforeSend: BeforeSend = (event) => {
      seen.push(question(event))
      if (seen.length === 1) void client.shutdown()
      return event
    }
    const made = setup({ beforeSend })
    client = made.client
    for (const q of ['P', 'Q', 'R', 'S']) client.capture({ ...base, turnId: q, question: q })
    await until(() => made.errors.length >= 4)
    expect(seen).toEqual(['P'])
    expect(made.sent).toEqual([])
    expect(made.errors.map((error) => error.code)).toEqual(['shutdown', 'shutdown', 'shutdown', 'shutdown'])
  })

  it('takes no hook slot for an event already finished: a hook that never settles for it would hold one for good', async () => {
    let client!: LSquare
    const started: unknown[] = []
    let open = () => {}
    const gate = new Promise<void>((resolve) => (open = resolve))
    const beforeSend: BeforeSend = (event) => {
      // Never settles for an event it can't read: one started for a finished event keeps its slot.
      if (event === undefined) return new Promise<never>(() => {})
      const q = question(event)
      started.push(q)
      if (q === 'X' || q === 'Y') return gate.then(() => event)
      if (started.length === 1) client.capture({ ...base, turnId: 'h', question: 'from hook' })
      return event
    }
    const made = setup({ maxQueueEvents: 3, maxConcurrentHooks: 2, beforeSend })
    client = made.client
    for (const q of ['A', 'B', 'C']) client.capture({ ...base, turnId: q, question: q })
    await client.flush()
    await client.flush()
    // Both hook slots are free: two later events' hooks run at once.
    for (const q of ['X', 'Y']) client.capture({ ...base, turnId: q, question: q })
    await until(() => started.includes('Y'))
    expect(started).toEqual(['A', 'C', 'from hook', 'X', 'Y'])
    open()
    expect(await client.flush()).toMatchObject({ sent: 2, pending: 0 })
  })
})

describe('jsonLength', () => {
  it('matches JSON.stringify, escapes included', () => {
    const values = [{ a: '\n' }, ['\\', '\u0000'], { 'k"ey': 'é\t' }, [1, undefined, null, true, -0, 1e21], 'plain']
    for (const value of values) expect(jsonLength(value, { remaining: 1_000_000 }), JSON.stringify(value)).toBe(JSON.stringify(value).length)
  })
})

describe('non-2xx responses', () => {
  it('acts on a 401 without waiting for its body', async () => {
    let calls = 0
    const fetch = (async (_url: unknown, init?: RequestInit) => {
      calls++
      const body = new ReadableStream({ start: (c) => init?.signal?.addEventListener('abort', () => c.error(new Error('aborted'))) })
      return new Response(body, { status: 401 })
    }) as typeof globalThis.fetch
    const errors: string[] = []
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch, requestTimeoutMs: 30, onError: (e) => errors.push(`${e.code}:${e.status}`) })
    client.capture(base)
    expect(await client.flush()).toMatchObject({ failed: 1 })
    await sleep(0)
    expect(calls).toBe(1)
    expect(errors).toEqual(['send_failed:401'])
  })
})

describe('third review', () => {
  // Each loop is capped inside the test, so a regression fails the test instead of hanging the runner.
  const CAP = 50

  it('onError capturing after shutdown cannot loop forever', async () => {
    let calls = 0
    let client: LSquare
    client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      onError: () => {
        if (++calls <= CAP) client.capture(base)
      },
    })
    await client.shutdown()
    client.capture(base)
    await sleep(10)
    expect(calls).toBeLessThanOrEqual(2)
  })

  it('onError capturing invalid events cannot loop forever', async () => {
    let calls = 0
    let client: LSquare
    client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      onError: () => {
        if (++calls <= CAP) client.capture(invalid)
      },
    })
    client.capture(invalid)
    await sleep(10)
    expect(calls).toBeLessThanOrEqual(2)
  })

  it('an async onError that captures after an await cannot loop forever', async () => {
    let calls = 0
    let client: LSquare
    client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      onError: async () => {
        await Promise.resolve()
        if (++calls <= CAP) client.capture(invalid)
      },
    })
    client.capture(invalid)
    await sleep(10)
    expect(calls).toBeLessThanOrEqual(2)
  })

  it('an onError that captures from a timer cannot loop forever', async () => {
    let calls = 0
    let client: LSquare
    client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      onError: () => {
        setTimeout(() => {
          if (++calls <= CAP) client.capture(invalid)
        }, 0)
      },
    })
    client.capture(invalid)
    await sleep(50)
    expect(calls).toBeLessThanOrEqual(2)
  })

  it('a failure later in the pipeline keeps the depth of the callback that captured it', async () => {
    let calls = 0
    let client: LSquare
    client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      beforeSend: () => {
        throw new Error('hook fails')
      },
      onError: () => {
        if (++calls <= CAP) client.capture(base)
      },
    })
    client.capture(base)
    await sleep(20)
    expect(calls).toBeLessThanOrEqual(2)
  })

  it('a dry-run logger that captures cannot loop forever', async () => {
    let lines = 0
    let client: LSquare
    client = new LSquare({
      dryRun: true,
      logger: {
        info: () => {
          if (++lines <= CAP) client.capture(base)
        },
        warn: () => {},
      },
    })
    client.capture(base)
    await sleep(20)
    expect(lines).toBeLessThanOrEqual(2)
  })

  it('refuses an event that cannot fit next to running hooks, without dropping others', async () => {
    const errors: Array<{ code: string; question: string | undefined }> = []
    const hanging: BeforeSend = (event) => ((event as TurnEvent).question === 'hooked' ? new Promise<LSquareEvent>(() => {}) : event)
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      fetch: (async () => new Promise<Response>(() => {})) as typeof globalThis.fetch,
      beforeSend: hanging,
      maxConcurrentHooks: 1,
      beforeSendTimeoutMs: 5000,
      // Room for the two small events, not for the big one next to the one in its hook.
      maxQueueBytes: 800,
      onError: (error) => errors.push({ code: error.code, question: error.eventId }),
    })
    client.capture({ ...base, turnId: 'hooked', question: 'hooked' })
    await sleep(0)
    client.capture({ ...base, turnId: 'waiting', question: 'waiting' })
    client.capture({ ...base, turnId: 'big', question: 'big', answer: 'x'.repeat(300) })
    await sleep(0)
    expect(errors).toEqual([{ code: 'queue_full', question: turnKey('org_1', 'conv1', 'big') }])
  })

  it('copies events for onError only when a custom handler needs them', async () => {
    const clone = vi.spyOn(globalThis, 'structuredClone')
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      fetch: (async () => new Response('', { status: 400 })) as typeof globalThis.fetch,
      logger: { info: () => {}, warn: () => {} },
    })
    client.capture(base)
    await client.flush()
    expect(clone).not.toHaveBeenCalled()
    clone.mockRestore()
  })
})

describe('fourth review', () => {
  // Work handed to something set up outside the callback loses the callback's depth. These loops
  // must still neither starve the event loop nor run unbounded.
  const CAP = 10_000

  it('a worker queue fed from onError neither starves timers nor loops unbounded', async () => {
    let calls = 0
    let timerRan = false
    const queue: Array<() => void> = []
    let wake: () => void = () => {}
    let stop = false
    ;(async () => {
      while (!stop) {
        if (queue.length === 0) await new Promise<void>((resolve) => (wake = resolve))
        queue.shift()?.()
      }
    })()
    let client: LSquare
    client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      onError: () => {
        queue.push(() => {
          if (++calls <= CAP) client.capture(invalid)
        })
        wake()
      },
    })
    setTimeout(() => (timerRan = true), 5)
    client.capture(invalid)
    await sleep(100)
    stop = true
    wake()
    expect(timerRan).toBe(true)
    expect(calls).toBeLessThanOrEqual(110)
  })

  it('a promise armed outside onError and resolved inside it is bounded too', async () => {
    let calls = 0
    let resolveNext: () => void = () => {}
    let client: LSquare
    const arm = (): void => {
      new Promise<void>((resolve) => (resolveNext = resolve)).then(() => {
        if (++calls <= CAP) client.capture(invalid)
        arm()
      })
    }
    arm()
    client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', onError: () => resolveNext() })
    client.capture(invalid)
    await sleep(100)
    expect(calls).toBeLessThanOrEqual(110)
  })

  it('reports over the rate limit are counted on the next one delivered', async () => {
    const delivered: Array<{ code: string; suppressed: number | undefined }> = []
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      maxQueueEvents: 1000,
      fetch: (async (_url: unknown, init?: RequestInit) =>
        new Promise<Response>((_, reject) => init?.signal?.addEventListener('abort', () => reject(new Error('aborted'))))) as typeof globalThis.fetch,
      onError: (error) => delivered.push({ code: error.code, suppressed: error.suppressed }),
    })
    for (let i = 0; i < 150; i++) client.capture(base)
    await client.flush({ timeoutMs: 20 })
    await sleep(50)
    client.capture(invalid)
    await sleep(10)
    expect(delivered.filter((d) => d.code === 'pending_after_flush')).toHaveLength(100)
    expect(delivered.at(-1)).toEqual({ code: 'invalid_id', suppressed: 50 })
  })
})

describe('fifth review', () => {
  it("an event's reports depend on where it was captured, not on which hook freed its slot", async () => {
    let releaseX: () => void = () => {}
    const errors: string[] = []
    let missing = 0
    let client: LSquare
    client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      fetch: (async (_url: unknown, init?: RequestInit) => ackFor(init)) as typeof globalThis.fetch,
      maxConcurrentHooks: 1,
      beforeSendTimeoutMs: 5000,
      beforeSend: (event) => {
        if ((event as TurnEvent).question === 'X') return new Promise<LSquareEvent>((resolve) => (releaseX = () => resolve(event)))
        if ((event as TurnEvent).question === 'Y') throw new Error('Y fails')
        return event
      },
      onError: (error) => {
        errors.push(`${error.code}:${error.eventId ?? ''}`)
        // Second-level callback captures X, so X is captured two levels deep.
        if (error.code === 'invalid_id' && ++missing === 1) client.capture(invalid)
        else if (error.code === 'invalid_id') client.capture({ ...base, turnId: 'x', question: 'X' })
      },
    })
    client.capture(invalid)
    await sleep(20)
    client.capture({ ...base, turnId: 'y', question: 'Y' }) // ordinary code: waits behind X's hook
    await sleep(5)
    releaseX()
    await sleep(20)
    expect(errors).toContain(`hook_failed:${turnKey('org_1', 'conv1', 'y')}`)
  })

  it('the default log adds what the rate limit suppressed', async () => {
    vi.useFakeTimers({ toFake: ['performance'] })
    try {
      const warn: string[] = []
      const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', logger: { info: () => {}, warn: (m) => warn.push(m) } })
      for (let i = 0; i < 150; i++) client.capture(invalid)
      await sleep(10)
      vi.advanceTimersByTime(61_000)
      client.capture(invalid)
      await sleep(10)
      expect(warn).toHaveLength(2)
      expect(warn[1]).toContain('(149 more since the last report)')
    } finally {
      vi.useRealTimers()
    }
  })

  it('a beforeSend that captures cannot loop forever', async () => {
    let calls = 0
    let timerRan = false
    let client: LSquare
    client = new LSquare({
      dryRun: true,
      logger: { info: () => {}, warn: () => {} },
      beforeSend: (event) => {
        if (++calls <= 10_000) client.capture(base)
        return event
      },
    })
    setTimeout(() => (timerRan = true), 5)
    client.capture(base)
    await sleep(50)
    expect(timerRan).toBe(true)
    expect(calls).toBeLessThanOrEqual(3)
  })
})

describe('callbacks held at once', () => {
  // At most 100 reports and 100 log lines are held from admission until the handler returns, or
  // settles if it's async, their event copies within maxQueueBytes. A frozen clock keeps the rate
  // where the test puts it.
  const hangingFetch = (async (_url: unknown, init?: RequestInit) =>
    new Promise<Response>((_, reject) => init?.signal?.addEventListener('abort', () => reject(new Error('aborted'))))) as typeof globalThis.fetch
  /** onError and loggers run in a later event-loop turn (setImmediate): let them run. */
  const tick = () => new Promise<void>((resolve) => setImmediate(() => setImmediate(resolve)))
  /** Varied words, so nothing is shortened: an event of over half of 4,096 bytes. */
  const longAnswer = Array.from({ length: 400 }, (_, i) => `word${i}`).join(' ')

  it('an async onError that never settles holds its slot, and one that settles gives it back', async () => {
    vi.useFakeTimers({ toFake: ['performance'] })
    try {
      const settle: Array<() => void> = []
      const delivered: Array<number | undefined> = []
      const client = new LSquare({
        apiKey: 'k',
        endpoint: 'https://x.test',
        onError: (error) => {
          delivered.push(error.suppressed)
          return new Promise<void>((resolve) => settle.push(resolve))
        },
      })
      for (let i = 0; i < 100; i++) client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(100)
      // A second on, the rate allows 100 more: the 100 still running don't.
      vi.advanceTimersByTime(1000)
      client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(100)
      settle[0]?.()
      await sleep(0)
      client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(101)
      expect(delivered.at(-1)).toBe(1)
      for (const resolve of settle) resolve()
    } finally {
      vi.useRealTimers()
    }
  })

  it('past the bytes the held copies may take, a report goes without its event until they are given back', async () => {
    const settle: Array<() => void> = []
    const events: Array<LSquareEvent | undefined> = []
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      maxQueueBytes: 4096,
      fetch: hangingFetch,
      onError: (error, event) => {
        if (error.code !== 'pending_after_flush') return
        events.push(event)
        return new Promise<void>((resolve) => settle.push(resolve))
      },
    })
    client.capture({ ...base, answer: longAnswer })
    // Each flush reports the same event as pending, with a copy while the bytes allow.
    for (let i = 0; i < 3; i++) await client.flush({ timeoutMs: 1 })
    await tick()
    expect(events.map((event) => event !== undefined)).toEqual([true, false, false])
    expect((events[0] as TurnEvent).answer).toBe(longAnswer)
    for (const resolve of settle) resolve()
    await sleep(0)
    await client.flush({ timeoutMs: 1 })
    await tick()
    expect(events.map((event) => event !== undefined)).toEqual([true, false, false, true])
    for (const resolve of settle) resolve()
    await client.shutdown({ timeoutMs: 0 })
  })

  it('a report delivered to an async logger holds its slot until the logger settles', async () => {
    vi.useFakeTimers({ toFake: ['performance'] })
    try {
      const settle: Array<() => void> = []
      const warn: string[] = []
      const client = new LSquare({
        apiKey: 'k',
        endpoint: 'https://x.test',
        logger: {
          info: () => {},
          warn: (message) => {
            warn.push(message)
            return new Promise<void>((resolve) => settle.push(resolve))
          },
        },
      })
      // No onError: each report is logged, at most once a minute.
      for (let i = 0; i < 101; i++) {
        vi.advanceTimersByTime(61_000)
        client.capture(invalid)
        await tick()
      }
      expect(warn).toHaveLength(100)
      for (const resolve of settle) resolve()
      await sleep(0)
      vi.advanceTimersByTime(61_000)
      client.capture(invalid)
      await tick()
      expect(warn).toHaveLength(101)
      expect(warn.at(-1)).toContain('(1 more since the last report)')
    } finally {
      vi.useRealTimers()
    }
  })

  it('a logger that never settles holds at most 100 lines, and the next one written notes the rest', async () => {
    vi.useFakeTimers({ toFake: ['performance'] })
    try {
      const settle: Array<() => void> = []
      const lines: string[] = []
      const client = new LSquare({
        dryRun: true,
        logger: {
          info: (message) => {
            lines.push(message)
            return new Promise<void>((resolve) => settle.push(resolve))
          },
          warn() {},
        },
      })
      for (let i = 0; i < 100; i++) client.capture(base)
      await tick()
      expect(lines).toHaveLength(100)
      // A second on, the rate allows 100 more: the 100 still being written don't.
      vi.advanceTimersByTime(1000)
      client.capture(base)
      await tick()
      expect(lines).toHaveLength(100)
      for (const resolve of settle) resolve()
      await sleep(0)
      client.capture(base)
      await tick()
      expect(lines).toHaveLength(101)
      expect(lines.at(-1)).toContain('[lsquare: 1 earlier log lines suppressed]')
    } finally {
      vi.useRealTimers()
    }
  })

  it("on Workers, reports whose later turn went with their request give back their slots 5 s on, and the count they carried", async () => {
    vi.stubGlobal('navigator', { userAgent: 'Cloudflare-Workers' })
    vi.useFakeTimers({ toFake: ['performance'] })
    const immediate = vi.spyOn(globalThis, 'setImmediate')
    try {
      const delivered: Array<number | undefined> = []
      const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', onError: (error) => void delivered.push(error.suppressed) })
      for (let i = 0; i < 100; i++) client.capture(invalid)
      await tick()
      // 20 over the rate: counted, and carried by the next report admitted.
      for (let i = 0; i < 20; i++) client.capture(invalid)
      vi.advanceTimersByTime(1000)
      // Their requests end before their turns run, which go with them.
      const gone: Array<() => void> = []
      immediate.mockImplementation(((callback: () => void) => {
        gone.push(callback)
        return {} as NodeJS.Immediate
      }) as unknown as typeof setImmediate)
      for (let i = 0; i < 100; i++) client.capture(invalid)
      immediate.mockRestore()
      expect(gone).toHaveLength(100)
      vi.advanceTimersByTime(1000)
      client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(100)
      // 5 s after they were due, their slots come back.
      vi.advanceTimersByTime(5000)
      client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(101)
      expect(delivered.at(-1)).toBe(21)
      // A turn that starts after all does nothing: its slot and its count went on already.
      for (const callback of gone) callback()
      client.capture(invalid)
      await tick()
      expect(delivered.slice(101)).toEqual([undefined])
    } finally {
      immediate.mockRestore()
      vi.useRealTimers()
      vi.unstubAllGlobals()
    }
  })

  it('on Workers, log lines whose later turn went with their request give back their slots, but a logger still running keeps its own', async () => {
    vi.stubGlobal('navigator', { userAgent: 'Cloudflare-Workers' })
    // Its timers too: a running logger's beat, in its own request, which lives (see the next test).
    vi.useFakeTimers({ toFake: ['performance', 'setInterval', 'clearInterval'] })
    const immediate = vi.spyOn(globalThis, 'setImmediate')
    try {
      const settle: Array<() => void> = []
      const lines: string[] = []
      const client = new LSquare({
        dryRun: true,
        logger: {
          info: (message) => {
            lines.push(message)
            return new Promise<void>((resolve) => settle.push(resolve))
          },
          warn() {},
        },
      })
      const gone: Array<() => void> = []
      immediate.mockImplementation(((callback: () => void) => {
        gone.push(callback)
        return {} as NodeJS.Immediate
      }) as unknown as typeof setImmediate)
      for (let i = 0; i < 100; i++) client.capture(base)
      // Each line is logged from a microtask of its capture.
      for (let i = 0; i < 3; i++) await Promise.resolve()
      immediate.mockRestore()
      expect(gone).toHaveLength(100)
      vi.advanceTimersByTime(1000)
      client.capture(base)
      await tick()
      expect(lines).toHaveLength(0)
      vi.advanceTimersByTime(5000)
      for (let i = 0; i < 100; i++) client.capture(base)
      await tick()
      expect(lines).toHaveLength(100)
      expect(lines[0]).toContain('[lsquare: 1 earlier log lines suppressed]')
      // These ran, and their logger hasn't settled: while their request lives, beating, they keep
      // their slots, however long it takes (up to the beat's limit).
      vi.advanceTimersByTime(60_000)
      client.capture(base)
      await tick()
      expect(lines).toHaveLength(100)
      for (const resolve of settle) resolve()
      await sleep(0)
      client.capture(base)
      await tick()
      expect(lines).toHaveLength(101)
      expect(lines.at(-1)).toContain('[lsquare: 1 earlier log lines suppressed]')
    } finally {
      immediate.mockRestore()
      vi.useRealTimers()
      vi.unstubAllGlobals()
    }
  })

  it('on Workers, an async onError whose request ended gives its slot back once its beat has stopped, and one whose request lives keeps it', async () => {
    vi.stubGlobal('navigator', { userAgent: 'Cloudflare-Workers' })
    vi.useFakeTimers({ toFake: ['performance', 'setInterval', 'clearInterval'] })
    const interval = vi.spyOn(globalThis, 'setInterval')
    try {
      const delivered: Array<number | undefined> = []
      const client = new LSquare({
        apiKey: 'k',
        endpoint: 'https://x.test',
        // Awaits a timer of its request, which never fires once that request has ended.
        onError: (error) => {
          delivered.push(error.suppressed)
          return new Promise<void>(() => {})
        },
      })
      // Their requests end once the reports are delivered, taking each handler's timers with them,
      // its beat included.
      interval.mockImplementation((() => ({}) as NodeJS.Timeout) as unknown as typeof setInterval)
      for (let i = 0; i < 100; i++) client.capture(invalid)
      await tick()
      interval.mockRestore()
      expect(delivered).toHaveLength(100)
      vi.advanceTimersByTime(1000)
      client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(100)
      // Still for 5 s: their slots come back, the count they carried having gone with them.
      vi.advanceTimersByTime(5000)
      client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(101)
      expect(delivered.at(-1)).toBe(1)
      // These run in requests that live: they beat, and keep their slots.
      for (let i = 0; i < 99; i++) client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(200)
      vi.advanceTimersByTime(30_000)
      client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(200)
      // A soft bound: a beat lasts at most a minute, after which the slot comes back all the same.
      vi.advanceTimersByTime(36_000)
      client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(201)
    } finally {
      interval.mockRestore()
      vi.useRealTimers()
      vi.unstubAllGlobals()
    }
  })

  it('on Workers, a report whose later turn went with its request is no longer waited for once its slot is back', async () => {
    vi.stubGlobal('navigator', { userAgent: 'Cloudflare-Workers' })
    vi.useFakeTimers({ toFake: ['performance'] })
    const immediate = vi.spyOn(globalThis, 'setImmediate')
    const gone: Array<() => void> = []
    try {
      const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', onError: () => {} })
      const internal = client[INTERNAL]
      // Its request ends before its turn runs, taking that turn, and any later one, with it.
      immediate.mockImplementation(((callback: () => void) => {
        gone.push(callback)
        return {} as NodeJS.Immediate
      }) as unknown as typeof setImmediate)
      client.capture(invalid)
      const settled = (promise: Promise<void>) => {
        let done = false
        void promise.then(() => (done = true))
        return async () => {
          for (let i = 0; i < 10; i++) await Promise.resolve()
          return done
        }
      }
      // Waited for: a turn after it, which goes the same way.
      expect(await settled(internal.delivered())()).toBe(false)
      expect(gone).toHaveLength(2)
      // 5 s after it was due, its slot is back, and nothing is left to wait for.
      vi.advanceTimersByTime(5000)
      expect(await settled(internal.delivered())()).toBe(true)
      // A turn that starts after all is not counted again.
      for (const callback of gone) callback()
      expect(await settled(internal.delivered())()).toBe(true)
      expect(gone).toHaveLength(2)
    } finally {
      immediate.mockRestore()
      vi.useRealTimers()
      vi.unstubAllGlobals()
    }
  })

  describe('on Workers, a clock that goes back, as an app\'s fake timers set performance.now() to 0', () => {
    // What a limit held before is as old after as before: neither stuck until the old clock's time
    // comes round again, nor taken for gone at once. A report made as the clock goes back sees it.
    let clock = 600_000
    /** Moves the clock on by `ms`, a second at a time, the beats of requests that live beating as it goes. */
    const pass = (ms: number) => {
      for (let left = ms; left > 0; left -= 1000) {
        clock += Math.min(1000, left)
        vi.advanceTimersByTime(Math.min(1000, left))
      }
    }
    const neverSettles = (delivered: Array<number | undefined>) => (error: { suppressed?: number | undefined }) => {
      delivered.push(error.suppressed)
      return new Promise<void>(() => {})
    }
    beforeEach(() => {
      vi.stubGlobal('navigator', { userAgent: 'Cloudflare-Workers' })
      clock = 600_000
      vi.spyOn(performance, 'now').mockImplementation(() => clock)
      // The beats, run as pass() moves the clock.
      vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    })
    afterEach(() => {
      vi.restoreAllMocks()
      vi.useRealTimers()
      vi.unstubAllGlobals()
    })

    it('reports whose later turn went with their request give back their slots 5 s after they were due', async () => {
      const immediate = vi.spyOn(globalThis, 'setImmediate')
      const delivered: Array<number | undefined> = []
      const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', onError: (error) => void delivered.push(error.suppressed) })
      // Their requests end before their turns run, which go with them.
      immediate.mockImplementation((() => ({}) as NodeJS.Immediate) as unknown as typeof setImmediate)
      for (let i = 0; i < 100; i++) client.capture(invalid)
      immediate.mockRestore()
      clock = 0
      client.capture(invalid)
      await tick()
      expect(delivered).toEqual([])
      // Not at once: one still due in a request that lives would yet run.
      pass(2000)
      client.capture(invalid)
      await tick()
      expect(delivered).toEqual([])
      pass(3000)
      client.capture(invalid)
      await tick()
      // Those dropped meanwhile are counted.
      expect(delivered).toEqual([2])
    })

    // Seen first by a report, which must not take back those that live at once (their last beat is
    // on the old clock), or by those beats, which must not leave the rest on the old clock.
    it.each(['a report', 'the beats of those that live'])('an async onError whose request ended gives its slot back 5 s after its beat stopped, and one whose request lives keeps it, the clock going back seen first by %s', async (first) => {
      const interval = vi.spyOn(globalThis, 'setInterval')
      const delivered: Array<number | undefined> = []
      const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', onError: neverSettles(delivered) })
      // Half end once their reports are delivered, taking their beats with them; half live.
      interval.mockImplementation((() => ({}) as NodeJS.Timeout) as unknown as typeof setInterval)
      for (let i = 0; i < 50; i++) client.capture(invalid)
      await tick()
      interval.mockRestore()
      for (let i = 0; i < 50; i++) client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(100)
      pass(1000)
      clock = 0
      if (first === 'a report') {
        client.capture(invalid)
        await tick()
        expect(delivered).toHaveLength(100)
      }
      // 6 s after they started: the ended ones' slots are back, those that live beat on.
      pass(5000)
      for (let i = 0; i < 60; i++) client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(150)
    })

    it("an async onError's beat stops a minute after it started all the same, and its slot comes back 5 s later", async () => {
      const delivered: Array<number | undefined> = []
      const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', onError: neverSettles(delivered) })
      for (let i = 0; i < 100; i++) client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(100)
      pass(1000)
      clock = 0
      client.capture(invalid)
      // 64 s after they started, 4 s after their beat stopped: still theirs.
      pass(63_000)
      client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(100)
      pass(1000)
      client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(101)
    })

    // beforeSend's one slot, held by a hook that never settles, its deadline 0.1 s after it started.
    function hooked() {
      const started: string[] = []
      const client = new LSquare({
        apiKey: 'k',
        endpoint: 'https://x.test',
        maxConcurrentHooks: 1,
        beforeSendTimeoutMs: 100,
        fetch: (async (_url: unknown, init?: RequestInit) => ackFor(init)) as typeof globalThis.fetch,
        onError() {},
        beforeSend: (event) => {
          started.push(String(event.conversationId))
          return event.conversationId === 'stuck' ? new Promise<null>(() => {}) : event
        },
      })
      const capture = (conversationId: string) => void client.capture({ ...base, conversationId })
      return { started, capture }
    }
    /** Lets the hook of the event just captured start: from a microtask of its capture. */
    const started = async () => {
      for (let i = 0; i < 3; i++) await Promise.resolve()
    }

    it.each(['before it started', 'before its deadline', 'after its deadline'])('a hook whose request ended gives its slot back 5 s after its last beat, the clock going back %s', async (when) => {
      const { started: names, capture } = hooked()
      // Its capture sees the clock go back.
      if (when === 'before it started') clock = 0
      // Its request ends as it starts, taking its deadline's timer and its beat with it.
      const timeout = vi.spyOn(globalThis, 'setTimeout').mockImplementation((() => ({}) as NodeJS.Timeout) as unknown as typeof setTimeout)
      const interval = vi.spyOn(globalThis, 'setInterval').mockImplementation((() => ({}) as NodeJS.Timeout) as unknown as typeof setInterval)
      capture('stuck')
      await started()
      timeout.mockRestore()
      interval.mockRestore()
      expect(names).toEqual(['stuck'])
      if (when === 'after its deadline') {
        pass(1000)
        capture('expires it')
      }
      if (when !== 'before it started') {
        clock = 0
        capture('sees the clock go back')
      }
      if (when !== 'after its deadline') {
        pass(1000)
        capture('expires it')
      }
      // 4 s after its last beat: still its own, neither taken for gone at once nor held until the
      // old clock's time comes round again.
      pass(3000)
      capture('early')
      await tick()
      expect(names).toEqual(['stuck'])
      pass(1000)
      capture('later')
      await tick()
      expect(names).toEqual(['stuck', 'later'])
    })

    // Seen first by a capture, which must not take it for gone at once (its last beat is on the
    // old clock), or by its beat, which must not leave the rest on the old clock.
    it.each(['a capture', 'its beat'])('a hook whose request lives keeps its slot, its beat stopping a minute past its deadline and the slot coming back 5 s later, the clock going back seen first by %s', async (first) => {
      const { started: names, capture } = hooked()
      capture('stuck')
      await started()
      expect(names).toEqual(['stuck'])
      pass(1000)
      clock = 0
      if (first === 'a capture') capture('sees the clock go back')
      else pass(1000)
      // Past its deadline, and beating on: still its own.
      pass(2000)
      capture('early')
      await tick()
      expect(names).toEqual(['stuck'])
      // Its beat stops at that of 61 s, 60.1 s after it started; 4 s later the slot is still its own...
      pass(62_000)
      capture('soon')
      await tick()
      expect(names).toEqual(['stuck'])
      // ...and 5 s later, it is back.
      pass(1000)
      capture('later')
      await tick()
      expect(names).toEqual(['stuck', 'later'])
    })
  })

  it("reports whose context can't be entered give back their slots and the count they carried", async () => {
    vi.useFakeTimers({ toFake: ['performance'] })
    const enter = vi.spyOn(AsyncResource.prototype, 'runInAsyncScope')
    try {
      const delivered: Array<{ code: string; suppressed: number | undefined }> = []
      const client = new LSquare({
        apiKey: 'k',
        endpoint: 'https://x.test',
        fetch: hangingFetch,
        onError: (error) => void delivered.push({ code: error.code, suppressed: error.suppressed }),
      })
      for (let i = 0; i < 100; i++) client.capture(base)
      await tick()
      // 20 over the rate: counted.
      for (let i = 0; i < 120; i++) client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(100)
      vi.advanceTimersByTime(1000)
      // As on Workers, for another request's: the events' context can't be entered, so none of the
      // 100 pending reports is delivered.
      enter.mockImplementation(() => {
        throw new Error('Cannot perform I/O on behalf of a different request.')
      })
      await client.flush({ timeoutMs: 1 })
      await tick()
      enter.mockRestore()
      expect(delivered).toHaveLength(100)
      vi.advanceTimersByTime(1000)
      client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(101)
      expect(delivered.at(-1)).toEqual({ code: 'invalid_id', suppressed: 20 })
      await client.shutdown({ timeoutMs: 0 })
    } finally {
      enter.mockRestore()
      vi.useRealTimers()
    }
  })

  it('the count a report carried survives when its event fails to copy', async () => {
    vi.useFakeTimers({ toFake: ['performance'] })
    const real = globalThis.structuredClone
    const clone = vi.spyOn(globalThis, 'structuredClone').mockImplementation((value, options) => {
      if ((value as TurnEvent | undefined)?.question === 'unclonable') throw new DOMException('It could not be cloned.', 'DataCloneError')
      return real(value, options)
    })
    try {
      const delivered: Array<{ code: string; suppressed: number | undefined }> = []
      const client = new LSquare({
        apiKey: 'k',
        endpoint: 'https://x.test',
        fetch: hangingFetch,
        onError: (error) => void delivered.push({ code: error.code, suppressed: error.suppressed }),
      })
      client.capture({ ...base, question: 'unclonable' })
      for (let i = 0; i < 150; i++) client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(100)
      vi.advanceTimersByTime(1000)
      // Its pending report takes the 50, then fails to copy its event.
      await client.flush({ timeoutMs: 1 })
      await tick()
      client.capture(invalid)
      await tick()
      expect(delivered).toHaveLength(101)
      expect(delivered.at(-1)).toEqual({ code: 'invalid_id', suppressed: 50 })
      await client.shutdown({ timeoutMs: 0 })
    } finally {
      clone.mockRestore()
      vi.useRealTimers()
    }
  })
})

describe('a clock that goes back, as an app\'s fake timers set performance.now() to 0: flushes, requests, retries and daily limits', () => {
  // What was due before is as far off after: neither held until the old clock's time comes round
  // again nor taken for gone at once. The timers are fake too, so time is the test's to count.
  let clock = 600_000
  /** Moves the clock and the timers on by `ms`, `step` at a time: what runs in a step reads the clock at its end. */
  const pass = (ms: number, step = 1000) => {
    for (let left = ms; left > 0; left -= step) {
      clock += Math.min(step, left)
      vi.advanceTimersByTime(Math.min(step, left))
    }
  }
  /** Lets what is queued run: microtasks, and reports' later turns (setImmediate, not faked). */
  const tick = () => new Promise<void>((resolve) => setImmediate(() => setImmediate(resolve)))
  const workers = () => vi.stubGlobal('navigator', { userAgent: 'Cloudflare-Workers' })
  const hanging = (async (_url: unknown, init?: RequestInit) =>
    new Promise<Response>((_, reject) => init?.signal?.addEventListener('abort', () => reject(new Error('aborted'))))) as typeof globalThis.fetch
  const retryLater = () => new Response('', { status: 503, headers: { 'retry-after': '1' } })
  /** A fetch answering `answer` (the request's number, from 1), and the conversations of each request, in order. */
  const sender = (answer: (n: number, init?: RequestInit) => Response | Promise<Response>) => {
    const sent: string[][] = []
    const fetch = (async (_url: unknown, init?: RequestInit) => {
      sent.push((JSON.parse(String(init?.body)).events as TurnEvent[]).map((e) => String(e.conversationId)))
      return answer(sent.length, init)
    }) as typeof globalThis.fetch
    return { sent, fetch }
  }
  /** A request that ends takes its timers with it: those set until mockRestore() never run. */
  const timersGone = () => vi.spyOn(globalThis, 'setTimeout').mockImplementation((() => ({}) as NodeJS.Timeout) as unknown as typeof setTimeout)
  beforeEach(() => {
    clock = 600_000
    vi.spyOn(performance, 'now').mockImplementation(() => clock)
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
  })
  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  // Seen as it goes back, by a capture's report, or only once the flush's 20 ms are up.
  it.each([
    ['Node', 'by a capture'],
    ['Node', 'only once its time is up'],
    ['Workers', 'by a capture'],
    ['Workers', 'only once its time is up'],
  ])("a flush given 20 ms, waiting for a call's event as the clock goes back, returns 20 ms on, leaving no timer behind (%s, seen %s)", async (runtime, seen) => {
    if (runtime === 'Workers') workers()
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', requestTimeoutMs: 500, maxAttempts: 1, fetch: hanging, onError() {} })
    // A call on an instrumented server whose event is never built, and an event whose request hangs.
    client[INTERNAL].track('call', new Promise<void>(() => {}))
    client.capture(base)
    let flushed: FlushResult | undefined
    void client.flush({ timeoutMs: 20 }).then((result) => (flushed = result))
    clock = 0
    if (seen === 'by a capture') client.capture(invalid)
    pass(19)
    await tick()
    expect(flushed).toBeUndefined()
    pass(1)
    for (let i = 0; i < 10 && !flushed; i++) await tick()
    expect(flushed).toEqual({ sent: 0, skipped: 0, failed: 0, pending: 2 })
    // The request's own timeout ends it, and nothing else is left to run.
    pass(1000)
    await tick()
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each([
    ['Node', 'before the flush'],
    ['Node', 'as it waits for the call'],
    ['Workers', 'before the flush'],
    ['Workers', 'as it waits for the call'],
  ])("a flush whose call's event is built after the clock goes back waits the rest of its time for the events, no more and no less (%s, seen %s)", async (runtime, when) => {
    if (runtime === 'Workers') workers()
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch: hanging, onError() {} })
    let built!: () => void
    client[INTERNAL].track('call', new Promise<void>((resolve) => (built = resolve)))
    client.capture(base)
    const goBack = () => {
      clock = 0
      client.capture(invalid)
    }
    if (when === 'before the flush') goBack()
    let flushed: FlushResult | undefined
    void client.flush({ timeoutMs: 1000 }).then((result) => (flushed = result))
    if (when === 'as it waits for the call') goBack()
    await tick()
    pass(10)
    // Built, but none was recorded: it counts as failed.
    built()
    await tick()
    pass(989)
    await tick()
    expect(flushed).toBeUndefined()
    pass(1)
    for (let i = 0; i < 10 && !flushed; i++) await tick()
    expect(flushed).toEqual({ sent: 0, skipped: 0, failed: 1, pending: 1 })
  })

  it.each(['Node', 'Workers'])('a flush waits for a retry due within its time, scheduled before the clock went back (%s)', async (runtime) => {
    if (runtime === 'Workers') workers()
    const { sent, fetch } = sender((n, init) => (n === 1 ? retryLater() : ackFor(init)))
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch, onError() {} })
    client.capture({ ...base, conversationId: 'retried' })
    await tick()
    expect(sent).toEqual([['retried']])
    clock = 0
    let flushed: FlushResult | undefined
    void client.flush({ timeoutMs: 2000 }).then((result) => (flushed = result))
    pass(999)
    await tick()
    expect(flushed).toBeUndefined()
    pass(1)
    for (let i = 0; i < 10 && !flushed; i++) await tick()
    expect(flushed).toEqual({ sent: 1, skipped: 0, failed: 0, pending: 0 })
    expect(sent).toEqual([['retried'], ['retried']])
  })

  it.each(['before it was sent', 'after'])("on Workers, a request whose timeout went with its request gives its slot back at its deadline, not once the old clock's time comes round again (the clock going back %s)", async (when) => {
    workers()
    // The first never answers.
    const { sent, fetch } = sender((n, init) => (n === 1 ? new Promise<Response>(() => {}) : ackFor(init)))
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', maxConcurrentRequests: 1, requestTimeoutMs: 1000, fetch, onError() {} })
    const capture = (conversationId: string) => void client.capture({ ...base, conversationId })
    // Its capture sees the clock go back.
    if (when === 'before it was sent') clock = 0
    // Its request ends as it is sent, taking its timeout's timer with it.
    const timeout = timersGone()
    capture('stuck')
    for (let i = 0; i < 50 && sent.length === 0; i++) await Promise.resolve()
    timeout.mockRestore()
    expect(sent).toEqual([['stuck']])
    if (when === 'after') {
      clock = 0
      capture('sees the clock go back')
      await tick()
    }
    pass(999)
    capture('early')
    await tick()
    expect(sent).toEqual([['stuck']])
    pass(1)
    capture('later')
    await tick()
    expect(sent.slice(1).flat().sort()).toEqual(when === 'after' ? ['early', 'later', 'sees the clock go back'] : ['early', 'later'])
  })

  it.each(['before it was scheduled', 'after'])('on Workers, a retry whose timer went with its request goes once its time has come, neither at once nor once the old clock\'s time comes round again (the clock going back %s)', async (when) => {
    workers()
    const { sent, fetch } = sender((n, init) => (n === 1 ? retryLater() : ackFor(init)))
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch, onError() {} })
    const capture = (conversationId: string) => void client.capture({ ...base, conversationId })
    const tries = () => sent.flat().filter((name) => name === 'retried').length
    if (when === 'before it was scheduled') clock = 0
    // Its request ends once it is answered, taking the retry's timer, a second on, with it.
    const timeout = timersGone()
    const scheduled = () => timeout.mock.calls.some(([, ms]) => ms === 1000)
    capture('retried')
    for (let i = 0; i < 50 && !scheduled(); i++) await Promise.resolve()
    expect(scheduled()).toBe(true)
    timeout.mockRestore()
    if (when === 'after') {
      clock = 0
      capture('sees the clock go back')
      await tick()
    }
    pass(999)
    capture('early')
    await tick()
    expect(tries()).toBe(1)
    pass(1)
    capture('later')
    await tick()
    expect(tries()).toBe(2)
  })

  it.each(['before the wait', 'as it waits'])("on Workers, the wrapper's wait for a call's event watches until its time is up, sending the event once the request holding the only slot is taken back (the clock going back %s)", async (when) => {
    workers()
    // The first never answers.
    const { sent, fetch } = sender((n, init) => (n === 1 ? new Promise<Response>(() => {}) : ackFor(init)))
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', maxConcurrentRequests: 1, requestTimeoutMs: 1000, fetch, onError() {} })
    // Its capture sees the clock go back.
    if (when === 'before the wait') clock = 0
    // Its request ends as it is sent, taking its timeout's timer with it.
    const timeout = timersGone()
    client.capture({ ...base, conversationId: 'stuck' })
    for (let i = 0; i < 50 && sent.length === 0; i++) await Promise.resolve()
    timeout.mockRestore()
    // Queued behind it, and nothing captured after: only the wait's watch pumps.
    client.capture({ ...base, conversationId: 'waited', turnId: 't1' })
    await tick()
    let settled = false
    void client[INTERNAL].settled(turnKey('org_1', 'waited', 't1'), 2000).then(() => (settled = true))
    if (when === 'as it waits') clock = 0
    // A step for each of the watch's checks (every 50 ms), so each reads the clock as it is then.
    pass(1100, 50)
    for (let i = 0; i < 10 && !settled; i++) await tick()
    expect(sent.slice(0, 2)).toEqual([['stuck'], ['waited']])
    expect(settled).toBe(true)
  })

  it.each(['before it started', 'after'])("on Workers, while a daily limit holds, a check goes every 15 minutes, one left unanswered giving way to the next, not once the old clock's time comes round again (the clock going back %s)", async (when) => {
    workers()
    const resetsAt = new Date(Date.now() + 3_600_000).toISOString()
    // Over the limit, except the first check, which never answers.
    const { sent, fetch } = sender((n, init) => {
      if (n === 2) return new Promise<Response>(() => {})
      const events = JSON.parse(String(init?.body)).events as TurnEvent[]
      return new Response(JSON.stringify({ rejected: events.map((e) => ({ eventId: e.eventId, reason: 'quota_exceeded', resetsAt })) }))
    })
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch, onError() {} })
    const capture = (conversationId: string) => void client.capture({ ...base, conversationId })
    // Its capture sees the clock go back.
    if (when === 'before it started') clock = 0
    capture('first')
    await tick()
    expect(sent).toEqual([['first']])
    if (when === 'after') {
      clock = 0
      capture('sees the clock go back')
      await tick()
    }
    pass(899_000)
    capture('early')
    await tick()
    expect(sent).toEqual([['first']])
    pass(1000)
    // Its request ends as it is sent, taking its timeout's timer with it.
    const timeout = timersGone()
    capture('check')
    for (let i = 0; i < 50 && sent.length === 1; i++) await Promise.resolve()
    timeout.mockRestore()
    expect(sent).toEqual([['first'], ['check']])
    // Unanswered: the next one 15 minutes after that.
    pass(899_000)
    capture('early again')
    await tick()
    expect(sent).toEqual([['first'], ['check']])
    pass(1000)
    capture('next check')
    await tick()
    expect(sent).toEqual([['first'], ['check'], ['next check']])
  })
})

describe("a clock that goes back, as an app's fake timers set performance.now() to 0: the wrapper's deadlines, durations and tool list reads", () => {
  // The wrapper's times are on the client's clock too: what was due before is as far off after,
  // neither held until the old clock's time comes round again nor taken for gone at once.
  let clock = 600_000
  /** Moves the clock and the timers on by `ms`, `step` at a time: what runs in a step reads the clock at its end. */
  const pass = (ms: number, step = 1000) => {
    for (let left = ms; left > 0; left -= step) {
      clock += Math.min(step, left)
      vi.advanceTimersByTime(Math.min(step, left))
    }
  }
  /** Lets what is queued run: microtasks, and reports' later turns (setImmediate, not faked). */
  const settle = async () => {
    for (let i = 0; i < 5; i++) await new Promise<void>((resolve) => setImmediate(() => setImmediate(resolve)))
  }
  const hanging = (async (_url: unknown, init?: RequestInit) =>
    new Promise<Response>((_, reject) => init?.signal?.addEventListener('abort', () => reject(new Error('aborted'))))) as typeof globalThis.fetch
  const acking = (async (_url: unknown, init?: RequestInit) => ackFor(init)) as typeof globalThis.fetch
  const quiet = { info() {}, warn() {} }
  const SENT = 'ses_AbCdEfGhIjKlMnOpQrStUv'
  /** A list whose tool declares no session_id: with its snapshot, a call's comes out. */
  const LISTED = { tools: [{ name: 'search', inputSchema: { type: 'object', properties: { q: { type: 'string' } } } }] }
  const EXTRAS = { extras: { sessions: true, intent: true, missingTools: true } }
  type Handler = (request: unknown, extra: unknown) => Promise<unknown>
  /**
   * A low-level server (SDK 1.x) instrumented with `options`, whose tool runs `tool` and whose list
   * `list` gives, called as the SDK would, through the handlers in its map. `call()` starts a call
   * with a session_id, with `signal` as its own. `seen` counts the calls that reached the tool, with
   * the arguments each got, those done, and the reads of the list, with each page's signal.
   */
  const lowLevel = (client: LSquare, options: InstrumentOptions, tool: () => unknown = () => undefined, list: () => unknown = () => LISTED) => {
    const low = new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: { listChanged: true } } })
    const seen = { args: [] as unknown[], done: 0, reads: 0, signals: [] as AbortSignal[], list }
    low.setRequestHandler(ListToolsRequestSchema, async (_request, extra) => {
      seen.reads++
      seen.signals.push(extra.signal)
      return (await seen.list()) as never
    })
    low.setRequestHandler(CallToolRequestSchema, async (request) => {
      seen.args.push(request.params.arguments)
      await tool()
      return { content: [] }
    })
    instrument(low, client, options)
    const handler = (low as unknown as { _requestHandlers: Map<string, Handler> })._requestHandlers.get('tools/call')!
    const call = (signal = new AbortController().signal) => {
      const extra = { signal, sendNotification: async () => {}, sendRequest: async () => {}, requestId: 1 }
      void handler({ method: 'tools/call', params: { name: 'search', arguments: { session_id: SENT } } }, extra).then(() => seen.done++)
    }
    return { seen, call }
  }
  /** A read's give-up timer goes, as switching an app's fake timers can discard it: those of 10 s set until mockRestore() never run. */
  const giveUpGone = () => {
    const fake = globalThis.setTimeout
    return vi.spyOn(globalThis, 'setTimeout').mockImplementation(((run: () => void, ms?: number) => (ms === 10_000 ? ({} as NodeJS.Timeout) : fake(run, ms))) as unknown as typeof setTimeout)
  }
  beforeEach(() => {
    clock = 600_000
    vi.spyOn(performance, 'now').mockImplementation(() => clock)
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
  })
  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  // Seen as it goes back, by a capture's report, or only once identify answers: the time between
  // the clock's last reading and then counts as none.
  it.each([
    ['waitUntil', 30_000, 'by a capture'],
    ['waitUntil', 30_000, 'only once identify answers'],
    ['flushBeforeReply', 2_000, 'by a capture'],
    ['flushBeforeReply', 2_000, 'only once identify answers'],
  ])("a call whose identify answers after the clock goes back is sent within its budget, %s's %i ms from the tool's return, its wait for the event given what is left of it (seen %s)", async (how, budget, seen) => {
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', requestTimeoutMs: 60_000, maxAttempts: 1, fetch: hanging, onError() {}, logger: quiet })
    const settled = vi.spyOn(client[INTERNAL], 'settled')
    let answer!: (identity: Identity) => void
    let handedOver = false
    const options: InstrumentOptions = how === 'waitUntil' ? { waitUntil: (promise) => void promise.then(() => (handedOver = true)) } : { flushBeforeReply: true }
    const { seen: server, call } = lowLevel(client, { ...options, identify: () => new Promise<Identity>((resolve) => (answer = resolve)) })
    call()
    await settle()
    // The tool has returned; identify is still running.
    expect(server.args).toHaveLength(1)
    clock = 0
    if (seen === 'by a capture') client.capture(invalid)
    pass(500)
    answer({ orgId: 'org_1' })
    await settle()
    const gap = seen === 'by a capture' ? 500 : 0
    expect(settled.mock.calls.map(([, timeoutMs]) => timeoutMs)).toEqual([budget - gap])
    // What waits for the sending: the hand-off, or the reply.
    const waited = () => (how === 'waitUntil' ? handedOver : server.done === 1)
    pass(budget - 501)
    await settle()
    expect(waited()).toBe(false)
    pass(1)
    await settle()
    expect(waited()).toBe(true)
  })

  // As the tool runs, seen by a capture's report; or before the call, seen as it arrives.
  it.each(['as the tool runs', 'before the call'])("a call records how long its tool ran, and waits its second for identify from the tool's return, across a clock that goes back (%s)", async (when) => {
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch: acking, onError() {}, logger: quiet })
    const record = vi.spyOn(client[INTERNAL], 'record')
    let returns!: () => void
    const tool = () => new Promise<void>((resolve) => (returns = resolve))
    // It ignores its signal and never answers.
    const { seen, call } = lowLevel(client, { identify: () => new Promise<Identity>(() => {}) }, tool)
    if (when === 'before the call') clock = 0
    call()
    await settle()
    expect(seen.args).toHaveLength(1)
    if (when === 'as the tool runs') {
      clock = 0
      client.capture(invalid)
    }
    pass(250)
    returns()
    await settle()
    pass(999)
    await settle()
    expect(record).not.toHaveBeenCalled()
    pass(1)
    await settle()
    expect(record.mock.calls.map(([recorded]) => recorded.durationMs)).toEqual([250])
  })

  // The read's give-up timer still running, or gone with nothing to end it.
  it.each(['running', 'gone'])("calls waiting on a tool list read as the clock goes back, one joining it after, wait what is left of the read's first second, not ten minutes more (its give-up timer %s)", async (timer) => {
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch: acking, onError() {}, logger: quiet })
    // The read never ends.
    const { seen, call } = lowLevel(client, EXTRAS, undefined, () => new Promise(() => {}))
    const timeout = timer === 'gone' ? giveUpGone() : undefined
    call()
    if (timeout) expect(timeout.mock.calls.filter(([, ms]) => ms === 10_000)).toHaveLength(1)
    timeout?.mockRestore()
    expect(seen.reads).toBe(1)
    clock = 0
    // Arriving, it sees the clock go back.
    call()
    await settle()
    pass(999)
    await settle()
    expect(seen.args).toEqual([])
    pass(1)
    await settle()
    // Passed on as they came, without the snapshot.
    expect(seen.args).toEqual([{ session_id: SENT }, { session_id: SENT }])
    expect(seen.reads).toBe(1)
  })

  it.each(['threw', 'was given up at 10 s'])("a tool list read that %s pauses reads for a minute, on the clock: not once the old clock's time comes round again", async (how) => {
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch: acking, onError() {}, logger: quiet })
    const { seen, call } = lowLevel(client, EXTRAS, undefined, how === 'threw' ? () => Promise.reject(new Error('down')) : () => new Promise(() => {}))
    call()
    await settle()
    if (how !== 'threw') {
      // Its call waits its second; the read is given up 9 seconds later.
      pass(1000)
      await settle()
      pass(9000)
      await settle()
    }
    expect(seen.done).toBe(1)
    seen.list = () => LISTED
    clock = 0
    // Arriving, it sees the clock go back.
    call()
    await settle()
    pass(59_999)
    call()
    await settle()
    expect(seen.done).toBe(3)
    expect(seen.reads).toBe(1)
    pass(1)
    call()
    await settle()
    expect(seen.reads).toBe(2)
    // It waited for the read, and the snapshot took its session_id out.
    expect(seen.args).toEqual([{ session_id: SENT }, { session_id: SENT }, { session_id: SENT }, {}])
  })

  // Its starting call living on, or cancelled, when the read is given up.
  it.each(['lives', 'was cancelled'])("a tool list read whose give-up timer went is given up by the first call 10 s after it started, on the clock, aborting its page: pausing reads for a minute, or only discarded when its starting call %s", async (starting) => {
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch: acking, onError() {}, logger: quiet })
    const { seen, call } = lowLevel(client, EXTRAS, undefined, () => new Promise(() => {}))
    const cancel = new AbortController()
    const timeout = giveUpGone()
    call(cancel.signal)
    expect(timeout.mock.calls.filter(([, ms]) => ms === 10_000)).toHaveLength(1)
    timeout.mockRestore()
    pass(1000)
    await settle()
    expect(seen.done).toBe(1)
    if (starting === 'was cancelled') cancel.abort()
    clock = 0
    // Arriving, it sees the clock go back. Past the read's first second, calls don't wait.
    call()
    await settle()
    pass(8999)
    call()
    await settle()
    expect(seen.done).toBe(3)
    expect(seen.signals[0]!.aborted).toBe(false)
    // From here, a read answers at once.
    seen.list = () => LISTED
    pass(1)
    // Gives it up, its page's signal aborted from a timer of its own.
    call()
    await settle()
    vi.advanceTimersByTime(0)
    expect(seen.signals[0]!.aborted).toBe(true)
    expect(seen.done).toBe(4)
    if (starting === 'was cancelled') {
      // Only discarded: the call that gave it up starts the next read, and gets its snapshot.
      expect(seen.reads).toBe(2)
      expect(seen.args.at(-1)).toEqual({})
      return
    }
    expect(seen.reads).toBe(1)
    expect(seen.args.at(-1)).toEqual({ session_id: SENT })
    pass(59_999)
    call()
    await settle()
    expect(seen.reads).toBe(1)
    pass(1)
    call()
    await settle()
    expect(seen.reads).toBe(2)
    expect(seen.args.at(-1)).toEqual({})
    expect(seen.done).toBe(6)
  })
})

describe('timeouts beyond Node\'s timer limit', () => {
  it('are capped, so Node never warns and a flush still waits', async () => {
    const warnings: string[] = []
    const onWarning = (warning: Error) => void warnings.push(warning.name)
    process.on('warning', onWarning)
    try {
      let answer!: () => void
      const client = new LSquare({
        apiKey: 'k',
        endpoint: 'https://x.test',
        flushTimeoutMs: 1e15,
        requestTimeoutMs: 1e15,
        beforeSendTimeoutMs: 1e15,
        logger: { info() {}, warn() {} },
        onError() {},
        fetch: (async (_url: string, init: RequestInit) => {
          await new Promise<void>((resolve) => (answer = resolve))
          const { events } = JSON.parse(String(init.body)) as { events: { eventId: string }[] }
          return new Response(JSON.stringify({ accepted: events.map((e) => e.eventId), duplicates: [], rejected: [] }))
        }) as unknown as typeof fetch,
      })
      client.capture({ conversationId: 'c', turnId: 't', orgId: 'o', toolName: 't', question: 'q' })
      const flushed = client.flush({ timeoutMs: 1e15 })
      await new Promise((resolve) => setTimeout(resolve, 50))
      answer()
      expect(await flushed).toMatchObject({ sent: 1 })
      await new Promise((resolve) => setImmediate(resolve))
      expect(warnings).not.toContain('TimeoutOverflowWarning')
      await client.shutdown()
    } finally {
      process.off('warning', onWarning)
    }
  })
})
