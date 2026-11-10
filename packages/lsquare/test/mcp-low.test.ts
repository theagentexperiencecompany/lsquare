// lsquare/mcp's extras on low-level servers (spec section 4, "Low-level servers"): off unless
// passed as true, decided by a snapshot of the server's own tool list.
import { AsyncLocalStorage, AsyncResource } from 'node:async_hooks'
import { Client as Client1 } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport as Memory1 } from '@modelcontextprotocol/sdk/inMemory.js'
import { McpServer as McpServer1 } from '@modelcontextprotocol/sdk/server/mcp.js'
import { Server as Server1 } from '@modelcontextprotocol/sdk/server/index.js'
import { CallToolRequestSchema, EmptyResultSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { Client as Client2 } from '@modelcontextprotocol/client'
import { InMemoryTransport as Memory2, Server as Server2 } from '@modelcontextprotocol/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { z } from 'zod'
import { validateEvent } from '../../contract/src/validate.js'
import { LSquare, type LSquareEvent, type LSquareOptions, type ToolCallEvent } from '../src/index.js'
import { listedOwnership } from '../src/mcp/extras.js'
import { instrument, type InstrumentOptions } from '../src/mcp/index.js'

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const tick = () => new Promise<void>((resolve) => setImmediate(() => setImmediate(resolve)))
const text = (value: string) => ({ content: [{ type: 'text' as const, text: value }] })
const ID = /^ses_[A-Za-z0-9_-]{22}$/
const SENT = 'ses_AbCdEfGhIjKlMnOpQrStUv'
const LINE = /^Task ID: (ses_[A-Za-z0-9_-]{22})\. Send it as session_id on later calls for this task, and pass it to any subagent working on it\.$/
const ON = { sessions: true, intent: true, missingTools: true }
const REPLY = 'Noted. No other tools are available right now.'

const issued = (reply: any): string | undefined => {
  const last = reply?.content?.at?.(-1)
  return last?.type === 'text' ? LINE.exec(last.text)?.[1] : undefined
}
const withoutLine = (reply: any) => (issued(reply) ? { ...reply, content: reply.content.slice(0, -1) } : reply)

const rejected: string[] = []
afterEach(() => {
  vi.useRealTimers()
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
  const client = new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test', fetch, logger: { info: (m) => logs.push(m), warn: (m) => logs.push(m) }, ...options })
  const calls = async () => {
    await client.flush()
    return events.filter((e): e is ToolCallEvent => e.kind === 'tool_call')
  }
  return { client, calls, logs }
}

interface Listed {
  name: string
  inputSchema?: unknown
  description?: string
}

/** A low-level server with a tool list the test changes as it goes, paged when asked, and what reached it. */
interface Fixture {
  server: any
  tools: Listed[]
  /** Each read of the list: its cursor and the context it was given. */
  lists: Array<{ cursor: unknown; context: any }>
  /** Each call that reached the handler: its tool and arguments. */
  seen: Array<{ name: string; args: unknown }>
  /** Run before each page is listed: a test can hold the list back, or make it fail. */
  beforeList: (context: any) => unknown
  /** Answers a call: the default echoes the arguments it got. */
  answer: (name: string, args: unknown, context: any) => unknown
  /** When set, the page listed for a cursor, instead of the tools. */
  page: ((cursor: unknown) => unknown) | undefined
}

interface Kit {
  name: string
  make(options?: { pageSize?: number }): Fixture
  connect(server: any): Promise<{ call(name: string, args?: Record<string, unknown>): Promise<any>; list(): Promise<any>; client: any }>
  /** The server's tools/list and tools/call handlers, called as the SDK would. */
  handler(server: any, method: 'tools/list' | 'tools/call'): (request: unknown, context: unknown) => Promise<any>
  /** A request context of the SDK's shape, for calling a handler directly. */
  context(more?: Record<string, unknown>): Record<string, unknown>
}

function fixture(server: any, pageSize: number | undefined, register: (method: 'tools/list' | 'tools/call', handler: (request: any, context: any) => unknown) => void): Fixture {
  const state: Fixture = {
    server,
    tools: [{ name: 'search', inputSchema: { type: 'object', properties: { q: { type: 'string' } } } }],
    lists: [],
    seen: [],
    beforeList: () => undefined,
    answer: (_name, args) => text(JSON.stringify(args)),
    page: undefined,
  }
  register('tools/list', async (request, context) => {
    const cursor = request.params?.cursor
    state.lists.push({ cursor, context })
    await state.beforeList(context)
    if (state.page) return state.page(cursor)
    if (pageSize === undefined) return { tools: state.tools }
    const from = cursor === undefined ? 0 : Number(cursor)
    const next = from + pageSize
    return { tools: state.tools.slice(from, next), ...(next < state.tools.length ? { nextCursor: String(next) } : {}) }
  })
  register('tools/call', async (request, context) => {
    state.seen.push({ name: request.params.name, args: request.params.arguments })
    return state.answer(request.params.name, request.params.arguments, context)
  })
  return state
}

const kit1: Kit = {
  name: 'SDK 1.x',
  make: ({ pageSize } = {}) => {
    const server = new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: { listChanged: true } } })
    return fixture(server, pageSize, (method, handler) => server.setRequestHandler(method === 'tools/list' ? ListToolsRequestSchema : CallToolRequestSchema, handler as never))
  },
  async connect(server) {
    const [a, b] = Memory1.createLinkedPair()
    await server.connect(a)
    const client = new Client1({ name: 'claude-code', version: '2.1' })
    await client.connect(b)
    return { call: (name, args = {}) => client.callTool({ name, arguments: args }), list: () => client.listTools(), client }
  },
  handler: (server, method) => server._requestHandlers.get(method),
  context: (more = {}) => ({ signal: new AbortController().signal, sendNotification: async () => {}, sendRequest: async () => {}, requestId: 1, ...more }),
}

const kit2: Kit = {
  name: 'SDK 2.x',
  make: ({ pageSize } = {}) => {
    const server = new Server2({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: { listChanged: true } } })
    return fixture(server, pageSize, (method, handler) => server.setRequestHandler(method, handler as never))
  },
  async connect(server) {
    const [a, b] = Memory2.createLinkedPair()
    await server.connect(a)
    const client = new Client2({ name: 'claude-code', version: '2.1' })
    await client.connect(b)
    return { call: (name, args = {}) => client.callTool({ name, arguments: args }), list: () => client.listTools(), client }
  },
  handler: (server, method) => server._requestHandlers.get(method),
  context: (more = {}) => ({ mcpReq: { id: 1, method: 'tools/call', signal: new AbortController().signal, envelope: {}, requestState: () => undefined, ...more } }),
}

const call = (name: string, args: Record<string, unknown> = {}) => ({ method: 'tools/call', params: { name, arguments: args } })
const tool = (listed: any, name: string) => listed.tools.find((entry: any) => entry.name === name)

describe.each([kit1, kit2])('$name: low-level extras', (kit) => {
  it('stays off unless each extra is passed as true', async () => {
    for (const options of [undefined, {}, { sessions: false }, { sessions: 'yes', intent: 1, missingTools: {} }]) {
      const lsquare = setup()
      const fixed = kit.make()
      instrument(fixed.server, lsquare.client, { extras: options as InstrumentOptions['extras'] })
      const connection = await kit.connect(fixed.server)
      expect((await connection.list()).tools).toEqual(fixed.tools)
      expect(await connection.call('search', { q: 'a', session_id: SENT, intent: 'why' })).toEqual(text(JSON.stringify({ q: 'a', session_id: SENT, intent: 'why' })))
      const [event] = await lsquare.calls()
      expect(event).not.toHaveProperty('sessionId')
      expect(event).not.toHaveProperty('intent')
      expect(fixed.lists).toHaveLength(1)
    }
  })

  it.each([
    [[true, false], true],
    [[false, true], false],
  ])('asks the inputs function once a call: answers %j', async (answers, noted) => {
    const lsquare = setup()
    let reads = 0
    const fixed = kit.make()
    instrument(fixed.server, lsquare.client, { extras: { missingTools: true }, results: false, inputs: () => (answers as boolean[])[reads++] ?? false })
    const reply = await (await kit.connect(fixed.server)).call('get_more_tools', { need: 'Cancel an order' })
    expect(JSON.stringify(reply).includes('Noted.')).toBe(noted)
    expect(reads).toBe(1)
    const [event] = await lsquare.calls()
    if (noted) expect(event).toMatchObject({ extra: 'missing_tools', input: { need: 'Cancel an order' } })
    else expect(event).not.toHaveProperty('extra')
  })

  it('records how a handler failed but none of its text with results off', async () => {
    const lsquare = setup()
    const fixed = kit.make()
    instrument(fixed.server, lsquare.client, { results: false })
    const connection = await kit.connect(fixed.server)
    fixed.answer = () => {
      throw new TypeError('the private note')
    }
    await connection.call('search', { q: 'a' }).catch(() => undefined)
    fixed.answer = () => {
      throw Object.assign(new Error('the private note'), { code: -32000 })
    }
    await connection.call('search', { q: 'b' }).catch(() => undefined)
    const [plain, coded] = await lsquare.calls()
    expect(plain!.error).toEqual({ type: 'TypeError', category: 'thrown' })
    expect(coded!.error).toEqual({ type: 'Error', code: -32000, category: 'protocol' })
    expect(JSON.stringify([plain, coded])).not.toContain('private note')
  })

  it.each([[false], [(name: string) => name !== 'get_more_tools']])("doesn't offer get_more_tools where its need wouldn't be recorded", async (inputs) => {
    const lsquare = setup()
    const fixed = kit.make()
    instrument(fixed.server, lsquare.client, { extras: { missingTools: true }, inputs })
    const connection = await kit.connect(fixed.server)
    expect((await connection.list()).tools.map((entry: any) => entry.name)).not.toContain('get_more_tools')
    const reply = await connection.call('get_more_tools', { need: 'Cancel an order' })
    expect(JSON.stringify(reply)).not.toContain('Noted.')
    // The server's own handler got the call, as it gets any other: nothing of the wrapper's answered it.
    expect(fixed.seen.at(-1)).toMatchObject({ name: 'get_more_tools' })
    const [event] = await lsquare.calls()
    expect(event).not.toHaveProperty('extra')
    expect(event).not.toHaveProperty('input')
  })

  it('turned on, lists the inputs and get_more_tools, takes them out of calls, and gives the first result a task ID', async () => {
    const lsquare = setup()
    const fixed = kit.make()
    instrument(fixed.server, lsquare.client, { extras: ON })
    const connection = await kit.connect(fixed.server)
    const listed = await connection.list()
    expect(Object.keys(tool(listed, 'search').inputSchema.properties)).toEqual(['q', 'session_id', 'intent'])
    expect(tool(listed, 'get_more_tools')).toMatchObject({ inputSchema: { required: ['need'] }, annotations: { readOnlyHint: true, openWorldHint: true } })
    const first = await connection.call('search', { q: 'a', intent: 'Find late orders' })
    expect(withoutLine(first)).toEqual(text('{"q":"a"}'))
    const id = issued(first)
    expect(id).toMatch(ID)
    expect(await connection.call('search', { q: 'b', session_id: id })).toEqual(text('{"q":"b"}'))
    expect(withoutLine(await connection.call('get_more_tools', { need: 'Refunds' }))).toEqual(text(REPLY))
    expect(fixed.seen.map((entry) => entry.args)).toEqual([{ q: 'a' }, { q: 'b' }])
    const [one, two, three] = await lsquare.calls()
    expect(one).toMatchObject({ sessionId: id, sessionSource: 'issued', intent: 'Find late orders', input: { q: 'a' } })
    expect(two).toMatchObject({ sessionId: id, sessionSource: 'sent' })
    expect(three).toMatchObject({ toolName: 'get_more_tools', extra: 'missing_tools', input: { need: 'Refunds' } })
  })

  it('turns each on by itself', async () => {
    for (const only of ['sessions', 'intent', 'missingTools'] as const) {
      const lsquare = setup()
      const fixed = kit.make()
      instrument(fixed.server, lsquare.client, { extras: { [only]: true } })
      const connection = await kit.connect(fixed.server)
      const listed = await connection.list()
      const properties = Object.keys(tool(listed, 'search').inputSchema.properties)
      const reply = await connection.call('search', { q: 'a', session_id: SENT, intent: 'why' })
      const missing = await connection.call('get_more_tools', { need: 'Refunds' }).catch((error: Error) => ({ error: error.message }))
      const [event] = await lsquare.calls()
      if (only === 'sessions') {
        expect(properties).toEqual(['q', 'session_id'])
        expect(fixed.seen[0]!.args).toEqual({ q: 'a', intent: 'why' })
        expect(event).toMatchObject({ sessionId: SENT, sessionSource: 'sent' })
        expect(event).not.toHaveProperty('intent')
      } else if (only === 'intent') {
        expect(properties).toEqual(['q', 'intent'])
        expect(fixed.seen[0]!.args).toEqual({ q: 'a', session_id: SENT })
        expect(event).toMatchObject({ intent: 'why' })
        expect(event).not.toHaveProperty('sessionId')
        expect(issued(reply)).toBeUndefined()
      } else {
        expect(properties).toEqual(['q'])
        expect(fixed.seen[0]!.args).toEqual({ q: 'a', session_id: SENT, intent: 'why' })
        expect(missing).toEqual(text(REPLY))
        expect(tool(listed, 'get_more_tools').inputSchema.properties).not.toHaveProperty('session_id')
      }
      expect(tool(listed, 'get_more_tools') !== undefined, only).toBe(only === 'missingTools')
    }
  })

  it('refuses get_more_tools without a string need as a protocol error, without an ID', async () => {
    const lsquare = setup()
    const fixed = kit.make()
    instrument(fixed.server, lsquare.client, { extras: ON })
    const handle = kit.handler(fixed.server, 'tools/call')
    for (const args of [{}, { need: 3 }]) {
      const reply = await handle(call('get_more_tools', args), kit.context())
      expect(reply).toMatchObject({ isError: true })
      expect(issued(reply)).toBeUndefined()
    }
    for (const event of await lsquare.calls()) {
      expect(event).toMatchObject({ extra: 'missing_tools', status: 'error', error: { category: 'protocol' } })
      expect(event).not.toHaveProperty('sessionId')
    }
    expect(fixed.seen).toEqual([])
  })

  it('calls the handler in the same step as its last check of the snapshot', async () => {
    const lsquare = setup()
    const fixed = kit.make()
    instrument(fixed.server, lsquare.client, { extras: ON })
    const handle = kit.handler(fixed.server, 'tools/call')
    await handle(call('search'), kit.context())
    // With a fresh snapshot the handler is reached before handle() returns: a change after it is a change after the call.
    const pending = handle(call('search', { session_id: 'first' }), kit.context())
    expect(fixed.seen).toHaveLength(2)
    fixed.tools[0] = { name: 'search', inputSchema: { type: 'object', properties: { session_id: { type: 'string' } } } }
    await fixed.server.sendToolListChanged().catch(() => {})
    await pending
    await handle(call('search', { session_id: 'second' }), kit.context())
    expect(fixed.seen.map((entry) => entry.args)).toEqual([{}, {}, { session_id: 'second' }])
  })

  it('reads every page of the list before a call, with a copy of the request context marked tools/list', async () => {
    const lsquare = setup()
    const fixed = kit.make({ pageSize: 1 })
    fixed.tools.push({ name: 'refund', inputSchema: { type: 'object', properties: {} } }, { name: 'resume', inputSchema: { type: 'object', properties: { session_id: { type: 'string' } } } })
    instrument(fixed.server, lsquare.client, { extras: ON })
    const authInfo = { token: 'secret', clientId: 'c', scopes: [] }
    const handle = kit.handler(fixed.server, 'tools/call')
    const context = kit === kit1 ? kit.context({ authInfo, _meta: { progressToken: 7 } }) : kit.context({ authInfo, _meta: { progressToken: 7 }, inputResponses: { a: {} }, requestState: () => 'state' })
    await handle(call('refund', { session_id: SENT, intent: 'why' }), context)
    await handle(call('resume', { session_id: 'theirs' }), kit.context())
    expect(fixed.lists.map((entry) => entry.cursor)).toEqual([undefined, '1', '2'])
    expect(fixed.seen.map((entry) => entry.args)).toEqual([{}, { session_id: 'theirs' }])
    const seenBy = fixed.lists[0]!.context
    if (kit === kit1) {
      expect(seenBy).toMatchObject({ authInfo, requestId: 1 })
      expect(seenBy).not.toHaveProperty('_meta')
      expect(seenBy.signal).not.toBe(context.signal)
    } else {
      expect(seenBy.mcpReq).toMatchObject({ method: 'tools/list', authInfo, id: 1 })
      expect(seenBy.mcpReq).not.toHaveProperty('_meta')
      expect(seenBy.mcpReq).not.toHaveProperty('inputResponses')
      expect(seenBy.mcpReq.requestState()).toBeUndefined()
      expect(seenBy.mcpReq.signal).not.toBe((context.mcpReq as any).signal)
    }
    // The call's own context is left as it was.
    expect(kit === kit1 ? context._meta : (context.mcpReq as any)._meta).toEqual({ progressToken: 7 })
  })

  it('reads the list again after list_changed, and after the tools/list handler is replaced', async () => {
    const lsquare = setup()
    const fixed = kit.make()
    instrument(fixed.server, lsquare.client, { extras: ON })
    const connection = await kit.connect(fixed.server)
    const handle = kit.handler(fixed.server, 'tools/call')
    await connection.call('search', { q: 'a', session_id: SENT })
    // The tool gains its own session_id, and the server says so.
    fixed.tools[0] = { name: 'search', inputSchema: { type: 'object', properties: { q: { type: 'string' }, session_id: { type: 'string' } } } }
    await fixed.server.sendToolListChanged()
    await connection.call('search', { q: 'b', session_id: 'theirs' })
    expect(fixed.seen.map((entry) => entry.args)).toEqual([{ q: 'a' }, { q: 'b', session_id: 'theirs' }])
    expect(fixed.lists).toHaveLength(2)
    // A new tools/list handler, without list_changed.
    const replacement = async () => ({ tools: [{ name: 'search', inputSchema: { type: 'object', properties: { q: { type: 'string' } } } }] })
    if (kit === kit1) fixed.server.setRequestHandler(ListToolsRequestSchema, replacement)
    else fixed.server.setRequestHandler('tools/list', replacement)
    await handle(call('search', { q: 'c', session_id: SENT }), kit.context())
    expect(fixed.seen[2]!.args).toEqual({ q: 'c' })
  })

  it('reads the list again when a client is told about a change from inside a handler', async () => {
    const lsquare = setup()
    const fixed = kit.make()
    instrument(fixed.server, lsquare.client, { extras: ON })
    const connection = await kit.connect(fixed.server)
    fixed.answer = async (name, args, context) => {
      if (name === 'add') {
        fixed.tools.push({ name: 'own', inputSchema: { type: 'object', properties: { intent: { type: 'string' } } } })
        const notification = { method: 'notifications/tools/list_changed' }
        await (kit === kit1 ? context.sendNotification(notification) : context.mcpReq.notify(notification))
      }
      return text(JSON.stringify(args))
    }
    await connection.call('add')
    await connection.call('own', { intent: 'theirs' })
    expect(fixed.seen.at(-1)!.args).toEqual({ intent: 'theirs' })
    expect(fixed.lists).toHaveLength(2)
  })

  it("marks the snapshot out of date when a client's listing disagrees with it, listing nothing more for what disagrees", async () => {
    const lsquare = setup()
    const fixed = kit.make()
    fixed.tools.push({ name: 'refund', inputSchema: { type: 'object', properties: {} } })
    instrument(fixed.server, lsquare.client, { extras: ON })
    const list = kit.handler(fixed.server, 'tools/list')
    const handle = kit.handler(fixed.server, 'tools/call')
    await handle(call('search'), kit.context())
    expect(fixed.lists).toHaveLength(1)
    // A tool changes without list_changed: the next listing shows it.
    fixed.tools[0] = { name: 'search', inputSchema: { type: 'object', properties: { q: { type: 'string' }, intent: { type: 'string' } } } }
    const changed = await list({ method: 'tools/list', params: {} }, kit.context())
    expect(tool(changed, 'search').inputSchema.properties).toEqual({ q: { type: 'string' }, intent: { type: 'string' } })
    expect(Object.keys(tool(changed, 'refund').inputSchema.properties)).toEqual(['session_id', 'intent'])
    expect(tool(changed, 'get_more_tools')).toBeUndefined()
    await handle(call('search', { intent: 'theirs' }), kit.context())
    expect(fixed.seen.at(-1)!.args).toEqual({ intent: 'theirs' })
    // A tool removed, in a list of one page.
    fixed.tools.pop()
    const shorter = await list({ method: 'tools/list', params: {} }, kit.context())
    expect(tool(shorter, 'get_more_tools')).toBeUndefined()
    const reads = fixed.lists.length
    await handle(call('search'), kit.context())
    expect(fixed.lists.length).toBeGreaterThan(reads)
    // A tool added: missing from the snapshot.
    fixed.tools.push({ name: 'added', inputSchema: { type: 'object' } })
    const longer = await list({ method: 'tools/list', params: {} }, kit.context())
    expect(tool(longer, 'added').inputSchema).toEqual({ type: 'object' })
    await handle(call('added', { session_id: SENT }), kit.context())
    expect(fixed.seen.at(-1)!.args).toEqual({})
  })

  it('leaves alone a tool whose listed schema says more than named properties', async () => {
    const lsquare = setup()
    const fixed = kit.make()
    fixed.tools = [
      { name: 'either', inputSchema: { type: 'object', anyOf: [{ properties: { a: {} } }] } },
      { name: 'open', inputSchema: { type: 'object', properties: {}, additionalProperties: true } },
      { name: 'patterned', inputSchema: { type: 'object', patternProperties: { '^s': {} } } },
      { name: 'limited', inputSchema: { type: 'object', properties: {}, maxProperties: 2 } },
    ]
    instrument(fixed.server, lsquare.client, { extras: ON })
    const connection = await kit.connect(fixed.server)
    const listed = await connection.list()
    for (const name of ['either', 'open', 'patterned', 'limited']) {
      expect(tool(listed, name).inputSchema, name).toEqual(fixed.tools.find((entry) => entry.name === name)!.inputSchema)
      const reply = await connection.call(name, { session_id: SENT, intent: 'why' })
      expect(issued(reply), name).toBeUndefined()
    }
    expect(fixed.seen.map((entry) => entry.args)).toEqual(Array(4).fill({ session_id: SENT, intent: 'why' }))
    for (const event of await lsquare.calls()) expect(event).not.toHaveProperty('sessionId')
  })

  it("answers get_more_tools only while the snapshot has tools and none of that name", async () => {
    const lsquare = setup()
    const fixed = kit.make({ pageSize: 1 })
    fixed.tools.push({ name: 'get_more_tools', inputSchema: { type: 'object', properties: { need: { type: 'string' } } } })
    instrument(fixed.server, lsquare.client, { extras: ON })
    const list = kit.handler(fixed.server, 'tools/list')
    const handle = kit.handler(fixed.server, 'tools/call')
    expect((await list({ method: 'tools/list', params: { cursor: '1' } }, kit.context())).tools.map((entry: any) => entry.name)).toEqual(['get_more_tools'])
    await handle(call('get_more_tools', { need: 'theirs' }), kit.context())
    expect(fixed.seen.at(-1)).toEqual({ name: 'get_more_tools', args: { need: 'theirs' } })

    const empty = kit.make()
    empty.tools = []
    instrument(empty.server, setup().client, { extras: ON })
    expect((await kit.handler(empty.server, 'tools/list')({ method: 'tools/list', params: {} }, kit.context())).tools).toEqual([])
    await kit.handler(empty.server, 'tools/call')(call('get_more_tools', { need: 'x' }), kit.context())
    expect(empty.seen).toHaveLength(1)
  })

  it("gives the ID line to the handler's own results, isError ones included, and not to a throw", async () => {
    const lsquare = setup()
    const fixed = kit.make()
    fixed.answer = (name) => {
      if (name === 'boom') throw new Error('broke')
      return name === 'empty' ? { ...text('Nothing found'), isError: true } : text('ok')
    }
    fixed.tools.push({ name: 'empty', inputSchema: { type: 'object' } }, { name: 'boom', inputSchema: { type: 'object' } })
    instrument(fixed.server, lsquare.client, { extras: ON })
    const handle = kit.handler(fixed.server, 'tools/call')
    expect(issued(await handle(call('search'), kit.context()))).toMatch(ID)
    expect(issued(await handle(call('empty'), kit.context()))).toMatch(ID)
    await expect(handle(call('boom'), kit.context())).rejects.toThrow('broke')
    const [, empty, boom] = await lsquare.calls()
    expect(empty).toMatchObject({ sessionSource: 'issued', error: { category: 'tool_error' } })
    expect(boom).not.toHaveProperty('sessionId')
  })
})

describe.each([kit1, kit2])('$name: reading the snapshot', (kit) => {
  it('discards a read overtaken by list_changed, for the calls already waiting on it too', async () => {
    const lsquare = setup()
    const fixed = kit.make()
    instrument(fixed.server, lsquare.client, { extras: ON })
    let release!: () => void
    fixed.beforeList = () => (fixed.lists.length === 1 ? new Promise<void>((resolve) => (release = resolve)) : undefined)
    const handle = kit.handler(fixed.server, 'tools/call')
    const waiting = handle(call('search', { q: 'a', session_id: 'theirs' }), kit.context())
    await sleep(20)
    // While the first read is held, the tool gains its own session_id.
    fixed.tools[0] = { name: 'search', inputSchema: { type: 'object', properties: { q: { type: 'string' }, session_id: { type: 'string' } } } }
    await fixed.server.sendToolListChanged().catch(() => {})
    release()
    await waiting
    expect(fixed.seen[0]!.args).toEqual({ q: 'a', session_id: 'theirs' })
    expect(fixed.lists).toHaveLength(2)
  })

  it('aborts only the page being read when the list changes mid-read, never one that has returned', async () => {
    const fixed = kit.make({ pageSize: 1 })
    fixed.tools.push({ name: 'refund', inputSchema: { type: 'object', properties: {} } })
    instrument(fixed.server, setup().client, { extras: ON })
    // Page 2 of the first read: the list changes while it is held open, across a timer.
    fixed.beforeList = () => {
      if (fixed.lists.length !== 2) return undefined
      void fixed.server.sendToolListChanged().catch(() => {})
      return sleep(20)
    }
    await kit.handler(fixed.server, 'tools/call')(call('search', { q: 'a' }), kit.context())
    await sleep(30)
    const signals = fixed.lists.map((entry) => (kit === kit1 ? entry.context.signal : entry.context.mcpReq.signal) as AbortSignal)
    // The read overtaken, then the one after it: page by page, each with a signal of its own.
    expect(fixed.lists.map((entry) => entry.cursor)).toEqual([undefined, '1', undefined, '1'])
    expect(new Set(signals).size).toBe(4)
    expect(signals.map((signal) => signal.aborted)).toEqual([false, true, false, false])
  })

  it("never aborts a page that has returned, even from the app's code chained on what its handler awaited", async () => {
    const fixed = kit.make()
    instrument(fixed.server, setup().client, { extras: ON })
    // The first read waits for the tools to load. Once they have, the app says the list changed,
    // from code that the same promise resumes right after the handler.
    let loaded: () => void = () => {}
    const registry = new Promise<void>((resolve) => (loaded = resolve))
    fixed.beforeList = () => {
      if (fixed.lists.length > 1) return undefined
      setTimeout(() => void registry.then(() => fixed.server.sendToolListChanged().catch(() => {})), 0)
      return registry
    }
    const waiting = kit.handler(fixed.server, 'tools/call')(call('search', { q: 'a' }), kit.context())
    await sleep(10)
    loaded()
    await waiting
    await sleep(10)
    const signals = fixed.lists.map((entry) => (kit === kit1 ? entry.context.signal : entry.context.mcpReq.signal) as AbortSignal)
    expect(fixed.lists).toHaveLength(2)
    expect(signals.map((signal) => signal.aborted)).toEqual([false, false])
  })

  /** A call of request A's whose read holds its first page open, with an abort listener on the page's signal. */
  function heldPage(request: AsyncLocalStorage<string>) {
    const fixed = kit.make()
    instrument(fixed.server, setup().client, { extras: ON })
    const heard: Array<string | undefined> = []
    let release: () => void = () => {}
    fixed.beforeList = (context) => {
      if (fixed.lists.length > 1) return undefined
      const signal = (kit === kit1 ? context.signal : context.mcpReq.signal) as AbortSignal
      signal.addEventListener('abort', () => heard.push(request.getStore()))
      return new Promise<void>((resolve) => (release = resolve))
    }
    const waiting = request.run('A', () => kit.handler(fixed.server, 'tools/call')(call('search', { q: 'a' }), kit.context()))
    return { fixed, heard, waiting, release: () => release() }
  }

  it("runs a page's abort listeners in the context its handler ran in, not that of the request that changed the list", async () => {
    // The app's request context, as tracing, logging or tenant code keeps it.
    const request = new AsyncLocalStorage<string>()
    const held = heldPage(request)
    await sleep(10)
    request.run('B', () => void held.fixed.server.sendToolListChanged().catch(() => {}))
    await held.waiting
    await sleep(10)
    expect(held.heard).toEqual(['A'])
    held.release()
  })

  it("doesn't abort the page from a request whose context isn't the page's and can't be entered (Workers)", async () => {
    const request = new AsyncLocalStorage<string>()
    const held = heldPage(request)
    await sleep(10)
    // As workerd refuses another request's context: runInAsyncScope throws before running anything.
    const refused = vi.spyOn(AsyncResource.prototype, 'runInAsyncScope').mockImplementation(() => {
      throw new Error('Cannot perform I/O on behalf of a different request.')
    })
    try {
      request.run('B', () => void held.fixed.server.sendToolListChanged().catch(() => {}))
    } finally {
      refused.mockRestore()
    }
    await held.waiting
    await sleep(10)
    expect(held.heard).toEqual([])
    held.release()
  })

  it('wakes the calls waiting on a read that a change discards, even if its handler never ends', async () => {
    const lsquare = setup()
    const fixed = kit.make()
    instrument(fixed.server, lsquare.client, { extras: ON })
    // The first read doesn't end until the test lets it, whatever its signal says; the next one is quick.
    let release!: () => void
    fixed.beforeList = () => (fixed.lists.length === 1 ? new Promise<void>((resolve) => (release = resolve)) : undefined)
    const handle = kit.handler(fixed.server, 'tools/call')
    const started = performance.now()
    const waiting = handle(call('search', { q: 'a', session_id: 'theirs' }), kit.context())
    await sleep(20)
    fixed.tools[0] = { name: 'search', inputSchema: { type: 'object', properties: { q: { type: 'string' }, session_id: { type: 'string' } } } }
    await fixed.server.sendToolListChanged().catch(() => {})
    await waiting
    expect(performance.now() - started).toBeLessThan(500)
    expect(fixed.lists).toHaveLength(2)
    expect(fixed.seen[0]!.args).toEqual({ q: 'a', session_id: 'theirs' })
    // The discarded read ending late never replaces the newer snapshot.
    release()
    await sleep(20)
    await handle(call('search', { q: 'b', session_id: 'theirs' }), kit.context())
    expect(fixed.lists).toHaveLength(2)
  })

  it('wakes the calls waiting on a read whose handler says the list changed as it starts', async () => {
    const fixed = kit.make()
    instrument(fixed.server, setup().client, { extras: ON })
    fixed.beforeList = () => {
      if (fixed.lists.length > 1) return undefined
      void fixed.server.sendToolListChanged().catch(() => {})
      return new Promise(() => {})
    }
    const started = performance.now()
    await kit.handler(fixed.server, 'tools/call')(call('search', { session_id: SENT }), kit.context())
    expect(performance.now() - started).toBeLessThan(500)
    expect(fixed.lists).toHaveLength(2)
    expect(fixed.seen[0]!.args).toEqual({})
  })

  it("keeps a call's one second when the read it waits on is overtaken", async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance', 'Date'] })
    const fixed = kit.make()
    instrument(fixed.server, setup().client, { extras: ON })
    fixed.beforeList = () => new Promise(() => {})
    const handle = kit.handler(fixed.server, 'tools/call')
    const waiting = handle(call('search', { session_id: SENT }), kit.context())
    await vi.advanceTimersByTimeAsync(600)
    await fixed.server.sendToolListChanged().catch(() => {})
    await vi.advanceTimersByTimeAsync(401)
    await waiting
    expect(fixed.lists).toHaveLength(2)
    expect(fixed.seen.map((entry) => entry.args)).toEqual([{ session_id: SENT }])
  })

  it('fails a read that breaks its bounds, passes the call on unchanged, and pauses reads until list_changed', async () => {
    const pages = (count: number) => Array.from({ length: count }, (_, index) => ({ name: `t${index}`, inputSchema: { type: 'object' } }))
    const broken: Array<[string, (fixed: Fixture) => void]> = [
      ['101 pages', (fixed) => (fixed.tools = pages(101))],
      ['a handler that throws', (fixed) => (fixed.beforeList = () => Promise.reject(new Error('down')))],
      ['no list of tools', (fixed) => (fixed.page = () => ({ items: fixed.tools }))],
      ['a nameless tool', (fixed) => (fixed.tools = [{ inputSchema: { type: 'object' } } as never])],
      ['a name listed twice', (fixed) => (fixed.tools = [fixed.tools[0]!, fixed.tools[0]!])],
      [
        'a cursor that throws',
        (fixed) =>
          (fixed.page = (cursor) =>
            cursor === undefined
              ? {
                  tools: [fixed.tools[0]],
                  get nextCursor() {
                    throw new Error('no cursor')
                  },
                }
              : { tools: [{ name: 'get_more_tools', inputSchema: { type: 'object' } }] }),
      ],
    ]
    for (const [label, breakIt] of broken) {
      const fixed = kit.make({ pageSize: 1 })
      breakIt(fixed)
      instrument(fixed.server, setup().client, { extras: ON })
      const handle = kit.handler(fixed.server, 'tools/call')
      const name = fixed.tools[0]?.name ?? 't0'
      await handle(call(name, { session_id: SENT }), kit.context())
      expect(fixed.seen[0]!.args, label).toEqual({ session_id: SENT })
      const reads = fixed.lists.length
      await handle(call(name, { session_id: SENT }), kit.context())
      expect(fixed.lists.length, label).toBe(reads)
      await fixed.server.sendToolListChanged().catch(() => {})
      fixed.tools = [{ name, inputSchema: { type: 'object' } }]
      fixed.beforeList = () => undefined
      fixed.page = undefined
      await handle(call(name, { session_id: SENT }), kit.context())
      expect(fixed.seen.at(-1)!.args, label).toEqual({})
    }
  })

  it('fails a read whose cursor comes back', async () => {
    const fixed = kit.make()
    instrument(fixed.server, setup().client, { extras: ON })
    let page = 0
    const loop = async () => ({ tools: [{ name: `t${page++}`, inputSchema: { type: 'object' } }], nextCursor: 'again' })
    if (kit === kit1) fixed.server.setRequestHandler(ListToolsRequestSchema, loop)
    else fixed.server.setRequestHandler('tools/list', loop)
    await kit.handler(fixed.server, 'tools/call')(call('t0', { session_id: SENT }), kit.context())
    expect(page).toBe(2)
    expect(fixed.seen[0]!.args).toEqual({ session_id: SENT })
  })

  it('takes a list of 10,000 tools, and fails one of 10,001', async () => {
    for (const [count, kept] of [
      [10_000, true],
      [10_001, false],
    ] as const) {
      const fixed = kit.make()
      fixed.tools = Array.from({ length: count }, (_, index) => ({ name: `t${index}`, inputSchema: { type: 'object' } }))
      instrument(fixed.server, setup().client, { extras: ON })
      await kit.handler(fixed.server, 'tools/call')(call('t9999', { session_id: SENT }), kit.context())
      expect(fixed.seen[0]!.args, String(count)).toEqual(kept ? {} : { session_id: SENT })
    }
  })

  it('waits at most a second from the start of a slow read, gives it up at 10 seconds, and starts none for a minute', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance', 'Date'] })
    const fixed = kit.make()
    instrument(fixed.server, setup().client, { extras: ON })
    fixed.beforeList = () => new Promise(() => {})
    const handle = kit.handler(fixed.server, 'tools/call')
    const first = handle(call('search', { session_id: SENT }), kit.context())
    await vi.advanceTimersByTimeAsync(600)
    // A call arriving during the read waits only for what is left of its first second.
    const second = handle(call('search', { session_id: SENT }), kit.context())
    await vi.advanceTimersByTimeAsync(399)
    expect(fixed.seen).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(2)
    await first
    await second
    expect(fixed.seen.map((entry) => entry.args)).toEqual([{ session_id: SENT }, { session_id: SENT }])
    // Past the first second, calls don't wait at all.
    await handle(call('search', { session_id: SENT }), kit.context())
    expect(fixed.seen).toHaveLength(3)
    await vi.advanceTimersByTimeAsync(10_000)
    await handle(call('search'), kit.context())
    await vi.advanceTimersByTimeAsync(40_000)
    await handle(call('search'), kit.context())
    expect(fixed.lists).toHaveLength(1)
    fixed.beforeList = () => undefined
    await vi.advanceTimersByTimeAsync(20_000)
    const late = handle(call('search', { session_id: SENT }), kit.context())
    await vi.advanceTimersByTimeAsync(10)
    await late
    expect(fixed.lists).toHaveLength(2)
    expect(fixed.seen.at(-1)!.args).toEqual({})
  })
})

describe('a read whose handler sends a request after the starting call was cancelled', () => {
  it('SDK 1.x: the send fails, so the read is discarded without the pause, and the next call reads again', async () => {
    const lsquare = setup()
    const fixed = kit1.make()
    instrument(fixed.server, lsquare.client, { extras: ON })
    let release!: () => void
    fixed.beforeList = async (context) => {
      if (fixed.lists.length === 1) {
        await new Promise<void>((resolve) => (release = resolve))
        await context.sendRequest({ method: 'ping' }, EmptyResultSchema)
      }
    }
    const { client } = await kit1.connect(fixed.server)
    const controller = new AbortController()
    const cancelled = client.callTool({ name: 'search', arguments: { session_id: SENT } }, undefined, { signal: controller.signal }).catch(() => 'cancelled')
    await sleep(20)
    controller.abort()
    await sleep(20)
    release()
    expect(await cancelled).toBe('cancelled')
    await sleep(20)
    await client.callTool({ name: 'search', arguments: { q: 'b', session_id: SENT } })
    expect(fixed.lists).toHaveLength(2)
    expect(fixed.seen.at(-1)!.args).toEqual({ q: 'b' })
  })

  it('SDK 2.x: the send still succeeds, so the read is kept', async () => {
    const lsquare = setup()
    const fixed = kit2.make()
    instrument(fixed.server, lsquare.client, { extras: ON })
    let release!: () => void
    fixed.beforeList = async (context) => {
      if (fixed.lists.length === 1) {
        await new Promise<void>((resolve) => (release = resolve))
        await context.mcpReq.send({ method: 'ping' })
      }
    }
    const { client } = await kit2.connect(fixed.server)
    const controller = new AbortController()
    const cancelled = client.callTool({ name: 'search', arguments: { session_id: SENT } }, { signal: controller.signal }).catch(() => 'cancelled')
    await sleep(20)
    controller.abort()
    await sleep(20)
    release()
    expect(await cancelled).toBe('cancelled')
    await sleep(20)
    await client.callTool({ name: 'search', arguments: { q: 'b', session_id: SENT } })
    expect(fixed.lists).toHaveLength(1)
    expect(fixed.seen.at(-1)!.args).toEqual({ q: 'b' })
  })
})

describe('a high-level server whose tools/call handler was replaced', () => {
  it('follows the low-level rules with only the extras passed as true, read from its tool list', async () => {
    const lsquare = setup()
    const server = new McpServer1({ name: 'orders', version: '1.0.0' })
    server.registerTool('search', { inputSchema: { q: z.string().optional() } }, async () => text('unused'))
    instrument(server, lsquare.client, { extras: { sessions: true } })
    const seen: unknown[] = []
    server.server.setRequestHandler(CallToolRequestSchema, async (request) => {
      seen.push(request.params.arguments)
      return text('custom')
    })
    const connection = await kit1.connect(server)
    const listed = await connection.list()
    expect(Object.keys(listed.tools[0].inputSchema.properties)).toEqual(['q', 'session_id'])
    expect(tool(listed, 'get_more_tools')).toBeUndefined()
    const reply = await connection.call('search', { q: 'a', session_id: SENT, intent: 'why' })
    expect(reply).toEqual(text('custom'))
    expect(seen).toEqual([{ q: 'a', intent: 'why' }])
    const [event] = await lsquare.calls()
    expect(event).toMatchObject({ sessionId: SENT, sessionSource: 'sent' })
    expect(event).not.toHaveProperty('intent')
  })
})

describe('dry run on a low-level server', () => {
  it("doesn't count get_more_tools as on with inputs off", async () => {
    const lsquare = setup({ apiKey: undefined, dryRun: true })
    instrument(kit2.make().server, lsquare.client, { extras: { sessions: true, missingTools: true }, inputs: false })
    await tick()
    const [line] = lsquare.logs.filter((entry) => entry.startsWith('[lsquare dry-run]')).map((entry) => JSON.parse(entry.replace('[lsquare dry-run] ', '')))
    expect(line.extras.on).toEqual(['session_id'])
  })

  it('says that get_more_tools depends on an inputs function', async () => {
    const lsquare = setup({ apiKey: undefined, dryRun: true })
    instrument(kit2.make().server, lsquare.client, { extras: { sessions: true, missingTools: true }, inputs: (name) => name !== 'get_more_tools' })
    await tick()
    const [line] = lsquare.logs.filter((entry) => entry.startsWith('[lsquare dry-run]')).map((entry) => JSON.parse(entry.replace('[lsquare dry-run] ', '')))
    expect(line.extras.get_more_tools).toBe('only where the inputs function answers true for it')
  })

  it('says the extras are off and how to turn them on, or which are on and where they come from', async () => {
    const lsquare = setup({ apiKey: undefined, dryRun: true })
    instrument(kit2.make().server, lsquare.client)
    instrument(kit2.make().server, lsquare.client, { extras: { sessions: true, missingTools: true } })
    await tick()
    const lines = lsquare.logs.filter((line) => line.startsWith('[lsquare dry-run]')).map((line) => JSON.parse(line.replace('[lsquare dry-run] ', '')))
    expect(lines).toEqual([
      {
        server: 'orders',
        level: 'low',
        records: 'every tools/call',
        extras:
          'off on a low-level server; extras: { sessions: true, intent: true, missingTools: true } turns them on, for a server whose instances list and serve their own tools, the same to every request',
      },
      {
        server: 'orders',
        level: 'low',
        records: 'every tools/call',
        extras: {
          on: ['session_id', 'get_more_tools'],
          from: "the server's own tool list: every page, read before anything changes and again after it changes; a call waits at most 1 second for it",
          for: 'instances that list the same tools to every request they serve',
        },
      },
    ])
  })
})

describe('reading a listed schema', () => {
  const read = (schema: unknown) => {
    const owner = listedOwnership(schema)
    return owner.readable ? [...owner.declared].sort() : 'unreadable'
  }

  it('reads named properties and required names, and nothing more', () => {
    expect(read({ type: 'object' })).toEqual([])
    expect(read({ type: 'object', properties: { q: {}, intent: {} }, required: ['session_id'], additionalProperties: false, title: 't', $defs: {} })).toEqual(['intent', 'session_id'])
    for (const [index, schema] of [
      undefined,
      [],
      { type: 'string' },
      { properties: {} },
      { type: 'object', properties: [] },
      { type: 'object', required: 'q' },
      { type: 'object', required: [1] },
      { type: 'object', additionalProperties: true },
      { type: 'object', additionalProperties: {} },
      { type: 'object', oneOf: [] },
      { type: 'object', $ref: '#/$defs/x' },
      {
        type: 'object',
        get properties() {
          throw new Error('no')
        },
      },
    ].entries()) {
      expect(read(schema), `schema ${index}`).toBe('unreadable')
    }
  })
})
