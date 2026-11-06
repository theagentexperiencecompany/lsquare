// Regression tests for the deep review's wrapper findings on connections, close and options (31
// October), one describe each, against real servers of both MCP SDKs: ts-mcp-11 (SDK 1.x's stdio
// server never flushing when its client shuts it), ts-mcp-12 (every other transport's close
// flushing), ts-mcp-13 (a tools/call handler that calls the one it replaced recorded twice), ts-mcp-14
// (serveStdio() not recognised as stdio) and ts-mcp-17 (`extras: false` turning every extra on).
// tests/frameworks/node/stdio.test.ts runs the stdio shutdown in a process of its own.
import { PassThrough } from 'node:stream'
import { Client as Client1 } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport as Memory1 } from '@modelcontextprotocol/sdk/inMemory.js'
import { McpServer as McpServer1 } from '@modelcontextprotocol/sdk/server/mcp.js'
import { Server as Server1 } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport as Stdio1 } from '@modelcontextprotocol/sdk/server/stdio.js'
import { CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { Client as Client2 } from '@modelcontextprotocol/client'
import { McpServer as McpServer2, InMemoryTransport as Memory2, Server as Server2 } from '@modelcontextprotocol/server'
import { serveStdio, StdioServerTransport as Stdio2 } from '@modelcontextprotocol/server/stdio'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { z } from 'zod'
import { validateEvent } from '../../contract/src/validate.js'
import { LSquare, type LSquareError, type LSquareEvent, type LSquareOptions, type ToolCallEvent } from '../src/index.js'
import { instrument, type InstrumentOptions } from '../src/mcp/index.js'

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const tick = () => new Promise<void>((resolve) => setImmediate(() => setImmediate(resolve)))
const text = (value: string) => ({ content: [{ type: 'text' as const, text: value }] })
const OFF = { sessions: false, intent: false, missingTools: false }
const LINE = /^Task ID: ses_[A-Za-z0-9_-]{22}\./

const rejected: string[] = []
afterEach(() => {
  expect(rejected).toEqual([])
  rejected.length = 0
})

type Answer = (attempt: number, events: LSquareEvent[]) => Response | Promise<Response>
const ack: Answer = (_attempt, events) => new Response(JSON.stringify({ accepted: events.map((e) => e.eventId), duplicates: [], rejected: [] }))

function setup(options: LSquareOptions = {}, answer: Answer = ack) {
  const events: LSquareEvent[] = []
  const errors: LSquareError[] = []
  const logs: string[] = []
  let attempts = 0
  const fetch = (async (_url: unknown, init?: RequestInit) => {
    const batch = JSON.parse(String(init?.body)).events as LSquareEvent[]
    for (const event of batch) {
      const verdict = validateEvent(event)
      if (!verdict.ok) rejected.push(`${event.kind}: ${verdict.reason}`)
    }
    const response = await answer(++attempts, batch)
    if (response.ok) events.push(...batch)
    return response
  }) as typeof globalThis.fetch
  const client = new LSquare({
    apiKey: 'lsq_test',
    endpoint: 'https://ingest.test',
    fetch,
    onError: (error) => errors.push(error),
    logger: { info: (line) => logs.push(line), warn: (line) => logs.push(line) },
    ...options,
  })
  const calls = () => events.filter((e): e is ToolCallEvent => e.kind === 'tool_call')
  return { client, events, errors, logs, calls, attempts: () => attempts }
}

/**
 * An ingestion whose first request fails with a network error, as a stdio server's often does when
 * its client shuts it, and whose retry is held until `release()`: the event waits for that retry,
 * which on Node is an unref'd timer, so only a flush keeps the process alive for it. Held, the
 * retry can't come before the flush has started, however soon its random delay ends.
 */
function failingOnce() {
  let release = () => {}
  const retried = new Promise<void>((resolve) => (release = resolve))
  const lsquare = setup({}, async (attempt, events) => {
    if (attempt === 1) throw new TypeError('fetch failed')
    await retried
    return ack(attempt, events)
  })
  return { ...lsquare, release }
}

/** Waits until `check` holds, at most 2 s. */
async function until(check: () => boolean) {
  for (let i = 0; i < 400 && !check(); i++) await sleep(5)
  expect(check()).toBe(true)
}

/** A stdio connection over two streams, driven as a stdio client drives one: line-delimited JSON-RPC. */
function stdioWire() {
  const stdin = new PassThrough()
  const stdout = new PassThrough()
  const replies = new Map<number, any>()
  let buffered = ''
  stdout.on('data', (chunk) => {
    buffered += String(chunk)
    let end: number
    while ((end = buffered.indexOf('\n')) >= 0) {
      const line = buffered.slice(0, end)
      buffered = buffered.slice(end + 1)
      if (!line.trim()) continue
      const message = JSON.parse(line)
      if (message.id !== undefined) replies.set(message.id, message)
    }
  })
  const send = (message: unknown) => stdin.write(`${JSON.stringify(message)}\n`)
  const request = async (id: number, method: string, params: unknown) => {
    send({ jsonrpc: '2.0', id, method, params })
    await until(() => replies.has(id))
    return replies.get(id)
  }
  const open = async (protocolVersion = '2025-11-25') => {
    await request(0, 'initialize', { protocolVersion, capabilities: {}, clientInfo: { name: 'claude-code', version: '2.1' } })
    send({ jsonrpc: '2.0', method: 'notifications/initialized' })
  }
  const call = async (id: number, name: string, args: Record<string, unknown> = {}) => (await request(id, 'tools/call', { name, arguments: args })).result
  /** A call sent without waiting for its reply. */
  const start = (id: number, name: string) => send({ jsonrpc: '2.0', id, method: 'tools/call', params: { name, arguments: {} } })
  return { stdin, stdout, open, call, start }
}

const stdioKits = [
  { name: 'SDK 1.x', server: (): any => new McpServer1({ name: 's', version: '1' }), transport: (stdin: PassThrough, stdout: PassThrough) => new Stdio1(stdin, stdout) },
  { name: 'SDK 2.x', server: (): any => new McpServer2({ name: 's', version: '1' }), transport: (stdin: PassThrough, stdout: PassThrough) => new Stdio2(stdin, stdout) },
]

// SDK 1.x's transport never hears its stdin end, so its server never closes: the wrapper listens
// for that end itself, from when the connection starts. SDK 2.x's hears it, and closes the server
// too: still one flush. The SDK 2.x cases pass without the fix as well; they guard against a second.
describe.each(stdioKits)('ts-mcp-11: $name over stdio, a client that shuts the server by closing its stdin', (kit) => {
  it('starts one flush, which waits for an event whose first send failed until its retry sends it', async () => {
    const lsquare = failingOnce()
    const server = instrument(kit.server(), lsquare.client, { extras: OFF })
    server.registerTool('t', {}, async () => text('ok'))
    const wire = stdioWire()
    await server.connect(kit.transport(wire.stdin, wire.stdout))
    await wire.open()
    expect(await wire.call(1, 't')).toEqual(text('ok'))
    const flush = vi.spyOn(lsquare.client, 'flush')
    // The MCP stdio shutdown: the client closes the server's stdin.
    wire.stdin.end()
    await until(() => flush.mock.calls.length > 0)
    await tick()
    lsquare.release()
    expect(flush).toHaveBeenCalledTimes(1)
    expect(await flush.mock.results[0]!.value).toMatchObject({ sent: 1, pending: 0 })
    expect(lsquare.attempts()).toBe(2)
    expect(lsquare.calls()).toHaveLength(1)
    expect(lsquare.errors).toEqual([])
  })

  // capture() outside any call sends a turn: a connection that made no tools/call has one to lose.
  it.each(['before', 'after'])('flushes once when its stdin ends on a connection that made no tools/call, instrumented %s it connected', async (when) => {
    const lsquare = failingOnce()
    const server = kit.server()
    server.registerTool('t', {}, async () => text('ok'))
    const wire = stdioWire()
    if (when === 'before') instrument(server, lsquare.client, { extras: OFF })
    await server.connect(kit.transport(wire.stdin, wire.stdout))
    await wire.open()
    if (when === 'after') instrument(server, lsquare.client, { extras: OFF })
    lsquare.client.capture({ conversationId: 'c-1', question: 'hello' })
    const flush = vi.spyOn(lsquare.client, 'flush')
    wire.stdin.end()
    await until(() => flush.mock.calls.length > 0)
    await tick()
    lsquare.release()
    expect(flush).toHaveBeenCalledTimes(1)
    expect(await flush.mock.results[0]!.value).toMatchObject({ sent: 1, pending: 0 })
    expect(lsquare.attempts()).toBe(2)
    expect(lsquare.errors).toEqual([])
  })

  // Its event is built once the tool returns, after the close flush started: it gets one of its own.
  it("sends the event of a call still running when its stdin ends once the call returns, through a flush that waits for the event's retry", async () => {
    const lsquare = failingOnce()
    let finish = () => {}
    const running = new Promise<void>((resolve) => (finish = resolve))
    let entered = false
    const server = instrument(kit.server(), lsquare.client, { extras: OFF })
    server.registerTool('t', {}, async () => {
      entered = true
      await running
      return text('ok')
    })
    const wire = stdioWire()
    await server.connect(kit.transport(wire.stdin, wire.stdout))
    await wire.open()
    wire.start(1, 't')
    await until(() => entered)
    const flush = vi.spyOn(lsquare.client, 'flush')
    wire.stdin.end()
    await until(() => flush.mock.calls.length === 1)
    finish()
    await until(() => flush.mock.calls.length === 2)
    await tick()
    lsquare.release()
    expect(flush).toHaveBeenCalledTimes(2)
    expect(await flush.mock.results[1]!.value).toMatchObject({ sent: 1, pending: 0 })
    expect(lsquare.attempts()).toBe(2)
    expect(lsquare.calls()).toHaveLength(1)
    expect(lsquare.errors).toEqual([])
  })

  it('flushes once when the server closes first, and not again when its stdin then ends', async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client, { extras: OFF })
    server.registerTool('t', {}, async () => text('ok'))
    const wire = stdioWire()
    await server.connect(kit.transport(wire.stdin, wire.stdout))
    await wire.open()
    await wire.call(1, 't')
    const flush = vi.spyOn(lsquare.client, 'flush')
    await server.close()
    wire.stdin.end()
    await tick()
    await sleep(20)
    expect(flush).toHaveBeenCalledTimes(1)
    // What the wrapper listened to on stdin, it no longer does.
    expect(wire.stdin.listenerCount('end') + wire.stdin.listenerCount('close')).toBe(0)
  })
})

describe('ts-mcp-12: only a stdio server flushes when it closes', () => {
  it.each([
    ['SDK 1.x', (): any => new McpServer1({ name: 's', version: '1' }), connect1],
    ['SDK 2.x', (): any => new McpServer2({ name: 's', version: '1' }), connect2],
  ] as const)('%s: a server on another transport closing, as per-request servers do after every request, flushes nothing and reports nothing', async (_name, make, connect) => {
    // An ingestion that answers nothing until the end: every event is still pending when a server closes.
    let release = () => {}
    const answered = new Promise<void>((resolve) => (release = resolve))
    const lsquare = setup({ flushTimeoutMs: 50 }, async (attempt, events) => {
      await answered
      return ack(attempt, events)
    })
    const flush = vi.spyOn(lsquare.client, 'flush')
    for (let request = 0; request < 5; request++) {
      const server = instrument(make(), lsquare.client, { extras: OFF })
      server.registerTool('t', {}, async () => text('ok'))
      const connection = await connect(server)
      expect(await connection.call('t')).toEqual(text('ok'))
      await server.close()
    }
    expect(flush).not.toHaveBeenCalled()
    // Longer than each close flush would have waited.
    await sleep(120)
    expect(lsquare.errors.filter((error) => error.code === 'pending_after_flush')).toEqual([])
    release()
    await lsquare.client.flush()
    expect(lsquare.calls()).toHaveLength(5)
  })
})

async function connect1(server: any) {
  const [a, b] = Memory1.createLinkedPair()
  await server.connect(a)
  const client = new Client1({ name: 'c', version: '1' })
  await client.connect(b)
  return { call: (name: string) => client.callTool({ name, arguments: {} }) }
}
async function connect2(server: any) {
  const [a, b] = Memory2.createLinkedPair()
  await server.connect(a)
  const client = new Client2({ name: 'c', version: '1' })
  await client.connect(b)
  return { call: (name: string) => client.callTool({ name, arguments: {} }) }
}

/** The four kinds of server, each with a tool `t`, and the way an app's middleware takes over tools/call on it. */
const delegating = [
  {
    name: 'SDK 1.x McpServer',
    make: (tool: () => Promise<unknown>) => {
      const server = new McpServer1({ name: 's', version: '1' })
      server.registerTool('t', {}, tool as never)
      return { server, low: server.server as any, connect: () => connect1(server) }
    },
    set: (low: any, handler: (request: any, extra: any) => unknown) => low.setRequestHandler(CallToolRequestSchema, handler),
  },
  {
    name: 'SDK 1.x Server',
    make: (tool: () => Promise<unknown>) => {
      const server = new Server1({ name: 's', version: '1' }, { capabilities: { tools: {} } })
      server.setRequestHandler(CallToolRequestSchema, (async () => tool()) as never)
      return { server, low: server as any, connect: () => connect1(server) }
    },
    set: (low: any, handler: (request: any, extra: any) => unknown) => low.setRequestHandler(CallToolRequestSchema, handler),
  },
  {
    name: 'SDK 2.x McpServer',
    make: (tool: () => Promise<unknown>) => {
      const server = new McpServer2({ name: 's', version: '1' })
      server.registerTool('t', {}, tool as never)
      return { server, low: server.server as any, connect: () => connect2(server) }
    },
    set: (low: any, handler: (request: any, extra: any) => unknown) => low.setRequestHandler('tools/call', handler),
  },
  {
    name: 'SDK 2.x Server',
    make: (tool: () => Promise<unknown>) => {
      const server = new Server2({ name: 's', version: '1' }, { capabilities: { tools: {} } })
      server.setRequestHandler('tools/call', (async () => tool()) as never)
      return { server, low: server as any, connect: () => connect2(server) }
    },
    set: (low: any, handler: (request: any, extra: any) => unknown) => low.setRequestHandler('tools/call', handler),
  },
]

describe.each(delegating)('ts-mcp-13: $name, a tools/call handler set after instrument() that calls the one it replaced', (kit) => {
  it.each([
    ['the context it was given', (extra: any) => extra],
    ['a copy of that context', (extra: any) => ({ ...extra })],
  ])('records each call once, with one identify, when it passes on %s', async (_label, pass) => {
    const lsquare = setup()
    let identifies = 0
    const options: InstrumentOptions = { extras: OFF, identify: () => (identifies++, { userId: 'u-1' }) }
    const { server, low, connect } = kit.make(async () => text('ok'))
    instrument(server, lsquare.client, options)
    // Middleware, as an app or a framework adds it: it takes the current entry and calls it.
    const inner = low._requestHandlers.get('tools/call')
    let through = 0
    kit.set(low, (request: any, extra: any) => (through++, inner(request, pass(extra))))
    const connection = await connect()
    expect(await connection.call('t')).toEqual(text('ok'))
    await lsquare.client.flush()
    expect(through).toBe(1)
    expect(lsquare.calls()).toHaveLength(1)
    expect(lsquare.calls()[0]).toMatchObject({ toolName: 't', status: 'completed', userId: 'u-1' })
    expect(identifies).toBe(1)
  })
})

describe('ts-mcp-13: a tool that calls another tool through a client while it runs', () => {
  // The control: the nested request comes inside the outer call's scope, with an ID of its own.
  it('is two calls, recorded twice: the nested one is another request', async () => {
    const lsquare = setup()
    const server = new McpServer1({ name: 's', version: '1' })
    let connection: { call: (name: string) => Promise<unknown> } | undefined
    server.registerTool('inner', {}, async () => text('inner'))
    server.registerTool('outer', {}, async () => {
      await connection!.call('inner')
      return text('outer')
    })
    instrument(server, lsquare.client, { extras: OFF })
    connection = await connect1(server)
    expect(await connection.call('outer')).toEqual(text('outer'))
    await lsquare.client.flush()
    expect(lsquare.calls().map((event) => event.toolName).sort()).toEqual(['inner', 'outer'])
  })
})

describe("ts-mcp-14: SDK 2.x's serveStdio(), with a client on a protocol version before 2026-07-28", () => {
  it("groups the connection's calls by one ID made for it, leaving results unchanged, and flushes when its stdin ends", async () => {
    const lsquare = setup()
    const wire = stdioWire()
    const flush = vi.spyOn(lsquare.client, 'flush')
    serveStdio(
      () => {
        const server = new McpServer2({ name: 's', version: '1' })
        server.registerTool('find', { inputSchema: z.object({ q: z.string().optional() }) }, async () => text('ok'))
        return instrument(server, lsquare.client)
      },
      { transport: new Stdio2(wire.stdin, wire.stdout) },
    )
    await wire.open('2025-11-25')
    // No ID line: the connection groups them.
    expect(await wire.call(1, 'find', { q: 'a' })).toEqual(text('ok'))
    expect(await wire.call(2, 'find', { q: 'b' })).toEqual(text('ok'))
    await lsquare.client.flush()
    const [one, two] = lsquare.calls()
    expect(one).toMatchObject({ protocolVersion: '2025-11-25', sessionSource: 'protocol', sessionId: expect.stringMatching(/^[0-9a-f]{64}$/) })
    expect(two!.sessionId).toBe(one!.sessionId)
    flush.mockClear()
    wire.stdin.end()
    await until(() => flush.mock.calls.length > 0)
    await tick()
    expect(flush).toHaveBeenCalledTimes(1)
  })
})

describe.each([
  { name: 'SDK 1.x', make: (): any => new McpServer1({ name: 's', version: '1' }), connect: connect1, list: listed1 },
  { name: 'SDK 2.x', make: (): any => new McpServer2({ name: 's', version: '1' }), connect: connect2, list: listed2 },
])('ts-mcp-17: $name, extras', (kit) => {
  it('false turns every extra off: the tools listed and the replies are exactly the server’s', async () => {
    const lsquare = setup()
    const server = kit.make()
    server.registerTool('t', { inputSchema: { q: z.string().optional() } } as never, async () => text('ok'))
    instrument(server, lsquare.client, { extras: false })
    const tools = await kit.list(server)
    expect(tools.map((tool: any) => [tool.name, Object.keys(tool.inputSchema?.properties ?? {})])).toEqual([['t', ['q']]])
    const connection = await kit.connect(server)
    expect(await connection.call('t')).toEqual(text('ok'))
    await lsquare.client.flush()
    expect(lsquare.calls()[0]).not.toHaveProperty('sessionId')
    expect(lsquare.logs).toEqual([])
  })

  // A list or a function is no options object either, whatever fields it has: none of them is read.
  it.each([
    ['0', () => 0],
    ['a list', () => []],
    ['a function with an extra turned off on it', () => Object.assign(() => {}, { sessions: false, intent: false, missingTools: false })],
  ])('%s, neither false nor an object, leaves the defaults, with one warning', async (_name, value) => {
    const lsquare = setup()
    const server = kit.make()
    server.registerTool('t', { inputSchema: { q: z.string().optional() } } as never, async () => text('ok'))
    instrument(server, lsquare.client, { extras: value() as never })
    const tools = await kit.list(server)
    expect(tools.map((tool: any) => [tool.name, Object.keys(tool.inputSchema?.properties ?? {}).sort()])).toEqual([
      ['t', ['intent', 'q', 'session_id']],
      ['get_more_tools', expect.any(Array)],
    ])
    const connection = await kit.connect(server)
    const reply: any = await connection.call('t')
    expect(reply.content.at(-1).text).toMatch(LINE)
    await lsquare.client.flush()
    expect(lsquare.logs.filter((line) => line.includes('extras is neither false nor an object'))).toHaveLength(1)
  })
})

async function listed1(server: any) {
  const [a, b] = Memory1.createLinkedPair()
  await server.connect(a)
  const client = new Client1({ name: 'c', version: '1' })
  await client.connect(b)
  const { tools } = await client.listTools()
  await client.close()
  await server.close()
  return tools
}
async function listed2(server: any) {
  const [a, b] = Memory2.createLinkedPair()
  await server.connect(a)
  const client = new Client2({ name: 'c', version: '1' })
  await client.connect(b)
  const { tools } = await client.listTools()
  await client.close()
  await server.close()
  return tools
}
