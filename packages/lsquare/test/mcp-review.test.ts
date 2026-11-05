// Regression tests for Codex's review of lsquare/mcp's first step, 28 October: one describe per
// finding, numbered as in the review, then the coverage it asked for.
import { PassThrough } from 'node:stream'
import { Client as Client1 } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport as Memory1 } from '@modelcontextprotocol/sdk/inMemory.js'
import { McpServer as McpServer1 } from '@modelcontextprotocol/sdk/server/mcp.js'
import { StdioServerTransport as Stdio1 } from '@modelcontextprotocol/sdk/server/stdio.js'
import { CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { Client as Client2 } from '@modelcontextprotocol/client'
import { McpServer as McpServer2, InMemoryTransport as Memory2 } from '@modelcontextprotocol/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { z } from 'zod'
import { validateEvent } from '../../contract/src/validate.js'
import { INTERNAL, type Internal } from '../src/client.js'
import { buildCall, type CallRecord } from '../src/event.js'
import { LSquare, type LSquareError, type LSquareEvent, type LSquareOptions, type ToolCallEvent, type TurnEvent } from '../src/index.js'
import { instrument, type InstrumentOptions } from '../src/mcp/index.js'
import { resolvePatterns } from '../src/redact.js'

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const tick = () => new Promise<void>((resolve) => setImmediate(() => setImmediate(resolve)))
const text = (value: string) => ({ content: [{ type: 'text' as const, text: value }] })
const json = (body: unknown, status = 200, headers: Record<string, string> = {}) => new Response(JSON.stringify(body), { status, headers })
/** The extras turned off, for tests of what the wrapper records: their replies stay exactly the tool's. */
const OFF = { sessions: false, intent: false, missingTools: false }

const rejected: string[] = []
afterEach(() => {
  expect(rejected).toEqual([])
  rejected.length = 0
})

type Reply = (events: LSquareEvent[]) => Response | Promise<Response>
const ack: Reply = (events) => json({ accepted: events.map((e) => e.eventId), duplicates: [], rejected: [] })

function setup(options: LSquareOptions = {}, reply: Reply = ack) {
  const events: LSquareEvent[] = []
  const errors: LSquareError[] = []
  const fetch = (async (_url: unknown, init?: RequestInit) => {
    const batch = JSON.parse(String(init?.body)).events as LSquareEvent[]
    for (const event of batch) {
      const verdict = validateEvent(event)
      if (!verdict.ok) rejected.push(`${event.kind}: ${verdict.reason}`)
    }
    events.push(...batch)
    return reply(batch)
  }) as typeof globalThis.fetch
  const client = new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test', fetch, onError: (e) => errors.push(e), logger: { info() {}, warn() {} }, ...options })
  const calls = () => events.filter((e): e is ToolCallEvent => e.kind === 'tool_call')
  const turns = () => events.filter((e): e is TurnEvent => e.kind === 'turn')
  return { client, events, calls, turns, errors, internal: client[INTERNAL] }
}

async function connect1(server: any, name = 'claude-code') {
  const [a, b] = Memory1.createLinkedPair()
  await server.connect(a)
  const client = new Client1({ name, version: '2.1' })
  await client.connect(b)
  return { call: (tool: string, args: Record<string, unknown> = {}) => client.callTool({ name: tool, arguments: args }), client }
}
async function connect2(server: any) {
  const [a, b] = Memory2.createLinkedPair()
  await server.connect(a)
  const client = new Client2({ name: 'claude-code', version: '2.1' })
  await client.connect(b)
  return { call: (tool: string, args: Record<string, unknown> = {}) => client.callTool({ name: tool, arguments: args }), client }
}

/** A server of the low-level shape the wrapper recognises, driven directly: for what in-memory clients can't send. */
function fakeServer(handler: (request: any, extra: any) => unknown) {
  const handlers = new Map<string, (request: any, extra: any) => unknown>()
  const server = {
    _requestHandlers: handlers,
    _serverInfo: { name: 'fake', version: '1' },
    client: { name: 'first', version: '1' } as unknown,
    getClientVersion() {
      return this.client
    },
    setRequestHandler(method: string, fn: (request: any, extra: any) => unknown) {
      handlers.set(method, fn)
    },
  }
  server.setRequestHandler('tools/call', handler)
  const call = (name: string, extra: Record<string, unknown> = {}, args: unknown = {}) =>
    Promise.resolve(handlers.get('tools/call')!({ method: 'tools/call', params: { name, arguments: args } }, extra))
  return { server, call }
}

function unhandledDuring() {
  const seen: unknown[] = []
  const listener = (reason: unknown) => seen.push(reason)
  process.on('unhandledRejection', listener)
  return { seen, stop: () => process.off('unhandledRejection', listener) }
}

describe('1. callbacks that reject', () => {
  it('never become unhandled rejections: waitUntil, and identify fields that are promises', async () => {
    const watch = unhandledDuring()
    try {
      const lsquare = setup()
      const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client, {
        waitUntil: async () => {
          throw new Error('host refused')
        },
        identify: () => ({ userId: Promise.reject(new Error('lookup')) as unknown as string }),
        extras: OFF,
      })
      server.registerTool('t', {}, async () => text('ok'))
      const connection = await connect1(server)
      expect(await connection.call('t')).toEqual(text('ok'))
      await sleep(30)
      await lsquare.client.flush()
      expect(lsquare.calls()[0]).toMatchObject({ identity: 'failed' })
    } finally {
      watch.stop()
    }
    expect(watch.seen).toEqual([])
  })
})

describe('2. the client failing inside', () => {
  it('never stops the tool, the close, or the reply past its deadline', async () => {
    const lsquare = setup({ flushTimeoutMs: 200 })
    const internal = lsquare.internal as Internal & Record<string, unknown>
    const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client, { flushBeforeReply: true, extras: OFF })
    server.registerTool('t', {}, async () => text('ran'))
    const connection = await connect1(server)
    // Sending never settles: the reply still comes within flushTimeoutMs.
    internal.settled = () => new Promise(() => {})
    let started = performance.now()
    expect(await connection.call('t')).toEqual(text('ran'))
    expect(performance.now() - started).toBeLessThan(600)
    // Call tracking fails: the tool runs anyway, once.
    internal.calls = () => {
      throw new Error('broken')
    }
    started = performance.now()
    expect(await connection.call('t')).toEqual(text('ran'))
    // Flushing throws on a stdio server's close: the close goes on.
    const stdio = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client, { extras: OFF })
    await stdio.connect(new Stdio1(new PassThrough(), new PassThrough()))
    internal.flush = () => {
      throw new Error('broken')
    }
    await expect(stdio.close()).resolves.toBeUndefined()
  })
})

describe('a client from another version of lsquare', () => {
  it.each(['clock', 'delivered'])('without %s, leaves the server as it is, with one warning, and its calls go through', async (missing) => {
    const lsquare = setup()
    // Another copy of lsquare, as a bundle or a second install can bring: its internals lack what this wrapper calls.
    const other = { [INTERNAL]: { ...lsquare.internal, [missing]: undefined } } as unknown as LSquare
    let invoked = 0
    const handler = () => {
      invoked++
      return text('ok')
    }
    const { server, call } = fakeServer(handler)
    const warned = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      expect(instrument(server, other)).toBe(server)
      expect(server._requestHandlers.get('tools/call')).toBe(handler)
      expect(await call('t')).toEqual(text('ok'))
      expect(invoked).toBe(1)
      expect(warned.mock.calls.map(([message]) => message)).toEqual([
        '[lsquare] instrument() needs a client from the same version of lsquare: the server is left as it is.',
      ])
    } finally {
      warned.mockRestore()
    }
  })
})

describe('3. which IDs capture() must supply when identify fails', () => {
  it('are those identify returned last, known as soon as it succeeds', async () => {
    const lsquare = setup()
    let release: () => void = () => {}
    const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client, {
      identify: ({ toolName }) => {
        if (toolName === 'slow') return { orgId: 'org_1', userId: 'u_1' }
        throw new Error('down')
      },
    })
    server.registerTool('slow', {}, () => new Promise((resolve) => (release = () => resolve(text('slow')))))
    server.registerTool('fast', {}, async () => {
      lsquare.client.capture({ userId: 'u_2', question: 'q' })
      return text('fast')
    })
    const connection = await connect1(server)
    const slow = connection.call('slow')
    await sleep(20)
    // slow's identify has succeeded with an org and a person; fast's fails, and its capture() has no org.
    await connection.call('fast')
    release()
    await slow
    await lsquare.client.flush()
    const fast = lsquare.calls().find((e) => e.toolName === 'fast')
    expect(fast).toMatchObject({ identity: 'failed' })
  })
})

describe('4. flush() and shutdown()', () => {
  it('wait for calls that have replied but are still waiting for identify', async () => {
    for (const end of ['flush', 'shutdown'] as const) {
      const lsquare = setup()
      const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client, { identify: () => sleep(200).then(() => ({ userId: 'u' })) })
      server.registerTool('t', {}, async () => text('ok'))
      const connection = await connect1(server)
      await connection.call('t')
      const result = end === 'flush' ? await lsquare.client.flush() : await lsquare.client.shutdown()
      expect(result).toMatchObject({ sent: 1 })
      expect(lsquare.calls()).toHaveLength(1)
    }
  })
})

describe('5. identify', () => {
  it("runs as a callback, outside the call: a capture() from it is never the call's agent part", async () => {
    const lsquare = setup()
    const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client, {
      identify: () => {
        lsquare.client.capture({ question: 'from identify' })
        return { userId: 'u' }
      },
    })
    server.registerTool('t', {}, async () => {
      await sleep(5)
      lsquare.client.capture({ question: 'from the tool' })
      return text('ok')
    })
    const connection = await connect1(server)
    await connection.call('t')
    await lsquare.client.flush()
    expect(lsquare.calls()[0]?.agent).toMatchObject({ question: 'from the tool' })
    expect(lsquare.turns().map((t) => [t.question, t.callId])).toEqual([['from identify', undefined]])
  })
})

describe('6. identify starts', () => {
  it("after the tool's own function has run its first step, on both SDKs", async () => {
    for (const sdk of [1, 2]) {
      const order: string[] = []
      const lsquare = setup()
      const options: InstrumentOptions = { identify: () => (order.push('identify'), {}) }
      if (sdk === 1) {
        const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client, options)
        server.registerTool('t', { inputSchema: { q: z.string().optional() } }, async () => (order.push('tool'), text('ok')))
        await (await connect1(server)).call('t')
      } else {
        const server = instrument(new McpServer2({ name: 's', version: '1' }), lsquare.client, options)
        server.registerTool('t', {}, async () => (order.push('tool'), text('ok')))
        await (await connect2(server)).call('t')
      }
      expect(order).toEqual(['tool', 'identify'])
    }
  })
})

describe('7. error categories', () => {
  it("a task tool's createTask throwing is thrown", async () => {
    const lsquare = setup()
    const high = {
      server: undefined as unknown as ReturnType<typeof fakeServer>['server'],
      registerTool() {},
      _toolHandlersInitialized: true,
      _registeredTools: {
        t: { handler: { createTask: async () => { throw new RangeError('no slots') } } },
      } as Record<string, any>,
    }
    const { server, call } = fakeServer(async (request, extra) => {
      try {
        return await high._registeredTools[request.params.name].handler.createTask(request.params.arguments, extra)
      } catch (error) {
        return { ...text((error as Error).message), isError: true }
      }
    })
    high.server = server
    instrument(high, lsquare.client)
    await call('t')
    await lsquare.client.flush()
    expect(lsquare.calls()[0]?.error).toEqual({ message: 'no slots', type: 'RangeError', category: 'thrown' })
  })

  it('an output the SDK refuses after the tool ran is protocol', async () => {
    const lsquare = setup()
    const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client)
    server.registerTool('t', { outputSchema: { n: z.number() } }, async () => text('no structured content') as never)
    await (await connect1(server)).call('t')
    await lsquare.client.flush()
    expect(lsquare.calls()[0]?.error).toMatchObject({ category: 'protocol', message: expect.stringContaining('Output validation') })
  })

  it("on a server whose tools/call handler the app replaced, a returned isError is the tool's own", async () => {
    const lsquare = setup()
    const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client)
    server.registerTool('t', {}, async () => text('unused'))
    server.server.setRequestHandler(CallToolRequestSchema, async () => ({ ...text('custom failure'), isError: true }))
    await (await connect1(server)).call('t')
    await lsquare.client.flush()
    expect(lsquare.calls()[0]?.error).toEqual({ message: 'custom failure', category: 'tool_error' })
  })
})

describe('8. the capture() boundary', () => {
  it("is the tool's return, not the SDK's end of the call", async () => {
    const lsquare = setup()
    const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client)
    server.registerTool('t', {}, () => {
      queueMicrotask(() => lsquare.client.capture({ question: 'after the return' }))
      return text('ok')
    })
    await (await connect1(server)).call('t')
    await sleep(10)
    await lsquare.client.flush()
    expect(lsquare.calls()[0]).not.toHaveProperty('agent')
    expect(lsquare.turns()[0]).toMatchObject({ question: 'after the return', callId: lsquare.calls()[0]?.callId })
  })
})

describe('9. what is read when', () => {
  it('a cancellation after the reply is no timeout, and the caller is the one when the call arrived', async () => {
    const lsquare = setup()
    const { server, call } = fakeServer(async () => text('ok'))
    instrument(server, lsquare.client, { identify: () => sleep(50).then(() => ({ userId: 'u' })) })
    const controller = new AbortController()
    await call('t', { signal: controller.signal })
    controller.abort()
    server.client = { name: 'second', version: '1' }
    await lsquare.client.flush()
    expect(lsquare.calls()[0]).toMatchObject({ status: 'completed', client: { name: 'first' } })
  })
})

describe('10. waitUntil', () => {
  it('gets a promise that waits for a retry due later, up to its limit', async () => {
    const lsquare = setup({}, () => json({}, 429, { 'retry-after': '120' }))
    const promises: Promise<unknown>[] = []
    const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client, { waitUntil: (p) => promises.push(p) })
    server.registerTool('t', {}, async () => text('ok'))
    await (await connect1(server)).call('t')
    let settled = false
    void promises[0]!.then(() => (settled = true))
    await sleep(150)
    expect(settled).toBe(false)
    await lsquare.client.shutdown({ timeoutMs: 0 })
    await tick()
    expect(settled).toBe(true)
  })
})

describe('11. flush() inside a tool', () => {
  it('sends the event before the reply even with waitUntil', async () => {
    const lsquare = setup()
    // identify takes a while: without the guarantee, the reply would come first.
    const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client, {
      waitUntil: () => {},
      identify: () => sleep(100).then(() => ({ userId: 'u' })),
    })
    server.registerTool('t', {}, async () => {
      await lsquare.client.flush()
      return text('ok')
    })
    await (await connect1(server)).call('t')
    expect(lsquare.calls()).toHaveLength(1)
  })

  /** An ingestion that holds every answer until `release()`. */
  function holding(options: LSquareOptions) {
    let release = () => {}
    const answers = new Promise<void>((resolve) => (release = resolve))
    let answered = 0
    const lsquare = setup(options, async (events) => {
      await answers
      answered++
      return ack(events)
    })
    return { ...lsquare, release, answered: () => answered }
  }

  it('replies within flushTimeoutMs and still hands the rest of the sending to waitUntil', async () => {
    const lsquare = holding({ flushTimeoutMs: 50 })
    const promises: Promise<unknown>[] = []
    const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client, { extras: OFF, waitUntil: (p) => promises.push(p) })
    server.registerTool('t', {}, async () => {
      lsquare.client.capture({ question: 'q' })
      await lsquare.client.flush()
      return text('ok')
    })
    expect(await (await connect1(server)).call('t')).toEqual(text('ok'))
    // The reply gave up waiting at flushTimeoutMs, the ingestion still holding its answer.
    expect(lsquare.answered()).toBe(0)
    expect(promises).toHaveLength(1)
    let settled = false
    void promises[0]!.then(() => (settled = true))
    await tick()
    expect(settled).toBe(false)
    lsquare.release()
    await promises[0]
    expect(lsquare.answered()).toBe(1)
    expect(lsquare.calls()).toMatchObject([{ agent: { question: 'q' } }])
  })

  it('hands the sending to waitUntil when the handler throws after flush() too', async () => {
    const lsquare = holding({ flushTimeoutMs: 50 })
    const promises: Promise<unknown>[] = []
    const { server, call } = fakeServer(async () => {
      lsquare.client.capture({ question: 'q' })
      await lsquare.client.flush()
      throw new Error('boom')
    })
    instrument(server, lsquare.client, { waitUntil: (p) => promises.push(p) })
    await expect(call('t')).rejects.toThrow('boom')
    expect(lsquare.answered()).toBe(0)
    expect(promises).toHaveLength(1)
    lsquare.release()
    await promises[0]
    expect(lsquare.calls()).toMatchObject([{ status: 'error', agent: { question: 'q' } }])
  })

  it.each(['flushBeforeReply', 'flush() inside the tool'])('passes on what the handler threw only once its event is sent, with %s', async (how) => {
    const lsquare = holding({ flushTimeoutMs: 10_000 })
    const { server, call } = fakeServer(async () => {
      if (how !== 'flushBeforeReply') await lsquare.client.flush()
      throw new Error('boom')
    })
    instrument(server, lsquare.client, how === 'flushBeforeReply' ? { flushBeforeReply: true, extras: OFF } : { extras: OFF })
    let failed = false
    const failing = call('t').catch((error: Error) => {
      failed = true
      return error
    })
    for (let i = 0; i < 100 && lsquare.calls().length === 0; i++) await tick()
    // Posted, the ingestion holding its answer: the error isn't passed on yet.
    expect(lsquare.calls()).toMatchObject([{ status: 'error' }])
    await tick()
    expect(failed).toBe(false)
    lsquare.release()
    expect(await failing).toMatchObject({ message: 'boom' })
    expect(lsquare.answered()).toBe(1)
  })
})

describe('12. protocolVersion on SDK 1.x', () => {
  it('comes from the initialize the client and server negotiated', async () => {
    const lsquare = setup()
    const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client)
    server.registerTool('t', {}, async () => text('ok'))
    await (await connect1(server)).call('t')
    await lsquare.client.flush()
    expect(lsquare.calls()[0]?.protocolVersion).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  })
})

describe('13 and coverage: rounds, tasks and the 2026 envelope', () => {
  it('a continuation round keeps its round when it fails; input_required and task results are marked', async () => {
    const lsquare = setup()
    const { server, call } = fakeServer(async (request) => {
      if (request.params.name === 'boom') throw new Error('round failed')
      if (request.params.name === 'ask') return { resultType: 'input_required', inputRequests: { name: { method: 'elicitation/create' } } }
      return { task: { taskId: 'task_1', status: 'working' } }
    })
    instrument(server, lsquare.client)
    const envelope = { 'io.modelcontextprotocol/clientInfo': { name: 'modern', version: '3' }, 'io.modelcontextprotocol/protocolVersion': '2026-07-28' }
    await call('boom', { mcpReq: { inputResponses: { name: { action: 'accept' } }, envelope } }).catch(() => {})
    await call('ask', { mcpReq: { envelope } })
    await call('job', { mcpReq: { envelope } })
    await lsquare.client.flush()
    const [boom, ask, job] = lsquare.calls()
    expect(boom).toMatchObject({ round: 'continuation', status: 'error', client: { name: 'modern' }, protocolVersion: '2026-07-28' })
    expect(ask).toMatchObject({ round: 'input_required', status: 'completed' })
    expect(job).toMatchObject({ task: { id: 'task_1', status: 'working' } })
    expect(job).not.toHaveProperty('result')
  })
})

describe('14. metadata from identify and capture()', () => {
  it('counts the merged result against the budget, not the values capture() replaced', () => {
    const big = 'x'.repeat(150_000)
    const record: CallRecord = {
      callId: 'c', capturedAt: '2026-10-28T00:00:00.000Z', toolName: 't', status: 'completed', durationMs: 1, ids: {}, diagnostics: false,
      metadata: { note: big }, agent: { metadata: { note: big } },
    }
    const built = buildCall(record, { sdk: { name: 'lsquare', version: '0.2.0', language: 'js' }, patterns: resolvePatterns(undefined), redactKeys: [], maxEventBytes: 256 * 1024 }, false)
    expect(built.ok).toBe(true)
  })
})

describe('15. turnRef', () => {
  it('comes only from a turnId the event keeps', async () => {
    const lsquare = setup()
    const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client)
    server.registerTool('t', {}, async () => {
      lsquare.client.capture({ conversationId: 'c', turnId: 'x'.repeat(300_000), question: 'q' })
      return text('ok')
    })
    await (await connect1(server)).call('t')
    await lsquare.client.flush()
    expect(lsquare.calls()[0]).not.toHaveProperty('turnRef')
    expect(lsquare.calls()[0]?.dropped).toMatchObject({ 'agent.turnId': 'too_large' })
  })
})

describe('16. a value that throws while it is read', () => {
  it('still gives an event, and the app its error', async () => {
    const lsquare = setup()
    const error = new Error('broke')
    Object.defineProperty(error, 'code', { get: () => { throw new Error('getter') } })
    const { server, call } = fakeServer(async () => {
      throw error
    })
    instrument(server, lsquare.client)
    await expect(call('t')).rejects.toBe(error)
    await lsquare.client.flush()
    expect(lsquare.calls()[0]?.error).toEqual({ message: 'broke', type: 'Error', category: 'thrown' })
  })
})

describe('17. coverage', () => {
  it('flushes on close because of the close, once, and only on stdio', async () => {
    const lsquare = setup()
    const stdio = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client)
    await stdio.connect(new Stdio1(new PassThrough(), new PassThrough()))
    const memory = instrument(new McpServer1({ name: 'm', version: '1' }), lsquare.client)
    memory.registerTool('t', {}, async () => text('ok'))
    await (await connect1(memory)).call('t')
    const flush = vi.spyOn(lsquare.client, 'flush')
    // The in-memory transport reports its close more than once: none flushes.
    await memory.close()
    expect(flush).not.toHaveBeenCalled()
    await stdio.close()
    expect(flush).toHaveBeenCalledTimes(1)
  })

  it('records schema-free tools on both SDKs', async () => {
    for (const sdk of [1, 2]) {
      const lsquare = setup()
      const server: any = instrument(sdk === 1 ? new McpServer1({ name: 's', version: '1' }) : new McpServer2({ name: 's', version: '1' }), lsquare.client)
      server.registerTool('t', {}, async () => text('no schema'))
      await (await (sdk === 1 ? connect1(server) : connect2(server))).call('t')
      await lsquare.client.flush()
      expect(lsquare.calls()[0]).toMatchObject({ toolName: 't', status: 'completed', result: text('no schema') })
    }
  })

  it('keeps concurrent calls apart, and a capture on another client is a turn of its own', async () => {
    const lsquare = setup()
    const other = setup()
    const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client)
    server.registerTool('t', { inputSchema: { q: z.string() } }, async ({ q }) => {
      await sleep(q === 'a' ? 20 : 5)
      lsquare.client.capture({ question: `for ${q}` })
      other.client.capture({ question: `other ${q}` })
      return text(q)
    })
    const connection = await connect1(server)
    await Promise.all([connection.call('t', { q: 'a' }), connection.call('t', { q: 'b' })])
    await lsquare.client.flush()
    await other.client.flush()
    expect(Object.fromEntries(lsquare.calls().map((e) => [(e.input as { q: string }).q, e.agent?.question]))).toEqual({ a: 'for a', b: 'for b' })
    expect(other.turns().map((t) => t.callId)).toEqual([undefined, undefined])
  })

  it('gives the same replies with and without instrument(), on both SDKs', async () => {
    for (const sdk of [1, 2]) {
      const build = () => {
        const server: any = sdk === 1 ? new McpServer1({ name: 's', version: '1' }) : new McpServer2({ name: 's', version: '1' })
        server.registerTool('ok', {}, async () => text('fine'))
        server.registerTool('fails', {}, async () => ({ ...text('no'), isError: true }))
        server.registerTool('throws', {}, async () => {
          throw new Error('boom')
        })
        return server
      }
      const replies = async (server: any) => {
        const connection = await (sdk === 1 ? connect1(server) : connect2(server))
        const out = []
        for (const name of ['ok', 'fails', 'throws', 'missing']) out.push(await connection.call(name).catch((e: Error) => ({ error: e.message })))
        return out
      }
      const plain = await replies(build())
      expect(await replies(instrument(build(), setup().client, { extras: OFF }))).toEqual(plain)
      // With the defaults, only the tool's own results change: they gain the ID line at their end.
      const defaults: any[] = await replies(instrument(build(), setup().client))
      expect(defaults.map((reply, index) => (index < 2 ? { ...reply, content: reply.content.slice(0, -1) } : reply))).toEqual(plain)
      for (const reply of defaults.slice(0, 2)) expect(reply.content.at(-1).text).toMatch(/^Task ID: ses_[A-Za-z0-9_-]{22}\. Send it as session_id on later calls/)
    }
  })

  it('replies within flushTimeoutMs on a slow network', async () => {
    // identify's wait and the sending share the one budget: added up they would take 550 ms.
    const lsquare = setup({ flushTimeoutMs: 300 }, () => new Promise(() => {}))
    const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client, { flushBeforeReply: true, identify: () => sleep(250).then(() => ({})) })
    server.registerTool('t', {}, async () => text('ok'))
    const connection = await connect1(server)
    const started = performance.now()
    await connection.call('t')
    expect(performance.now() - started).toBeLessThan(450)
    await lsquare.client.shutdown({ timeoutMs: 0 })
  })
})

describe('18. the results option', () => {
  it('is asked once per call', async () => {
    const lsquare = setup()
    let asked = 0
    const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client, { results: () => (++asked, true) })
    server.registerTool('t', {}, async () => ({ ...text('no'), isError: true }))
    await (await connect1(server)).call('t')
    await lsquare.client.flush()
    expect(asked).toBe(1)
    expect(lsquare.calls()[0]).toMatchObject({ result: { isError: true }, error: { message: 'no' } })
  })
})

// Codex's check of the fixes, numbered as in it.
describe('check 1. an identify that answers after its deadline', () => {
  it("doesn't change which IDs capture() must supply", async () => {
    const lsquare = setup()
    let late: (value: Record<string, never>) => void = () => {}
    const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client, {
      identify: ({ toolName }) => {
        if (toolName === 'first') return { orgId: 'org_1', userId: 'u_1' }
        if (toolName === 'slow') return new Promise((resolve) => (late = resolve))
        throw new Error('down')
      },
    })
    server.registerTool('first', {}, async () => text('a'))
    server.registerTool('slow', {}, async () => text('b'))
    server.registerTool('last', {}, async () => {
      lsquare.client.capture({ userId: 'u_3', question: 'q' })
      return text('c')
    })
    const connection = await connect1(server)
    await connection.call('first')
    await connection.call('slow')
    await sleep(1_100)
    late({})
    await sleep(10)
    await connection.call('last')
    await lsquare.client.flush()
    expect(lsquare.calls().find((e) => e.toolName === 'last')).toMatchObject({ identity: 'failed' })
  })
})

describe('check 2. promises from the app', () => {
  it('in an inputs function, identify fields after an invalid one, or a result field never go unhandled', async () => {
    const watch = unhandledDuring()
    try {
      const lsquare = setup()
      const { server, call } = fakeServer(async () => {
        const result: Record<string, unknown> = { ...text('ok') }
        Object.defineProperty(result, 'resultType', { get: () => Promise.reject(new Error('getter')), enumerable: false })
        return result
      })
      instrument(server, lsquare.client, {
        // Not allowed by the types, but an app may still pass one.
        inputs: (async () => {
          throw new Error('predicate')
        }) as unknown as () => boolean,
        identify: () => ({ orgId: 123 as unknown as string, userId: Promise.reject(new Error('id')) as unknown as string }),
      })
      await call('t')
      await sleep(30)
      await lsquare.client.flush()
      expect(lsquare.calls()).toHaveLength(1)
    } finally {
      watch.stop()
    }
    expect(watch.seen).toEqual([])
  })
})

describe('check 3. the client failing inside, at other places', () => {
  it('calls the handler once when entering the call scope fails, and survives a throwing internal counter or server field', async () => {
    const lsquare = setup()
    const internal = lsquare.internal as Internal & Record<string, unknown>
    let runs = 0
    const { server, call } = fakeServer(async () => (runs++, text('ran')))
    instrument(server, lsquare.client, { identify: () => ({}) })
    const calls = internal.calls()
    internal.calls = () => ({ run: () => { throw new Error('scope') }, getStore: () => undefined, exit: (fn: () => unknown) => fn() }) as unknown as typeof calls
    expect(await call('t')).toEqual(text('ran'))
    expect(runs).toBe(1)
    internal.calls = () => calls
    Object.defineProperty(internal, 'identifying', { get: () => { throw new Error('counter') } })
    expect(await call('t')).toEqual(text('ran'))

    const high = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client)
    Object.defineProperty(high, '_toolHandlersInitialized', { get: () => { throw new Error('field') } })
    high.server.registerCapabilities({ tools: {} })
    const handler = async () => text('mine')
    high.server.setRequestHandler(CallToolRequestSchema, handler)
    expect((high.server as unknown as { _requestHandlers: Map<string, unknown> })._requestHandlers.has('tools/call')).toBe(true)
  })
})

describe('check 4. flush() counts', () => {
  it('the events of calls it waited for, and calls still being built as pending', async () => {
    const lsquare = setup()
    const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client, {
      waitUntil: () => {},
      identify: () => sleep(200).then(() => ({ userId: 'u' })),
    })
    server.registerTool('t', {}, async () => text('ok'))
    const connection = await connect1(server)
    await connection.call('t')
    expect(await lsquare.client.flush({ timeoutMs: 50 })).toMatchObject({ pending: 1 })
    expect(await lsquare.client.flush()).toMatchObject({ sent: 1, pending: 0 })
  })
})

describe('check 5. identify calling flush()', () => {
  it("doesn't wait on its own call", async () => {
    const lsquare = setup()
    const server = instrument(new McpServer1({ name: 's', version: '1' }), lsquare.client, {
      identify: async () => {
        await sleep(5)
        await lsquare.client.flush()
        return { userId: 'u' }
      },
    })
    server.registerTool('t', {}, async () => text('ok'))
    await (await connect1(server)).call('t')
    await lsquare.client.flush()
    expect(lsquare.calls()[0]).toMatchObject({ userId: 'u' })
    expect(lsquare.calls()[0]).not.toHaveProperty('identity')
  })
})

describe('check 6. the capture() boundary on both SDKs', () => {
  it("is the tool's own return, though 2.x wraps it in an async executor", async () => {
    for (const sdk of [1, 2]) {
      const lsquare = setup()
      const server: any = instrument(sdk === 1 ? new McpServer1({ name: 's', version: '1' }) : new McpServer2({ name: 's', version: '1' }), lsquare.client)
      server.registerTool('t', {}, () => {
        queueMicrotask(() => lsquare.client.capture({ question: 'after the return' }))
        return text('ok')
      })
      await (await (sdk === 1 ? connect1(server) : connect2(server))).call('t')
      await sleep(10)
      await lsquare.client.flush()
      expect(lsquare.calls()[0], `SDK ${sdk}.x`).not.toHaveProperty('agent')
      expect(lsquare.turns()[0]?.callId).toBe(lsquare.calls()[0]?.callId)
    }
  })

  it('keeps tapping a 2.x tool after update() regenerates its executor', async () => {
    const lsquare = setup()
    const server = instrument(new McpServer2({ name: 's', version: '1' }), lsquare.client)
    const tool = server.registerTool('t', {}, async () => text('one'))
    const connection = await connect2(server)
    await connection.call('t')
    tool.update({ callback: async () => {
      throw new Error('two')
    } })
    await connection.call('t')
    await lsquare.client.flush()
    expect(lsquare.calls().map((e) => e.error?.category ?? 'ok')).toEqual(['ok', 'thrown'])
  })
})

describe('check 7. a result that fails while it is read', () => {
  // The wrapper reads the outcome without throwing; the event then meets the core's rule that
  // data which can't be read is never sent: dropped, and reported as such.
  it('leaves the reply untouched and reports the event dropped', async () => {
    const lsquare = setup()
    const content = new Proxy([], { get: (target, key) => (key === 'length' ? (() => { throw new Error('length') })() : Reflect.get(target, key)) })
    const reply = { content, isError: true }
    const { server, call } = fakeServer(async () => reply)
    instrument(server, lsquare.client)
    expect(await call('t')).toBe(reply)
    await lsquare.client.flush()
    await sleep(10)
    expect(lsquare.errors.map((e) => e.code)).toEqual(['redaction_failed'])
  })
})

describe('check 8. waitUntil', () => {
  it("gets a promise bounded by the wrapper's own deadline, whatever the client does", async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      const lsquare = setup()
      const internal = lsquare.internal as Internal & Record<string, unknown>
      internal.settled = () => new Promise(() => {})
      const promises: Promise<unknown>[] = []
      const { server, call } = fakeServer(async () => text('ok'))
      instrument(server, lsquare.client, { waitUntil: (p) => promises.push(p) })
      await call('t')
      let settled = false
      void promises[0]!.then(() => (settled = true))
      await vi.advanceTimersByTimeAsync(29_000)
      expect(settled).toBe(false)
      await vi.advanceTimersByTimeAsync(2_000)
      expect(settled).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  // Workers ends the request once its waitUntil promises settle, taking the later turns of reports
  // still to come with it: what was reported about the call has reached onError by then.
  it("settles only once the call's failure has reached onError", async () => {
    const lsquare = setup({}, () => json({}, 400))
    const promises: Promise<unknown>[] = []
    const { server, call } = fakeServer(async () => text('ok'))
    instrument(server, lsquare.client, { waitUntil: (p) => promises.push(p) })
    await call('t')
    await promises[0]
    expect(lsquare.errors.map((e) => e.code)).toEqual(['send_failed'])
  })

  it("settles once the call's failure has reached onError under an app's fake timers too", async () => {
    const realSetTimeout = setTimeout
    // vitest's defaults fake setImmediate too: the report's own turn never comes. The client is made
    // on the fake clock, whose performance.now() starts at 0, so its rate limit sees no time go back.
    vi.useFakeTimers()
    try {
      const lsquare = setup({}, () => json({}, 400))
      const promises: Promise<unknown>[] = []
      const { server, call } = fakeServer(async () => text('ok'))
      instrument(server, lsquare.client, { waitUntil: (p) => promises.push(p) })
      await call('t')
      expect(promises).toHaveLength(1)
      const settled = await Promise.race([promises[0]!.then(() => true), new Promise<boolean>((resolve) => realSetTimeout(() => resolve(false), 2_000))])
      expect(settled).toBe(true)
      expect(lsquare.errors.map((e) => e.code)).toEqual(['send_failed'])
    } finally {
      vi.useRealTimers()
    }
  })

  it('settles only once a call whose event was refused as it was queued has been reported', async () => {
    const lsquare = setup({ maxQueueBytes: 64 })
    const promises: Promise<unknown>[] = []
    const { server, call } = fakeServer(async () => text('ok'))
    instrument(server, lsquare.client, { waitUntil: (p) => promises.push(p) })
    await call('t')
    await promises[0]
    expect(lsquare.errors.map((e) => e.code)).toEqual(['queue_full'])
  })

  it('gets a promise when recording the call fails, settling once that has reached onError', async () => {
    const lsquare = setup()
    const internal = lsquare.internal as Internal & Record<string, unknown>
    internal.track = () => {
      throw new Error('broken')
    }
    const promises: Promise<unknown>[] = []
    const { server, call } = fakeServer(async () => text('ok'))
    instrument(server, lsquare.client, { waitUntil: (p) => promises.push(p) })
    expect(await call('t')).toEqual(text('ok'))
    expect(promises).toHaveLength(1)
    await promises[0]
    expect(lsquare.errors.map((e) => e.code)).toEqual(['internal_error'])
  })

  it("lets go of the call's data when recording it fails before its event is built, though a turn captured inside it is held", async () => {
    let release = () => {}
    const answers = new Promise<void>((resolve) => (release = resolve))
    const lsquare = setup({}, async (events) => {
      await answers
      return ack(events)
    })
    Object.defineProperty(lsquare.internal, 'flushTimeoutMs', {
      get() {
        throw new Error('broken')
      },
    })
    const refs: Record<string, WeakRef<object>> = {}
    const promises: Promise<unknown>[] = []
    const { server, call } = fakeServer(async () => {
      const agent = { note: 'the agent part' }
      refs.agent = new WeakRef(agent)
      lsquare.client.capture({ question: 'agent part', metadata: agent })
      lsquare.client.capture({ conversationId: 'c1', turnId: 2, question: 'a turn of its own' })
      return text('ok')
    })
    instrument(server, lsquare.client, { waitUntil: (p) => promises.push(p), extras: OFF })
    const ask = () => {
      const blob = { data: 'a'.repeat(1024 * 1024) }
      refs.arguments = new WeakRef(blob)
      return call('t', {}, { blob }).then(() => {})
    }
    await ask()
    await promises[0]
    expect(lsquare.errors.map((e) => e.code)).toEqual(['internal_error'])
    // The turn, posted and held.
    for (let i = 0; i < 100 && lsquare.turns().length === 0; i++) await tick()
    expect(lsquare.turns()).toHaveLength(1)
    const gc = (globalThis as { gc?: () => void }).gc as () => void
    for (let i = 0; i < 10; i++) {
      await sleep(5)
      gc()
    }
    expect(Object.keys(refs).filter((name) => refs[name]!.deref() !== undefined)).toEqual([])
    release()
    await lsquare.client.flush()
    expect(lsquare.calls()).toEqual([])
  })

  it("a reply held for flushBeforeReply doesn't wait for reports to reach onError, as under an app's fake timers", async () => {
    const realSetTimeout = setTimeout
    // vitest's defaults fake setImmediate too: the report of the invalid capture never gets its turn.
    // Made on the fake clock, as above, so the rate limit admits the report.
    vi.useFakeTimers()
    try {
      const lsquare = setup()
      const { server, call } = fakeServer(async () => text('ok'))
      instrument(server, lsquare.client, { flushBeforeReply: true, extras: OFF })
      lsquare.client.capture({ conversationId: 7 as unknown as string, question: 'q' })
      const replied = await Promise.race([call('t').then(() => true), new Promise<boolean>((resolve) => realSetTimeout(() => resolve(false), 2_000))])
      expect(replied).toBe(true)
      expect(lsquare.calls()).toHaveLength(1)
      expect(lsquare.errors).toEqual([])
      // The report was still to come: flush() runs it.
      const flushed = await Promise.race([lsquare.client.flush().then(() => true), new Promise<boolean>((resolve) => realSetTimeout(() => resolve(false), 2_000))])
      expect(flushed).toBe(true)
      expect(lsquare.errors.map((e) => e.code)).toEqual(['invalid_id'])
    } finally {
      vi.useRealTimers()
    }
  })
})


// Codex's check of those fixes, numbered as in it.
describe('recheck 1. a 2.x tool written as a function', () => {
  it('sees the same this as without instrument(), before and after update()', async () => {
    function probe(this: unknown) {
      return text(this === undefined ? 'unbound' : 'bound')
    }
    const run = async (instrumented: boolean) => {
      const plain = new McpServer2({ name: 's', version: '1' })
      const server: any = instrumented ? instrument(plain, setup().client, { extras: OFF }) : plain
      const tool = server.registerTool('t', {}, probe)
      const connection = await connect2(server)
      const seen = [await connection.call('t')]
      tool.update({ callback: probe, paramsSchema: z.object({ q: z.string().optional() }) })
      seen.push(await connection.call('t', { q: 'x' }))
      return seen
    }
    const without = await run(false)
    expect(without.map((result: any) => result.content[0].text)).toEqual(['unbound', 'unbound'])
    expect(await run(true)).toEqual(without)
  })
})

describe("recheck 2. checking identify's result failing", () => {
  it('is handled at once: no unhandled rejection while the tool still runs', async () => {
    const watch = unhandledDuring()
    const warnings: string[] = []
    const onWarning = (warning: Error) => warnings.push(warning.name)
    process.on('warning', onWarning)
    try {
      const lsquare = setup()
      const internal = lsquare.internal as Internal & Record<string, unknown>
      internal.validId = () => {
        throw new Error('check')
      }
      const { server, call } = fakeServer(async () => (await sleep(50), text('ok')))
      instrument(server, lsquare.client, { identify: () => ({ userId: 'u' }) })
      expect(await call('t')).toEqual(text('ok'))
      await lsquare.client.flush()
      await sleep(10)
    } finally {
      watch.stop()
      process.off('warning', onWarning)
    }
    expect(watch.seen).toEqual([])
    expect(warnings).not.toContain('PromiseRejectionHandledWarning')
  })
})

describe('recheck 3. flush() and shutdown() count the calls they took', () => {
  // Both replies are back; identify still runs, 10 ms for one call and 100 ms for the other.
  const twoCalls = async () => {
    const lsquare = setup()
    const { server, call } = fakeServer(async () => text('ok'))
    instrument(server, lsquare.client, { identify: ({ toolName }) => sleep(toolName === 'fast' ? 10 : 100).then(() => ({ userId: 'u' })) })
    await Promise.all([call('fast'), call('slow')])
    return lsquare.client
  }

  it('even one whose event was sent before the other was built', async () => {
    expect(await (await twoCalls()).flush()).toMatchObject({ sent: 2, pending: 0 })
    expect(await (await twoCalls()).shutdown()).toMatchObject({ sent: 2, failed: 0 })
  })

  it("and those still being built, when they don't wait: pending for flush(), failed for shutdown()", async () => {
    const client = await twoCalls()
    expect(await client.flush({ timeoutMs: 0 })).toMatchObject({ pending: 2 })
    expect(await client.flush()).toMatchObject({ sent: 2, pending: 0 })
    expect(await (await twoCalls()).shutdown({ timeoutMs: 0 })).toMatchObject({ failed: 2 })
  })
})

describe('recheck 4. a call whose event is refused before it is queued', () => {
  it('counts as failed for the flush() or shutdown() that took it', async () => {
    const refused = async () => {
      const lsquare = setup()
      const { server, call } = fakeServer(async () => {
        lsquare.client.capture({ userId: 123 as unknown as string, question: 'q' })
        return text('ok')
      })
      instrument(server, lsquare.client, { identify: () => sleep(30).then(() => ({})) })
      await call('t')
      return lsquare
    }
    const flushed = await refused()
    expect(await flushed.client.flush()).toEqual({ sent: 0, skipped: 0, failed: 1, pending: 0 })
    const shut = await refused()
    expect(await shut.client.shutdown()).toEqual({ sent: 0, skipped: 0, failed: 1, pending: 0 })
    await sleep(10)
    expect(flushed.errors.map((e) => e.code)).toEqual(['invalid_id'])
  })
})

