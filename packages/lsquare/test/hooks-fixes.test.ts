// TypeScript's side of the hook lifecycle fixes of the deep review's last Python and parity findings,
// 31 October 2026 (packages/python/tests/hooks_fixes_test.py and
// packages/python/tests/wrapper/hooks_fixes_wrapper_test.py). parity-25 is a fix in both SDKs:
// identify isn't called once the client is shut down, and a call's identify_failed is reported only
// for an event the client admits. The rest pin what TypeScript already does.
import { Client as Client1 } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport as Memory1 } from '@modelcontextprotocol/sdk/inMemory.js'
import { McpServer as McpServer1 } from '@modelcontextprotocol/sdk/server/mcp.js'
import { Client as Client2 } from '@modelcontextprotocol/client'
import { McpServer as McpServer2, InMemoryTransport as Memory2 } from '@modelcontextprotocol/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { z } from 'zod'
import { INTERNAL } from '../src/client.js'
import { LSquare, type LSquareError, type LSquareEvent, type ToolCallEvent } from '../src/index.js'
import { instrument, type InstrumentOptions } from '../src/mcp/index.js'

type Tool = (args: Record<string, unknown>, extra: unknown) => unknown
const OFF = { sessions: false, intent: false, missingTools: false }
const text = (value: string) => ({ content: [{ type: 'text' as const, text: value }] })
/** onError and loggers run in a later event-loop turn (setImmediate): let them run. */
const tick = () => new Promise<void>((resolve) => setImmediate(() => setImmediate(resolve)))

const sdks = [
  {
    name: 'SDK 1.x',
    high(tools: Record<string, Tool>) {
      const server = new McpServer1({ name: 'orders', version: '1.0.0' })
      for (const [name, tool] of Object.entries(tools)) server.registerTool(name, { inputSchema: { q: z.string().optional() } }, tool as never)
      return server
    },
    async connect(server: any) {
      const [a, b] = Memory1.createLinkedPair()
      await server.connect(a)
      const client = new Client1({ name: 'claude-code', version: '2.1' })
      await client.connect(b)
      return { call: (name: string, args: Record<string, unknown> = {}) => client.callTool({ name, arguments: args }) }
    },
  },
  {
    name: 'SDK 2.x',
    high(tools: Record<string, Tool>) {
      const server = new McpServer2({ name: 'orders', version: '1.0.0' })
      for (const [name, tool] of Object.entries(tools)) server.registerTool(name, { inputSchema: z.object({ q: z.string().optional() }) }, tool as never)
      return server
    },
    async connect(server: any) {
      const [a, b] = Memory2.createLinkedPair()
      await server.connect(a)
      const client = new Client2({ name: 'claude-code', version: '2.1' })
      await client.connect(b)
      return { call: (name: string, args: Record<string, unknown> = {}) => client.callTool({ name, arguments: args }) }
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
  const client = new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test', fetch, onError: (error) => errors.push(error), logger: { info() {}, warn() {} } })
  const calls = () => events.filter((e): e is ToolCallEvent => e.kind === 'tool_call')
  return { client, events, errors, calls }
}

/** Lets the client's later turns run until `check()` holds (two seconds at most). */
async function until(check: () => boolean): Promise<boolean> {
  for (let i = 0; i < 400 && !check(); i++) await tick()
  return check()
}

describe.each(sdks)('parity-25 on $name: identify_failed only for a call the client admits', (sdk) => {
  async function serve(tools: Record<string, Tool>, options: InstrumentOptions, lsquare: ReturnType<typeof setup>) {
    const server = sdk.high(tools)
    instrument(server, lsquare.client, options)
    return sdk.connect(server)
  }

  it("doesn't call identify after shutdown(): only the shutdown is reported", async () => {
    const lsquare = setup()
    const called: string[] = []
    const connection = await serve(
      { search: async () => text('ok') },
      {
        extras: OFF,
        identify: ({ toolName }) => {
          called.push(toolName)
          return { userId: 'u_1' }
        },
      },
      lsquare,
    )
    await connection.call('search')
    await lsquare.client.shutdown()
    for (let i = 0; i < 3; i++) await connection.call('search')
    await tick()
    expect(called).toEqual(['search'])
    expect(lsquare.errors.map((error) => error.code)).toEqual(['shutdown'])
    expect(lsquare.calls()).toHaveLength(1)
  })

  it('reports only the shutdown for a call in flight then, whose identify fails after it', async () => {
    const lsquare = setup()
    let fail!: (error: Error) => void
    const connection = await serve(
      { search: async () => text('ok') },
      { extras: OFF, identify: () => new Promise((_resolve, reject) => (fail = reject)) },
      lsquare,
    )
    await connection.call('search')
    const result = await lsquare.client.shutdown({ timeoutMs: 0 })
    expect(result.failed).toBe(1)
    fail(new Error('down'))
    expect(await until(() => lsquare.errors.length > 0)).toBe(true)
    await tick()
    expect(lsquare.errors.map((error) => error.code)).toEqual(['shutdown'])
    expect(lsquare.calls()).toEqual([])
  })

  it('still reports a genuine failure for a call the client admits (guard)', async () => {
    const lsquare = setup()
    const connection = await serve(
      { search: async () => text('ok') },
      {
        extras: OFF,
        identify: () => {
          throw new Error('down')
        },
      },
      lsquare,
    )
    await connection.call('search')
    await lsquare.client.flush()
    await tick()
    expect(lsquare.errors.map((error) => error.message)).toEqual(['identify threw; the call was sent as limited diagnostics.'])
    expect(lsquare.calls()).toHaveLength(1)
  })

  it('reports it itself for a client from another copy of lsquare, whose record() takes no diagnostic (guard)', async () => {
    const lsquare = setup()
    // As an older client's Internal: no closed().
    delete (lsquare.client as any)[INTERNAL].closed
    const connection = await serve(
      { search: async () => text('ok') },
      {
        extras: OFF,
        identify: () => {
          throw new Error('down')
        },
      },
      lsquare,
    )
    await connection.call('search')
    await lsquare.client.flush()
    await tick()
    expect(lsquare.errors.map((error) => error.code)).toEqual(['identify_failed'])
    expect(lsquare.calls()).toHaveLength(1)
  })
})

describe('the client reports a call diagnostic as it admits the event (parity-25)', () => {
  const record = (callId: string) => ({
    callId,
    capturedAt: '2026-10-31T00:00:00.000Z',
    toolName: 'search',
    status: 'completed',
    durationMs: 1,
    ids: { userId: 'u_1' },
    diagnostics: false,
  })

  it('reports it before the event, once admitted, and never once shut down', async () => {
    const lsquare = setup()
    const internal = (lsquare.client as any)[INTERNAL]
    internal.record(record('11111111-1111-4111-8111-111111111111'), { code: 'identify_failed', message: 'identify threw; the call was sent as limited diagnostics.' })
    await lsquare.client.flush()
    await lsquare.client.shutdown()
    internal.record(record('22222222-2222-4222-8222-222222222222'), { code: 'identify_failed', message: 'identify threw; the call was sent as limited diagnostics.' })
    await tick()
    expect(lsquare.errors.map((error) => error.code)).toEqual(['identify_failed', 'shutdown'])
    expect(lsquare.calls().map((call) => call.callId)).toEqual(['11111111-1111-4111-8111-111111111111'])
  })

  it('never reports it for an event a shutdown() refuses as it is built, from the app code that building runs', async () => {
    const lsquare = setup()
    const internal = (lsquare.client as any)[INTERNAL]
    const value = {
      get q() {
        void lsquare.client.shutdown()
        return 'x'
      },
    }
    internal.record({ ...record('33333333-3333-4333-8333-333333333333'), input: { value } }, { code: 'identify_failed', message: 'identify threw; the call was sent as limited diagnostics.' })
    await tick()
    expect(lsquare.errors.map((error) => error.code)).toEqual(['shutdown'])
    expect(lsquare.calls()).toEqual([])
  })

  // Codex's first review of these fixes: the build's failure branches reported what the call said of
  // identify, and the failure, with no look at a shutdown() the build's app code made.
  const failing = {
    throws: (shut: () => void) => ({
      get userId() {
        shut()
        throw new Error('down')
      },
    }),
    invalid: (shut: () => void) => ({
      get userId() {
        shut()
        return 'x'.repeat(600)
      },
    }),
  }

  it.each(['throws', 'invalid'] as const)('reports only the shutdown for an event whose build fails after a getter shut the client down (%s)', async (kind) => {
    const lsquare = setup()
    const internal = (lsquare.client as any)[INTERNAL]
    const ids = failing[kind](() => void lsquare.client.shutdown())
    internal.record({ ...record('44444444-4444-4444-8444-444444444444'), ids }, { code: 'identify_failed', message: 'identify threw; the call was sent as limited diagnostics.' })
    await tick()
    expect(lsquare.errors.map((error) => error.code)).toEqual(['shutdown'])
    expect(lsquare.calls()).toEqual([])
  })

  it.each([
    ['throws', 'redaction_failed'],
    ['invalid', 'invalid_id'],
  ] as const)('still reports the diagnostic, then the failure, for an event whose build fails while the client is open (%s, guard)', async (kind, code) => {
    const lsquare = setup()
    const internal = (lsquare.client as any)[INTERNAL]
    const ids = failing[kind](() => {})
    internal.record({ ...record('55555555-5555-4555-8555-555555555555'), ids }, { code: 'identify_failed', message: 'identify threw; the call was sent as limited diagnostics.' })
    await tick()
    expect(lsquare.errors.map((error) => error.code)).toEqual(['identify_failed', code])
    expect(lsquare.calls()).toEqual([])
    await lsquare.client.shutdown()
  })
})

describe('parity-5: a hook slot is given back once, by the hook that holds it (guard)', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('on Node, a beforeSend past its deadline keeps its slot until it settles, then the next starts, and nothing is released twice', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'performance'] })
    const started: string[] = []
    const settle = new Map<string, (out: null) => void>()
    const codes: string[] = []
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      maxConcurrentHooks: 1,
      beforeSendTimeoutMs: 100,
      fetch: (async () => new Response('{"accepted":[]}')) as unknown as typeof fetch,
      logger: { info() {}, warn() {} },
      onError: (error) => void codes.push(error.code),
      beforeSend: (event) => {
        const id = String(event.conversationId)
        started.push(id)
        return new Promise((resolve) => settle.set(id, resolve))
      },
    })
    const turn = (conversationId: string) => ({ conversationId, turnId: 't', orgId: 'o', toolName: 't', question: 'q' })
    client.capture(turn('stuck'))
    await vi.advanceTimersByTimeAsync(10)
    client.capture(turn('next'))
    // Past both deadlines: the stuck one failed, its hook still running, holding the one slot.
    await vi.advanceTimersByTimeAsync(200)
    expect(started).toEqual(['stuck'])
    expect(codes).toEqual(['hook_timeout', 'hook_timeout'])
    settle.get('stuck')!(null)
    await vi.advanceTimersByTimeAsync(10)
    client.capture(turn('one'))
    client.capture(turn('two'))
    await vi.advanceTimersByTimeAsync(10)
    // One slot, released once: one hook at a time still.
    expect(started).toEqual(['stuck', 'one'])
    settle.get('one')!(null)
    await vi.advanceTimersByTimeAsync(10)
    expect(started).toEqual(['stuck', 'one', 'two'])
  })
})
