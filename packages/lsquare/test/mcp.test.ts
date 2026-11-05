// lsquare/mcp against real MCP servers and clients of both SDKs, over in-memory transports.
import { Client as Client1 } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport as Memory1 } from '@modelcontextprotocol/sdk/inMemory.js'
import { McpServer as McpServer1 } from '@modelcontextprotocol/sdk/server/mcp.js'
import { Server as Server1 } from '@modelcontextprotocol/sdk/server/index.js'
import { CallToolRequestSchema, McpError } from '@modelcontextprotocol/sdk/types.js'
import { Client as Client2 } from '@modelcontextprotocol/client'
import { McpServer as McpServer2, InMemoryTransport as Memory2, ProtocolError, Server as Server2 } from '@modelcontextprotocol/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { validateEvent } from '../../contract/src/validate.js'
import { z } from 'zod'
import { LSquare, type LSquareError, type LSquareEvent, type LSquareOptions, type ToolCallEvent, type TurnEvent } from '../src/index.js'
import { instrument, type InstrumentOptions } from '../src/mcp/index.js'
import { turnKey } from '../src/normalize.js'

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const tick = () => new Promise<void>((resolve) => setImmediate(() => setImmediate(resolve)))
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

type Tool = (args: Record<string, unknown>, extra: unknown) => unknown

/** One SDK's way of building servers and connecting a client to them. */
interface Sdk {
  name: string
  high(tools: Record<string, Tool>): any
  low(handler: (request: any, extra: any) => unknown): any
  connect(server: any): Promise<{ call(name: string, args?: Record<string, unknown>): Promise<any>; close(): Promise<void> }>
  protocolError(message: string): Error
}

const sdk1: Sdk = {
  name: 'SDK 1.x',
  high(tools) {
    const server = new McpServer1({ name: 'orders', version: '1.0.0' })
    for (const [name, tool] of Object.entries(tools)) server.registerTool(name, { inputSchema: { q: z.string().optional() } }, tool as never)
    return server
  },
  low(handler) {
    const server = new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
    server.setRequestHandler(CallToolRequestSchema, handler as never)
    return server
  },
  async connect(server) {
    const [a, b] = Memory1.createLinkedPair()
    await server.connect(a)
    const client = new Client1({ name: 'claude-code', version: '2.1' })
    await client.connect(b)
    return {
      call: (name, args = {}) => client.callTool({ name, arguments: args }).catch((error: { code?: unknown; message?: unknown }) => ({ error: { code: error.code, message: error.message } })),
      close: () => client.close(),
    }
  },
  protocolError: (message) => new McpError(-32602, message),
}

const sdk2: Sdk = {
  name: 'SDK 2.x',
  high(tools) {
    const server = new McpServer2({ name: 'orders', version: '1.0.0' })
    for (const [name, tool] of Object.entries(tools)) server.registerTool(name, { inputSchema: z.object({ q: z.string().optional() }) }, tool as never)
    return server
  },
  low(handler) {
    const server = new Server2({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
    server.setRequestHandler('tools/call', handler as never)
    return server
  },
  async connect(server) {
    const [a, b] = Memory2.createLinkedPair()
    await server.connect(a)
    const client = new Client2({ name: 'claude-code', version: '2.1' })
    await client.connect(b)
    return {
      call: (name, args = {}) => client.callTool({ name, arguments: args }).catch((error: { code?: unknown; message?: unknown }) => ({ error: { code: error.code, message: error.message } })),
      close: () => client.close(),
    }
  },
  protocolError: (message) => new ProtocolError(-32602, message),
}

function setup(options: LSquareOptions = {}) {
  const events: LSquareEvent[] = []
  const errors: LSquareError[] = []
  const logs: string[] = []
  const sent: number[] = []
  const fetch = (async (_url: unknown, init?: RequestInit) => {
    const batch = JSON.parse(String(init?.body)).events as LSquareEvent[]
    for (const event of batch) {
      const verdict = validateEvent(event)
      if (!verdict.ok) rejected.push(`${event.kind} ${event.eventId}: ${verdict.reason}`)
    }
    events.push(...batch)
    sent.push(performance.now())
    return new Response(JSON.stringify({ accepted: batch.map((e) => e.eventId), duplicates: [], rejected: [] }))
  }) as typeof globalThis.fetch
  const client = new LSquare({
    apiKey: 'lsq_test',
    endpoint: 'https://ingest.test',
    fetch,
    onError: (error) => errors.push(error),
    logger: { info: (m) => logs.push(m), warn: (m) => logs.push(m) },
    ...options,
  })
  const calls = () => events.filter((e): e is ToolCallEvent => e.kind === 'tool_call')
  const turns = () => events.filter((e): e is TurnEvent => e.kind === 'turn')
  return { client, events, calls, turns, errors, logs, sent }
}

/** As setup(), with an ingestion that holds each batch's answer until the test releases that batch. */
function held(options: LSquareOptions = {}) {
  const batches: Array<{ events: LSquareEvent[]; answered: boolean; release: () => void }> = []
  const fetch = (async (_url: unknown, init?: RequestInit) => {
    const events = JSON.parse(String(init?.body)).events as LSquareEvent[]
    for (const event of events) {
      const verdict = validateEvent(event)
      if (!verdict.ok) rejected.push(`${event.kind} ${event.eventId}: ${verdict.reason}`)
    }
    let release = () => {}
    const answer = new Promise<void>((resolve) => (release = resolve))
    const batch = { events, answered: false, release }
    batches.push(batch)
    await answer
    batch.answered = true
    return new Response(JSON.stringify({ accepted: events.map((e) => e.eventId), duplicates: [], rejected: [] }))
  }) as typeof globalThis.fetch
  return { ...setup({ fetch, ...options }), batches }
}

/** Waits until `check` holds, at most 2 s. */
async function until(check: () => boolean | Promise<boolean>) {
  for (let i = 0; i < 400 && !(await check()); i++) await sleep(5)
  expect(await check()).toBe(true)
}

/** Which of `refs` are still alive after ten collections, each in a later turn. */
async function survivors(refs: Record<string, WeakRef<object>>) {
  const gc = (globalThis as { gc?: () => void }).gc
  if (gc === undefined) throw new Error('run with --expose-gc')
  for (let i = 0; i < 10; i++) {
    await sleep(5)
    gc()
  }
  return Object.keys(refs).filter((name) => refs[name]!.deref() !== undefined)
}

const text = (value: string) => ({ content: [{ type: 'text' as const, text: value }] })
/** The extras turned off, for tests of what the wrapper records: their replies stay exactly the tool's. */
const OFF = { sessions: false, intent: false, missingTools: false }

/** Every event sent in these tests, as the ingestion would judge it. */
const rejected: string[] = []
afterEach(() => {
  vi.unstubAllGlobals()
  expect(rejected).toEqual([])
  rejected.length = 0
})

describe.each([sdk1, sdk2])('$name, high-level', (sdk) => {
  async function serve(tools: Record<string, Tool>, options: InstrumentOptions = {}, client?: ReturnType<typeof setup>) {
    const lsquare = client ?? setup()
    const server = instrument(sdk.high(tools), lsquare.client, options)
    const connection = await sdk.connect(server)
    return { ...lsquare, server, connection }
  }

  it('records a call: its input, result, outline and who called, under the call ID', async () => {
    const { connection, client, calls } = await serve({ search: async ({ q }) => text(`3 orders for ${q}`) }, { extras: OFF })
    expect(await connection.call('search', { q: 'late' })).toEqual(text('3 orders for late'))
    await client.flush()
    const [event] = calls()
    expect(event).toMatchObject({
      kind: 'tool_call',
      toolName: 'search',
      status: 'completed',
      input: { q: 'late' },
      result: { content: [{ type: 'text', text: '3 orders for late' }] },
      server: { name: 'orders', version: '1.0.0' },
      client: { name: 'claude-code', version: '2.1' },
    })
    expect(event!.eventId).toMatch(UUID)
    expect(event!.callId).toBe(event!.eventId)
    expect(event!.durationMs).toBeGreaterThanOrEqual(0)
  })

  it('tells a thrown error from a returned one and from the SDK refusing the call', async () => {
    const { connection, client, calls } = await serve({
      boom: async () => {
        throw new TypeError('lookup failed')
      },
      empty: async () => ({ ...text('Nothing found'), isError: true }),
    })
    const thrown = await connection.call('boom')
    expect(thrown.isError).toBe(true)
    await connection.call('empty')
    const unknown = await connection.call('nope')
    await client.flush()
    const [boom, empty, nope] = calls()
    expect(boom).toMatchObject({ status: 'error', error: { message: 'lookup failed', type: 'TypeError', category: 'thrown' } })
    expect(empty).toMatchObject({ status: 'error', error: { message: 'Nothing found', category: 'tool_error' } })
    expect(nope).toMatchObject({ toolName: 'nope', status: 'error', error: { category: 'protocol' } })
    if (sdk === sdk2) {
      // 2.x answers an unknown tool with a protocol error, which keeps its code.
      expect(unknown.error.code).toBe(-32602)
      expect(nope!.error).toMatchObject({ code: -32602 })
    } else {
      // 1.x turns it into an isError result: a message and no code.
      expect(unknown.isError).toBe(true)
      expect(nope!.error).not.toHaveProperty('code')
    }
  })

  it('records no inputs or results where told not to, nor an isError result’s text', async () => {
    const { connection, client, calls } = await serve(
      { kept: async () => text('a'), hidden: async () => ({ ...text('secret result'), isError: true }) },
      { inputs: (name) => name === 'kept', results: false },
    )
    await connection.call('kept', { q: 'x' })
    await connection.call('hidden', { q: 'y' })
    await client.flush()
    const [kept, hidden] = calls()
    expect(kept).toHaveProperty('input', { q: 'x' })
    expect(kept).not.toHaveProperty('result')
    expect(hidden).not.toHaveProperty('input')
    expect(hidden!.error).toEqual({ category: 'tool_error' })
  })

  it('records no inputs or results for a call its function throws on or answers with a promise, saying so once per option', async () => {
    const policies: Record<string, boolean> = { kept: true }
    const { connection, client, calls, errors, logs } = await serve(
      { kept: async () => text('a'), hidden: async () => ({ ...text('secret result'), isError: true }) },
      {
        extras: OFF,
        // No entry for hidden: the lookup throws.
        inputs: (name) => (policies[name] as unknown as { valueOf(): boolean }).valueOf(),
        // Not allowed by the types, but an app may still pass one.
        results: (async () => false) as unknown as () => boolean,
      },
    )
    expect(await connection.call('hidden', { q: 'y' })).toEqual({ ...text('secret result'), isError: true })
    await connection.call('hidden', { q: 'z' })
    await connection.call('kept', { q: 'x' })
    await client.flush()
    await tick()
    const [first, second, kept] = calls()
    for (const hidden of [first, second]) {
      expect(hidden).not.toHaveProperty('input')
      expect(hidden).not.toHaveProperty('result')
      expect(hidden!.error).toEqual({ category: 'tool_error' })
    }
    expect(kept).toHaveProperty('input', { q: 'x' })
    expect(kept).not.toHaveProperty('result')
    expect(errors).toEqual([])
    expect(logs).toEqual([
      "[lsquare] The inputs function threw or returned a promise: inputs aren't recorded for the calls it answered that way.",
      "[lsquare] The results function threw or returned a promise: results aren't recorded for the calls it answered that way.",
    ])
  })

  it('records no inputs for a call whose function answers with something whose then throws when read', async () => {
    const { connection, client, calls, logs } = await serve(
      { search: async () => text('a') },
      // Not allowed by the types: an answer that looks like a promise, and throws when asked.
      { extras: OFF, inputs: (() => ({ get then() { throw new Error('then') } })) as unknown as () => boolean },
    )
    await connection.call('search', { q: 'x' })
    await client.flush()
    await tick()
    expect(calls()[0]).not.toHaveProperty('input')
    expect(logs).toEqual(["[lsquare] The inputs function threw or returned a promise: inputs aren't recorded for the calls it answered that way."])
  })

  it('covers tools registered after instrument(), and a second instrument() changes nothing', async () => {
    const lsquare = setup()
    const server = instrument(sdk.high({}), lsquare.client)
    expect(instrument(server, lsquare.client)).toBe(server)
    server.registerTool('late', { inputSchema: sdk === sdk1 ? { q: z.string().optional() } : z.object({ q: z.string().optional() }) }, async () => text('ok'))
    const connection = await sdk.connect(server)
    await connection.call('late')
    await lsquare.client.flush()
    expect(lsquare.calls().map((e) => e.toolName)).toEqual(['late'])
  })

  it("makes a capture() inside the tool the call's agent part: its IDs fill the event's, with the turn's reference", async () => {
    const lsquare = setup()
    let callId: string | undefined
    const { connection } = await serve(
      {
        ask: async ({ q }) => {
          callId = lsquare.client.currentCallId()
          lsquare.client.capture({ orgId: 'org_1', conversationId: 'c/1', turnId: 7, question: q as string, answer: 'Two.', metadata: { plan: 'pro' } })
          lsquare.client.capture({ question: 'a second turn' })
          setTimeout(() => lsquare.client.capture({ question: 'after the reply' }), 5)
          return text('Two.')
        },
      },
      { identify: () => ({ orgId: 'org_from_identify', userId: 'u_1', metadata: { plan: 'free', region: 'eu' } }) },
      lsquare,
    )
    await connection.call('ask', { q: 'How many?' })
    await sleep(20)
    await lsquare.client.flush()
    const [call] = lsquare.calls()
    expect(call).toMatchObject({
      callId,
      orgId: 'org_1',
      userId: 'u_1',
      conversationId: 'c/1',
      turnRef: turnKey('org_1', 'c/1', '7'),
      agent: { question: 'How many?', answer: 'Two.', turnId: '7', status: 'completed' },
      metadata: { plan: 'pro', region: 'eu' },
    })
    expect(lsquare.turns().map((t) => [t.question, t.callId])).toEqual([
      ['a second turn', callId],
      ['after the reply', callId],
    ])
    expect(lsquare.client.currentCallId()).toBeUndefined()
  })

  it('sends limited diagnostics when identify fails, unless capture() supplied the IDs', async () => {
    const lsquare = setup()
    let fail = true
    const { connection } = await serve(
      {
        plain: async () => text('a'),
        captured: async () => {
          lsquare.client.capture({ userId: 'u_9', question: 'q' })
          return text('b')
        },
      },
      {
        identify: () => {
          if (fail) throw new Error('auth down')
          return { userId: 'u_1' }
        },
      },
      lsquare,
    )
    await connection.call('plain', { q: 'jane@acme.io' })
    await connection.call('captured')
    await lsquare.client.flush()
    await tick()
    const [plain, captured] = lsquare.calls()
    expect(plain).toMatchObject({ identity: 'failed', toolName: 'plain', status: 'completed' })
    expect(plain).not.toHaveProperty('input')
    expect(captured).toMatchObject({ userId: 'u_9' })
    expect(captured).not.toHaveProperty('identity')
    expect(lsquare.errors.map((e) => e.code)).toEqual(['identify_failed', 'identify_failed'])
    fail = false
    await connection.call('plain')
    await lsquare.client.flush()
    expect(lsquare.calls()[2]).toMatchObject({ userId: 'u_1' })
  })

  it("never delays the reply for identify, and waits for it at most a second after the tool returns", async () => {
    const lsquare = setup()
    const { connection } = await serve({ fast: async () => text('ok') }, { identify: () => new Promise(() => {}) }, lsquare)
    const started = performance.now()
    await connection.call('fast')
    expect(performance.now() - started).toBeLessThan(200)
    await sleep(1_100)
    await lsquare.client.flush()
    expect(lsquare.calls()[0]).toMatchObject({ identity: 'failed' })
    expect(lsquare.errors[0]?.message).toContain('took longer than its deadline')
  })

  it('skips identify past the hook limit, and treats invalid IDs as a failure', async () => {
    const lsquare = setup({ maxConcurrentHooks: 1 })
    let release: () => void = () => {}
    const signals: AbortSignal[] = []
    const { connection } = await serve(
      { a: async () => text('a'), b: async () => text('b'), c: async () => text('c') },
      {
        identify: ({ toolName, signal }) => {
          signals.push(signal)
          if (toolName === 'a') return new Promise((resolve) => (release = () => resolve({ userId: 'u_a' })))
          return { userId: 'bad\u0000id' }
        },
      },
      lsquare,
    )
    // a holds the only slot past its deadline: b isn't identified at all.
    await Promise.all([connection.call('a'), connection.call('b')])
    await sleep(1_100)
    expect(signals).toHaveLength(1)
    expect(signals[0]!.aborted).toBe(true)
    release()
    await sleep(10)
    await connection.call('c')
    await lsquare.client.flush()
    await tick()
    // b's event goes first: skipped, it waits for nothing, while a's waits out its deadline.
    expect(lsquare.calls().map((e) => [e.toolName, e.identity])).toEqual([
      ['b', 'failed'],
      ['a', 'failed'],
      ['c', 'failed'],
    ])
    expect(lsquare.errors.map((e) => e.message.split(';')[0])).toEqual([
      'identify skipped at the hook limit',
      'identify took longer than its deadline',
      'identify returned an invalid userId',
    ])
  })

  it("never aborts identify's signal once it has answered, when the event is built or at its deadline", async () => {
    const lsquare = setup()
    const signals: AbortSignal[] = []
    let fired = 0
    const { connection } = await serve(
      { sync: async () => text('ok'), async: async () => text('ok') },
      {
        extras: OFF,
        identify: ({ toolName, signal }) => {
          signals.push(signal)
          signal.addEventListener('abort', () => fired++)
          return toolName === 'sync' ? { userId: 'u_1' } : Promise.resolve({ userId: 'u_1' })
        },
      },
      lsquare,
    )
    await connection.call('sync')
    await connection.call('async')
    await lsquare.client.flush()
    // Past the deadline as well: an abort left to it would have fired by now.
    await sleep(1_100)
    expect(lsquare.calls().map((e) => e.userId)).toEqual(['u_1', 'u_1'])
    expect(signals.map((signal) => signal.aborted)).toEqual([false, false])
    expect(fired).toBe(0)
  })

  it('sends the event before the reply with flushBeforeReply, or after flush() inside the tool', async () => {
    for (const options of [{ flushBeforeReply: true }, {}] as InstrumentOptions[]) {
      const lsquare = setup()
      const { connection } = await serve(
        {
          ask: async () => {
            if (!options.flushBeforeReply) await lsquare.client.flush()
            return text('ok')
          },
        },
        options,
        lsquare,
      )
      await connection.call('ask')
      // Sent before the reply reached the client, without any flush after it.
      expect(lsquare.calls()).toHaveLength(1)
    }
  })

  it('hands each call’s sending to waitUntil', async () => {
    const lsquare = setup()
    const promises: Promise<unknown>[] = []
    const { connection } = await serve({ ask: async () => text('ok') }, { waitUntil: (promise) => promises.push(promise) }, lsquare)
    await connection.call('ask')
    expect(promises).toHaveLength(1)
    await promises[0]
    expect(lsquare.calls()).toHaveLength(1)
  })

  it('waits for the turns captured inside the call with its event, in waitUntil and in a reply that waits', async () => {
    for (const how of ['waitUntil', 'flushBeforeReply'] as const) {
      const lsquare = held({ flushTimeoutMs: 10_000 })
      const handed: Promise<unknown>[] = []
      const options: InstrumentOptions = how === 'waitUntil' ? { waitUntil: (promise) => handed.push(promise) } : { flushBeforeReply: true }
      const { connection } = await serve(
        {
          ask: async () => {
            lsquare.client.capture({ question: 'agent part' })
            lsquare.client.capture({ conversationId: 'c1', turnId: 2, question: 'a turn of its own' })
            // The turn goes first, in a batch of its own.
            await until(() => lsquare.batches.length === 1)
            return text('ok')
          },
        },
        { ...options, extras: OFF },
        lsquare,
      )
      const reply = connection.call('ask')
      // What waits: the promise waitUntil was handed (before the reply), or the reply itself.
      const waited = how === 'waitUntil' ? reply.then(() => handed[0]) : reply
      let done = false
      void waited.then(() => (done = true))
      await until(() => lsquare.batches.length === 2)
      const [turn, call] = lsquare.batches
      expect([turn!.events.map((e) => e.kind), call!.events.map((e) => e.kind)]).toEqual([['turn'], ['tool_call']])
      call!.release()
      // The call's event is sent: only the turn is left.
      await until(async () => (await lsquare.client.flush({ timeoutMs: 0 })).pending === 1)
      await tick()
      expect(done, how).toBe(false)
      turn!.release()
      await waited
      expect(turn!.answered).toBe(true)
    }
  })

  it('waits for each turn captured inside the call on its own, two that share an event ID included', async () => {
    let skip = () => {}
    const skipped = new Promise<void>((resolve) => (skip = resolve))
    // The first turn's hook holds until it is skipped; the rest go at once.
    const lsquare = held({ beforeSend: (event) => (event.kind === 'turn' && event.question === 'first' ? skipped.then(() => null) : event) })
    const handed: Promise<unknown>[] = []
    const { connection } = await serve(
      {
        ask: async () => {
          lsquare.client.capture({ question: 'agent part' })
          // The same conversationId and turnId: one event ID for both.
          lsquare.client.capture({ conversationId: 'c1', turnId: 2, question: 'first' })
          lsquare.client.capture({ conversationId: 'c1', turnId: 2, question: 'second' })
          await until(() => lsquare.batches.length === 1)
          return text('ok')
        },
      },
      { extras: OFF, waitUntil: (promise) => handed.push(promise) },
      lsquare,
    )
    await connection.call('ask')
    await until(() => lsquare.batches.length === 2)
    const [second, call] = lsquare.batches
    expect([second!.events.map((e) => (e as TurnEvent).question), call!.events.map((e) => e.kind)]).toEqual([['second'], ['tool_call']])
    let settled = false
    void handed[0]!.then(() => (settled = true))
    skip()
    call!.release()
    // The first turn skipped, the call's event sent: only the second turn is left.
    await until(async () => (await lsquare.client.flush({ timeoutMs: 0 })).pending === 1)
    await tick()
    expect(settled).toBe(false)
    second!.release()
    await handed[0]
    expect(second!.answered).toBe(true)
  })

  // What the call was given, returned or threw, and its agent part, are let go once its event is
  // built: a host may hold what waitUntil was handed for as long as the ingestion takes, and a turn
  // captured inside the call keeps the call's scope (in its capture's context) until it is sent.
  it.each([
    ['returns', 'Node'],
    ['throws', 'Node'],
    ['returns', 'Workers'],
    ['throws', 'Workers'],
  ])('keeps nothing of a call whose tool %s alive while its sending is held, on %s, recording neither inputs nor results', async (how, runtime) => {
    if (runtime === 'Workers') vi.stubGlobal('navigator', { userAgent: 'Cloudflare-Workers' })
    const lsquare = held()
    const handed: Promise<unknown>[] = []
    const refs: Record<string, WeakRef<object>> = {}
    const { connection } = await serve(
      {
        ask: async () => {
          const agent = { note: 'the agent part' }
          refs.agent = new WeakRef(agent)
          lsquare.client.capture({ question: 'agent part', metadata: agent })
          lsquare.client.capture({ conversationId: 'c1', turnId: 2, question: 'a turn of its own' })
          const out = how === 'returns' ? text('r'.repeat(1024 * 1024)) : Object.assign(new Error('boom'), { detail: 'e'.repeat(1024 * 1024) })
          refs[how] = new WeakRef(out)
          if (out instanceof Error) throw out
          return out
        },
      },
      { extras: OFF, inputs: false, results: false, waitUntil: (promise) => handed.push(promise) },
      lsquare,
    )
    // Part of the arguments as the request carries them, made here alone: the SDK passes it on as it is.
    const call = () => {
      const blob = { data: 'a'.repeat(1024 * 1024) }
      refs.arguments = new WeakRef(blob)
      return connection.call('ask', { q: 'q', blob }).then(() => {})
    }
    await call()
    // The call's event and the turn, posted and held.
    await until(() => lsquare.batches.flatMap((batch) => batch.events).length === 2)
    expect(handed).toHaveLength(1)
    let settled = false
    void handed[0]!.then(() => (settled = true))
    expect(await survivors(refs)).toEqual([])
    expect(settled).toBe(false)
    for (const batch of lsquare.batches) batch.release()
    await handed[0]
    const event = lsquare.batches.flatMap((batch) => batch.events).find((e) => e.kind === 'tool_call')
    expect(event).toMatchObject({ status: how === 'returns' ? 'completed' : 'error', agent: { question: 'agent part' }, metadata: { note: 'the agent part' } })
    expect(event).not.toHaveProperty('input')
    expect(event).not.toHaveProperty('result')
  })

  it("keeps none of a call's arguments alive through an identify that ignores its signal and runs on", async () => {
    const lsquare = setup({ flushTimeoutMs: 50 })
    // Holds nothing of what it was given; its promise lives as long as the test keeps its resolver.
    const settle: Array<() => void> = []
    const identify = () => new Promise<undefined>((resolve) => settle.push(() => resolve(undefined)))
    const { connection } = await serve({ ask: async () => text('ok') }, { extras: OFF, inputs: false, flushBeforeReply: true, identify }, lsquare)
    const refs: Record<string, WeakRef<object>> = {}
    const call = () => {
      const blob = { data: 'a'.repeat(1024 * 1024) }
      refs.arguments = new WeakRef(blob)
      return connection.call('ask', { q: 'q', blob }).then(() => {})
    }
    await call()
    // Sent without it, at its deadline, while it runs on.
    expect(lsquare.calls()).toHaveLength(1)
    expect(settle).toHaveLength(1)
    expect(await survivors(refs)).toEqual([])
    for (const resolve of settle) resolve()
    await lsquare.client.flush()
    expect(lsquare.errors.map((error) => error.message)).toEqual(['identify took longer than its deadline; the call was sent as limited diagnostics.'])
  })

  // What waits on that identify holds the hook limit's count, the ID fields last learned and the ID
  // check: neither the server it was instrumented on nor the client, which a host may make per request.
  it.each(['server', 'server and client'])('keeps neither a per-request %s nor what its tool holds alive through an identify that ignores its signal and runs on', async (made) => {
    const shared = setup({ flushTimeoutMs: 50 })
    const settle: Array<() => void> = []
    const identify = () => new Promise<undefined>((resolve) => settle.push(() => resolve(undefined)))
    const refs: Record<string, WeakRef<object>> = {}
    const errors: string[] = []
    const onError = (error: LSquareError) => void errors.push(error.message)
    const request = async () => {
      const lsquare = made === 'server' ? shared : setup({ flushTimeoutMs: 50, onError })
      const held = { data: 'h'.repeat(1024 * 1024) }
      refs.held = new WeakRef(held)
      const server = instrument(sdk.high({ ask: async () => text(held.data.slice(0, 2)) }), lsquare.client, { extras: OFF, flushBeforeReply: true, identify })
      refs.server = new WeakRef(server)
      if (lsquare !== shared) refs.client = new WeakRef(lsquare.client)
      const connection = await sdk.connect(server)
      expect(await connection.call('ask')).toEqual(text('hh'))
      // Sent without it, at its deadline, while it runs on.
      expect(lsquare.calls()).toHaveLength(1)
      await connection.close()
      await server.close()
    }
    await request()
    expect(settle).toHaveLength(1)
    expect(await survivors(refs)).toEqual([])
    for (const resolve of settle) resolve()
    await shared.client.flush()
    await tick()
    expect([...shared.errors.map((error) => error.message), ...errors]).toEqual(['identify took longer than its deadline; the call was sent as limited diagnostics.'])
  })

  it('waits for identify up to a second in the background, whatever flushTimeoutMs is', async () => {
    // Neither waitUntil nor a reply that waits: flushTimeoutMs bounds neither the sending nor identify.
    const lsquare = setup({ flushTimeoutMs: 0 })
    const { connection } = await serve({ ask: async () => text('ok') }, { extras: OFF, identify: () => sleep(50).then(() => ({ userId: 'u-1' })) }, lsquare)
    await connection.call('ask')
    await lsquare.client.flush({ timeoutMs: 2_000 })
    expect(lsquare.calls()).toMatchObject([{ userId: 'u-1' }])
  })

  it('never makes a call fail, whatever the SDK does', async () => {
    const lsquare = setup({ beforeSend: () => { throw new Error('hook') }, onError: () => { throw new Error('handler') } })
    const { connection } = await serve({ ask: async () => text('ok') }, { identify: () => { throw new Error('identify') }, extras: OFF }, lsquare)
    expect(await connection.call('ask')).toEqual(text('ok'))
  })
})

describe('SDK 1.x, a tool that starts another one and returns without waiting for it', () => {
  // The other tool's function is the tapped one (SDK 1.x calls a registered tool's own `handler`),
  // run in the call's scope: once the call has let go, what it returns is recorded nowhere.
  it('keeps nothing of what that one returns once the call has let go, though a turn captured inside it is held', async () => {
    const lsquare = held()
    const refs: Record<string, WeakRef<object>> = {}
    let finish = () => {}
    const later = new Promise<void>((resolve) => (finish = resolve))
    let detached = false
    const server = new McpServer1({ name: 'orders', version: '1.0.0' })
    const other = server.registerTool('other', {}, async () => {
      if (!detached) return text('quick')
      await later
      const out = text('o'.repeat(1024 * 1024))
      refs.other = new WeakRef(out)
      return out
    })
    server.registerTool('ask', {}, async (extra) => {
      lsquare.client.capture({ question: 'agent part' })
      lsquare.client.capture({ conversationId: 'c1', turnId: 2, question: 'a turn of its own' })
      detached = true
      void (other.handler as (extra: unknown) => Promise<unknown>)(extra)
      return text('ok')
    })
    instrument(server, lsquare.client, { extras: OFF })
    const connection = await sdk1.connect(server)
    // Called on its own first, which taps its function.
    expect(await connection.call('other')).toEqual(text('quick'))
    expect(await connection.call('ask')).toEqual(text('ok'))
    // The first call's event, then the second's turn and its event: built, so let go of.
    await until(() => lsquare.batches.flatMap((batch) => batch.events).length === 3)
    finish()
    await until(() => refs.other !== undefined)
    expect(await survivors(refs)).toEqual([])
    for (const batch of lsquare.batches) batch.release()
    await lsquare.client.flush()
    expect(lsquare.batches.flatMap((batch) => batch.events).map((event) => event.kind)).toEqual(['tool_call', 'turn', 'tool_call'])
  })
})

describe.each([sdk1, sdk2])('$name, low-level', (sdk) => {
  it('records calls, a thrown error as thrown and one with a JSON-RPC code as protocol', async () => {
    const lsquare = setup()
    const server = instrument(
      sdk.low(async (request) => {
        const name = request.params.name
        if (name === 'boom') throw new Error('broke')
        if (name === 'bad') throw sdk.protocolError('Bad params')
        return text(`ran ${name}`)
      }),
      lsquare.client,
    )
    const connection = await sdk.connect(server)
    expect(await connection.call('search', { q: 'x' })).toMatchObject(text('ran search'))
    expect((await connection.call('boom')).error).toBeDefined()
    expect((await connection.call('bad')).error).toMatchObject({ code: -32602 })
    await lsquare.client.flush()
    const [search, boom, bad] = lsquare.calls()
    expect(search).toMatchObject({ toolName: 'search', status: 'completed', input: { q: 'x' }, server: { name: 'orders' } })
    expect(boom).toMatchObject({ status: 'error', error: { message: 'broke', category: 'thrown' } })
    expect(bad).toMatchObject({ status: 'error', error: { message: expect.stringContaining('Bad params'), code: -32602, category: 'protocol' } })
  })

  it("keeps none of a call's arguments or agent part alive while its sending is held, the tool being the handler", async () => {
    const lsquare = held()
    const handed: Promise<unknown>[] = []
    const refs: Record<string, WeakRef<object>> = {}
    const server = instrument(
      sdk.low(async () => {
        const agent = { note: 'the agent part' }
        refs.agent = new WeakRef(agent)
        lsquare.client.capture({ question: 'agent part', metadata: agent })
        lsquare.client.capture({ conversationId: 'c1', turnId: 2, question: 'a turn of its own' })
        return text('ok')
      }),
      lsquare.client,
      { inputs: false, waitUntil: (promise) => handed.push(promise) },
    )
    const connection = await sdk.connect(server)
    const call = () => {
      const blob = { data: 'a'.repeat(1024 * 1024) }
      refs.arguments = new WeakRef(blob)
      return connection.call('ask', { q: 'q', blob }).then(() => {})
    }
    await call()
    await until(() => lsquare.batches.flatMap((batch) => batch.events).length === 2)
    expect(await survivors(refs)).toEqual([])
    for (const batch of lsquare.batches) batch.release()
    await handed[0]
    expect(lsquare.batches.flatMap((batch) => batch.events).find((event) => event.kind === 'tool_call')).toMatchObject({ metadata: { note: 'the agent part' } })
  })
})

describe('instrument()', () => {
  it('warns once on a host that stops work after the reply, and when both waitUntil and flushBeforeReply are set', async () => {
    const lsquare = setup()
    process.env.VERCEL = '1'
    try {
      instrument(sdk1.high({}), lsquare.client)
      instrument(sdk1.high({}), lsquare.client)
      instrument(sdk1.high({}), lsquare.client, { flushBeforeReply: true })
      instrument(sdk1.high({}), lsquare.client, { waitUntil: () => {}, flushBeforeReply: true })
    } finally {
      delete process.env.VERCEL
    }
    await tick()
    expect(lsquare.logs).toEqual([
      '[lsquare] This looks like Vercel, which can stop work once a reply is sent: pass waitUntil, or flushBeforeReply: true, to instrument(), or events may be lost.',
      '[lsquare] Both waitUntil and flushBeforeReply are set: waitUntil is used, and flushBeforeReply ignored.',
    ])
  })

  it('leaves the server as it is without an API key, saying so once', async () => {
    const lsquare = setup({ apiKey: undefined })
    const server = sdk1.high({ ask: async () => text('ok') })
    const before = server.server._requestHandlers.get('tools/call')
    instrument(server, lsquare.client)
    instrument(sdk1.high({}), lsquare.client)
    expect(server.server._requestHandlers.get('tools/call')).toBe(before)
    await tick()
    expect(lsquare.logs.filter((line) => line.includes('instrument()'))).toEqual(['[lsquare] No apiKey: instrument() leaves servers as they are.'])
  })

  it('wraps fully in dry run, and says so', async () => {
    const lsquare = setup({ apiKey: undefined, dryRun: true })
    const server = instrument(sdk2.high({ ask: async () => text('ok') }), lsquare.client)
    const connection = await sdk2.connect(server)
    await connection.call('ask')
    await lsquare.client.flush()
    await tick()
    expect(JSON.parse(lsquare.logs[0]!.replace('[lsquare dry-run] ', ''))).toEqual({
      server: 'orders',
      level: 'high',
      records: 'every tools/call',
      extras: {
        session_id: '1 of the 1 tools registered so far',
        intent: '1 of the 1 tools registered so far',
        later: "tools registered later follow the same rules, and a tool whose listed schema can't take an input is listed as it is",
        get_more_tools: 'added while the server has an enabled tool of its own',
        idLine: "added to a task's first result when its call can't be grouped by its protocol session or a conversationId",
      },
    })
    expect(lsquare.logs[1]).toContain('"kind":"tool_call"')
  })

  it("leaves a server it doesn't recognise as it is, with a warning, and never throws", async () => {
    const lsquare = setup()
    const thing = { setRequestHandler() {} }
    expect(instrument(thing, lsquare.client)).toBe(thing)
    expect(instrument(null, lsquare.client)).toBeNull()
    expect(instrument(sdk1.high({}), {} as LSquare)).toBeDefined()
    await tick()
    expect(lsquare.logs).toEqual(["[lsquare] instrument() doesn't recognise this server: it is left as it is."])
  })
})
