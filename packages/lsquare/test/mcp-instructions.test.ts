// lsquare/mcp's server instructions (spec section 4): each call carries the instructions the server
// gives clients, read from the instance as the call arrives, against real servers and clients of
// both MCP SDKs.
import { Client as Client1 } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport as Memory1 } from '@modelcontextprotocol/sdk/inMemory.js'
import { Server as Server1 } from '@modelcontextprotocol/sdk/server/index.js'
import { McpServer as McpServer1 } from '@modelcontextprotocol/sdk/server/mcp.js'
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { Client as Client2 } from '@modelcontextprotocol/client'
import { McpServer as McpServer2, InMemoryTransport as Memory2, Server as Server2 } from '@modelcontextprotocol/server'
import { afterEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { validateEvent } from '../../contract/src/validate.js'
import { LSquare, type LSquareEvent, type LSquareOptions, type ToolCallEvent } from '../src/index.js'
import { instrument } from '../src/mcp/index.js'

const text = (value: string) => ({ content: [{ type: 'text' as const, text: value }] })
const prose = (chars: number) => 'lorem ipsum '.repeat(Math.ceil(chars / 12)).slice(0, chars)
const INSTRUCTIONS = 'Search orders before refunding one.'
const INFO = { name: 'orders', version: '1.0.0' }

const rejected: string[] = []
afterEach(() => {
  expect(rejected).toEqual([])
  rejected.length = 0
})

function setup(options: LSquareOptions = {}) {
  const events: LSquareEvent[] = []
  const fetch = (async (_url: unknown, init?: RequestInit) => {
    const batch = JSON.parse(String(init?.body)).events as LSquareEvent[]
    for (const event of batch) {
      const verdict = validateEvent(event)
      if (!verdict.ok) rejected.push(`${event.kind}: ${verdict.reason}`)
    }
    events.push(...batch)
    return new Response(JSON.stringify({ accepted: batch.map((e) => e.eventId), duplicates: [], rejected: [] }))
  }) as typeof globalThis.fetch
  const client = new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test', fetch, logger: { info: () => {}, warn: () => {} }, onError: () => {}, ...options })
  const order: string[] = []
  /** The calls sent so far, in the order they were made. */
  const calls = async () => {
    await client.flush()
    return events.filter((e): e is ToolCallEvent => e.kind === 'tool_call').sort((a, b) => order.indexOf(a.callId) - order.indexOf(b.callId))
  }
  return { client, calls, order }
}

type Lsquare = ReturnType<typeof setup>

interface Connection {
  call(name: string, args?: Record<string, unknown>): Promise<any>
  /** The instructions the client was given when it connected. */
  instructions(): string | undefined
}

/** Calls one after the other, so their events are told apart by the order they were made in. */
async function callAll(lsquare: Lsquare, connection: Connection, ...names: Array<string | [string, Record<string, unknown>]>): Promise<ToolCallEvent[]> {
  const before = (await lsquare.calls()).length
  for (const each of names) {
    const [name, args] = typeof each === 'string' ? [each, {}] : each
    await connection.call(name, args)
    for (const event of await lsquare.calls()) if (!lsquare.order.includes(event.callId)) lsquare.order.push(event.callId)
  }
  return (await lsquare.calls()).slice(before)
}

const caught = (error: { code?: unknown; message?: unknown }) => ({ error: { code: error.code, message: error.message } })

async function connect1(server: any): Promise<Connection> {
  const [a, b] = Memory1.createLinkedPair()
  await server.connect(a)
  const client = new Client1({ name: 'claude-code', version: '2.1' })
  await client.connect(b)
  return { call: (name, args = {}) => client.callTool({ name, arguments: args }).catch(caught), instructions: () => client.getInstructions() }
}

async function connect2(server: any): Promise<Connection> {
  const [a, b] = Memory2.createLinkedPair()
  await server.connect(a)
  const client = new Client2({ name: 'claude-code', version: '2.1' })
  await client.connect(b)
  return { call: (name, args = {}) => client.callTool({ name, arguments: args }).catch(caught), instructions: () => client.getInstructions() }
}

interface HighKit {
  name: string
  /** A high-level server made with these instructions: none when left out. */
  server(instructions?: unknown): any
  connect(server: any): Promise<Connection>
}

const options = (instructions: unknown) => (instructions === undefined ? {} : { instructions: instructions as string })
const high1: HighKit = { name: 'SDK 1.x', server: (instructions) => new McpServer1(INFO, options(instructions)), connect: connect1 }
const high2: HighKit = { name: 'SDK 2.x', server: (instructions) => new McpServer2(INFO, options(instructions)), connect: connect2 }

describe.each([high1, high2])('$name: server instructions on a high-level server', (kit) => {
  const ok = async () => text('ok')
  const tools = (server: any) => {
    server.registerTool('search', { description: 'Search orders by text.', inputSchema: z.object({}) }, ok)
    server.registerTool('plain', { inputSchema: z.object({}) }, ok)
    return server
  }

  it('carries the instructions the client was given, on every call, whichever the tool', async () => {
    const lsquare = setup()
    const connection = await kit.connect(tools(instrument(kit.server(INSTRUCTIONS), lsquare.client)))
    expect(connection.instructions()).toBe(INSTRUCTIONS)
    const [search, plain, again] = await callAll(lsquare, connection, 'search', 'plain', 'search')
    expect(search).toMatchObject({ toolName: 'search', toolDescription: 'Search orders by text.', serverInstructions: INSTRUCTIONS })
    expect(plain).toMatchObject({ toolName: 'plain', serverInstructions: INSTRUCTIONS })
    expect(plain).not.toHaveProperty('toolDescription')
    expect(again?.serverInstructions).toBe(INSTRUCTIONS)
  })

  it.each([
    ['none', undefined],
    ['an empty string', ''],
  ])('has none on a server made with %s, which gives clients none', async (_what, instructions) => {
    const lsquare = setup()
    const connection = await kit.connect(tools(instrument(kit.server(instructions), lsquare.client)))
    expect(connection.instructions()).toBeUndefined()
    const [event] = await callAll(lsquare, connection, 'search')
    expect(event).toMatchObject({ toolName: 'search', status: 'completed' })
    expect(event).not.toHaveProperty('serverInstructions')
    expect(event).not.toHaveProperty('dropped')
  })

  it.each([[42], [{ text: INSTRUCTIONS }], [[INSTRUCTIONS]]])('has none, without a word, when the instance holds %j', async (held) => {
    const lsquare = setup()
    const server = tools(instrument(kit.server(INSTRUCTIONS), lsquare.client))
    const connection = await kit.connect(server)
    // Not a string: nothing a client could have been given as instructions.
    server.server._instructions = held
    const [event] = await callAll(lsquare, connection, 'search')
    expect(event).toMatchObject({ toolName: 'search', status: 'completed' })
    expect(event).not.toHaveProperty('serverInstructions')
    expect(event).not.toHaveProperty('dropped')
  })

  it('records none with instructions: false, and the client is still given them', async () => {
    const lsquare = setup()
    const connection = await kit.connect(tools(instrument(kit.server(INSTRUCTIONS), lsquare.client, { instructions: false })))
    expect(connection.instructions()).toBe(INSTRUCTIONS)
    const [event] = await callAll(lsquare, connection, 'search')
    expect(event).toMatchObject({ toolName: 'search', toolDescription: 'Search orders by text.' })
    expect(event).not.toHaveProperty('serverInstructions')
  })

  it.each([[true], [undefined], [null], [0], ['no'], [() => false]])('records them with instructions: %j: only false turns them off', async (given) => {
    const lsquare = setup()
    const connection = await kit.connect(tools(instrument(kit.server(INSTRUCTIONS), lsquare.client, { instructions: given as never })))
    const [event] = await callAll(lsquare, connection, 'search')
    expect(event?.serverInstructions).toBe(INSTRUCTIONS)
  })

  it('keeps them with descriptions: false, and the description with instructions: false: each has its own option', async () => {
    const lsquare = setup()
    const connection = await kit.connect(tools(instrument(kit.server(INSTRUCTIONS), lsquare.client, { descriptions: false })))
    const [event] = await callAll(lsquare, connection, 'search')
    expect(event?.serverInstructions).toBe(INSTRUCTIONS)
    expect(event).not.toHaveProperty('toolDescription')
  })

  it('cuts long ones at 2,000 characters, and redacts them as any text', async () => {
    const long = setup()
    const [cut] = await callAll(long, await kit.connect(tools(instrument(kit.server(prose(2_500)), long.client))), 'plain')
    expect(cut?.serverInstructions).toBe(`${prose(2_000)}…[+500 chars]`)
    expect(cut?.truncated).toEqual(['serverInstructions'])
    const mail = setup()
    const [redacted] = await callAll(mail, await kit.connect(tools(instrument(kit.server('Mail billing@acme.io for refunds.'), mail.client))), 'plain')
    expect(redacted?.serverInstructions).toBe('Mail [EMAIL_1] for refunds.')
    expect(redacted?.redactions).toEqual({ email: 1 })
  })

  it('gives beforeSend the instructions, which may change them or take them out', async () => {
    const seen: unknown[] = []
    const lsquare = setup({
      beforeSend: (event) => {
        if (event.kind !== 'tool_call') return event
        seen.push(event.serverInstructions)
        const { serverInstructions, ...rest } = event
        return event.toolName === 'plain' ? rest : { ...rest, serverInstructions: `${serverInstructions} (reviewed)` }
      },
    })
    const [search, plain] = await callAll(lsquare, await kit.connect(tools(instrument(kit.server(INSTRUCTIONS), lsquare.client))), 'search', 'plain')
    expect(seen).toEqual([INSTRUCTIONS, INSTRUCTIONS])
    expect(search?.serverInstructions).toBe(`${INSTRUCTIONS} (reviewed)`)
    expect(plain).not.toHaveProperty('serverInstructions')
  })

  it('has none on a limited-diagnostics event, and has them for get_more_tools and for a tool the server never registered', async () => {
    const lsquare = setup()
    let fail = true
    const server = tools(
      instrument(kit.server(INSTRUCTIONS), lsquare.client, {
        identify: () => {
          if (fail) throw new Error('no session')
          return { userId: 'u_1' }
        },
      }),
    )
    const connection = await kit.connect(server)
    const [diagnostics] = await callAll(lsquare, connection, 'search')
    expect(diagnostics).toMatchObject({ toolName: 'search', identity: 'failed' })
    expect(diagnostics).not.toHaveProperty('serverInstructions')
    fail = false
    const [missing, unknown] = await callAll(lsquare, connection, ['get_more_tools', { need: 'refunds' }], 'nope')
    // The server's, not the tool's: the wrapper's own tool is called under them too.
    expect(missing).toMatchObject({ toolName: 'get_more_tools', extra: 'missing_tools', serverInstructions: INSTRUCTIONS })
    expect(missing).not.toHaveProperty('toolDescription')
    expect(unknown).toMatchObject({ toolName: 'nope', status: 'error', serverInstructions: INSTRUCTIONS })
  })

  it('reads the instance, never a polluted Object.prototype', async () => {
    const lsquare = setup()
    const connection = await kit.connect(tools(instrument(kit.server(), lsquare.client)))
    Object.defineProperty(Object.prototype, '_instructions', { value: 'Planted.', configurable: true, writable: true })
    try {
      const [event] = await callAll(lsquare, connection, 'search')
      expect(event).toMatchObject({ toolName: 'search', status: 'completed' })
      expect(event).not.toHaveProperty('serverInstructions')
    } finally {
      delete (Object.prototype as { _instructions?: unknown })._instructions
    }
  })
})

interface LowKit {
  name: string
  /** A low-level server with one tool, made with these instructions. */
  server(instructions?: string): any
  connect(server: any): Promise<Connection>
  /** The context the SDK gives a handler, for a call made without a connection. */
  context(): Record<string, unknown>
}

const SCHEMA = { type: 'object', properties: { q: { type: 'string' } } }
const CAPABILITIES = { capabilities: { tools: {} } }

function handlers(server: any, register: (method: 'tools/list' | 'tools/call', handler: () => unknown) => void): any {
  register('tools/list', async () => ({ tools: [{ name: 'search', description: 'Search orders.', inputSchema: SCHEMA }] }))
  register('tools/call', async () => text('ok'))
  return server
}

const low1: LowKit = {
  name: 'SDK 1.x',
  server(instructions) {
    const server = new Server1(INFO, { ...CAPABILITIES, ...options(instructions) })
    return handlers(server, (method, handler) => server.setRequestHandler(method === 'tools/list' ? ListToolsRequestSchema : CallToolRequestSchema, handler as never))
  },
  connect: connect1,
  context: () => ({ signal: new AbortController().signal, sendNotification: async () => {}, sendRequest: async () => {}, requestId: 1 }),
}

const low2: LowKit = {
  name: 'SDK 2.x',
  server(instructions) {
    const server = new Server2(INFO, { ...CAPABILITIES, ...options(instructions) })
    return handlers(server, (method, handler) => server.setRequestHandler(method, handler as never))
  },
  connect: connect2,
  context: () => ({ mcpReq: { id: 1, method: 'tools/call', signal: new AbortController().signal, envelope: {}, requestState: () => undefined } }),
}

describe.each([low1, low2])('$name: server instructions on a low-level server', (kit) => {
  it('carries them from the first call, before any listing: they are the instance’s, not a listing’s', async () => {
    const lsquare = setup()
    const connection = await kit.connect(instrument(kit.server(INSTRUCTIONS), lsquare.client))
    expect(connection.instructions()).toBe(INSTRUCTIONS)
    const [event] = await callAll(lsquare, connection, 'search')
    expect(event).toMatchObject({ toolName: 'search', serverInstructions: INSTRUCTIONS })
    // The description is the one the instance last listed, and it has listed none yet.
    expect(event).not.toHaveProperty('toolDescription')
  })

  it('carries them on a call that reaches an instance no client initialized, as a stateless server’s do', async () => {
    const lsquare = setup()
    const server = instrument(kit.server(INSTRUCTIONS), lsquare.client)
    await server._requestHandlers.get('tools/call')({ method: 'tools/call', params: { name: 'search', arguments: {} } }, kit.context())
    const [event] = await lsquare.calls()
    expect(event).toMatchObject({ toolName: 'search', status: 'completed', serverInstructions: INSTRUCTIONS })
  })

  it('has none on a server made without, and records none with instructions: false', async () => {
    const without = setup()
    const [none] = await callAll(without, await kit.connect(instrument(kit.server(), without.client)), 'search')
    expect(none).toMatchObject({ toolName: 'search', status: 'completed' })
    expect(none).not.toHaveProperty('serverInstructions')
    const off = setup()
    const [event] = await callAll(off, await kit.connect(instrument(kit.server(INSTRUCTIONS), off.client, { instructions: false })), 'search')
    expect(event).toMatchObject({ toolName: 'search', status: 'completed' })
    expect(event).not.toHaveProperty('serverInstructions')
  })
})
