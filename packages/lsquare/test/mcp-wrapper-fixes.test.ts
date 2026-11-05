// The deep review's last wrapper and parity findings on TypeScript's side, on both SDKs: a tool that
// runs another registered tool (py-mcp-18), a later capture()'s absent callId (parity-9), an
// in-call orgId that isn't a string (parity-21), and the guards for what TypeScript already did
// right: an identify failure after a success without IDs (parity-8), and a cancelled call whose
// tool returns anyway (parity-12). Then Codex's second review of the Python wrapper fixes: a
// promise continuation the tool left running, which runs another registered tool once the tool
// has returned.
import { Client as Client1 } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport as Memory1 } from '@modelcontextprotocol/sdk/inMemory.js'
import { McpServer as McpServer1 } from '@modelcontextprotocol/sdk/server/mcp.js'
import { Client as Client2 } from '@modelcontextprotocol/client'
import { McpServer as McpServer2, InMemoryTransport as Memory2 } from '@modelcontextprotocol/server'
import { describe, expect, it } from 'vitest'
import { z } from 'zod'
import { LSquare, type LSquareError, type LSquareEvent, type LSquareOptions, type ToolCallEvent, type TurnEvent } from '../src/index.js'
import { instrument, type InstrumentOptions } from '../src/mcp/index.js'

type Tool = (args: Record<string, unknown>, extra: unknown) => unknown

interface Connection {
  call(name: string, args?: Record<string, unknown>, signal?: AbortSignal): Promise<any>
  close(): Promise<void>
}

/** One SDK's high-level server, the registrations its registerTool() returned, and a client. */
interface Sdk {
  name: string
  /** `outputs`: a tool's output schema, as a shape, which the SDK validates once the tool has returned. */
  high(tools: Record<string, Tool>, outputs?: Record<string, z.ZodRawShape>): { server: any; registered: Record<string, any> }
  /** Runs a registered tool's function as the server holds it, through what registerTool() returned. */
  run(registered: any, args: unknown, extra: unknown): unknown
  connect(server: any): Promise<Connection>
}

const sdk1: Sdk = {
  name: 'SDK 1.x',
  high(tools, outputs = {}) {
    const server = new McpServer1({ name: 'orders', version: '1.0.0' })
    const registered: Record<string, any> = {}
    for (const [name, tool] of Object.entries(tools)) {
      const output = outputs[name] ? { outputSchema: outputs[name] } : {}
      registered[name] = server.registerTool(name, { inputSchema: { q: z.string().optional() }, ...output }, tool as never)
    }
    return { server, registered }
  },
  // 1.x's public RegisteredTool.handler is the function the wrapper taps.
  run: (registered, args, extra) => registered.handler(args, extra),
  async connect(server) {
    const [a, b] = Memory1.createLinkedPair()
    await server.connect(a)
    const client = new Client1({ name: 'claude-code', version: '2.1' })
    await client.connect(b)
    return {
      call: (name, args = {}, signal) =>
        client.callTool({ name, arguments: args }, undefined, signal ? { signal } : undefined).catch((error: { code?: unknown; message?: unknown }) => ({ error: { code: error.code, message: error.message } })),
      close: () => client.close(),
    }
  },
}

const sdk2: Sdk = {
  name: 'SDK 2.x',
  high(tools, outputs = {}) {
    const server = new McpServer2({ name: 'orders', version: '1.0.0' })
    const registered: Record<string, any> = {}
    for (const [name, tool] of Object.entries(tools)) {
      const output = outputs[name] ? { outputSchema: z.object(outputs[name]) } : {}
      registered[name] = server.registerTool(name, { inputSchema: z.object({ q: z.string().optional() }), ...output }, tool as never)
    }
    return { server, registered }
  },
  // 2.x dispatches the registration's executor, around its handler: the wrapper taps through it.
  run: (registered, args, extra) => registered.executor(args, extra),
  async connect(server) {
    const [a, b] = Memory2.createLinkedPair()
    await server.connect(a)
    const client = new Client2({ name: 'claude-code', version: '2.1' })
    await client.connect(b)
    return {
      call: (name, args = {}, signal) =>
        client.callTool({ name, arguments: args }, signal ? { signal } : undefined).catch((error: { code?: unknown; message?: unknown }) => ({ error: { code: error.code, message: error.message } })),
      close: () => client.close(),
    }
  },
}

function setup(options: LSquareOptions = {}) {
  const events: LSquareEvent[] = []
  const errors: LSquareError[] = []
  const fetch = (async (_url: unknown, init?: RequestInit) => {
    const batch = JSON.parse(String(init?.body)).events as LSquareEvent[]
    events.push(...batch)
    return new Response(JSON.stringify({ accepted: batch.map((e) => e.eventId), duplicates: [], rejected: [] }))
  }) as typeof globalThis.fetch
  const client = new LSquare({
    apiKey: 'lsq_test',
    endpoint: 'https://ingest.test',
    fetch,
    onError: (error) => errors.push(error),
    logger: { info: () => {}, warn: () => {} },
    ...options,
  })
  const calls = () => events.filter((e): e is ToolCallEvent => e.kind === 'tool_call')
  const turns = () => events.filter((e): e is TurnEvent => e.kind === 'turn')
  return { client, events, calls, turns, errors }
}

const text = (value: string) => ({ content: [{ type: 'text' as const, text: value }] })
const OFF: InstrumentOptions = { extras: false }

describe.each([sdk1, sdk2])('$name, a tool that runs another registered tool itself', (sdk) => {
  it("records its own outcome, not the other tool's throw", async () => {
    const { client, calls } = setup()
    const held: { registered?: Record<string, any> } = {}
    const { server, registered } = sdk.high({
      inner: async () => {
        throw new Error('inner secret failure')
      },
      outer: async (args, extra) => {
        await Promise.resolve(sdk.run(held.registered!.inner, args, extra)).catch(() => {})
        return { ...text('outer says no'), isError: true }
      },
    })
    held.registered = registered
    instrument(server, client)
    const connection = await sdk.connect(server)
    // inner's own call taps it.
    await connection.call('inner')
    const reply = await connection.call('outer')
    await client.flush()
    const outer = calls().find((event) => event.toolName === 'outer')
    expect(reply.isError).toBe(true)
    expect(reply.content.at(-1).text).toMatch(/^Task ID: ses_/)
    expect(outer!.error).toEqual({ message: 'outer says no', category: 'tool_error' })
    expect(outer!.sessionSource).toBe('issued')
    await connection.close()
  })
})

describe.each([sdk1, sdk2])('$name, a later capture() inside a call', (sdk) => {
  it('is linked to its call when its callId is left out, null or empty, as in Python', async () => {
    const { client, turns } = setup()
    let callId: string | undefined
    const { server } = sdk.high({
      ask: async () => {
        callId = client.currentCallId()
        client.capture({ question: 'first' })
        client.capture({ question: 'omitted' })
        client.capture({ question: 'null', callId: null })
        client.capture({ question: 'empty', callId: '' })
        client.capture({ question: 'literal', callId: 'empty' })
        client.capture({ question: 'other', callId: 'call-elsewhere' })
        return text('ok')
      },
    })
    instrument(server, client, OFF)
    const connection = await sdk.connect(server)
    await connection.call('ask')
    await client.flush()
    const byQuestion = Object.fromEntries(turns().map((turn) => [turn.question, turn]))
    for (const question of ['omitted', 'null', 'empty']) {
      expect(byQuestion[question]!.callId, question).toBe(callId)
      expect(byQuestion[question]!.dropped, question).toBeUndefined()
    }
    expect(byQuestion.literal!.callId).toBe('empty')
    expect(byQuestion.other!.callId).toBe('call-elsewhere')
    await connection.close()
  })
})

describe.each([sdk1, sdk2])('$name, an in-call orgId that is not a string', (sdk) => {
  const throwing = {
    toJSON() {
      throw new Error('no JSON')
    },
  }
  it.each([
    ['a number', 5],
    ['a bigint', 5n],
    ['an object JSON cannot write', throwing],
  ])('drops the call as invalid_id, naming it: %s', async (_label, orgId) => {
    const { client, calls, errors } = setup()
    const { server } = sdk.high({
      answering: async () => {
        client.capture({ question: 'q', conversationId: 'c1', turnId: 't1', orgId: orgId as never })
        return text('ok')
      },
    })
    instrument(server, client, OFF)
    const connection = await sdk.connect(server)
    const reply = await connection.call('answering')
    await client.flush()
    expect(reply.content).toEqual([{ type: 'text', text: 'ok' }])
    expect(calls()).toEqual([])
    expect(errors.map((error) => [error.code, error.paths])).toEqual([['invalid_id', ['ids.orgId']]])
    await connection.close()
  })

  it('drops only that turn when a later capture has it, as REFERENCE.md says', async () => {
    const { client, calls, turns, errors } = setup()
    const { server } = sdk.high({
      answering: async () => {
        client.capture({ question: 'first' })
        client.capture({ question: 'second', orgId: 5 as never })
        return text('ok')
      },
    })
    instrument(server, client, OFF)
    const connection = await sdk.connect(server)
    await connection.call('answering')
    await client.flush()
    expect(calls().map((call) => call.agent?.question)).toEqual(['first'])
    expect(turns()).toEqual([])
    // A turn's own orgId, at its top level.
    expect(errors.map((error) => [error.code, error.paths])).toEqual([['invalid_id', ['orgId']]])
    await connection.close()
  })
})

describe.each([sdk1, sdk2])('$name, an identify failure after a success that returned no IDs', (sdk) => {
  it.each([
    ['nothing', undefined],
    ['only empty IDs', { userId: '', orgId: null }],
  ])('sends the call in full, as Python now does: %s', async (_label, anonymous) => {
    const { client, calls, errors } = setup()
    let count = 0
    const { server } = sdk.high({ search: async (args) => text(`found ${String(args.q)}`) })
    instrument(server, client, {
      ...OFF,
      identify: () => {
        count += 1
        if (count === 1) return anonymous as never
        throw new Error('down')
      },
    })
    const connection = await sdk.connect(server)
    await connection.call('search', { q: 'one' })
    await connection.call('search', { q: 'two' })
    await client.flush()
    const second = calls()[1]!
    expect(second.identity).toBeUndefined()
    expect(second.input).toEqual({ q: 'two' })
    expect(second.result).toBeDefined()
    expect(errors.filter((error) => error.code === 'identify_failed').map((error) => error.message)).toEqual([
      "identify threw; the call was sent with capture()'s IDs.",
    ])
    await connection.close()
  })
})

describe.each([sdk1, sdk2])('$name, a cancelled call whose tool returns anyway', (sdk) => {
  it("is a timeout, as Python now reads the request's own cancellation", async () => {
    const { client, calls } = setup()
    let entered: () => void = () => {}
    const started = new Promise<void>((resolve) => (entered = resolve))
    let heard: () => void = () => {}
    const aborted = new Promise<void>((resolve) => (heard = resolve))
    // Handed the call's sending as the call ends: the event is built by then.
    let ended: (sending: Promise<unknown>) => void = () => {}
    const handed = new Promise<Promise<unknown>>((resolve) => (ended = resolve))
    const { server } = sdk.high({
      slow: async (_args, extra) => {
        const signal = ((extra as { signal?: AbortSignal }).signal ?? (extra as { mcpReq: { signal: AbortSignal } }).mcpReq.signal)
        signal.addEventListener('abort', () => heard())
        entered()
        // It hears the cancellation, and returns its cleanup result all the same.
        await aborted
        return text('cleaned up')
      },
    })
    instrument(server, client, { ...OFF, waitUntil: (sending) => ended(sending) })
    const connection = await sdk.connect(server)
    const controller = new AbortController()
    const reply = connection.call('slow', {}, controller.signal)
    await started
    controller.abort()
    await reply
    await (await handed)
    const [event] = calls()
    expect(event!.status).toBe('error')
    expect(event!.error!.category).toBe('timeout')
    expect(event!.result).toBeUndefined()
    await connection.close()
  })
})

// Codex's second review of the Python wrapper fixes, 31 October 2026: a run's scope was set aside
// only while the run was live, so a continuation the tool chained inside its run, settled once the
// tool had returned while the call goes on, ran another tool's function as the call's tool: that
// tool's throw became the call's outcome, and the reply lost its ID line.
describe.each([sdk1, sdk2])('$name, a promise continuation the tool left running', (sdk) => {
  it.each([
    ['throws', false],
    ['rejects', true],
  ])('records nothing on the call when it runs another registered tool that %s, once the tool has returned', async (_label, rejects) => {
    const { client, calls } = setup()
    const held: { registered?: Record<string, any> } = {}
    const order: string[] = []
    let audits = 0
    // Settled once the tool has returned, as the SDK reads its result's isError (to skip checking
    // its output): the continuation the tool chained on it runs next, in the tool's run's context,
    // before the call ends.
    let release: () => void = () => {}
    const released = new Promise<void>((resolve) => (release = resolve))
    const fails = () => {
      audits += 1
      throw new Error('audit secret failure')
    }
    const audit = rejects ? async () => fails() : fails
    const { server, registered } = sdk.high(
      {
        audit,
        outer: async (args, extra) => {
          void released.then(() => {
            order.push('audit')
            try {
              return Promise.resolve(sdk.run(held.registered!.audit, args, extra)).catch(() => order.push('audit raised'))
            } catch {
              return order.push('audit raised')
            }
          })
          order.push('outer returns')
          return {
            ...text('outer says no'),
            get isError() {
              if (!order.includes('isError read')) order.push('isError read')
              release()
              return true
            },
          }
        },
      },
      { outer: { n: z.number() } },
    )
    held.registered = registered
    instrument(server, client)
    const connection = await sdk.connect(server)
    // audit's own call taps it.
    await connection.call('audit')
    const reply = await connection.call('outer')
    await client.flush()
    expect(order.slice(0, 4)).toEqual(['outer returns', 'isError read', 'audit', 'audit raised'])
    // Its own call, then the continuation's: it ran, and recorded nothing on the outer call.
    expect(audits).toBe(2)
    expect(calls().map((event) => [event.toolName, event.status, event.error])).toEqual([
      ['audit', 'error', { message: 'audit secret failure', type: 'Error', category: 'thrown' }],
      ['outer', 'error', { message: 'outer says no', category: 'tool_error' }],
    ])
    expect(reply.isError).toBe(true)
    expect(reply.content[0].text).toBe('outer says no')
    expect(reply.content.at(-1).text).toMatch(/^Task ID: ses_/)
    expect(calls()[1]!.sessionSource).toBe('issued')
    await connection.close()
  })
})
