// Regression tests for the deep review's wrapper findings on calls, identify and low-level tool
// lists (31 October), one describe each: ts-mcp-5 (an identify settling past its deadline before
// its timer ran), ts-core-28 (flushTimeoutMs 0 with a flush() inside the tool), ts-mcp-6 (rejected
// promises the wrapper reads in a tools/list page or a listed schema) and ts-mcp-16 (waits for a
// snapshot kept until the list next changes). Then the first Codex review's: ts-core-14's wrapper
// half (identify's result, the IDs and the options read from a polluted Object.prototype) and the
// wrapper's reads running a thenable's `then`. Then the second's: how a call ended, read from a
// polluted Object.prototype (in ts-core-14's describe), and the rejected promise a `then` getter
// gives when the wrapper asks whether a callback answered with a thenable. Then the third's, with
// the sweep after it (also in ts-core-14's describe): a result's task, a thrown error, and the
// other fields of what the app or an SDK hands over, read from a polluted Object.prototype; and
// the check of that sweep, on real stdio and HTTP transports.
import { PassThrough } from 'node:stream'
import v8 from 'node:v8'
import { Client as Client1 } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport as Memory1 } from '@modelcontextprotocol/sdk/inMemory.js'
import { McpServer as McpServer1 } from '@modelcontextprotocol/sdk/server/mcp.js'
import { Server as Server1 } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport as Stdio1 } from '@modelcontextprotocol/sdk/server/stdio.js'
import { WebStandardStreamableHTTPServerTransport as Http1 } from '@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js'
import { CallToolRequestSchema, InitializeRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { Client as Client2 } from '@modelcontextprotocol/client'
import { McpServer as McpServer2, InMemoryTransport as Memory2, preloadSchemas, Server as Server2, WebStandardStreamableHTTPServerTransport as Http2 } from '@modelcontextprotocol/server'
import { serveStdio, StdioServerTransport as Stdio2 } from '@modelcontextprotocol/server/stdio'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { z } from 'zod'
import { validateEvent } from '../../contract/src/validate.js'
import { INTERNAL } from '../src/client.js'
import { LSquare, type LSquareError, type LSquareEvent, type LSquareOptions, type ToolCallEvent, type TurnEvent } from '../src/index.js'
import { instrument, type Identity, type InstrumentOptions } from '../src/mcp/index.js'
import { mark, newLists, newWait, waitOnce, type Reader } from '../src/mcp/lists.js'
import { adopted, declared, own, read, silence } from '../src/mcp/read.js'
import { turnKey } from '../src/normalize.js'
import { createHash } from 'node:crypto'

const hashed = (id: string): string => createHash('sha256').update(id).digest('hex')
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const text = (value: string) => ({ content: [{ type: 'text' as const, text: value }] })
const OFF = { sessions: false, intent: false, missingTools: false }

const rejected: string[] = []
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  expect(rejected).toEqual([])
  rejected.length = 0
})

function setup(options: LSquareOptions = {}) {
  const events: LSquareEvent[] = []
  const errors: LSquareError[] = []
  /** Every batch, as its request starts. */
  const started: LSquareEvent[][] = []
  const fetch = (async (_url: unknown, init?: RequestInit) => {
    const batch = JSON.parse(String(init?.body)).events as LSquareEvent[]
    started.push(batch)
    for (const event of batch) {
      const verdict = validateEvent(event)
      if (!verdict.ok) rejected.push(`${event.kind}: ${verdict.reason}`)
    }
    events.push(...batch)
    return new Response(JSON.stringify({ accepted: batch.map((e) => e.eventId), duplicates: [], rejected: [] }))
  }) as typeof globalThis.fetch
  const client = new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test', fetch, onError: (e) => errors.push(e), logger: { info() {}, warn() {} }, ...options })
  const calls = () => events.filter((e): e is ToolCallEvent => e.kind === 'tool_call')
  const turns = () => events.filter((e): e is TurnEvent => e.kind === 'turn')
  const startedCalls = () => started.flat().filter((e) => e.kind === 'tool_call').length
  return { client, calls, turns, startedCalls, errors, internal: client[INTERNAL] }
}

async function connect(sdk: 1 | 2, server: any) {
  const [a, b] = sdk === 1 ? Memory1.createLinkedPair() : Memory2.createLinkedPair()
  await server.connect(a)
  const client = sdk === 1 ? new Client1({ name: 'claude-code', version: '2.1' }) : new Client2({ name: 'claude-code', version: '2.1' })
  await client.connect(b as never)
  return {
    call: (tool: string, args: Record<string, unknown> = {}) => client.callTool({ name: tool, arguments: args }) as Promise<unknown>,
    tools: async () => ((await client.listTools()) as { tools: Array<{ name: string; inputSchema: { properties?: Record<string, unknown> } }> }).tools,
  }
}

const high = (sdk: 1 | 2): any => (sdk === 1 ? new McpServer1({ name: 'orders', version: '1.0.0' }) : new McpServer2({ name: 'orders', version: '1.0.0' }))

/** The rejections nothing handled while it listened, as Node reports them (one ends a Node process). */
function unhandledDuring() {
  const seen: unknown[] = []
  const listener = (reason: unknown) => seen.push(reason)
  process.on('unhandledRejection', listener)
  return { seen, stop: () => process.off('unhandledRejection', listener) }
}

/** Promises alive now, after a full collection: what a wait leaves behind, counted exactly. */
function promises(): number {
  const gc = (globalThis as { gc?: () => void }).gc
  if (gc === undefined) throw new Error('run with --expose-gc')
  gc()
  return (v8 as unknown as { queryObjects(ctor: unknown, options: { format: 'count' }): number }).queryObjects(Promise, { format: 'count' })
}

describe('ts-mcp-5: an identify that settles past its deadline before its timer runs', () => {
  // A blocked event loop: time passes, and timers wait. Moving the clock on as identify answers
  // does the same without blocking the test for a second: the deadline's timer, due 1 s after the
  // tool returned, hasn't run when the answer comes, 1.1 s later by the clock.
  it.each([1, 2] as const)('is refused as late, and teaches no ID fields (SDK %d.x)', async (sdk) => {
    const lsquare = setup()
    const real = performance.now.bind(performance)
    let skew = 0
    vi.spyOn(performance, 'now').mockImplementation(() => real() + skew)
    const late = (identity: Identity) => new Promise<Identity>((resolve) => setTimeout(() => ((skew += 1_100), resolve(identity)), 10))
    let answer: () => Promise<Identity> = () => late({ orgId: 'org_1', userId: 'u-1' })
    const server = instrument(high(sdk), lsquare.client, { extras: OFF, identify: () => answer() })
    server.registerTool('t', {}, async () => text('ok'))
    const connection = await connect(sdk, server)
    expect(await connection.call('t')).toEqual(text('ok'))
    await lsquare.client.flush({ timeoutMs: 2_000 })
    // A late `{}` would have made capture()'s IDs enough for any later failure.
    answer = () => late({})
    await connection.call('t')
    await lsquare.client.flush({ timeoutMs: 2_000 })
    const [first, second] = lsquare.calls()
    for (const event of [first, second]) {
      expect(event).toMatchObject({ identity: 'failed' })
      expect(event).not.toHaveProperty('userId')
      expect(event).not.toHaveProperty('result')
    }
    expect(lsquare.errors.map((error) => error.message)).toEqual(Array(2).fill('identify took longer than its deadline; the call was sent as limited diagnostics.'))
    expect(lsquare.internal.identified.keys).toBeUndefined()
  })

  it('still takes one that answers in time, the clock moving on after', async () => {
    const lsquare = setup()
    const real = performance.now.bind(performance)
    let skew = 0
    vi.spyOn(performance, 'now').mockImplementation(() => real() + skew)
    const server = instrument(high(1), lsquare.client, {
      extras: OFF,
      identify: () => new Promise<Identity>((resolve) => setTimeout(() => (resolve({ userId: 'u-1' }), setTimeout(() => (skew += 1_100), 0)), 10)),
    })
    server.registerTool('t', {}, async () => text('ok'))
    await (await connect(1, server)).call('t')
    await lsquare.client.flush({ timeoutMs: 2_000 })
    expect(lsquare.calls()).toMatchObject([{ userId: 'u-1' }])
    expect(lsquare.errors).toEqual([])
    expect(lsquare.internal.identified.keys).toEqual(['userId'])
  })
})

describe('ts-core-28: flushTimeoutMs 0 and a flush() inside the tool', () => {
  const ways = ['without waitUntil', 'with waitUntil', 'with flushBeforeReply'] as const
  const options = (how: (typeof ways)[number], handed: Promise<unknown>[]) => ({
    ...(how === 'with waitUntil' ? { waitUntil: (promise: Promise<unknown>) => void handed.push(promise) } : {}),
    ...(how === 'with flushBeforeReply' ? { flushBeforeReply: true } : {}),
  })
  const noted = (lsquare: ReturnType<typeof setup>) => async () => {
    // A common pattern: capture(), then await flush(), inside the tool.
    lsquare.client.capture({ question: 'find late orders' })
    await lsquare.client.flush()
    return text('noted')
  }

  // 0 waits for nothing: the reply doesn't wait for identify, and identify keeps its second.
  for (const sdk of [1, 2] as const) {
    it.each(ways)(`still gives identify its second, %s (SDK ${sdk}.x)`, async (how) => {
      const lsquare = setup({ flushTimeoutMs: 0 })
      let answer!: (identity: Identity) => void
      const handed: Promise<unknown>[] = []
      const server = instrument(high(sdk), lsquare.client, { extras: OFF, identify: () => new Promise<Identity>((resolve) => (answer = resolve)), ...options(how, handed) })
      server.registerTool('noted', {}, noted(lsquare))
      // The reply comes while identify still runs.
      expect(await (await connect(sdk, server)).call('noted')).toEqual(text('noted'))
      await sleep(20)
      answer({ orgId: 'org_1', userId: 'u-1' })
      await Promise.all(handed)
      await lsquare.client.flush({ timeoutMs: 2_000 })
      expect(lsquare.calls()).toMatchObject([{ orgId: 'org_1', userId: 'u-1', agent: { question: 'find late orders' } }])
      expect(lsquare.errors).toEqual([])
      expect(handed).toHaveLength(how === 'with waitUntil' ? 1 : 0)
    })
  }

  // The reply still takes its turn: an event whose identify has answered starts sending before it,
  // which is what flush() inside the tool is for, on a host that stops work once it has replied.
  it.each(ways)("starts each call's request before its reply when identify has answered, %s (SDK 1.x)", async (how) => {
    const lsquare = setup({ flushTimeoutMs: 0 })
    const handed: Promise<unknown>[] = []
    const server = instrument(high(1), lsquare.client, { extras: OFF, identify: (): Identity => ({ userId: 'u-1' }), ...options(how, handed) })
    server.registerTool('noted', {}, noted(lsquare))
    const connection = await connect(1, server)
    for (let call = 1; call <= 3; call++) {
      expect(await connection.call('noted')).toEqual(text('noted'))
      expect(lsquare.startedCalls()).toBe(call)
    }
    await Promise.all(handed)
    await lsquare.client.flush({ timeoutMs: 2_000 })
    expect(lsquare.calls()).toMatchObject(Array(3).fill({ userId: 'u-1' }))
  })
})

describe("ts-mcp-6: a low-level server's tools/list page with rejected promises in it", () => {
  // The wrapper's own read, which no client asked for: a tools/call alone starts it.
  it.each([
    ['a nextCursor getter', () => ({ tools: [{ name: 'echo', inputSchema: { type: 'object', properties: {} } }], get nextCursor() { return Promise.reject(new Error('lookup failed')) } })],
    ['a tools getter', () => ({ get tools() { return Promise.reject(new Error('lookup failed')) } })],
    ['a tools value', () => ({ tools: Promise.reject(new Error('lookup failed')) })],
  ])('fails the snapshot read, the call going on unchanged, with none unhandled: %s', async (_name, page) => {
    const watch = unhandledDuring()
    try {
      const lsquare = setup()
      const server = new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
      let listed = 0
      server.setRequestHandler(ListToolsRequestSchema, async () => (listed++, page() as never))
      server.setRequestHandler(CallToolRequestSchema, async () => text('ok'))
      instrument(server, lsquare.client, { extras: { sessions: true } })
      expect(await (await connect(1, server)).call('echo')).toEqual(text('ok'))
      expect(listed).toBe(1)
      await sleep(20)
      await lsquare.client.flush()
      expect(lsquare.calls()).toHaveLength(1)
    } finally {
      watch.stop()
    }
    expect(watch.seen).toEqual([])
  })
})

describe("ts-mcp-6: a low-level server's listed schema with a getter the listing's JSON never calls", () => {
  const lookup = () => Promise.reject(new Error('lookup failed'))
  // A getter a class defines: JSON leaves it out, as stdio and HTTP send the page.
  class Schema {
    type = 'object'
    get properties(): unknown {
      return lookup()
    }
  }
  /** A `$ref` that isn't enumerable: JSON leaves it out. */
  const hiddenRef = () => Object.defineProperty({ type: 'object', properties: { q: { type: 'string' } } }, '$ref', { get: lookup, enumerable: false })
  /** A list with a key that isn't an index: JSON gives only its items. */
  const keyedList = () => ({ type: 'object', properties: { q: { type: 'string' } }, required: Object.defineProperty(['q'], 'note', { get: lookup, enumerable: true }) })
  const cases = [
    ['a class getter for its properties', 'list', () => new Schema()],
    ['a class getter for its properties', 'call', () => new Schema()],
    ['a $ref that is not enumerable', 'list', hiddenRef],
    ['a list with a key that is not an index', 'list', keyedList],
  ] as const

  // The control: the bare server over the same transport leaves none unhandled, so the wrapper must not either.
  it.each(cases.flatMap(([name, first, schema]) => (['bare', 'instrumented'] as const).map((how) => [name, first, how, schema] as const)))(
    "leaves no rejection unhandled on the client's own tools/list: %s, after a %s (%s)",
    async (_name, first, how, schema) => {
      const watch = unhandledDuring()
      try {
        const lsquare = setup()
        const server = new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
        server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: 'echo', inputSchema: schema() as never }] }))
        server.setRequestHandler(CallToolRequestSchema, async () => text('ok'))
        if (how === 'instrumented') instrument(server, lsquare.client, { extras: { sessions: true } })
        const [a, b] = Memory1.createLinkedPair()
        // As stdio and HTTP send it: as JSON, which calls no getter but an object's own enumerable
        // ones. Handed across as it is, the client's own checks would read every getter.
        const send = a.send.bind(a)
        a.send = (message: any, options?: any) => send(JSON.parse(JSON.stringify(message)), options)
        await server.connect(a)
        const client = new Client1({ name: 'claude-code', version: '2.1' })
        await client.connect(b)
        if (first === 'call') expect((await client.callTool({ name: 'echo', arguments: {} })).content).toContainEqual({ type: 'text', text: 'ok' })
        const listed = await client.listTools()
        expect(listed.tools.map((tool) => tool.name)).toEqual(['echo'])
        await sleep(20)
        await lsquare.client.shutdown({ timeoutMs: 0 })
      } finally {
        watch.stop()
      }
      expect(watch.seen).toEqual([])
    },
  )
})

describe("ts-mcp-16: waits for a low-level server's snapshot", () => {
  // Each wait once reacted to a promise the instance kept until the list next changed: one a list
  // that fails, or keeps timing out, and never sends list_changed may never have. The waits run in
  // functions of their own, so nothing of the test's own keeps them.
  it('keep nothing once they end, reads failing on and on', async () => {
    const lists = newLists({ now: () => 0 })
    const reader: Reader = { handler: () => async () => Promise.reject(new Error('down')), callback: (run) => run() }
    // Its starting request was cancelled: a failed read pauses nothing, so every wait reads again.
    const context = { signal: AbortSignal.abort() }
    const waits = async (count: number) => {
      let woken = 0
      for (let i = 0; i < count; i++) if (await waitOnce(lists, reader, context, newWait())) woken++
      return woken
    }
    await waits(1)
    await sleep(10)
    const before = promises()
    expect(await waits(2_000)).toBe(2_000)
    await sleep(10)
    expect(promises() - before).toBeLessThan(100)
  })

  it('keep nothing once they end, reads timing out with the list unchanged', async () => {
    let now = 0
    const lists = newLists({ now: () => now })
    const reader: Reader = { handler: () => () => new Promise(() => {}), callback: (run) => run() }
    // Calls in the first second of a read that hangs: each waits until 1 s after it started. The
    // read is then given up at 10 s, by the first call to find it past then, and its pause lifted.
    const cycle = async () => {
      const waits = Array.from({ length: 1_000 }, () => waitOnce(lists, reader, {}, newWait()))
      now += 1_000
      await vi.advanceTimersByTimeAsync(1_000)
      const woken = (await Promise.all(waits)).filter(Boolean).length
      now += 9_000
      const after = await waitOnce(lists, reader, {}, newWait())
      const reading = lists.reading
      lists.pausedUntil = 0
      return { woken, after, reading }
    }
    await sleep(10)
    const before = promises()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    for (let round = 0; round < 2; round++) expect(await cycle()).toEqual({ woken: 0, after: false, reading: undefined })
    vi.useRealTimers()
    await sleep(10)
    expect(promises() - before).toBeLessThan(100)
  })

  it('still wake when the list changes meanwhile', async () => {
    const lists = newLists({ now: () => 0 })
    let calls = 0
    const reader: Reader = { handler: () => () => (calls++, new Promise(() => {})), callback: (run) => run() }
    const waiting = waitOnce(lists, reader, {}, newWait())
    mark(lists, true)
    expect(await waiting).toBe(true)
    expect(calls).toBe(1)
  })

  it('still wake when the handler says the list changed as the read starts', async () => {
    const lists = newLists({ now: () => 0 })
    let calls = 0
    // The handler sends list_changed the moment it's called: the wait that started the read is woken.
    const reader: Reader = { handler: () => () => (calls++, mark(lists, true), new Promise(() => {})), callback: (run) => run() }
    expect(await waitOnce(lists, reader, {}, newWait())).toBe(true)
    expect(calls).toBe(1)
  })
})

describe("ts-core-14, the wrapper's half: a polluted Object.prototype", () => {
  // Another dependency's prototype pollution: what identify returns, the IDs the wrapper works out
  // and instrument()'s options are read as their own fields or their class's, never Object.prototype's.
  const proto = Object.prototype as Record<string, unknown>
  const added = new Set<string>()
  /**
   * Runs `run` with Object.prototype holding `fields`, removed however it ends. Not enumerable
   * while calls run: with an enumerable one, even a bare SDK 1.x server never answers the call.
   * Reading a field by name, as the wrapper does, finds it all the same.
   */
  async function polluted<T>(fields: Record<string, unknown>, run: () => T | Promise<T>, enumerable = false): Promise<T> {
    for (const [key, value] of Object.entries(fields)) {
      added.add(key)
      Object.defineProperty(proto, key, { value, enumerable, writable: true, configurable: true })
    }
    try {
      return await run()
    } finally {
      for (const key of Object.keys(fields)) delete proto[key]
    }
  }
  // A test that timed out inside polluted() never got to its finally.
  afterEach(() => {
    for (const key of added) delete proto[key]
  })
  const IDS = { orgId: 'org_polluted', userId: 'u_polluted', conversationId: 'c_polluted' }

  for (const sdk of [1, 2] as const) {
    it(`can't spare a call whose identify threw the limited diagnostics, with a userId capture() never gave (SDK ${sdk}.x)`, async () => {
      const lsquare = setup()
      const server = instrument(high(sdk), lsquare.client, {
        extras: OFF,
        identify: () => {
          throw new Error('auth down')
        },
      })
      server.registerTool('t', {}, async () => text('rows'))
      const connection = await connect(sdk, server)
      await polluted(IDS, async () => {
        expect(await connection.call('t')).toEqual(text('rows'))
        await lsquare.client.flush({ timeoutMs: 2_000 })
      })
      const [event] = lsquare.calls()
      expect(event).toMatchObject({ identity: 'failed', toolName: 't' })
      for (const key of ['orgId', 'userId', 'conversationId', 'result', 'input']) expect(event).not.toHaveProperty(key)
      expect(lsquare.errors.map((error) => error.message)).toEqual(['identify threw; the call was sent as limited diagnostics.'])
    })

    it(`takes none of identify's fields from it, so it learns none either (SDK ${sdk}.x)`, async () => {
      const lsquare = setup()
      const server = instrument(high(sdk), lsquare.client, { extras: OFF, identify: (): Identity => ({ userId: 'u-1' }) })
      server.registerTool('t', {}, async () => text('rows'))
      const connection = await connect(sdk, server)
      await polluted({ ...IDS, userId: undefined, metadata: { plan: 'polluted' } }, async () => {
        await connection.call('t')
        await lsquare.client.flush({ timeoutMs: 2_000 })
      })
      const [event] = lsquare.calls()
      expect(event).toMatchObject({ userId: 'u-1' })
      for (const key of ['orgId', 'conversationId', 'metadata', 'identity']) expect(event).not.toHaveProperty(key)
      expect(lsquare.internal.identified.keys).toEqual(['userId'])
    })
  }

  it("can't put the turn a call links to in another org", async () => {
    const lsquare = setup()
    const server = instrument(high(1), lsquare.client, { extras: OFF, identify: (): Identity => ({ userId: 'u-1' }) })
    server.registerTool('t', {}, async () => {
      lsquare.client.capture({ conversationId: 'c-1', turnId: 't-1', question: 'late orders?' })
      return text('rows')
    })
    const connection = await connect(1, server)
    await polluted({ orgId: 'org_polluted' }, async () => {
      await connection.call('t')
      await lsquare.client.flush({ timeoutMs: 2_000 })
    })
    const [event] = lsquare.calls()
    expect(event).toMatchObject({ userId: 'u-1', conversationId: 'c-1', turnRef: turnKey(undefined, 'c-1', 't-1') })
    expect(event).not.toHaveProperty('orgId')
  })

  // Codex's second review: how the call ended is read from the result's own fields, as they go to
  // the client (`resultType`, `task`), and worked out into an object of the wrapper's own, whose
  // optional fields (`round`, `task`, `error`) a polluted prototype could fill too. Each on the
  // SDKs that still answer a call with it polluted: SDK 1.x takes a `task` for the client asking
  // for one, and SDK 2.x refuses an `input_required` result without its requests, and its zod
  // reads a polluted `error` as its own error map. (A polluted `isError` the SDKs' own checks copy
  // into the result, which then goes to the client as an error.)
  const ENDED = [
    ['round', 'input_required', [1, 2]],
    ['resultType', 'input_required', [1]],
    ['task', { id: 'task_polluted', taskId: 'task_polluted', status: 'working' }, [2]],
    ['error', { category: 'thrown', message: 'polluted' }, [1]],
  ] as const
  const ended = ENDED.flatMap(([key, value, sdks]) => sdks.map((sdk) => [key, sdk, value] as const))
  it.each(ended)("takes no %s from it for how a call ended, nor costs an ordinary tool its ID line (SDK %s.x)", async (key, sdk, value) => {
    const lsquare = setup()
    const server = instrument(high(sdk), lsquare.client, { extras: { intent: false, missingTools: false } })
    server.registerTool('t', {}, async () => text('rows'))
    const connection = await connect(sdk, server)
    const reply = (await polluted({ [key]: value }, async () => {
      const reply = await connection.call('t')
      await lsquare.client.flush({ timeoutMs: 2_000 })
      return reply
    })) as { content: Array<{ text: string }> }
    expect(reply.content.map((block) => block.text)).toEqual(['rows', expect.stringMatching(/^Task ID: ses_[A-Za-z0-9_-]{22}\. /)])
    const [event] = lsquare.calls()
    expect(event).toMatchObject({ status: 'completed', sessionSource: 'issued', result: { content: [{ type: 'text', text: 'rows' }] } })
    for (const field of ['round', 'task', 'error']) expect(event).not.toHaveProperty(field)
  })

  // Codex's third review, and the sweep after it: the task a result holds, an error the tool or
  // the handler throws, and every other field of what the app or an SDK hands over, read as it
  // goes to the client or as the SDK itself reads it, never from a polluted prototype alone.
  /** Calls `tool` with `fields` polluted, flushes, and gives back what the client got, or what it threw. */
  async function pollutedCall(lsquare: ReturnType<typeof setup>, connection: Awaited<ReturnType<typeof connect>>, fields: Record<string, unknown>, tool = 't', args: Record<string, unknown> = {}) {
    return polluted(fields, async () => {
      const reply = await connection.call(tool, args).catch((error: unknown) => ({ threw: error }))
      await lsquare.client.flush({ timeoutMs: 2_000 })
      return reply as { content: Array<{ type: string; text: string }>; isError?: boolean; threw?: { code?: unknown; message?: unknown } }
    })
  }
  const ID_LINE = expect.stringMatching(/^Task ID: ses_[A-Za-z0-9_-]{22}\. /)
  /** A low-level server whose tool `t` answers with `answer()`, listed with an object schema. */
  function low(sdk: 1 | 2, answer: () => unknown): any {
    const server: any = sdk === 1 ? new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } }) : new Server2({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
    const list = () => ({ tools: [{ name: 't', inputSchema: { type: 'object', properties: { q: { type: 'string' } } } }] })
    server.setRequestHandler(sdk === 1 ? ListToolsRequestSchema : 'tools/list', list)
    server.setRequestHandler(sdk === 1 ? CallToolRequestSchema : 'tools/call', async () => answer())
    return server
  }

  it.each([1, 2] as const)("takes a task's ID and status from the task itself, so an ordinary result with an empty one keeps its ID line (SDK %d.x)", async (sdk) => {
    const lsquare = setup()
    const server = instrument(high(sdk), lsquare.client, { extras: { intent: false, missingTools: false } })
    server.registerTool('t', {}, async () => ({ ...text('rows'), task: {} }))
    const reply = await pollutedCall(lsquare, await connect(sdk, server), { taskId: 'task_polluted', status: 'working' })
    expect(reply.content.map((block) => block.text)).toEqual(['rows', ID_LINE])
    const [event] = lsquare.calls()
    expect(event).toMatchObject({ status: 'completed', sessionSource: 'issued', result: { content: [{ type: 'text', text: 'rows' }] } })
    expect(event).not.toHaveProperty('task')
  })

  it.each([1, 2] as const)('records the message the client got of a {} the tool throws, as a string, and no type (SDK %d.x)', async (sdk) => {
    const lsquare = setup()
    const server = instrument(high(sdk), lsquare.client, { extras: OFF })
    server.registerTool('t', {}, async () => {
      throw {}
    })
    const reply = await pollutedCall(lsquare, await connect(sdk, server), { message: 'polluted', name: 'PollutedError' })
    expect(reply).toEqual({ content: [{ type: 'text', text: '[object Object]' }], isError: true })
    expect(lsquare.calls()[0]?.error).toEqual({ message: '[object Object]', category: 'thrown' })
  })

  it.each([1, 2] as const)("still records an error class's own message and type, Error.prototype's included (SDK %d.x)", async (sdk) => {
    class LookupError extends Error {}
    LookupError.prototype.name = 'LookupError'
    const lsquare = setup()
    const server = instrument(high(sdk), lsquare.client, { extras: OFF })
    const errors: Record<string, Error> = { typed: new TypeError('lookup failed'), classed: new LookupError('no rows'), empty: new RangeError() }
    for (const [name, error] of Object.entries(errors)) {
      server.registerTool(name, {}, async () => {
        throw error
      })
    }
    const connection = await connect(sdk, server)
    const replies = []
    for (const name of Object.keys(errors)) replies.push(await pollutedCall(lsquare, connection, { message: 'polluted', name: 'PollutedError' }, name))
    expect(replies.map((reply) => reply.content[0]?.text)).toEqual(['lookup failed', 'no rows', ''])
    expect(lsquare.calls().map((event) => event.error)).toEqual([
      { message: 'lookup failed', type: 'TypeError', category: 'thrown' },
      { message: 'no rows', type: 'LookupError', category: 'thrown' },
      { message: '', type: 'RangeError', category: 'thrown' },
    ])
  })

  it.each([1, 2] as const)('takes no type from it for an error a low-level handler throws, nor from a real class, its message the one the client got (SDK %d.x)', async (sdk) => {
    class LookupError extends Error {}
    LookupError.prototype.name = 'LookupError'
    const thrown: unknown[] = [{ message: 'boom' }, new LookupError('no rows')]
    const lsquare = setup()
    const server = low(sdk, () => {
      throw thrown.shift()
    })
    instrument(server, lsquare.client)
    const connection = await connect(sdk, server)
    const replies = [await pollutedCall(lsquare, connection, { name: 'PollutedError' }), await pollutedCall(lsquare, connection, { name: 'PollutedError' })]
    expect(replies.map((reply) => String(reply.threw?.message))).toEqual([expect.stringContaining('boom'), expect.stringContaining('no rows')])
    expect(lsquare.calls().map((event) => event.error)).toEqual([
      { message: 'boom', category: 'thrown' },
      { message: 'no rows', type: 'LookupError', category: 'thrown' },
    ])
  })

  it.each([1, 2] as const)("records an escaping error's code and message as the SDK's JSON-RPC reply reads them, inherited ones too (SDK %d.x)", async (sdk) => {
    // Both SDKs answer with `error.code` and `error.message`, read through the prototype: the
    // event says what the client got, polluted or not.
    const lsquare = setup()
    const server = low(sdk, () => {
      throw {}
    })
    instrument(server, lsquare.client)
    const reply = await pollutedCall(lsquare, await connect(sdk, server), { message: 'polluted', code: -32001 })
    expect(reply.threw).toMatchObject({ code: -32001, message: expect.stringContaining('polluted') })
    expect(lsquare.calls()[0]?.error).toEqual({ message: 'polluted', code: -32001, category: 'protocol' })
  })

  // A stdio transport's streams, an SDK 2.x channel's `_wire` and SDK 1.x's tool definition's
  // `executor`: none of the call's objects has them itself. Listed first, unpolluted. (A zod
  // object's `catchall` is no longer one: see Codex's fourth review, below.)
  const LINE_KEPT = [
    ['_stdin and _stdout', { _stdin: {}, _stdout: {} }, [1, 2]],
    ['_wire', { _wire: { _stdin: {}, _stdout: {} } }, [1, 2]],
    ['executor', { executor: function polluted() {} }, [1]],
  ] as const
  const kept = LINE_KEPT.flatMap(([name, fields, sdks]) => sdks.map((sdk) => [name, sdk, fields] as const))
  it.each(kept)('takes no %s from it, so an ordinary tool keeps its ID line, and the extras their inputs (SDK %s.x)', async (_name, sdk, fields) => {
    const lsquare = setup()
    const server = instrument(high(sdk), lsquare.client, { extras: { missingTools: false } })
    server.registerTool('t', { inputSchema: { q: z.string().optional() } }, async () => text('rows'))
    const connection = await connect(sdk, server)
    expect(Object.keys((await connection.tools())[0]!.inputSchema.properties ?? {}).sort()).toEqual(['intent', 'q', 'session_id'])
    const reply = await pollutedCall(lsquare, connection, fields, 't', { q: 'late', intent: 'find late orders' })
    expect(reply.content.map((block) => block.text)).toEqual(['rows', ID_LINE])
    expect(lsquare.calls()[0]).toMatchObject({ sessionSource: 'issued', intent: 'find late orders', input: { q: 'late' } })
  })

  it("takes no executor from it on SDK 1.x, so a tool's own throw is still the tool's", async () => {
    const lsquare = setup()
    const server = instrument(high(1), lsquare.client, { extras: OFF })
    server.registerTool('t', {}, async () => {
      throw new TypeError('lookup failed')
    })
    await pollutedCall(lsquare, await connect(1, server), { executor: function polluted() {} })
    expect(lsquare.calls()[0]?.error).toEqual({ message: 'lookup failed', type: 'TypeError', category: 'thrown' })
  })

  // What the request's context says of the call: SDK 1.x's has no mcpReq, http, inputResponses or
  // requestState, and SDK 2.x's no signal or requestInfo of its own (each where the SDK itself
  // doesn't copy the polluted one into the context it hands over). SDK 1.x does copy a polluted
  // requestInfo into its context from an in-memory transport, which passes none: the very object
  // Object.prototype holds, which the wrapper takes for no request's (the check of the sweep).
  const CONTEXT = [
    ['inputResponses', { inputResponses: {} }, [1]],
    ['requestState', { requestState: 'state' }, [1]],
    ['signal', { signal: { aborted: true } }, [2]],
    ['mcpReq', { mcpReq: { envelope: { 'io.modelcontextprotocol/clientInfo': { name: 'other' }, 'io.modelcontextprotocol/protocolVersion': '2099-01-01' } } }, [1]],
    ['http', { http: { req: { headers: { 'mcp-protocol-version': '2099-01-01' } } } }, [1]],
    ['requestInfo', { requestInfo: { headers: { 'mcp-protocol-version': '2099-01-01' } } }, [1, 2]],
  ] as const
  const context = CONTEXT.flatMap(([name, fields, sdks]) => sdks.map((sdk) => [name, sdk, fields] as const))
  it.each(context)("takes no %s from it for the call's round, outcome, client or protocol version (SDK %s.x)", async (_name, sdk, fields) => {
    const lsquare = setup()
    const server = instrument(high(sdk), lsquare.client, { extras: OFF })
    server.registerTool('t', {}, async () => text('rows'))
    expect(await pollutedCall(lsquare, await connect(sdk, server), fields)).toEqual(text('rows'))
    const [event] = lsquare.calls()
    expect(event).toMatchObject({ status: 'completed', client: { name: 'claude-code', version: '2.1' }, protocolVersion: '2025-11-25', result: { content: [{ type: 'text', text: 'rows' }] } })
    for (const field of ['round', 'error']) expect(event).not.toHaveProperty(field)
  })

  it.each([
    ['high', 1],
    ['high', 2],
    ['low', 1],
    ['low', 2],
  ] as const)('takes no nextCursor from it for a %s-level listing, which keeps get_more_tools on its last page (SDK %s.x)', async (level, sdk) => {
    const lsquare = setup()
    let server: any
    if (level === 'high') {
      server = instrument(high(sdk), lsquare.client)
      server.registerTool('t', {}, async () => text('rows'))
    } else {
      server = instrument(low(sdk, () => text('rows')), lsquare.client, { extras: { sessions: true, intent: true, missingTools: true } })
    }
    const connection = await connect(sdk, server)
    const tools = await polluted({ nextCursor: 'more' }, () => connection.tools())
    expect(tools.map((tool) => tool.name)).toEqual(['t', 'get_more_tools'])
  })

  it('takes no environment variable from it to warn of a serverless host, while one of its own still warns', async () => {
    const warned = async () => {
      const lines: string[] = []
      const lsquare = setup({ logger: { info() {}, warn: (line) => void lines.push(line) } })
      instrument(high(1), lsquare.client)
      // Logged on the client's own turn: flush() resolves once the line has reached the logger.
      await lsquare.client.flush({ timeoutMs: 0 })
      return lines.filter((line) => line.includes('stop work once a reply is sent'))
    }
    // process.env inherits from Object.prototype, as the global object does.
    expect(await polluted({ AWS_LAMBDA_FUNCTION_NAME: 'orders', K_SERVICE: 'orders', VERCEL: '1' }, warned)).toEqual([])
    process.env.VERCEL = '1'
    try {
      expect(await warned()).toEqual([expect.stringContaining('This looks like Vercel')])
    } finally {
      delete process.env.VERCEL
    }
  })

  it("still takes identify's fields from its own prototypes: a class's getters, Object.create()", async () => {
    class Who {
      get orgId() {
        return 'org_1'
      }
      get userId() {
        return 'u-1'
      }
    }
    const answers: unknown[] = [new Who(), Object.create({ userId: 'u-2', metadata: { plan: 'pro' } })]
    const lsquare = setup()
    const server = instrument(high(1), lsquare.client, { extras: OFF, identify: () => answers.shift() as Identity })
    server.registerTool('t', {}, async () => text('rows'))
    const connection = await connect(1, server)
    await connection.call('t')
    await connection.call('t')
    await lsquare.client.flush({ timeoutMs: 2_000 })
    expect(lsquare.calls()).toMatchObject([{ orgId: 'org_1', userId: 'u-1' }, { userId: 'u-2', metadata: { plan: 'pro' } }])
    expect(lsquare.errors).toEqual([])
  })

  it('reads no option of instrument(), nor an extra, from it', async () => {
    const lsquare = setup()
    const server = high(1)
    server.registerTool('t', { inputSchema: { q: z.string().optional() } }, async () => text('rows'))
    // Read once, as instrument() runs: enumerable, as pollution usually is.
    await polluted(
      { identify: () => ({ userId: 'u_polluted' }), inputs: false, sessions: false, intent: false, missingTools: false },
      () => instrument(server, lsquare.client, { extras: {} }),
      true,
    )
    const connection = await connect(1, server)
    const tools = await connection.tools()
    expect(tools.map((tool) => [tool.name, Object.keys(tool.inputSchema.properties ?? {}).sort()])).toEqual([
      ['t', ['intent', 'q', 'session_id']],
      ['get_more_tools', expect.any(Array)],
    ])
    await connection.call('t', { q: 'late' })
    await lsquare.client.flush({ timeoutMs: 2_000 })
    const [event] = lsquare.calls()
    expect(event).toMatchObject({ input: { q: 'late' } })
    expect(event).not.toHaveProperty('userId')
  })

  it('still reads options and extras from their own prototypes: a class, Object.create()', async () => {
    class Options {
      get identify() {
        return (): Identity => ({ userId: 'u-1' })
      }
      get extras() {
        return Object.create({ sessions: false, intent: false, missingTools: false }) as InstrumentOptions['extras']
      }
    }
    const lsquare = setup()
    const server = instrument(high(1), lsquare.client, new Options() as InstrumentOptions)
    server.registerTool('t', { inputSchema: { q: z.string().optional() } }, async () => text('rows'))
    const connection = await connect(1, server)
    expect((await connection.tools()).map((tool) => [tool.name, Object.keys(tool.inputSchema.properties ?? {})])).toEqual([['t', ['q']]])
    expect(await connection.call('t', { q: 'late' })).toEqual(text('rows'))
    await lsquare.client.flush({ timeoutMs: 2_000 })
    expect(lsquare.calls()).toMatchObject([{ userId: 'u-1' }])
  })

  // The check of that sweep, on real transports, where an in-memory one hid them: stdio and
  // Streamable HTTP run both SDKs' JSON-RPC parse on each message, which copies a polluted `_meta`
  // into a request whose client sent none, as its own; and neither SDK's stdio transport (nor
  // serveStdio()'s channel), nor SDK 1.x's stateless HTTP one, has a `sessionId` of its own, which
  // both SDKs copy into the context. Each test with a client that sends what a real one does.
  const CLIENT_INFO = 'io.modelcontextprotocol/clientInfo'
  const VERSION = 'io.modelcontextprotocol/protocolVersion'
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
  type Reply = { result?: { content: Array<{ text: string }> }; error?: unknown }
  const texts = (reply: Reply) => reply.result?.content.map((block) => block.text)

  /** A stdio connection over two streams, as a stdio client drives one: line-delimited JSON-RPC. */
  function stdioWire() {
    const stdin = new PassThrough()
    const stdout = new PassThrough()
    const replies = new Map<number, Reply>()
    let buffered = ''
    stdout.on('data', (chunk) => {
      buffered += String(chunk)
      let end: number
      while ((end = buffered.indexOf('\n')) >= 0) {
        const line = buffered.slice(0, end)
        buffered = buffered.slice(end + 1)
        const message = line.trim() ? JSON.parse(line) : {}
        if (typeof message.id === 'number') replies.set(message.id, message)
      }
    })
    const send = (message: unknown) => stdin.write(`${JSON.stringify(message)}\n`)
    const request = async (id: number, method: string, params?: unknown) => {
      send({ jsonrpc: '2.0', id, method, ...(params === undefined ? {} : { params }) })
      for (let i = 0; i < 400 && !replies.has(id); i++) await sleep(5)
      expect(replies.has(id)).toBe(true)
      return replies.get(id)!
    }
    return { stdin, stdout, request, send }
  }

  /**
   * `make`'s server, over a real stdio transport of SDK 1.x or 2.x, or behind serveStdio(), its
   * connection open. `sessionId` is set on the transport, as neither SDK does.
   */
  async function overStdio(kind: 'SDK 1.x' | 'SDK 2.x' | 'serveStdio()', make: (server: any) => any, sessionId?: string) {
    const wire = stdioWire()
    const transport: any = kind === 'SDK 1.x' ? new Stdio1(wire.stdin, wire.stdout) : new Stdio2(wire.stdin, wire.stdout)
    if (sessionId !== undefined) transport.sessionId = sessionId
    if (kind === 'serveStdio()') serveStdio(() => make(new McpServer2({ name: 'orders', version: '1.0.0' })), { transport })
    else await make(high(kind === 'SDK 1.x' ? 1 : 2)).connect(transport)
    await wire.request(0, 'initialize', { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'claude-code', version: '2.1' } })
    wire.send({ jsonrpc: '2.0', method: 'notifications/initialized' })
    let id = 1
    return { call: (params: unknown) => wire.request(id++, 'tools/call', params) }
  }

  /**
   * `server` over a real Streamable HTTP transport of SDK `sdk`'s: a stateful one, which issues the
   * session `issued-by-server` to a client that has sent initialize, or a stateless one, which a
   * client calls straight away. Each request declares 2025-11-25 in its header, as a client does.
   */
  async function overHttp(sdk: 1 | 2, server: any, stateful: boolean) {
    const options = stateful ? { sessionIdGenerator: () => 'issued-by-server', enableJsonResponse: true } : { enableJsonResponse: true }
    const transport: any = sdk === 1 ? new Http1(options) : new Http2(options)
    await server.connect(transport)
    const post = (body: unknown, session: boolean): Promise<Response> =>
      transport.handleRequest(
        new Request('http://localhost/mcp', {
          method: 'POST',
          headers: { 'content-type': 'application/json', accept: 'application/json, text/event-stream', 'mcp-protocol-version': '2025-11-25', ...(session ? { 'mcp-session-id': 'issued-by-server' } : {}) },
          body: JSON.stringify(body),
        }),
      )
    if (stateful) {
      await post({ jsonrpc: '2.0', id: 0, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'cursor', version: '1.7' } } }, false)
      await post({ jsonrpc: '2.0', method: 'notifications/initialized' }, true)
    }
    return { call: async (params: unknown) => (await (await post({ jsonrpc: '2.0', id: 1, method: 'tools/call', params }, stateful)).json()) as Reply }
  }

  /** A high-level server with the sessions extra on, and the tool `t`, with an optional `q`. */
  const sessionsOn = (lsquare: ReturnType<typeof setup>) => (server: any) => {
    instrument(server, lsquare.client, { extras: { intent: false, missingTools: false } })
    server.registerTool('t', { inputSchema: { q: z.string().optional() } }, async () => text('rows'))
    return server
  }
  /** Runs `run` with `fields` polluted, then flushes, and gives back what `run` did. */
  const pollutedFlushed = <T>(lsquare: ReturnType<typeof setup>, fields: Record<string, unknown>, run: () => Promise<T>) =>
    polluted(fields, async () => {
      const done = await run()
      await lsquare.client.flush({ timeoutMs: 2_000 })
      return done
    })
  const STDIO = ['SDK 1.x', 'SDK 2.x', 'serveStdio()'] as const

  it.each(STDIO)('takes no sessionId from it on stdio (%s), whose transport has none: the connection keeps the ID made for it', async (kind) => {
    const lsquare = setup()
    const connection = await overStdio(kind, sessionsOn(lsquare))
    const replies = await pollutedFlushed(lsquare, { sessionId: 'sess_polluted_0123456789' }, async () => [await connection.call({ name: 't', arguments: {} }), await connection.call({ name: 't', arguments: {} })])
    expect(replies.map(texts)).toEqual([['rows'], ['rows']])
    const [one, two] = lsquare.calls()
    expect(one).toMatchObject({ sessionSource: 'protocol', sessionId: expect.stringMatching(/^[0-9a-f]{64}$/) })
    expect(two!.sessionId).toBe(one!.sessionId)
  })

  it.each(['SDK 1.x', 'SDK 2.x'] as const)("never reads the context's sessionId on stdio (%s): one set on the transport isn't the connection's either", async (kind) => {
    // So a stdio connection's ID never depends on what the context says, polluted or not.
    const lsquare = setup()
    const connection = await overStdio(kind, sessionsOn(lsquare), 'set-on-the-transport')
    await connection.call({ name: 't', arguments: {} })
    await lsquare.client.flush({ timeoutMs: 2_000 })
    expect(lsquare.calls()[0]).toMatchObject({ sessionSource: 'protocol', sessionId: expect.stringMatching(/^[0-9a-f]{64}$/) })
  })

  it.each([1, 2] as const)("takes no sessionId from it over stateless HTTP, SDK 1.x's transport having none, so the call keeps its ID line, while a stateful one's own still groups it (SDK %d.x)", async (sdk) => {
    const lsquare = setup()
    const stateless = await overHttp(sdk, sessionsOn(lsquare)(high(sdk)), false)
    const stateful = await overHttp(sdk, sessionsOn(lsquare)(high(sdk)), true)
    const call = { name: 't', arguments: {} }
    const replies = await pollutedFlushed(lsquare, { sessionId: 'sess_polluted_0123456789' }, async () => [await stateless.call(call), await stateful.call(call)])
    expect(replies.map(texts)).toEqual([['rows', ID_LINE], ['rows']])
    expect(lsquare.calls()).toMatchObject([{ sessionSource: 'issued' }, { sessionId: hashed('issued-by-server'), sessionSource: 'protocol' }])
  })

  it.each(STDIO)("takes no client or protocol version from a _meta of its on stdio (%s): initialize's are the call's, and the connection keeps its ID", async (kind) => {
    const lsquare = setup()
    const connection = await overStdio(kind, sessionsOn(lsquare))
    const meta = { _meta: { [CLIENT_INFO]: { name: 'other', version: '6' }, [VERSION]: '2026-07-28' } }
    // The SDKs' parse also copies it into their reply: its texts are the tool's.
    const reply = await pollutedFlushed(lsquare, meta, () => connection.call({ name: 't', arguments: {} }))
    expect(texts(reply)).toEqual(['rows'])
    expect(lsquare.calls()[0]).toMatchObject({ client: { name: 'claude-code', version: '2.1' }, protocolVersion: '2025-11-25', sessionSource: 'protocol', sessionId: expect.stringMatching(/^[0-9a-f]{64}$/) })
  })

  it.each([1, 2] as const)("takes no protocol version or client from a _meta of its over stateful HTTP: the header's and initialize's are the call's, which keeps the server's session (SDK %d.x)", async (sdk) => {
    const lsquare = setup()
    const connection = await overHttp(sdk, sessionsOn(lsquare)(high(sdk)), true)
    const meta = { _meta: { [CLIENT_INFO]: { name: 'other', version: '6' }, [VERSION]: '2026-07-28' } }
    const reply = await pollutedFlushed(lsquare, meta, () => connection.call({ name: 't', arguments: {} }))
    expect(texts(reply)).toEqual(['rows'])
    expect(lsquare.calls()[0]).toMatchObject({ client: { name: 'cursor', version: '1.7' }, protocolVersion: '2025-11-25', sessionId: hashed('issued-by-server'), sessionSource: 'protocol' })
  })

  it.each([1, 2] as const)('takes no client or protocol version from a _meta of its in memory either, where SDK 2.x lifts it into its envelope (SDK %d.x)', async (sdk) => {
    const lsquare = setup()
    const server = instrument(high(sdk), lsquare.client, { extras: OFF })
    server.registerTool('t', {}, async () => text('rows'))
    const reply = await pollutedCall(lsquare, await connect(sdk, server), { _meta: { [CLIENT_INFO]: { name: 'other', version: '6' }, [VERSION]: '2099-01-01' } })
    expect(reply.content.map((block) => block.text)).toEqual(['rows'])
    expect(lsquare.calls()[0]).toMatchObject({ client: { name: 'claude-code', version: '2.1' }, protocolVersion: '2025-11-25' })
  })

  it.each(['SDK 1.x', 'SDK 2.x'] as const)('still reads a _meta the client sent itself, though it names the very version polluted (%s)', async (kind) => {
    // A copy of the polluted one holds its values and nothing else; the client's holds its own.
    const lsquare = setup()
    const connection = await overStdio(kind, sessionsOn(lsquare))
    const sent = { [VERSION]: '2026-07-28', [CLIENT_INFO]: { name: 'own-client', version: '3' }, 'io.modelcontextprotocol/clientCapabilities': {} }
    const reply = await pollutedFlushed(lsquare, { _meta: { [VERSION]: '2026-07-28' } }, () => connection.call({ name: 't', arguments: {}, _meta: sent }))
    // 2026-07-28 has no sessions: the call gets an ID.
    expect(texts(reply)).toEqual(['rows', ID_LINE])
    expect(lsquare.calls()[0]).toMatchObject({ client: { name: 'own-client', version: '3' }, protocolVersion: '2026-07-28', sessionSource: 'issued' })
  })

  it.each([1, 2] as const)("takes no arguments from it for a call that sends none: no input, nor a session_id the agent never sent, while the client's own are recorded (SDK %d.x)", async (sdk) => {
    // The SDK's own zod hands the tool, with its input schema, the polluted `q`: the event's
    // input is the client's.
    const lsquare = setup()
    const connection = await overStdio(sdk === 1 ? 'SDK 1.x' : 'SDK 2.x', sessionsOn(lsquare))
    const sent = `ses_${'A'.repeat(22)}`
    const replies = await pollutedFlushed(lsquare, { arguments: { q: 'polluted', session_id: `ses_${'P'.repeat(22)}` } }, async () => [
      await connection.call({ name: 't' }),
      await connection.call({ name: 't', arguments: { q: 'own', session_id: sent } }),
    ])
    expect(replies.map(texts)).toEqual([['rows'], ['rows']])
    const [none, theirs] = lsquare.calls()
    expect(none!.input).toEqual({})
    expect(none).toMatchObject({ sessionSource: 'protocol', sessionId: expect.stringMatching(/^[0-9a-f]{64}$/) })
    expect(theirs).toMatchObject({ input: { q: 'own' }, sessionId: sent, sessionSource: 'sent' })
  })

  // Codex's fourth review. zod 4 reads an object's `catchall` and `checks` through the prototype
  // as it builds the schema, and runs what it found: a schema built with them polluted is the
  // tool's to read any input with, as one the wrapper can't read is.
  const SENT = { q: 'late', intent: 'find late orders', session_id: `ses_${'A'.repeat(22)}` }
  it.each(['SDK 1.x', 'SDK 2.x'] as const)("leaves a tool whose schema zod built with a polluted catchall the inputs zod keeps for it, as it would get them bare (%s)", async (kind) => {
    // As below: SDK 2.x's wire schemas made first, so each test sees the same SDK.
    preloadSchemas()
    const lines: string[] = []
    const lsquare = setup({ logger: { info() {}, warn: (line) => void lines.push(line) } })
    const got: Record<string, string[]> = {}
    const make = (as: 'bare' | 'instrumented') => (server: any) => {
      if (as === 'instrumented') instrument(server, lsquare.client, { extras: { missingTools: false } })
      server.registerTool('t', { inputSchema: z.object({ q: z.string() }) }, async (args: Record<string, unknown>) => ((got[as] = Object.keys(args).sort()), text('rows')))
      return server
    }
    const replies = await pollutedFlushed(lsquare, { catchall: z.any() }, async () => {
      const bare = await overStdio(kind, make('bare'))
      const instrumented = await overStdio(kind, make('instrumented'))
      return [await bare.call({ name: 't', arguments: SENT }), await instrumented.call({ name: 't', arguments: SENT })]
    })
    expect(got).toEqual({ bare: ['intent', 'q', 'session_id'], instrumented: ['intent', 'q', 'session_id'] })
    // Its arguments as sent, without the extras: no ID line, no intent of the wrapper's.
    expect(replies.map(texts)).toEqual([['rows'], ['rows']])
    const [event] = lsquare.calls()
    expect(event).toMatchObject({ input: SENT, sessionSource: 'protocol' })
    expect(event).not.toHaveProperty('intent')
    expect(lines).toEqual(['[lsquare] Tool "t" gets no session_id or intent: its input schema can\'t be read safely (its catchall comes from a polluted Object.prototype, which zod may run), so any input could be its own.'])
  })

  it.each(['SDK 1.x', 'SDK 2.x'] as const)("leaves a strict tool whose schema zod built with polluted checks the inputs it refuses, which its checks then read, as it would bare (%s)", async (kind) => {
    const lsquare = setup()
    // SDK 2.x builds its wire schemas on its first message: built with the polluted checks too,
    // they would break the SDK itself, bare or not. As an app on Workers does, made first.
    preloadSchemas()
    // A check zod runs on every schema built while it is polluted: here, what it sees refused.
    const refused: Record<string, string[]> = {}
    let as: 'bare' | 'instrumented' = 'bare'
    const check = { _zod: { onattach: [], def: { check: 'custom' }, check: (payload: { issues: Array<{ code: string; keys?: string[] }> }) => {
      for (const issue of payload.issues) if (issue.code === 'unrecognized_keys') refused[as] = [...issue.keys!].sort()
    } } }
    const make = (instrumented: boolean) => (server: any) => {
      if (instrumented) instrument(server, lsquare.client, { extras: { missingTools: false } })
      server.registerTool('t', { inputSchema: z.strictObject({ q: z.string() }) }, async () => text('rows'))
      return server
    }
    const replies = await pollutedFlushed(lsquare, { checks: [check] }, async () => {
      const bare = await overStdio(kind, make(false))
      const instrumented = await overStdio(kind, make(true))
      const first = await bare.call({ name: 't', arguments: SENT })
      as = 'instrumented'
      return [first, await instrumented.call({ name: 't', arguments: SENT })]
    })
    expect(refused).toEqual({ bare: ['intent', 'session_id'], instrumented: ['intent', 'session_id'] })
    expect(replies[1]).toEqual(replies[0])
    expect(JSON.stringify(replies[0])).toMatch(/intent/)
    expect(lsquare.calls()[0]).toMatchObject({ input: SENT, status: 'error', sessionSource: 'protocol' })
  })

  it.each([1, 2] as const)("can't tell a catchall polluted after the schema was built from one zod runs: the tool keeps its arguments as sent, without the extras, while it lasts (SDK %d.x)", async (sdk) => {
    // zod runs only one it found as it built the schema: this one it doesn't, which the wrapper
    // can't see. Listed first, unpolluted, with the extras.
    const lsquare = setup()
    const server = instrument(high(sdk), lsquare.client, { extras: { missingTools: false } })
    let got: string[] | undefined
    server.registerTool('t', { inputSchema: { q: z.string().optional() } }, async (args: Record<string, unknown>) => ((got = Object.keys(args)), text('rows')))
    const connection = await connect(sdk, server)
    expect(Object.keys((await connection.tools())[0]!.inputSchema.properties ?? {}).sort()).toEqual(['intent', 'q', 'session_id'])
    const reply = await pollutedCall(lsquare, connection, { catchall: z.unknown() }, 't', { q: 'late', intent: 'find late orders' })
    expect(got).toEqual(['q'])
    expect(reply.content.map((block) => block.text)).toEqual(['rows'])
    expect(lsquare.calls()[0]).toMatchObject({ input: { q: 'late', intent: 'find late orders' } })
    expect(lsquare.calls()[0]).not.toHaveProperty('intent')
  })

  it.each(['SDK 1.x', 'SDK 2.x'] as const)("still reads a _meta of strings alone the client sent, though each is the polluted one's: a string can't show it was copied (%s)", async (kind) => {
    const lsquare = setup()
    const connection = await overStdio(kind, sessionsOn(lsquare))
    const reply = await pollutedFlushed(lsquare, { _meta: { [VERSION]: '2026-07-28' } }, () => connection.call({ name: 't', arguments: {}, _meta: { [VERSION]: '2026-07-28' } }))
    // 2026-07-28 has no sessions: the call gets an ID.
    expect(texts(reply)).toEqual(['rows', ID_LINE])
    expect(lsquare.calls()[0]).toMatchObject({ protocolVersion: '2026-07-28', sessionSource: 'issued' })
  })

  it.each(['SDK 1.x', 'SDK 2.x'] as const)("can't tell a polluted _meta of strings alone from one the client sent: its version is the call's (%s)", async (kind) => {
    // The other side of the test above, a limitation: the SDKs' parse copies the polluted one
    // into a request whose client sent none, and no string of it shows that.
    const lsquare = setup()
    const connection = await overStdio(kind, sessionsOn(lsquare))
    const reply = await pollutedFlushed(lsquare, { _meta: { [VERSION]: '2026-07-28' } }, () => connection.call({ name: 't', arguments: {} }))
    expect(texts(reply)).toEqual(['rows', ID_LINE])
    expect(lsquare.calls()[0]).toMatchObject({ protocolVersion: '2026-07-28', sessionSource: 'issued' })
  })

  it.each([1, 2] as const)("keeps a stateful HTTP transport's own session, though a polluted sessionId names it too: the transport declares it, as a stateless one doesn't (SDK %d.x)", async (sdk) => {
    const lsquare = setup()
    const stateful = await overHttp(sdk, sessionsOn(lsquare)(high(sdk)), true)
    const stateless = await overHttp(sdk, sessionsOn(lsquare)(high(sdk)), false)
    const call = { name: 't', arguments: {} }
    const replies = await pollutedFlushed(lsquare, { sessionId: 'issued-by-server' }, async () => [await stateful.call(call), await stateless.call(call)])
    expect(replies.map(texts)).toEqual([['rows'], ['rows', ID_LINE]])
    expect(lsquare.calls()).toMatchObject([{ sessionId: hashed('issued-by-server'), sessionSource: 'protocol' }, { sessionSource: 'issued' }])
  })

  it.each([1, 2] as const)("takes that evidence from the transport the call came on, read before a low-level server's snapshot wait, though the server moves to another meanwhile (SDK %d.x)", async (sdk) => {
    const lsquare = setup()
    let release!: () => void
    const blocked = new Promise<void>((resolve) => (release = resolve))
    let listed = false
    const server: any = sdk === 1 ? new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } }) : new Server2({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
    server.setRequestHandler(sdk === 1 ? ListToolsRequestSchema : 'tools/list', async () => {
      listed = true
      await blocked
      return { tools: [{ name: 't', inputSchema: { type: 'object', properties: {} } }] }
    })
    server.setRequestHandler(sdk === 1 ? CallToolRequestSchema : 'tools/call', async () => text('rows'))
    instrument(server, lsquare.client, { extras: { sessions: true, intent: false, missingTools: false } })
    const connection = await overHttp(sdk, server, true)
    await polluted({ sessionId: 'issued-by-server' }, async () => {
      void connection.call({ name: 't', arguments: {} }).catch(() => undefined)
      for (let i = 0; i < 400 && !listed; i++) await sleep(5)
      expect(listed).toBe(true)
      await server.close()
      await server.connect(sdk === 1 ? new Http1({ sessionIdGenerator: () => 'another' }) : new Http2({ sessionIdGenerator: () => 'another' }))
      release()
      for (let i = 0; i < 400 && lsquare.calls().length === 0; i++) await lsquare.client.flush({ timeoutMs: 50 }).then(() => sleep(5))
    })
    expect(lsquare.calls()).toMatchObject([{ sessionId: hashed('issued-by-server'), sessionSource: 'protocol' }])
  })

  // Codex's fifth review. zod copies the checks it finds as it builds a schema into the function it
  // runs the schema with, `_zod.run`: a strict schema built while `checks` was polluted runs them
  // still once the pollution is gone, though its def no longer shows them. Its run isn't its parse.
  /** A check zod runs on a schema built while it is polluted: what it saw refused, and kept, by `as`. */
  function recorder() {
    const seen: Record<string, { refused: string[]; kept: string[] }> = {}
    let as = 'bare'
    const check = {
      _zod: {
        onattach: [],
        def: { check: 'custom' },
        check: (payload: { value: unknown; issues: Array<{ code: string; keys?: string[] }> }) => {
          const refused = payload.issues.flatMap((issue) => (issue.code === 'unrecognized_keys' ? issue.keys! : []))
          seen[as] = { refused: [...refused].sort(), kept: Object.keys(payload.value as object).sort() }
        },
      },
    }
    return { seen, check, now: (name: string) => void (as = name) }
  }
  const unreadableLine = (why: string) => `[lsquare] Tool "t" gets no session_id or intent: its input schema can't be read safely (${why}), so any input could be its own.`

  it.each(['SDK 1.x', 'SDK 2.x'] as const)('leaves a strict tool whose schema zod built with polluted checks, gone since, the inputs it refuses, which those checks still read, as it would bare (%s)', async (kind) => {
    preloadSchemas()
    const lines: string[] = []
    const lsquare = setup({ logger: { info() {}, warn: (line) => void lines.push(line) } })
    const { seen, check, now } = recorder()
    const schema = await polluted({ checks: [check] }, () => z.strictObject({ q: z.string() }))
    const make = (instrumented: boolean) => (server: any) => {
      if (instrumented) instrument(server, lsquare.client, { extras: { missingTools: false } })
      server.registerTool('t', { inputSchema: schema }, async () => text('rows'))
      return server
    }
    const bare = await overStdio(kind, make(false))
    const instrumented = await overStdio(kind, make(true))
    const replies = [await bare.call({ name: 't', arguments: SENT })]
    now('instrumented')
    replies.push(await instrumented.call({ name: 't', arguments: SENT }))
    await lsquare.client.flush({ timeoutMs: 2_000 })
    expect(seen).toEqual({ bare: { refused: ['intent', 'session_id'], kept: ['q'] }, instrumented: { refused: ['intent', 'session_id'], kept: ['q'] } })
    expect(replies[1]).toEqual(replies[0])
    expect(JSON.stringify(replies[0])).toMatch(/intent/)
    expect(lsquare.calls()[0]).toMatchObject({ input: SENT, status: 'error', sessionSource: 'protocol' })
    expect(lines).toEqual([unreadableLine('it refuses keys it does not declare, and zod runs checks on it')])
  })

  it.each(['SDK 1.x', 'SDK 2.x'] as const)('still takes the extras out for a stripping tool whose schema zod built with polluted checks, gone since: its checks never see them, bare or not (%s)', async (kind) => {
    preloadSchemas()
    const lsquare = setup()
    const { seen, check, now } = recorder()
    const got: Record<string, string[]> = {}
    const schema = await polluted({ checks: [check] }, () => z.object({ q: z.string() }))
    const make = (as: 'bare' | 'instrumented') => (server: any) => {
      if (as === 'instrumented') instrument(server, lsquare.client, { extras: { missingTools: false } })
      server.registerTool('t', { inputSchema: schema }, async (args: Record<string, unknown>) => ((got[as] = Object.keys(args).sort()), text('rows')))
      return server
    }
    const bare = await overStdio(kind, make('bare'))
    const instrumented = await overStdio(kind, make('instrumented'))
    const replies = [await bare.call({ name: 't', arguments: SENT })]
    now('instrumented')
    replies.push(await instrumented.call({ name: 't', arguments: SENT }))
    await lsquare.client.flush({ timeoutMs: 2_000 })
    expect(replies.map(texts)).toEqual([['rows'], ['rows']])
    expect(got).toEqual({ bare: ['q'], instrumented: ['q'] })
    expect(seen).toEqual({ bare: { refused: [], kept: ['q'] }, instrumented: { refused: [], kept: ['q'] } })
    expect(lsquare.calls()[0]).toMatchObject({ input: { q: 'late' }, intent: 'find late orders', sessionId: SENT.session_id, sessionSource: 'sent' })
  })

  // Codex's fifth review, item 2: reading a schema asks nothing of what the app's code gives it
  // that JavaScript answers itself, so a promise there would be nobody's: a value a getter threw
  // is told by identity (no `instanceof`), Object.prototype is asked whether it has a field (no
  // `in`), and a Proxy along the way ends the reading before any of its traps runs.
  /** What nothing handled while `run` ran, and a little after, as Node reports it. */
  async function unhandledIn<T>(run: () => Promise<T>): Promise<{ result: T; seen: unknown[] }> {
    const watch = unhandledDuring()
    try {
      const result = await run()
      await sleep(20)
      return { result, seen: watch.seen }
    } finally {
      watch.stop()
    }
  }
  /**
   * A Proxy of `target` (an empty object by default) whose `trap` answers with a rejected promise,
   * as an async trap that fails does: for `key` alone when given, the target's own answer otherwise.
   */
  const trapping = (trap: 'getPrototypeOf' | 'has' | 'getOwnPropertyDescriptor', target: object = {}, key?: string) =>
    new Proxy(target, {
      [trap]: (object: object, asked?: PropertyKey) => {
        if (key !== undefined && asked !== key) return (Reflect[trap] as (...args: unknown[]) => unknown)(object, asked)
        return Promise.reject(new Error(`the app's ${trap} trap`))
      },
    })
  /** `schema` registered as `t` on a bare and an instrumented server, over stdio, and called with SENT on each. */
  async function bareAndInstrumented(lsquare: ReturnType<typeof setup>, kind: 'SDK 1.x' | 'SDK 2.x', schema: unknown) {
    const make = (instrumented: boolean) => (server: any) => {
      if (instrumented) instrument(server, lsquare.client, { extras: { missingTools: false } })
      server.registerTool('t', { inputSchema: schema }, async () => text('rows'))
      return server
    }
    return unhandledIn(async () => {
      const bare = await overStdio(kind, make(false))
      const instrumented = await overStdio(kind, make(true))
      const replies = [await bare.call({ name: 't', arguments: SENT }), await instrumented.call({ name: 't', arguments: SENT })]
      await lsquare.client.flush({ timeoutMs: 2_000 })
      return replies
    })
  }

  it.each(['SDK 1.x', 'SDK 2.x'] as const)("leaves no rejected promise unhandled when reading a tool's schema throws a Proxy whose getPrototypeOf gives one: what was thrown is never asked anything (%s)", async (kind) => {
    const lines: string[] = []
    const lsquare = setup({ logger: { info() {}, warn: (line) => void lines.push(line) } })
    const schema = z.object({ q: z.string() })
    // zod read its checks as it built the schema: a getter there now runs for the wrapper alone.
    Object.defineProperty(schema._zod.def, 'checks', {
      get() {
        throw trapping('getPrototypeOf')
      },
      configurable: true,
    })
    const { result, seen } = await bareAndInstrumented(lsquare, kind, schema)
    expect(seen).toEqual([])
    expect(result.map(texts)).toEqual([['rows'], ['rows']])
    expect(lsquare.calls()[0]).toMatchObject({ input: SENT, sessionSource: 'protocol' })
    expect(lines).toEqual([unreadableLine('it could not be read')])
  })

  const TRAPS = (['has', 'getOwnPropertyDescriptor'] as const).flatMap((trap) => (['SDK 1.x', 'SDK 2.x'] as const).map((kind) => [trap, kind] as const))
  it.each(TRAPS)("leaves no rejected promise unhandled when a tool's zod def is a Proxy whose %s trap gives one for its checks: none of its traps runs (%s)", async (trap, kind) => {
    const lines: string[] = []
    const lsquare = setup({ logger: { info() {}, warn: (line) => void lines.push(line) } })
    const schema = z.object({ q: z.string() })
    // zod parses with what it read as it built the schema; the def put in its place is read by the
    // SDKs too, which never ask it about its checks, as the wrapper does.
    ;(schema._zod as { def: object }).def = trapping(trap, schema._zod.def, 'checks')
    const { result, seen } = await bareAndInstrumented(lsquare, kind, schema)
    expect(seen).toEqual([])
    expect(result.map(texts)).toEqual([['rows'], ['rows']])
    expect(lsquare.calls()[0]).toMatchObject({ input: SENT, sessionSource: 'protocol' })
    expect(lines).toEqual([unreadableLine('part of it is a Proxy, whose traps would run as it is read')])
  })

  // Round 5's check: a polluted value is told by whether Object.prototype holds the key, and how,
  // never by comparing what it holds now with what the SDK copied: a getter there may answer anew
  // each time it is read, so its copy is never the value read again.
  /**
   * Runs `run` with Object.prototype holding a getter for `key` that answers `make()` anew each
   * time it is read, and a setter that makes what is written the object's own field, as an app's
   * accessor would; removed however it ends.
   */
  async function answeringAnew<T>(key: string, make: () => unknown, run: () => Promise<T>): Promise<T> {
    added.add(key)
    Object.defineProperty(proto, key, {
      configurable: true,
      get: make,
      set(this: object, value: unknown) {
        Object.defineProperty(this, key, { value, writable: true, enumerable: true, configurable: true })
      },
    })
    try {
      return await run()
    } finally {
      delete proto[key]
    }
  }

  it.each([1, 2] as const)("takes no sessionId from a getter of its that answers anew each time it is read, over stateless HTTP: the call keeps its ID line, while a stateful transport's own still groups it (SDK %d.x)", async (sdk) => {
    // A stateless transport serves one request: a server and transport for each, as SDK 1.x's examples make them.
    const lsquare = setup()
    const first = await overHttp(sdk, sessionsOn(lsquare)(high(sdk)), false)
    const second = await overHttp(sdk, sessionsOn(lsquare)(high(sdk)), false)
    const stateful = await overHttp(sdk, sessionsOn(lsquare)(high(sdk)), true)
    const call = { name: 't', arguments: {} }
    let count = 0
    const replies = await answeringAnew('sessionId', () => `sess_polluted_${String(count++).padStart(10, '0')}`, async () => {
      const done = [await first.call(call), await second.call(call), await stateful.call(call)]
      await lsquare.client.flush({ timeoutMs: 2_000 })
      return done
    })
    expect(replies.map(texts)).toEqual([['rows', ID_LINE], ['rows', ID_LINE], ['rows']])
    expect(lsquare.calls()).toMatchObject([{ sessionSource: 'issued' }, { sessionSource: 'issued' }, { sessionId: hashed('issued-by-server'), sessionSource: 'protocol' }])
  })

  const FRESH_META = [
    ['a getter of its that builds a new _meta each time it is read', '_meta', () => ({ [CLIENT_INFO]: { name: 'other', version: '6' }, [VERSION]: '2026-07-28' })],
    ['a _meta of its whose client info is a getter answering anew', '_meta', undefined],
  ] as const
  const freshMeta = FRESH_META.flatMap(([name, key, make]) => (['SDK 1.x', 'SDK 2.x'] as const).map((kind) => [name, kind, key, make] as const))
  it.each(freshMeta)("takes no client or protocol version from %s, on stdio (%s): initialize's are the call's", async (name, kind, key, make) => {
    const lsquare = setup()
    const connection = await overStdio(kind, sessionsOn(lsquare))
    const call = () => connection.call({ name: 't', arguments: {} })
    let reply: Reply
    if (make) {
      reply = await answeringAnew(key, make, async () => {
        const done = await call()
        await lsquare.client.flush({ timeoutMs: 2_000 })
        return done
      })
    } else {
      // A field of the polluted _meta is a getter: the SDK's copy holds what it gave then.
      const meta = { [VERSION]: '2026-07-28' }
      Object.defineProperty(meta, CLIENT_INFO, { get: () => ({ name: 'other', version: '6' }), enumerable: true })
      reply = await pollutedFlushed(lsquare, { _meta: meta }, call)
    }
    expect(texts(reply)).toEqual(['rows'])
    expect(lsquare.calls()[0]).toMatchObject({ client: { name: 'claude-code', version: '2.1' }, protocolVersion: '2025-11-25', sessionSource: 'protocol' })
    expect(name).toBeTruthy()
  })

  it("takes no request info from a getter of its that builds a new one each time it is read, in memory on SDK 1.x: its header names no version", async () => {
    const lsquare = setup()
    const server = instrument(high(1), lsquare.client, { extras: OFF })
    server.registerTool('t', {}, async () => text('rows'))
    const connection = await connect(1, server)
    const reply = await answeringAnew('requestInfo', () => ({ headers: { 'mcp-protocol-version': '1999-01-01' } }), async () => {
      const done = await connection.call('t')
      await lsquare.client.flush({ timeoutMs: 2_000 })
      return done
    })
    expect(reply).toEqual(text('rows'))
    expect(lsquare.calls()[0]).toMatchObject({ protocolVersion: '2025-11-25' })
  })

  // Round 5's check, the write side: Object.defineProperty's default makes a field that isn't
  // writable, and a getter alone has no setter, so an assignment adding a field of that name to an
  // ordinary object throws in strict code; with a setter, the setter takes the value instead. The
  // SDK adds each field of what it builds as an object literal does. Each kind reads as absent.
  const KINDS: Array<[string, PropertyDescriptor]> = [
    ["a field that isn't writable", { value: undefined, configurable: true }],
    ['a getter alone', { get: () => undefined, configurable: true }],
    ['a setter that keeps nothing', { get: () => undefined, set() {}, configurable: true }],
  ]
  /** Every field of the call's event, its parts' included, and the record and outcome it is built from. */
  const CALL_FIELDS = `status sessionId sessionSource durationMs server client protocolVersion input result intent agent metadata
turnRef extra round task error message type code category content text isError structuredContent question answer turnId
toolCalls usage inputTokens version name redactions dropped truncated userId conversationId ids identity plan`.split(/\s+/)
  const VOLATILE = new Set(['eventId', 'callId', 'capturedAt', 'durationMs', 'sessionId', 'turnRef'])
  const steady = (events: LSquareEvent[]) => events.map((event) => Object.fromEntries(Object.entries(event).filter(([key]) => !VOLATILE.has(key))))
  /**
   * A client whose fetch answers with a response of its own making: the runtime's Response
   * assigns its status as it is made, which a polluted `status` that isn't writable makes throw.
   */
  function handMade() {
    const events: LSquareEvent[] = []
    const fetch = (async (_url: unknown, init?: RequestInit) => {
      const batch = JSON.parse(String(init?.body)).events as LSquareEvent[]
      events.push(...batch)
      const bytes = new TextEncoder().encode(JSON.stringify({ accepted: batch.map((e) => e.eventId), duplicates: [], rejected: [] }))
      let sent = false
      const reader = { read: async () => (sent ? { done: true, value: undefined } : ((sent = true), { done: false, value: bytes })), cancel: async () => {} }
      return { status: 200, type: 'basic', headers: { get: () => null }, body: { getReader: () => reader } }
    }) as unknown as typeof globalThis.fetch
    const client = new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test', fetch, logger: { info() {}, warn() {} } })
    return { client, calls: () => events.filter((e): e is ToolCallEvent => e.kind === 'tool_call') }
  }
  const writes = (['SDK 1.x', 'SDK 2.x'] as const).flatMap((kind) => KINDS.map(([name, descriptor]) => [name, kind, descriptor] as const))
  it.each(writes)("records each call whole though Object.prototype holds each of its event's field names as %s, over stdio (%s)", async (_name, kind, descriptor) => {
    const run = async () => {
      const lsquare = handMade()
      const make = (server: any) => {
        instrument(server, lsquare.client, { inputs: true, results: true, extras: OFF, identify: () => ({ userId: 'u-1', metadata: { plan: 'pro' } }) })
        server.registerTool('t', { inputSchema: { q: z.string() } }, async ({ q }: { q: string }) => {
          lsquare.client.capture({ conversationId: 'c-1', turnId: 't-1', question: 'which orders?', answer: 'two', toolCalls: [{ name: 'n', input: { a: 1 } }], usage: { inputTokens: 1 } })
          return { content: [{ type: 'text' as const, text: `rows ${q}` }], structuredContent: { rows: 2 } }
        })
        server.registerTool('boom', {}, async () => {
          throw new TypeError('boom')
        })
        return server
      }
      const connection = await overStdio(kind, make)
      return { lsquare, connection }
    }
    const calls = async (connection: Awaited<ReturnType<typeof overStdio>>) => [await connection.call({ name: 't', arguments: { q: 'x' } }), await connection.call({ name: 'boom', arguments: {} })]
    const clean = await run()
    const cleanReplies = await calls(clean.connection)
    await clean.lsquare.client.flush({ timeoutMs: 2_000 })
    const expected = steady(clean.lsquare.calls())
    expect(expected).toHaveLength(2)
    const changed: string[] = []
    const sdkItself: string[] = []
    for (const key of CALL_FIELDS) {
      const { lsquare, connection } = await run()
      added.add(key)
      Object.defineProperty(proto, key, descriptor)
      let replies: unknown
      try {
        replies = await calls(connection)
        await lsquare.client.flush({ timeoutMs: 2_000 })
      } catch (error) {
        // No reply at all (the wire's own check throws).
        replies = { threw: String(error) }
      } finally {
        delete proto[key]
      }
      // A name the MCP SDK itself can't take so (its zod builds what it parses by assignment) changes
      // its own replies, bare too: only where they are as they were are the events compared.
      if (JSON.stringify(replies) !== JSON.stringify(cleanReplies)) sdkItself.push(key)
      else if (JSON.stringify(steady(lsquare.calls())) !== JSON.stringify(expected)) changed.push(key)
    }
    expect(changed).toEqual([])
    // The SDK can't take a result's own field names, nor an error's message: the core's test of
    // every field name builds those without it. Every other field of the event is checked here.
    expect(sdkItself.filter((key) => !['type', 'content', 'text', 'isError', 'structuredContent', 'name', 'message'].includes(key))).toEqual([])
  })
})

describe("the wrapper's reads of the app's values run no thenable's then", () => {
  // Only a native promise is silenced: another thenable's `then` is the app's code, and may do
  // work (a query builder runs its query). The wrapper adopts one only where it must wait for a
  // callback's answer, as `await` would: the tool's own function, a tools/list handler.
  it('reading a property, its own or declared, or silencing it, never calls a thenable it finds', async () => {
    let called = 0
    const thenable = { then: () => void called++ }
    const holder = { value: thenable }
    expect(read(holder, 'value')).toBe(thenable)
    expect(own(holder, 'value')).toBe(thenable)
    silence(thenable)
    expect(declared(holder, 'value')).toBe(thenable)
    // Adopting it would call its `then` from a microtask.
    await new Promise((resolve) => setImmediate(resolve))
    expect(called).toBe(0)
  })

  it.each([1, 2] as const)("never runs one found in identify's result, a thrown error or the tool's result (SDK %d.x)", async (sdk) => {
    let called = 0
    const query = () => ({ then: () => void called++ })
    const lsquare = setup()
    const server = instrument(high(sdk), lsquare.client, { extras: OFF, identify: () => ({ userId: 'u-1', metadata: query() }) as never })
    server.registerTool('fails', {}, async () => {
      throw Object.assign(new Error('lookup failed'), { code: query() })
    })
    server.registerTool('rows', {}, async () => ({ ...text('rows'), task: query(), resultType: query() }))
    const connection = await connect(sdk, server)
    await connection.call('fails')
    await connection.call('rows')
    await lsquare.client.flush({ timeoutMs: 2_000 })
    expect(lsquare.calls()).toMatchObject([{ toolName: 'fails', userId: 'u-1' }, { toolName: 'rows', userId: 'u-1' }])
    expect(called).toBe(0)
  })

  it("still waits for a tool that answers with a thenable, as the MCP SDK does", async () => {
    const lsquare = setup()
    const server = new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
    // A handler's answer is awaited: a thenable there is adopted, by the SDK and the wrapper alike.
    server.setRequestHandler(CallToolRequestSchema, () => ({ then: (resolve: (value: unknown) => void) => resolve(text('later')) }) as never)
    instrument(server, lsquare.client)
    expect(await (await connect(1, server)).call('t')).toEqual(text('later'))
    await lsquare.client.flush({ timeoutMs: 2_000 })
    expect(lsquare.calls()).toMatchObject([{ toolName: 't', status: 'completed', result: { content: [{ type: 'text', text: 'later' }] } }])
  })
})

describe("asking whether a callback answered with a thenable leaves no rejected promise unhandled", () => {
  // Codex's second review. The answer is silenced first, then its `then` read once and silenced
  // too: a getter may give a rejected promise (a lookup that failed). One that throws still makes
  // no thenable, and a thenable a getter gives is never called. A native promise is one by its
  // internal slot, whatever its `then` (round 5's check), and is handed to Promise.resolve() as it
  // is, as `await` takes one: its `then` is never read.
  const lookup = () => Promise.reject(new Error('lookup failed'))
  /** A promise that rejects, and whose `then` throws when read: it answered, but can't be asked. */
  const guarded = () => Object.defineProperty(lookup(), 'then', { get: () => { throw new Error('then') } })
  const turnOver = () => new Promise((resolve) => setImmediate(resolve))

  it('adopted() silences what a then getter gives, adopting none, takes a native promise by its slot, its throwing then unread, and runs the then it read, in a job of its own', async () => {
    // Codex's sixth review: what it adopts it adopts as `await` would, through Promise.resolve(),
    // the `then` it read to tell being the one run, never at once (round 5 ran it at once, and
    // gave the answer boxed).
    const watch = unhandledDuring()
    let reads = 0
    let called = 0
    let calledAtOnce = -1
    let answers: Array<Promise<unknown> | undefined> = []
    let settledTo: unknown[] = []
    try {
      answers = [
        adopted({ get then() { return reads++, lookup() } }),
        adopted(guarded()),
        adopted({ get then() { return { then: () => void called++ } } }),
        adopted({ then: (resolve: (value: unknown) => void) => void (called++, resolve('ran')) }),
      ]
      calledAtOnce = called
      settledTo = await Promise.all(answers.map((answer) => answer?.then((value) => value, (error: Error) => error.message)))
      // Checked once Node has looked for unhandled rejections, so none is left for the next test.
      await turnOver()
    } finally {
      watch.stop()
    }
    expect(answers.map((answer) => answer === undefined)).toEqual([true, false, true, false])
    expect(settledTo).toEqual([undefined, 'lookup failed', undefined, 'ran'])
    expect(calledAtOnce).toBe(0)
    expect(called).toBe(1)
    expect(reads).toBe(1)
    expect(watch.seen).toEqual([])
  })

  it("adopted() silences what the then it runs returns, which JavaScript would drop: an async then that throws once it has resolved", async () => {
    const watch = unhandledDuring()
    let value: unknown
    try {
      value = await adopted({ then: async (resolve: (value: unknown) => void) => (resolve('ok'), await Promise.resolve(), Promise.reject(new Error('after'))) })
      await turnOver()
    } finally {
      watch.stop()
    }
    expect(value).toBe('ok')
    expect(watch.seen).toEqual([])
  })

  it.each([1, 2] as const)("an inputs function's answer whose then getter gives one still counts, read once (SDK %d.x)", async (sdk) => {
    const watch = unhandledDuring()
    let reads = 0
    const logs: string[] = []
    try {
      const lsquare = setup({ logger: { info() {}, warn: (line) => void logs.push(line) } })
      const inputs = () => ({ get then() { return reads++, lookup() } })
      const server = instrument(high(sdk), lsquare.client, { extras: OFF, inputs: inputs as unknown as () => boolean })
      server.registerTool('t', { inputSchema: { q: z.string() } }, async () => text('rows'))
      await (await connect(sdk, server)).call('t', { q: 'late' })
      await lsquare.client.flush({ timeoutMs: 2_000 })
      await turnOver()
      expect(lsquare.calls()).toMatchObject([{ input: { q: 'late' } }])
    } finally {
      watch.stop()
    }
    expect(watch.seen).toEqual([])
    expect(reads).toBe(1)
    expect(logs).toEqual([])
  })

  it.each([1, 2] as const)('a results function answering with a promise whose then throws records no results, saying so, its promise silenced (SDK %d.x)', async (sdk) => {
    const watch = unhandledDuring()
    const logs: string[] = []
    try {
      const lsquare = setup({ logger: { info() {}, warn: (line) => void logs.push(line) } })
      const server = instrument(high(sdk), lsquare.client, { extras: OFF, results: guarded as unknown as () => boolean })
      server.registerTool('t', {}, async () => text('rows'))
      await (await connect(sdk, server)).call('t')
      await lsquare.client.flush({ timeoutMs: 2_000 })
      await turnOver()
      expect(lsquare.calls()).toHaveLength(1)
      expect(lsquare.calls()[0]).not.toHaveProperty('result')
    } finally {
      watch.stop()
    }
    expect(watch.seen).toEqual([])
    expect(logs).toEqual(["[lsquare] The results function threw or returned a promise: results aren't recorded for the calls it answered that way."])
  })
})

// Shared by the fifth and sixth reviews' tests below.
const rejecting = (what: string) => Promise.reject(new Error(what))
/** What nothing handled while `run` ran, and a little after, as Node reports it. */
async function settled<T>(run: () => Promise<T>): Promise<{ result: T; seen: unknown[] }> {
  const watch = unhandledDuring()
  try {
    const result = await run()
    await sleep(20)
    return { result, seen: watch.seen }
  } finally {
    watch.stop()
  }
}

/** `server` over a real stdio transport of SDK `sdk`'s, initialized; `patch` sees the streams and the transport first. */
async function stdio(sdk: 1 | 2, server: any, patch?: (stdin: PassThrough, transport: any) => void) {
  const stdin = new PassThrough()
  const stdout = new PassThrough()
  const transport: any = sdk === 1 ? new Stdio1(stdin, stdout) : new Stdio2(stdin, stdout)
  patch?.(stdin, transport)
  const replies = new Map<number, any>()
  let buffered = ''
  stdout.on('data', (chunk) => {
    buffered += String(chunk)
    for (let end = buffered.indexOf('\n'); end >= 0; end = buffered.indexOf('\n')) {
      const message = JSON.parse(buffered.slice(0, end))
      buffered = buffered.slice(end + 1)
      if (typeof message.id === 'number') replies.set(message.id, message)
    }
  })
  let id = 0
  const request = async (method: string, params: unknown = {}) => {
    const mine = id++
    stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: mine, method, params })}\n`)
    for (let i = 0; i < 400 && !replies.has(mine); i++) await sleep(5)
    return replies.get(mine)
  }
  await server.connect(transport)
  const initialized = await request('initialize', { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'claude-code', version: '2.1' } })
  stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' })}\n`)
  return { request, stdin, initialized }
}

describe("Codex's fifth review's sweep: what the wrapper checks it reads once, and it asks the app's values nothing JavaScript answers itself", () => {
  // Each read here is one no bare SDK makes. A getter read twice may give, the second time, what
  // the first check never saw; and the answer of a Proxy trap that JavaScript consumes itself
  // (getPrototypeOf through `instanceof` or `for...in`, a method lookup) can't be given a handler.
  /** An own accessor for `key` that gives `first` as it is first read, and a rejected promise after; a write makes it a plain field. */
  function onceThenRejecting(object: object, key: string, first: unknown): void {
    let reads = 0
    Object.defineProperty(object, key, {
      configurable: true,
      get: () => (reads++ === 0 ? first : rejecting(`${key}, read again`)),
      set(value) {
        Object.defineProperty(object, key, { value, writable: true, configurable: true })
      },
    })
  }
  const LISTED = { name: 't', inputSchema: { type: 'object', properties: { q: { type: 'string' } } } }
  /** A low-level server whose tools/list answers `list()`, and whose tool `t` answers 'rows'. */
  function lowServer(sdk: 1 | 2, list: () => unknown = () => ({ tools: [LISTED] })): any {
    const server: any = sdk === 1 ? new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: { listChanged: true } } }) : new Server2({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: { listChanged: true } } })
    server.setRequestHandler(sdk === 1 ? ListToolsRequestSchema : 'tools/list', list)
    server.setRequestHandler(sdk === 1 ? CallToolRequestSchema : 'tools/call', async () => text('rows'))
    return server
  }
  const EXTRAS = { sessions: true, intent: true, missingTools: false }

  it.each([1, 2] as const)('calls the setRequestHandler it recognised a server by, never one a getter gives as it is read again (SDK %d.x)', async (sdk) => {
    const lsquare = setup()
    const server: any = sdk === 1 ? new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } }) : new Server2({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
    onceThenRejecting(server, 'setRequestHandler', Object.getPrototypeOf(server).setRequestHandler)
    const { result, seen } = await settled(async () => {
      instrument(server, lsquare.client, { extras: OFF })
      server.setRequestHandler(sdk === 1 ? CallToolRequestSchema : 'tools/call', async () => text('rows'))
      const reply = await (await connect(sdk, server)).call('t')
      await lsquare.client.flush({ timeoutMs: 2_000 })
      return reply
    })
    expect(seen).toEqual([])
    expect(result).toEqual(text('rows'))
    expect(lsquare.calls()).toMatchObject([{ toolName: 't', status: 'completed' }])
  })

  it.each([
    ['SDK 1.x', 1, 'notification'],
    ['SDK 2.x', 2, '_notificationViaCodec'],
  ] as const)('wraps the method it checked to hear the list change, read once, so the change still goes out (%s, %s)', async (_kind, sdk, key) => {
    const lsquare = setup()
    const server = lowServer(sdk)
    onceThenRejecting(server, key, Object.getPrototypeOf(server)[key])
    const { seen } = await settled(async () => {
      instrument(server, lsquare.client, { extras: EXTRAS })
      await connect(sdk, server)
      await server.sendToolListChanged()
    })
    expect(seen).toEqual([])
  })

  it("taps the tool function it checked, read once: the call still reaches the tool (SDK 1.x's handler)", async () => {
    const lsquare = setup()
    const server = instrument(high(1), lsquare.client, { extras: OFF })
    server.registerTool('t', {}, async () => text('rows'))
    const tool = server._registeredTools.t
    onceThenRejecting(tool, 'handler', tool.handler)
    const { result, seen } = await settled(async () => {
      const reply = await (await connect(1, server)).call('t')
      await lsquare.client.flush({ timeoutMs: 2_000 })
      return reply
    })
    expect(seen).toEqual([])
    expect(result).toEqual(text('rows'))
    expect(lsquare.calls()).toMatchObject([{ toolName: 't', status: 'completed' }])
  })

  it.each([1, 2] as const)("tells a server's handler map by its internal slot, asking a Proxy of one nothing (SDK %d.x)", async (sdk) => {
    const lines: string[] = []
    const lsquare = setup({ logger: { info() {}, warn: (line) => void lines.push(line) } })
    const server = lowServer(sdk)
    const handlers = new Proxy(server._requestHandlers, { getPrototypeOf: () => rejecting('getPrototypeOf') })
    const proxied = new Proxy(server, { get: (target, key, receiver) => (key === '_requestHandlers' ? handlers : Reflect.get(target, key, receiver)) })
    const { seen } = await settled(async () => {
      instrument(proxied, lsquare.client)
      await sleep(5)
    })
    expect(seen).toEqual([])
    expect(lines).toEqual(["[lsquare] instrument() doesn't recognise this server: it is left as it is."])
  })

  it.each([1, 2] as const)("copies a low-level server's listed tool as JSON writes it, never reading a symbol key's getter, which no client gets (SDK %d.x)", async (sdk) => {
    const lsquare = setup()
    const tool = { ...LISTED }
    Object.defineProperty(tool, Symbol('the app'), { get: () => rejecting('a symbol key'), enumerable: true })
    const server = lowServer(sdk, () => ({ tools: [tool] }))
    instrument(server, lsquare.client, { extras: EXTRAS })
    const { result, seen } = await settled(async () => (await connect(sdk, server)).tools())
    expect(seen).toEqual([])
    expect(Object.keys(result[0]!.inputSchema.properties ?? {})).toEqual(['q', 'session_id', 'intent'])
  })

  it.each([1, 2] as const)("reads a low-level server's list of tools as JSON does, by index, never asking it for its methods (SDK %d.x)", async (sdk) => {
    const lsquare = setup()
    const tools = () => new Proxy([LISTED], { get: (target, key, receiver) => (key === 'map' ? rejecting('the list, asked for map') : Reflect.get(target, key, receiver)) })
    const server = lowServer(sdk, () => ({ tools: tools() }))
    instrument(server, lsquare.client, { extras: EXTRAS })
    const { result, seen } = await settled(async () => (await connect(sdk, server)).tools())
    expect(seen).toEqual([])
    expect(Object.keys(result[0]!.inputSchema.properties ?? {})).toEqual(['q', 'session_id', 'intent'])
  })

  it.each([1, 2] as const)("lists a low-level server's listed schema as JSON does, never asking it for its prototype, over stdio (SDK %d.x)", async (sdk) => {
    // In memory, the client's own zod asks the very object for it: over stdio it gets the JSON.
    const lsquare = setup()
    const schema = () => new Proxy({ type: 'object', properties: { q: { type: 'string' } } }, { getPrototypeOf: () => rejecting('getPrototypeOf') })
    const server = lowServer(sdk, () => ({ tools: [{ name: 't', inputSchema: schema() }] }))
    instrument(server, lsquare.client, { extras: EXTRAS })
    const { result, seen } = await settled(async () => (await (await stdio(sdk, server)).request('tools/list')).result.tools)
    expect(seen).toEqual([])
    expect(Object.keys(result[0].inputSchema.properties)).toEqual(['q', 'session_id', 'intent'])
  })

  it.each([1, 2] as const)("silences what a stdio connection's stdin answers as the wrapper starts and stops hearing its end (SDK %d.x)", async (sdk) => {
    // Only the wrapper's listeners get the promise: the SDK's own (SDK 2.x hears the end too) get the stream.
    const lsquare = setup()
    const server = instrument(high(sdk), lsquare.client, { extras: OFF })
    server.registerTool('t', {}, async () => text('rows'))
    const answering = (stdin: PassThrough, transport: any, method: 'on' | 'off') => {
      const own = PassThrough.prototype[method]
      stdin[method] = function (this: PassThrough, event: string | symbol, listener: (...args: unknown[]) => void) {
        const answer = Reflect.apply(own, this, [event, listener])
        return (event === 'end' || event === 'close') && listener !== transport._onstdinclose ? rejecting(`stdin.${method}`) : answer
      } as never
    }
    const { result, seen } = await settled(async () => {
      const connection = await stdio(sdk, server, (stdin, transport) => {
        answering(stdin, transport, 'on')
        answering(stdin, transport, 'off')
      })
      const reply = await connection.request('tools/call', { name: 't', arguments: {} })
      await server.close()
      await lsquare.client.flush({ timeoutMs: 2_000 })
      return reply
    })
    expect(seen).toEqual([])
    expect(result.result.content).toEqual([{ type: 'text', text: 'rows' }])
    expect(lsquare.calls()).toMatchObject([{ toolName: 't' }])
  })

  describe("the check of that sweep: what the app throws or answers is asked nothing JavaScript answers itself, and what the wrapper adopts it reads once", () => {
    // Round 5's check, each on a real stdio transport where the defect is transport-dependent.
    // Codex's sixth review took out its Proxy rules (a Proxy read through get alone, or as
    // Object.keys() lists it), which lost an ordinary Proxy's own fields, and the tests that held
    // them: what a hostile trap gives JavaScript itself is out of scope, as the spec now says.
    /** A low-level server of SDK `sdk`'s whose tool `t` runs `call`, and whose list answers `list()`. */
    function lowCalling(sdk: 1 | 2, call: () => Promise<unknown>, list: () => unknown = () => ({ tools: [LISTED] })): any {
      const server: any = sdk === 1 ? new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } }) : new Server2({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
      server.setRequestHandler(sdk === 1 ? ListToolsRequestSchema : 'tools/list', list)
      server.setRequestHandler(sdk === 1 ? CallToolRequestSchema : 'tools/call', call)
      return server
    }
    const LEVELS = ([1, 2] as const).flatMap((sdk) => (['high', 'low'] as const).map((level) => [sdk, level] as const))

    /** `t` throwing `thrown()`, at `level`, on a bare server, then an instrumented one, over stdio: each reply, and what each left unhandled. */
    async function throwing(lsquare: ReturnType<typeof setup>, sdk: 1 | 2, level: 'high' | 'low', thrown: () => unknown) {
      const make = (instrumented: boolean) => {
        const fail = async () => {
          throw thrown()
        }
        if (level === 'low') {
          const server = lowCalling(sdk, fail)
          if (instrumented) instrument(server, lsquare.client, { extras: false })
          return server
        }
        const server = high(sdk)
        if (instrumented) instrument(server, lsquare.client, { extras: false })
        server.registerTool('t', {}, fail)
        return server
      }
      // SDK 2.x asks a thrown value for a descriptor itself, bare: the wrapper adds none.
      const bare = await settled(async () => (await stdio(sdk, make(false))).request('tools/call', { name: 't', arguments: {} }))
      const wrapped = await settled(async () => {
        const reply = await (await stdio(sdk, make(true))).request('tools/call', { name: 't', arguments: {} })
        await lsquare.client.flush({ timeoutMs: 2_000 })
        return reply
      })
      return { bare, wrapped }
    }

    it.each(LEVELS)("records what a tool throws, a transparent Proxy of an object without a message, as the SDK writes it, as for that object: 'Unknown error' only where it escapes (SDK %d.x, %s level)", async (sdk, level) => {
      // Codex's sixth review: round 5 never wrote a Proxy as a string, so an ordinary one lost the
      // message the client got ("[object Object]"), which the object itself keeps.
      const written: unknown[] = []
      for (const thrown of [() => ({}), () => new Proxy({}, {})]) {
        const lsquare = setup()
        const { bare, wrapped } = await throwing(lsquare, sdk, level, thrown)
        expect(wrapped.result.result ?? wrapped.result.error).toEqual(bare.result.result ?? bare.result.error)
        const [call] = lsquare.calls()
        written.push(call?.error?.message)
        // What the client got, where the tool's own throw becomes an isError result.
        if (level === 'high') expect(call?.error?.message).toBe(bare.result.result.content[0].text)
      }
      expect(written).toEqual(level === 'high' ? ['[object Object]', '[object Object]'] : ['Unknown error', 'Unknown error'])
    })

    it.each([1, 2] as const)("records a low-level server handed over as a Proxy whose get trap gives a rejected promise for a symbol it lacks: its mark is read as any of its values, and isn't one (SDK %d.x)", async (sdk) => {
      const lsquare = setup()
      const target = lowServer(sdk)
      const proxied = new Proxy(target, { get: (object, key, receiver) => (typeof key === 'symbol' && !(key in object) ? rejecting(`get ${String(key)}`) : Reflect.get(object, key, receiver)) })
      const { result, seen } = await settled(async () => {
        instrument(proxied, lsquare.client, { extras: false })
        const reply = await (await connect(sdk, proxied)).call('t')
        await lsquare.client.flush({ timeoutMs: 2_000 })
        return reply
      })
      expect(seen).toEqual([])
      expect(result).toEqual(text('rows'))
      expect(lsquare.calls()).toMatchObject([{ toolName: 't', status: 'completed' }])
    })

    it.each([1, 2] as const)("adopts a thenable the tool's function answers with through the then it checked, whose getter would give a rejected promise read again (SDK %d.x)", async (sdk) => {
      const lsquare = setup()
      let reads = 0
      const server = instrument(high(sdk), lsquare.client, { extras: false })
      server.registerTool('t', {}, () => ({
        get then() {
          return reads++ === 0 ? (resolve: (value: unknown) => void) => resolve(text('rows')) : rejecting('then, read again')
        },
      }))
      const { result, seen } = await settled(async () => {
        const reply = await (await connect(sdk, server)).call('t')
        await lsquare.client.flush({ timeoutMs: 2_000 })
        return reply
      })
      expect(seen).toEqual([])
      expect(reads).toBe(1)
      expect(result).toEqual(text('rows'))
      expect(lsquare.calls()).toMatchObject([{ status: 'completed' }])
    })

  })
})

describe("Codex's sixth review: what the wrapper adopts it adopts as `await` does, and a transparent Proxy reads as what it wraps", () => {
  // Round 5 adopted a thenable itself: its `then` run at once, a native promise through
  // Promise.prototype.then (a subclass's own `then` skipped), and a thenable it resolved with
  // adopted in turn by recursion. Each test compares the instrumented server's reply over real
  // stdio with the bare one's, on both SDKs, at both levels. And it read a Proxy's fields
  // through get alone, unless Object.prototype had one of the name: a transparent Proxy then lost
  // its own field wherever Object.prototype was polluted with that name.
  const LEVELS = ([1, 2] as const).flatMap((sdk) => (['high', 'low'] as const).map((level) => [sdk, level] as const))
  /** A Promise subclass's instance, settled with `value`, whose own `then` hands on `adapt(value)`, as an ORM's or a tracer's may. */
  function adapting<T>(value: T, adapt: (value: T) => unknown): Promise<unknown> {
    class Adapting extends Promise<unknown> {
      override then<A = unknown, B = never>(onFulfilled?: ((value: unknown) => A | PromiseLike<A>) | null, onRejected?: ((reason: unknown) => B | PromiseLike<B>) | null): Promise<A | B> {
        return super.then(onFulfilled && ((item) => onFulfilled(adapt(item as T))), onRejected)
      }
    }
    return new Adapting((resolve) => resolve(value))
  }
  /** A tool `t` of SDK `sdk`'s, at `level`, answering `answer()`; instrumented with `lsquare`'s client when given. */
  function answering(sdk: 1 | 2, level: 'high' | 'low', answer: () => unknown, lsquare?: ReturnType<typeof setup>): any {
    if (level === 'low') {
      const server: any = sdk === 1 ? new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } }) : new Server2({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
      server.setRequestHandler(sdk === 1 ? ListToolsRequestSchema : 'tools/list', () => ({ tools: [{ name: 't', inputSchema: { type: 'object' } }] }))
      server.setRequestHandler(sdk === 1 ? CallToolRequestSchema : 'tools/call', answer)
      if (lsquare) instrument(server, lsquare.client, { extras: false })
      return server
    }
    const server = high(sdk)
    if (lsquare) instrument(server, lsquare.client, { extras: false })
    server.registerTool('t', {}, answer)
    return server
  }
  /** `t`'s reply over stdio, bare and instrumented, and the instrumented one's events. */
  async function replies(sdk: 1 | 2, level: 'high' | 'low', answer: () => unknown) {
    const lsquare = setup()
    const bare = await (await stdio(sdk, answering(sdk, level, answer))).request('tools/call', { name: 't', arguments: {} })
    const instrumented = await (await stdio(sdk, answering(sdk, level, answer, lsquare))).request('tools/call', { name: 't', arguments: {} })
    await lsquare.client.flush({ timeoutMs: 2_000 })
    return { bare, instrumented, calls: lsquare.calls() }
  }
  const raw = (value: unknown) => (value as { content?: Array<{ text?: string }> })?.content?.[0]?.text === 'raw'

  it.each(LEVELS)("gives the client what a tool's Promise subclass hands on through its own then, as bare (SDK %d.x, %s level)", async (sdk, level) => {
    const { bare, instrumented, calls } = await replies(sdk, level, () => adapting(text('raw'), (value) => (raw(value) ? text('adapted') : value)))
    expect(bare.result).toEqual(text('adapted'))
    expect(instrumented.result).toEqual(bare.result)
    expect(calls).toMatchObject([{ status: 'completed', result: { content: [{ type: 'text', text: 'adapted' }] } }])
  })

  it.each(LEVELS)("runs a tool's thenable's then in a job of its own, after a microtask queued before it answered, as bare (SDK %d.x, %s level)", async (sdk, level) => {
    const observing = () => {
      let seen = 'sync'
      queueMicrotask(() => (seen = 'async'))
      return { then: (resolve: (value: unknown) => void) => resolve(text(seen)) }
    }
    const { bare, instrumented, calls } = await replies(sdk, level, observing)
    expect(bare.result).toEqual(text('async'))
    expect(instrumented.result).toEqual(bare.result)
    expect(calls).toMatchObject([{ status: 'completed', result: { content: [{ text: 'async' }] } }])
  })

  it.each(LEVELS)("adopts a tool's thenable resolving with a thenable, 20,000 deep, as JavaScript does, job by job (SDK %d.x, %s level)", async (sdk, level) => {
    const nested = (depth: number): unknown => (depth === 0 ? text('deep') : { then: (resolve: (value: unknown) => void) => resolve(nested(depth - 1)) })
    const { bare, instrumented, calls } = await replies(sdk, level, () => nested(20_000))
    expect(bare.result).toEqual(text('deep'))
    expect(instrumented.result).toEqual(bare.result)
    expect(calls).toMatchObject([{ status: 'completed', result: { content: [{ text: 'deep' }] } }])
  })

  it.each(LEVELS)("fails a tool's thenable whose then getter throws as bare, its then read once, as await reads it (SDK %d.x, %s level)", async (sdk, level) => {
    // Round 5 took such an answer for no thenable and handed it on, for the SDK to read again.
    let reads = 0
    const answer = () => ({
      get then() {
        reads++
        throw new Error('then getter')
      },
    })
    const { bare, instrumented, calls, readsBare, readsInstrumented } = await (async () => {
      const lsquare = setup()
      const bare = await (await stdio(sdk, answering(sdk, level, answer))).request('tools/call', { name: 't', arguments: {} })
      const readsBare = reads
      reads = 0
      const instrumented = await (await stdio(sdk, answering(sdk, level, answer, lsquare))).request('tools/call', { name: 't', arguments: {} })
      const readsInstrumented = reads
      await lsquare.client.flush({ timeoutMs: 2_000 })
      return { bare, instrumented, calls: lsquare.calls(), readsBare, readsInstrumented }
    })()
    expect(instrumented.result ?? instrumented.error).toEqual(bare.result ?? bare.error)
    expect(readsBare).toBe(1)
    expect(readsInstrumented).toBe(readsBare)
    expect(calls).toMatchObject([{ status: 'error', error: { message: 'then getter' } }])
  })

  it.each([1, 2] as const)("adopts what identify answers, a Promise subclass, through its own then, as await does (SDK %d.x)", async (sdk) => {
    const lsquare = setup()
    const server = instrument(high(sdk), lsquare.client, { extras: false, identify: () => adapting({ userId: 'raw' }, () => ({ userId: 'adapted' })) as never })
    server.registerTool('t', {}, async () => text('rows'))
    await (await stdio(sdk, server)).request('tools/call', { name: 't', arguments: {} })
    await lsquare.client.flush({ timeoutMs: 2_000 })
    expect(lsquare.calls()).toMatchObject([{ userId: 'adapted' }])
  })

  it.each([1, 2] as const)("lists what a low-level tools/list handler's Promise subclass hands on through its own then, as bare (SDK %d.x)", async (sdk) => {
    const make = (lsquare?: ReturnType<typeof setup>) => {
      const server: any = sdk === 1 ? new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } }) : new Server2({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
      const page = (name: string) => ({ tools: [{ name, inputSchema: { type: 'object', properties: { q: { type: 'string' } } } }] })
      server.setRequestHandler(sdk === 1 ? ListToolsRequestSchema : 'tools/list', () => adapting(page('raw'), () => page('adapted')))
      server.setRequestHandler(sdk === 1 ? CallToolRequestSchema : 'tools/call', async () => text('rows'))
      if (lsquare) instrument(server, lsquare.client, { extras: { sessions: true, intent: true, missingTools: false } })
      return server
    }
    const names = async (server: any) => ((await (await stdio(sdk, server)).request('tools/list')).result.tools as Array<{ name: string }>).map((tool) => tool.name)
    const bare = await names(make())
    expect(bare).toEqual(['adapted'])
    expect(await names(make(setup()))).toEqual(bare)
  })

  /** Runs `run` with Object.prototype holding `key` as `value`, not enumerable (as a polluted JSON key would be), removed however `run` ends. */
  async function pollutedWith<T>(key: string, value: unknown, run: () => Promise<T>): Promise<T> {
    Object.defineProperty(Object.prototype, key, { value, writable: true, configurable: true })
    try {
      return await run()
    } finally {
      delete (Object.prototype as Record<string, unknown>)[key]
    }
  }

  it.each([1, 2] as const)("keeps the orgId what identify answers has of its own, handed over as a transparent Proxy, though Object.prototype has one too, as for the object itself (SDK %d.x)", async (sdk) => {
    const orgs: unknown[] = []
    for (const answer of [() => ({ orgId: 'org_real', userId: 'u-1' }), () => new Proxy({ orgId: 'org_real', userId: 'u-1' }, {})]) {
      const lsquare = setup()
      const server = instrument(high(sdk), lsquare.client, { extras: false, identify: answer })
      server.registerTool('t', {}, async () => text('rows'))
      await pollutedWith('orgId', 'org_polluted', async () => {
        await (await stdio(sdk, server)).request('tools/call', { name: 't', arguments: {} })
        await lsquare.client.flush({ timeoutMs: 2_000 })
      })
      expect(lsquare.calls()).toMatchObject([{ userId: 'u-1' }])
      orgs.push(lsquare.calls()[0]?.orgId)
    }
    expect(orgs).toEqual(['org_real', 'org_real'])
  })

  it("reads a transparent Proxy's own and declared fields as its target's, a field that isn't enumerable too, though Object.prototype has one of the name", async () => {
    // own() asked a Proxy whether Object.keys() listed the key, declared() whether Object.prototype had it.
    class Config {
      get region() {
        return 'eu'
      }
    }
    const target = Object.defineProperty(Object.assign(new Config(), { orgId: 'org_real' }), 'hidden', { value: 'kept', enumerable: false })
    const proxy = new Proxy(target, {})
    const read = (value: object) => [own(value, 'orgId'), own(value, 'hidden'), declared(value, 'orgId'), declared(value, 'region'), own(value, 'region')]
    const seen = await pollutedWith('orgId', 'org_polluted', async () => pollutedWith('region', 'polluted', async () => [read(target), read(proxy)]))
    expect(seen[0]).toEqual(['org_real', 'kept', 'org_real', 'eu', undefined])
    expect(seen[1]).toEqual(seen[0])
  })

  // Round 6's check. Round 6 silenced a native promise before adopting it: Promise.prototype.then
  // made one more instance of a Promise subclass, through its species, which neither SDK makes.
  // And it adopted a native promise as `await` adopts one everywhere, where SDK 2.x's executor
  // (an async function returning the tool's answer) and both SDKs' dispatch (resolving with a
  // handler's answer) adopt by a resolve function, which runs a native promise's own `then` too.

  /** A Promise subclass whose constructor counts the instances made, and whose `then` hands on how many it saw made when it was called. */
  function counting() {
    const made = { count: 0 }
    class Counting extends Promise<unknown> {
      constructor(executor: (resolve: (value: unknown) => void, reject: (reason: unknown) => void) => void) {
        super(executor)
        made.count++
      }
      override then<A = unknown, B = never>(onFulfilled?: ((value: unknown) => A | PromiseLike<A>) | null, onRejected?: ((reason: unknown) => B | PromiseLike<B>) | null): Promise<A | B> {
        const seen = made.count
        return super.then(onFulfilled && (() => onFulfilled(text(`made ${seen}`))), onRejected)
      }
    }
    return { answer: () => new Counting((resolve) => resolve(text('raw'))), made }
  }

  /**
   * `promise`, a native promise of this realm's Promise, given a `then` of its own that counts its
   * runs and hands on `handOn()(value)`: `handOn` is called as that `then` is, so it can tell when.
   */
  function ownThen(promise: Promise<unknown>, handOn: () => (value: unknown) => unknown, runs = { count: 0 }): Promise<unknown> {
    ;(promise as any).then = function (this: Promise<unknown>, onFulfilled?: (value: unknown) => unknown, onRejected?: (reason: unknown) => unknown) {
      runs.count++
      const adapt = handOn()
      return Promise.prototype.then.call(this, onFulfilled && ((value: unknown) => onFulfilled(adapt(value))), onRejected)
    }
    return promise
  }

  it.each(LEVELS)("makes a tool's Promise subclass's instances as often as bare, its then seeing as many made (SDK %d.x, %s level)", async (sdk, level) => {
    const lsquare = setup()
    const bare = counting()
    const bareReply = await (await stdio(sdk, answering(sdk, level, bare.answer))).request('tools/call', { name: 't', arguments: {} })
    const wrapped = counting()
    const wrappedReply = await (await stdio(sdk, answering(sdk, level, wrapped.answer, lsquare))).request('tools/call', { name: 't', arguments: {} })
    await lsquare.client.flush({ timeoutMs: 2_000 })
    // The tool's own instance, then the one its then makes through the species, as await makes it.
    expect([bareReply.result, bare.made.count]).toEqual([text('made 1'), 2])
    expect([wrappedReply.result, wrapped.made.count]).toEqual([bareReply.result, bare.made.count])
    expect(lsquare.calls()).toMatchObject([{ status: 'completed', result: { content: [{ text: 'made 1' }] } }])
  })

  it.each(LEVELS)("gives the client what a tool's native promise with a then of its own hands on, as bare: SDK 2.x's executor runs that then, 1.x's await and both SDKs' low-level dispatch don't (SDK %d.x, %s level)", async (sdk, level) => {
    const lsquare = setup()
    const runs = { bare: { count: 0 }, wrapped: { count: 0 } }
    const answer = (counted: { count: number }) => () => ownThen(Promise.resolve(text('raw')), () => () => text('adapted'), counted)
    const bare = await (await stdio(sdk, answering(sdk, level, answer(runs.bare)))).request('tools/call', { name: 't', arguments: {} })
    const wrapped = await (await stdio(sdk, answering(sdk, level, answer(runs.wrapped), lsquare))).request('tools/call', { name: 't', arguments: {} })
    await lsquare.client.flush({ timeoutMs: 2_000 })
    const runsThen = sdk === 2 && level === 'high'
    expect([bare.result, runs.bare.count]).toEqual([text(runsThen ? 'adapted' : 'raw'), runsThen ? 1 : 0])
    expect([wrapped.result, runs.wrapped.count]).toEqual([bare.result, runs.bare.count])
    expect(lsquare.calls()).toMatchObject([{ status: 'completed', result: { content: [{ text: runsThen ? 'adapted' : 'raw' }] } }])
  })

  it.each(LEVELS)("gives the client what a tool's native promise hands on as bare, its constructor getter answering Promise, then Object: SDK 1.x's await Promise.resolve() reads it to adopt, 2.x's executor doesn't (SDK %d.x, %s level)", async (sdk, level) => {
    // Round 6 matched bare here only because its silence() made the first read. Changing getters
    // are (d): what matters is that the wrapper runs what the SDK runs, so it reads what it reads.
    const reads = { bare: { count: 0 }, wrapped: { count: 0 } }
    const answer = (counted: { count: number }) => () => {
      const promise = ownThen(Promise.resolve(text('raw')), () => () => text('adapted'))
      Object.defineProperty(promise, 'constructor', { get: () => (counted.count++ === 0 ? Promise : Object) })
      return promise
    }
    const lsquare = setup()
    const bare = await (await stdio(sdk, answering(sdk, level, answer(reads.bare)))).request('tools/call', { name: 't', arguments: {} })
    const wrapped = await (await stdio(sdk, answering(sdk, level, answer(reads.wrapped), lsquare))).request('tools/call', { name: 't', arguments: {} })
    await lsquare.client.flush({ timeoutMs: 2_000 })
    expect(bare.result).toEqual(text('adapted'))
    expect([wrapped.result, reads.wrapped.count]).toEqual([bare.result, reads.bare.count])
  })

  it.each([1, 2] as const)("answers initialize as bare where its handler answers with a native promise with a then of its own, adopted as the SDK's dispatch adopts it (SDK %d.x)", async (sdk) => {
    const make = (lsquare?: ReturnType<typeof setup>) => {
      const server: any = sdk === 1 ? new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } }) : new Server2({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
      // The SDK's own answer, a native promise, given a then of its own that renames the server.
      server.setRequestHandler(sdk === 1 ? InitializeRequestSchema : 'initialize', (request: unknown) =>
        ownThen(Promise.resolve(server._oninitialize(request)), () => (value) => ({ ...(value as object), serverInfo: { name: 'adapted', version: '1.0.0' } })),
      )
      server.setRequestHandler(sdk === 1 ? ListToolsRequestSchema : 'tools/list', () => ({ tools: [{ name: 't', inputSchema: { type: 'object' } }] }))
      server.setRequestHandler(sdk === 1 ? CallToolRequestSchema : 'tools/call', async () => text('rows'))
      if (lsquare) instrument(server, lsquare.client, { extras: false })
      return server
    }
    const bare = (await stdio(sdk, make())).initialized
    // 1.x's dispatch resolves with the handler's promise, as setRequestHandler's Promise.resolve()
    // hands it back; 2.x's awaits it first.
    expect(bare.result.serverInfo.name).toBe(sdk === 1 ? 'adapted' : 'orders')
    const wrapped = (await stdio(sdk, make(setup()))).initialized
    expect(wrapped.result).toEqual(bare.result)
  })

  it.each([1, 2] as const)("lists what a low-level tools/list handler's native promise with a then of its own hands on, that then run in a job as the dispatch runs it, the snapshot read so too (SDK %d.x)", async (sdk) => {
    const make = (lsquare?: ReturnType<typeof setup>) => {
      const server: any = sdk === 1 ? new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } }) : new Server2({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
      const page = (name: string) => ({ tools: [{ name, inputSchema: { type: 'object', properties: { q: { type: 'string' } } } }] })
      server.setRequestHandler(sdk === 1 ? ListToolsRequestSchema : 'tools/list', () => {
        let seen = 'at once'
        queueMicrotask(() => (seen = 'in a job'))
        return ownThen(Promise.resolve(page('raw')), () => {
          const at = seen
          return () => page(at)
        })
      })
      server.setRequestHandler(sdk === 1 ? CallToolRequestSchema : 'tools/call', async () => text('rows'))
      if (lsquare) instrument(server, lsquare.client, { extras: { sessions: true, intent: true, missingTools: false } })
      return server
    }
    const tools = async (server: any) => (await (await stdio(sdk, server)).request('tools/list')).result.tools as Array<{ name: string; inputSchema: { properties: Record<string, unknown> } }>
    const bare = await tools(make())
    // 1.x's dispatch resolves with the handler's promise; 2.x's awaits it first.
    expect(bare.map((tool) => tool.name)).toEqual([sdk === 1 ? 'in a job' : 'raw'])
    const wrapped = await tools(make(setup()))
    expect(wrapped.map((tool) => tool.name)).toEqual(bare.map((tool) => tool.name))
    // The snapshot, read from the same handler, agrees with the page: the extras' inputs are added.
    expect(Object.keys(wrapped[0]!.inputSchema.properties)).toEqual(['q', 'session_id', 'intent'])
  })

  it.each([1, 2] as const)("lists what a high-level server's own tools/list handler, set in McpServer's place, hands on through a native promise's then of its own, in a job as the dispatch runs it (SDK %d.x)", async (sdk) => {
    const make = (lsquare?: ReturnType<typeof setup>) => {
      const server = high(sdk)
      if (lsquare) instrument(server, lsquare.client, { extras: { sessions: true, intent: true, missingTools: false } })
      server.registerTool('t', {}, async () => text('rows'))
      server.server.setRequestHandler(sdk === 1 ? ListToolsRequestSchema : 'tools/list', () => {
        let seen = 'at once'
        queueMicrotask(() => (seen = 'in a job'))
        return ownThen(Promise.resolve({ tools: [{ name: 'raw', inputSchema: { type: 'object' } }] }), () => {
          const at = seen
          return () => ({ tools: [{ name: at, inputSchema: { type: 'object' } }] })
        })
      })
      return server
    }
    const names = async (server: any) => ((await (await stdio(sdk, server)).request('tools/list')).result.tools as Array<{ name: string }>).map((tool) => tool.name)
    const bare = await names(make())
    // 1.x's dispatch resolves with the handler's promise; 2.x's awaits it first.
    expect(bare).toEqual([sdk === 1 ? 'in a job' : 'raw'])
    expect(await names(make(setup()))).toEqual(bare)
  })

  it.each([1, 2] as const)("follows what identify answers, a native promise with a then of its own, as await does: that then never runs (SDK %d.x)", async (sdk) => {
    const runs = { count: 0 }
    const answer = () => ownThen(Promise.resolve({ userId: 'raw' }), () => () => ({ userId: 'adapted' }), runs)
    const lsquare = setup()
    const server = instrument(high(sdk), lsquare.client, { extras: false, identify: answer as never })
    server.registerTool('t', {}, async () => text('rows'))
    await (await stdio(sdk, server)).request('tools/call', { name: 't', arguments: {} })
    await lsquare.client.flush({ timeoutMs: 2_000 })
    expect(runs.count).toBe(0)
    expect(lsquare.calls()).toMatchObject([{ userId: 'raw' }])
    // As `await` gives it: Promise.resolve() hands the promise back as it is, and its own then is skipped.
    expect(await answer()).toEqual({ userId: 'raw' })
  })
})

describe("Codex's seventh review: what the app's code answers is adopted where that code ran", () => {
  // Round 6 adopted what identify answered, and what a low-level tools/list handler answered the
  // snapshot's read with, once their callback had been left, and the tool's own answer outside
  // its run: the `then` that runs, in a job, is that code's own, yet a capture() there landed in
  // the call (as its agent part, or a turn of it) where one from the code itself doesn't.
  const LEVELS = ([1, 2] as const).flatMap((sdk) => (['high', 'low'] as const).map((level) => [sdk, level] as const))
  /** `value`, settled by the `then` of a thenable, or of a Promise subclass's instance, that runs `side()` first; or, `itself`, `value` after `side()`. */
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
  /** What the client was sent: each call's agent part's question, and each turn's question and whether it is linked to a call. */
  const sent = (lsquare: ReturnType<typeof setup>) => ({
    agents: lsquare.calls().map((call) => call.agent?.question),
    turns: lsquare.turns().map((turn) => [turn.question, turn.callId !== undefined]),
  })
  const lowServer = (sdk: 1 | 2): any => (sdk === 1 ? new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } }) : new Server2({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } }))

  it.each(LEVELS.flatMap(([sdk, level]) => ANSWERS.map(([how, answer]) => [sdk, level, how, answer] as const)))(
    "records a capture() from the then of what identify answers as a turn of its own, as one from identify itself, never the call's agent part (SDK %d.x, %s level, answering %s)",
    async (sdk, level, _how, answer) => {
      // At the low level identify starts beside the handler, outside the call: there a control.
      const lsquare = setup()
      const identify = () => answer({ userId: 'u' }, () => lsquare.client.capture({ question: 'from identify' })) as never
      const tool = async () => {
        await sleep(5)
        lsquare.client.capture({ question: 'from the tool' })
        return text('ok')
      }
      let server: any
      if (level === 'high') {
        server = instrument(high(sdk), lsquare.client, { extras: false, identify })
        server.registerTool('t', {}, tool)
      } else {
        server = lowServer(sdk)
        server.setRequestHandler(sdk === 1 ? ListToolsRequestSchema : 'tools/list', () => ({ tools: [{ name: 't', inputSchema: { type: 'object' } }] }))
        server.setRequestHandler(sdk === 1 ? CallToolRequestSchema : 'tools/call', tool)
        instrument(server, lsquare.client, { extras: false, identify })
      }
      expect((await (await stdio(sdk, server)).request('tools/call', { name: 't', arguments: {} })).result).toEqual(text('ok'))
      await lsquare.client.flush({ timeoutMs: 2_000 })
      expect(sent(lsquare)).toEqual({ agents: ['from the tool'], turns: [['from identify', false]] })
      expect(lsquare.calls()).toMatchObject([{ userId: 'u' }])
    },
  )

  it.each([1, 2] as const)(
    "records a capture() from the own then of the native promise a low-level tools/list handler answers the snapshot's read with as a turn of its own, never one of the call the read was made from (SDK %d.x)",
    async (sdk) => {
      // A tool of a server on stdio lists the tools of another, low-level and instrumented too,
      // in-process: the page's handler runs inside the outer call, as any code the tool calls; the
      // snapshot's runs as a callback. SDK 1.x's dispatch, and so the snapshot, runs a native
      // promise's own then; 2.x's stored handler awaits the promise first, so it never runs: a control.
      const lsquare = setup()
      const inner = lowServer(sdk)
      let listed = 0
      inner.setRequestHandler(sdk === 1 ? ListToolsRequestSchema : 'tools/list', () => {
        const promise = Promise.resolve({ tools: [{ name: 'x', inputSchema: { type: 'object', properties: { q: { type: 'string' } } } }] })
        // The page first, as the dispatch calls the handler; then the snapshot's read.
        if (++listed > 1) {
          ;(promise as any).then = function (this: Promise<unknown>, onFulfilled?: (value: unknown) => unknown, onRejected?: (reason: unknown) => unknown) {
            lsquare.client.capture({ question: 'from the snapshot' })
            return Promise.prototype.then.call(this, onFulfilled, onRejected)
          }
        }
        return promise
      })
      inner.setRequestHandler(sdk === 1 ? CallToolRequestSchema : 'tools/call', async () => text('inner'))
      instrument(inner, lsquare.client, { extras: { sessions: true, intent: true, missingTools: false } })
      const connection = await connect(sdk, inner)
      const outer = instrument(high(sdk), lsquare.client, { extras: false })
      outer.registerTool('t', {}, async () => {
        const tools = await connection.tools()
        await sleep(5)
        return text(Object.keys(tools[0]!.inputSchema.properties ?? {}).join(' '))
      })
      // The snapshot was read: the listed tool has the extras' inputs.
      expect((await (await stdio(sdk, outer)).request('tools/call', { name: 't', arguments: {} })).result).toEqual(text('q session_id intent'))
      await lsquare.client.flush({ timeoutMs: 2_000 })
      expect(listed).toBe(2)
      expect(sent(lsquare)).toEqual({ agents: [undefined], turns: sdk === 1 ? [['from the snapshot', false]] : [] })
    },
  )

  it.each([1, 2] as const)(
    "records a capture() that the then of a tool's thenable makes after it answered as a later turn of the call, as one the tool's own function makes after answering (SDK %d.x, high level)",
    async (sdk) => {
      // Two microtasks after the answer: past the run's capture() boundary, before the call's end.
      const run = async (answer: (later: () => void) => unknown) => {
        const lsquare = setup()
        const server = instrument(high(sdk), lsquare.client, { extras: false })
        const later = () => void Promise.resolve().then(() => {}).then(() => lsquare.client.capture({ question: 'after answering' }))
        server.registerTool('t', {}, () => answer(later))
        expect((await (await stdio(sdk, server)).request('tools/call', { name: 't', arguments: {} })).result).toEqual(text('ok'))
        await lsquare.client.flush({ timeoutMs: 2_000 })
        return sent(lsquare)
      }
      const itself = await run((later) => (later(), text('ok')))
      expect(itself).toEqual({ agents: [undefined], turns: [['after answering', true]] })
      expect(await run((later) => ({ then: (resolve: (value: unknown) => void) => (resolve(text('ok')), later()) }))).toEqual(itself)
    },
  )
})

describe('JSON in a text block, as a real server sends it', () => {
  it.each([1, 2] as const)('hides what is under secret key names, and the client still gets it (SDK %d.x)', async (sdk) => {
    const lsquare = setup()
    const server = instrument(high(sdk), lsquare.client, { extras: OFF })
    const row = { db_password: 'hunter2', plan: 'pro' }
    server.registerTool('t', {}, async () => ({ content: [{ type: 'text' as const, text: JSON.stringify(row, null, 2) }], structuredContent: row }))
    const connection = await connect(sdk, server)
    const reply = await connection.call('t')
    await lsquare.client.flush({ timeoutMs: 2_000 })
    expect(JSON.stringify(reply)).toContain('hunter2')
    const [event] = lsquare.calls()
    expect(event?.result).toEqual({
      content: [{ type: 'text', text: '{\n  "db_password": "[SECRET_1]",\n  "plan": "pro"\n}' }],
      structuredContent: { db_password: '[SECRET_1]', plan: 'pro' },
    })
    expect(event?.redactions).toEqual({ secret: 2 })
  })
})
