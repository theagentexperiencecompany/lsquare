// lsquare/mcp's tool descriptions (spec section 4): each call carries its tool's description as
// agents were listed it, from the registry on a high-level server and from the instance's own
// listings on a low-level one, against real servers and clients of both MCP SDKs.
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
import { describe as note, newLists } from '../src/mcp/lists.js'

const text = (value: string) => ({ content: [{ type: 'text' as const, text: value }] })
const prose = (chars: number) => 'lorem ipsum '.repeat(Math.ceil(chars / 12)).slice(0, chars)
const ON = { sessions: true, intent: true, missingTools: true }
const UNANSWERED = "[lsquare] The descriptions function threw or returned a promise: descriptions aren't recorded for the calls it answered that way."

const rejected: string[] = []
afterEach(() => {
  expect(rejected).toEqual([])
  rejected.length = 0
})

function setup(options: LSquareOptions = {}) {
  const events: LSquareEvent[] = []
  const logs: string[] = []
  const fetch = (async (_url: unknown, init?: RequestInit) => {
    const batch = JSON.parse(String(init?.body)).events as LSquareEvent[]
    for (const event of batch) {
      const verdict = validateEvent(event)
      if (!verdict.ok) rejected.push(`${event.kind}: ${verdict.reason}`)
    }
    events.push(...batch)
    return new Response(JSON.stringify({ accepted: batch.map((e) => e.eventId), duplicates: [], rejected: [] }))
  }) as typeof globalThis.fetch
  const client = new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test', fetch, logger: { info: (m) => logs.push(m), warn: (m) => logs.push(m) }, onError: () => {}, ...options })
  /** The calls sent so far, in the order they were made. */
  const calls = async () => {
    await client.flush()
    return events.filter((e): e is ToolCallEvent => e.kind === 'tool_call').sort((a, b) => order.indexOf(a.callId) - order.indexOf(b.callId))
  }
  const order: string[] = []
  return { client, calls, logs, order }
}

type Lsquare = ReturnType<typeof setup>

interface Connection {
  call(name: string, args?: Record<string, unknown>): Promise<any>
  list(cursor?: string): Promise<any>
}

/** Calls one after the other, so their events are told apart by the order they were made in. */
async function callAll(lsquare: Lsquare, connection: Connection, ...names: Array<string | [string, Record<string, unknown>]>): Promise<ToolCallEvent[]> {
  const before = (await lsquare.calls()).length
  for (const each of names) {
    const [name, args] = typeof each === 'string' ? [each, {}] : each
    await connection.call(name, args)
    const seen = await lsquare.calls()
    for (const event of seen) if (!lsquare.order.includes(event.callId)) lsquare.order.push(event.callId)
  }
  return (await lsquare.calls()).slice(before)
}

const caught = (error: { code?: unknown; message?: unknown }) => ({ error: { code: error.code, message: error.message } })

interface HighKit {
  name: string
  server(): any
  connect(server: any): Promise<Connection>
  /** Sets the low-level server's handler for a method, as an app replacing McpServer's own would. */
  replace(server: any, method: 'tools/list' | 'tools/call', handler: (request: any, context: any) => unknown): void
}

const high1: HighKit = {
  name: 'SDK 1.x',
  server: () => new McpServer1({ name: 'orders', version: '1.0.0' }),
  async connect(server) {
    const [a, b] = Memory1.createLinkedPair()
    await server.connect(a)
    const client = new Client1({ name: 'claude-code', version: '2.1' })
    await client.connect(b)
    return { call: (name, args = {}) => client.callTool({ name, arguments: args }).catch(caught), list: (cursor) => client.listTools(cursor ? { cursor } : {}) }
  },
  replace: (server, method, handler) => server.server.setRequestHandler(method === 'tools/list' ? ListToolsRequestSchema : CallToolRequestSchema, handler as never),
}

const high2: HighKit = {
  name: 'SDK 2.x',
  server: () => new McpServer2({ name: 'orders', version: '1.0.0' }),
  async connect(server) {
    const [a, b] = Memory2.createLinkedPair()
    await server.connect(a)
    const client = new Client2({ name: 'claude-code', version: '2.1' })
    await client.connect(b)
    return { call: (name, args = {}) => client.callTool({ name, arguments: args }).catch(caught), list: (cursor) => client.listTools(cursor ? { cursor } : {}) }
  },
  replace: (server, method, handler) => server.server.setRequestHandler(method, handler as never),
}

const handlerOf = (server: any, method: string) => (server.server ?? server)._requestHandlers.get(method)

describe.each([high1, high2])('$name: tool descriptions on a high-level server', (kit) => {
  const ok = async () => text('ok')

  it("carries each tool's registered description, and none for a tool without one", async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client)
    server.registerTool('search', { description: 'Search orders by text.', inputSchema: z.object({ q: z.string() }) }, ok)
    server.registerTool('plain', { inputSchema: z.object({}) }, ok)
    server.registerTool('empty', { description: '', inputSchema: z.object({}) }, ok)
    const connection = await kit.connect(server)
    const [search, plain, empty] = await callAll(lsquare, connection, ['search', { q: 'a' }], 'plain', 'empty')
    expect(search).toMatchObject({ toolName: 'search', toolDescription: 'Search orders by text.' })
    expect(plain).toMatchObject({ toolName: 'plain' })
    expect(plain).not.toHaveProperty('toolDescription')
    expect(empty).not.toHaveProperty('toolDescription')
  })

  it('needs no listing: the first call of a new instance has it', async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client, { extras: false })
    server.registerTool('search', { description: 'Search orders by text.', inputSchema: z.object({}) }, ok)
    const [event] = await callAll(lsquare, await kit.connect(server), 'search')
    expect(event?.toolDescription).toBe('Search orders by text.')
  })

  it('reads it as the tool is when the call comes: a description changed later is the next call’s', async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client)
    const registered = server.registerTool('search', { description: 'Search orders.', inputSchema: z.object({}) }, ok)
    const connection = await kit.connect(server)
    const [first] = await callAll(lsquare, connection, 'search')
    registered.update({ description: 'Search orders and returns.' })
    const [second] = await callAll(lsquare, connection, 'search')
    expect([first?.toolDescription, second?.toolDescription]).toEqual(['Search orders.', 'Search orders and returns.'])
  })

  it('records none with descriptions: false', async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client, { descriptions: false })
    server.registerTool('search', { description: 'Search orders.', inputSchema: z.object({}) }, ok)
    const [event] = await callAll(lsquare, await kit.connect(server), 'search')
    expect(event).toMatchObject({ toolName: 'search', status: 'completed' })
    expect(event).not.toHaveProperty('toolDescription')
  })

  it('asks a descriptions function once a call, by tool', async () => {
    const lsquare = setup()
    const asked: string[] = []
    const server = instrument(kit.server(), lsquare.client, {
      descriptions: (toolName) => {
        asked.push(toolName)
        return toolName !== 'internal'
      },
    })
    server.registerTool('search', { description: 'Search orders.', inputSchema: z.object({}) }, ok)
    server.registerTool('internal', { description: 'Reads db-7.internal.', inputSchema: z.object({}) }, ok)
    const [search, internal] = await callAll(lsquare, await kit.connect(server), 'search', 'internal')
    expect(search?.toolDescription).toBe('Search orders.')
    expect(internal).not.toHaveProperty('toolDescription')
    expect(asked).toEqual(['search', 'internal'])
  })

  it.each([
    ['throws', () => { throw new Error('no') }],
    ['returns a promise', () => Promise.resolve(true)],
  ])('records none for a call whose descriptions function %s, and says so once', async (_how, descriptions) => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client, { descriptions: descriptions as never })
    server.registerTool('search', { description: 'Search orders.', inputSchema: z.object({}) }, ok)
    const events = await callAll(lsquare, await kit.connect(server), 'search', 'search')
    expect(events).toHaveLength(2)
    for (const event of events) expect(event).not.toHaveProperty('toolDescription')
    expect(lsquare.logs.filter((line) => line === UNANSWERED)).toHaveLength(1)
  })

  it('cuts a long one at 1,000 characters, and redacts it as any text', async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client)
    server.registerTool('long', { description: prose(1500), inputSchema: z.object({}) }, ok)
    server.registerTool('mail', { description: 'Mail billing@acme.io for refunds.', inputSchema: z.object({}) }, ok)
    const [long, mail] = await callAll(lsquare, await kit.connect(server), 'long', 'mail')
    expect(long?.toolDescription).toBe(`${prose(1000)}…[+500 chars]`)
    expect(long?.truncated).toEqual(['toolDescription'])
    expect(mail?.toolDescription).toBe('Mail [EMAIL_1] for refunds.')
    expect(mail?.redactions).toEqual({ email: 1 })
  })

  it('gives beforeSend the description, which may change it or take it out', async () => {
    const seen: unknown[] = []
    const lsquare = setup({
      beforeSend: (event) => {
        if (event.kind !== 'tool_call') return event
        seen.push(event.toolDescription)
        const { toolDescription, ...rest } = event
        return event.toolName === 'hidden' ? rest : { ...rest, toolDescription: `${toolDescription} (reviewed)` }
      },
    })
    const server = instrument(kit.server(), lsquare.client)
    server.registerTool('search', { description: 'Search orders.', inputSchema: z.object({}) }, ok)
    server.registerTool('hidden', { description: 'Internal.', inputSchema: z.object({}) }, ok)
    const [search, hidden] = await callAll(lsquare, await kit.connect(server), 'search', 'hidden')
    expect(seen).toEqual(['Search orders.', 'Internal.'])
    expect(search?.toolDescription).toBe('Search orders. (reviewed)')
    expect(hidden).not.toHaveProperty('toolDescription')
  })

  it('has none on a limited-diagnostics event, nor for get_more_tools or a tool the server never registered', async () => {
    const lsquare = setup()
    let fail = true
    const server = instrument(kit.server(), lsquare.client, {
      identify: () => {
        if (fail) throw new Error('no session')
        return { userId: 'u_1' }
      },
    })
    server.registerTool('search', { description: 'Search orders.', inputSchema: z.object({}) }, ok)
    const connection = await kit.connect(server)
    const [diagnostics] = await callAll(lsquare, connection, 'search')
    expect(diagnostics).toMatchObject({ toolName: 'search', identity: 'failed' })
    expect(diagnostics).not.toHaveProperty('toolDescription')
    fail = false
    const [missing, unknown, search] = await callAll(lsquare, connection, ['get_more_tools', { need: 'refunds' }], 'nope', 'search')
    expect(missing).toMatchObject({ toolName: 'get_more_tools', extra: 'missing_tools' })
    expect(missing).not.toHaveProperty('toolDescription')
    expect(unknown).toMatchObject({ toolName: 'nope', status: 'error' })
    expect(unknown).not.toHaveProperty('toolDescription')
    expect(search?.toolDescription).toBe('Search orders.')
  })

  it('leaves the listing alone without extras: the registry is where its descriptions are', async () => {
    const lsquare = setup()
    const server = kit.server()
    server.registerTool('search', { description: 'Search orders.', inputSchema: z.object({}) }, ok)
    const list = handlerOf(server, 'tools/list')
    instrument(server, lsquare.client, { extras: false })
    expect(handlerOf(server, 'tools/list')).toBe(list)
  })

  it('reads a server whose tools/call handler was replaced as a low-level one: the description is the one it last listed', async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client, { extras: false })
    server.registerTool('search', { description: 'Search orders.', inputSchema: z.object({}) }, ok)
    kit.replace(server, 'tools/call', async () => text('mine'))
    const connection = await kit.connect(server)
    const [before] = await callAll(lsquare, connection, 'search')
    expect((await connection.list()).tools.map((tool: any) => tool.description)).toEqual(['Search orders.'])
    const [after] = await callAll(lsquare, connection, 'search')
    expect(before).not.toHaveProperty('toolDescription')
    expect(after?.toolDescription).toBe('Search orders.')
  })
})

interface Listed {
  name: string
  description?: unknown
  inputSchema?: unknown
}

interface Fixture {
  server: any
  tools: Listed[]
  /** How many times the server's own tools/list handler ran. */
  lists: number
  /** When set, the page listed for a cursor, instead of the tools. */
  page: ((cursor: unknown) => unknown) | undefined
}

interface LowKit {
  name: string
  make(options?: { pageSize?: number }): Fixture
  connect(server: any): Promise<Connection>
  register(server: any, method: 'tools/list' | 'tools/call', handler: (request: any, context: any) => unknown): void
  context(): Record<string, unknown>
}

const SCHEMA = { type: 'object', properties: { q: { type: 'string' } } }

function fixture(kit: LowKit, server: any, pageSize: number | undefined): Fixture {
  const state: Fixture = { server, tools: [{ name: 'search', description: 'Search orders.', inputSchema: SCHEMA }, { name: 'plain', inputSchema: SCHEMA }], lists: 0, page: undefined }
  kit.register(server, 'tools/list', async (request) => {
    const cursor = request.params?.cursor
    state.lists++
    if (state.page) return state.page(cursor)
    if (pageSize === undefined) return { tools: state.tools }
    const from = cursor === undefined ? 0 : Number(cursor)
    const next = from + pageSize
    return { tools: state.tools.slice(from, next), ...(next < state.tools.length ? { nextCursor: String(next) } : {}) }
  })
  kit.register(server, 'tools/call', async () => text('ok'))
  return state
}

const low1: LowKit = {
  name: 'SDK 1.x',
  make: ({ pageSize } = {}) => fixture(low1, new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: { listChanged: true } } }), pageSize),
  async connect(server) {
    const [a, b] = Memory1.createLinkedPair()
    await server.connect(a)
    const client = new Client1({ name: 'claude-code', version: '2.1' })
    await client.connect(b)
    return { call: (name, args = {}) => client.callTool({ name, arguments: args }).catch(caught), list: (cursor) => client.listTools(cursor ? { cursor } : {}) }
  },
  register: (server, method, handler) => server.setRequestHandler(method === 'tools/list' ? ListToolsRequestSchema : CallToolRequestSchema, handler as never),
  context: () => ({ signal: new AbortController().signal, sendNotification: async () => {}, sendRequest: async () => {}, requestId: 1 }),
}

const low2: LowKit = {
  name: 'SDK 2.x',
  make: ({ pageSize } = {}) => fixture(low2, new Server2({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: { listChanged: true } } }), pageSize),
  async connect(server) {
    const [a, b] = Memory2.createLinkedPair()
    await server.connect(a)
    const client = new Client2({ name: 'claude-code', version: '2.1' })
    await client.connect(b)
    return { call: (name, args = {}) => client.callTool({ name, arguments: args }).catch(caught), list: (cursor) => client.listTools(cursor ? { cursor } : {}) }
  },
  register: (server, method, handler) => server.setRequestHandler(method, handler as never),
  context: () => ({ mcpReq: { id: 1, method: 'tools/list', signal: new AbortController().signal, envelope: {}, requestState: () => undefined } }),
}

describe.each([low1, low2])('$name: tool descriptions on a low-level server', (kit) => {
  it('has none until the instance lists its tools, then the one it listed', async () => {
    const lsquare = setup()
    const fixed = kit.make()
    instrument(fixed.server, lsquare.client)
    const connection = await kit.connect(fixed.server)
    const [before] = await callAll(lsquare, connection, 'search')
    // Never read by the wrapper itself: the extras are off.
    expect(fixed.lists).toBe(0)
    expect((await connection.list()).tools).toEqual(fixed.tools)
    const [search, plain] = await callAll(lsquare, connection, 'search', 'plain')
    expect(before).toMatchObject({ toolName: 'search', status: 'completed' })
    expect(before).not.toHaveProperty('toolDescription')
    expect(search?.toolDescription).toBe('Search orders.')
    expect(plain).not.toHaveProperty('toolDescription')
    expect(fixed.lists).toBe(1)
  })

  it('follows the listings: a description changed, then one taken away', async () => {
    const lsquare = setup()
    const fixed = kit.make()
    instrument(fixed.server, lsquare.client)
    const connection = await kit.connect(fixed.server)
    await connection.list()
    fixed.tools = [{ name: 'search', description: 'Search orders and returns.', inputSchema: SCHEMA }]
    // Still the one last listed, until the next listing.
    const [stale] = await callAll(lsquare, connection, 'search')
    await connection.list()
    const [changed] = await callAll(lsquare, connection, 'search')
    fixed.tools = [{ name: 'search', inputSchema: SCHEMA }]
    await connection.list()
    const [gone] = await callAll(lsquare, connection, 'search')
    expect([stale?.toolDescription, changed?.toolDescription]).toEqual(['Search orders.', 'Search orders and returns.'])
    expect(gone).not.toHaveProperty('toolDescription')
  })

  it('notes every page as it goes by', async () => {
    const lsquare = setup()
    const fixed = kit.make({ pageSize: 1 })
    fixed.tools = [
      { name: 'search', description: 'Search orders.', inputSchema: SCHEMA },
      { name: 'refund', description: 'Refund an order.', inputSchema: SCHEMA },
    ]
    instrument(fixed.server, lsquare.client)
    const connection = await kit.connect(fixed.server)
    // Each page asked for by itself: SDK 2.x's client follows the cursors on its own.
    const handler = handlerOf(fixed.server, 'tools/list')
    const first = await handler({ method: 'tools/list', params: {} }, kit.context())
    expect(first).toEqual({ tools: [fixed.tools[0]], nextCursor: '1' })
    const [early] = await callAll(lsquare, connection, 'refund')
    expect(await handler({ method: 'tools/list', params: { cursor: first.nextCursor } }, kit.context())).toEqual({ tools: [fixed.tools[1]] })
    const [search, refund] = await callAll(lsquare, connection, 'search', 'refund')
    expect(early).not.toHaveProperty('toolDescription')
    expect([search?.toolDescription, refund?.toolDescription]).toEqual(['Search orders.', 'Refund an order.'])
  })

  it('gives the client the page as the handler answered it', async () => {
    const lsquare = setup()
    const fixed = kit.make()
    instrument(fixed.server, lsquare.client)
    const page = { tools: [{ name: 'search', description: 'Search orders.', inputSchema: SCHEMA }], _meta: { served: 'east' } }
    fixed.page = () => page
    const handler = handlerOf(fixed.server, 'tools/list')
    expect(await handler({ method: 'tools/list', params: {} }, kit.context())).toBe(page)
    for (const odd of [{ tools: 'none' }, { tools: [null, 3, { name: 7 }, { name: 'odd', description: 12 }] }, null, 'no page']) {
      fixed.page = () => odd
      expect(await handler({ method: 'tools/list', params: {} }, kit.context())).toBe(odd)
    }
    const failure = new Error('the list is down')
    fixed.page = () => {
      throw failure
    }
    await expect(handler({ method: 'tools/list', params: {} }, kit.context())).rejects.toBe(failure)
    // What the first page listed is still the tool's; a description that isn't a string is none.
    const connection = await kit.connect(fixed.server)
    const [search, odd] = await callAll(lsquare, connection, 'search', 'odd')
    expect(search?.toolDescription).toBe('Search orders.')
    expect(odd).not.toHaveProperty('toolDescription')
  })

  it('with descriptions: false and no extras, leaves the listing unwrapped and reads nothing of it', async () => {
    const lsquare = setup()
    const fixed = kit.make()
    const list = handlerOf(fixed.server, 'tools/list')
    instrument(fixed.server, lsquare.client, { descriptions: false })
    expect(handlerOf(fixed.server, 'tools/list')).toBe(list)
    let read = 0
    fixed.page = () => ({
      tools: [
        {
          name: 'search',
          inputSchema: SCHEMA,
          get description() {
            read++
            return 'Search orders.'
          },
        },
      ],
    })
    const connection = await kit.connect(fixed.server)
    await connection.list()
    // Once, by the SDK, as it writes the reply.
    const written = read
    const [event] = await callAll(lsquare, connection, 'search')
    expect(event).not.toHaveProperty('toolDescription')
    expect(read).toBe(written)
  })

  it('with the extras on, has it from the first call: the snapshot the extras read lists it', async () => {
    const lsquare = setup()
    const fixed = kit.make()
    instrument(fixed.server, lsquare.client, { extras: ON })
    const connection = await kit.connect(fixed.server)
    const [first] = await callAll(lsquare, connection, 'search')
    expect(first?.toolDescription).toBe('Search orders.')
    // And from a page a client is listed, extras added or not.
    fixed.tools = [{ name: 'search', description: 'Search orders and returns.', inputSchema: SCHEMA }]
    await connection.list()
    const [second] = await callAll(lsquare, connection, 'search')
    expect(second?.toolDescription).toBe('Search orders and returns.')
  })

  it('with the extras on and descriptions: false, reads no description, from a page or the snapshot', async () => {
    const lsquare = setup()
    const fixed = kit.make()
    let read = 0
    const described = () => ({
      name: 'search',
      inputSchema: SCHEMA,
      get description() {
        read++
        return 'Search orders.'
      },
    })
    fixed.page = () => ({ tools: [described()] })
    instrument(fixed.server, lsquare.client, { extras: ON, descriptions: false })
    const connection = await kit.connect(fixed.server)
    const [event] = await callAll(lsquare, connection, 'search')
    expect(event).not.toHaveProperty('toolDescription')
    // The snapshot's read of the list ran the handler, and never read the description.
    expect(fixed.lists).toBeGreaterThan(0)
    expect(read).toBe(0)
  })

  it('forgets what a tools/list handler listed once another takes its place', async () => {
    const lsquare = setup()
    const fixed = kit.make()
    instrument(fixed.server, lsquare.client)
    const connection = await kit.connect(fixed.server)
    await connection.list()
    const [listed] = await callAll(lsquare, connection, 'search')
    kit.register(fixed.server, 'tools/list', async () => ({ tools: [{ name: 'search', description: 'Find an order.', inputSchema: SCHEMA }] }))
    const [forgotten] = await callAll(lsquare, connection, 'search')
    await connection.list()
    const [again] = await callAll(lsquare, connection, 'search')
    expect(listed?.toolDescription).toBe('Search orders.')
    expect(forgotten).not.toHaveProperty('toolDescription')
    expect(again?.toolDescription).toBe('Find an order.')
  })
})

describe('the descriptions an instance keeps', () => {
  const clock = { now: () => 0 }

  it('keeps none when descriptions are off', () => {
    const lists = newLists(clock)
    note(lists, 'search', 'Search orders.')
    expect(lists.described).toBeUndefined()
  })

  it('keeps a string that isn’t empty, and takes a name out for anything else', () => {
    const lists = newLists(clock, true)
    note(lists, 'search', 'Search orders.')
    note(lists, 'refund', 'Refund an order.')
    expect([...lists.described!]).toEqual([['search', 'Search orders.'], ['refund', 'Refund an order.']])
    for (const none of ['', undefined, null, 7, { text: 'x' }]) {
      note(lists, 'search', 'Search orders.')
      note(lists, 'search', none)
      expect(lists.described!.has('search')).toBe(false)
    }
  })

  it('keeps at most 10,000 names: past that a new one isn’t noted, and one it has still follows its listings', () => {
    const lists = newLists(clock, true)
    for (let index = 0; index < 10_000; index++) note(lists, `tool_${index}`, 'A tool.')
    note(lists, 'one_more', 'One more.')
    note(lists, 'tool_0', 'The first tool.')
    expect(lists.described!.size).toBe(10_000)
    expect(lists.described!.has('one_more')).toBe(false)
    expect(lists.described!.get('tool_0')).toBe('The first tool.')
    note(lists, 'tool_1', undefined)
    note(lists, 'one_more', 'One more.')
    expect(lists.described!.get('one_more')).toBe('One more.')
  })
})
