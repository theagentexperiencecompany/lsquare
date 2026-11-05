// lsquare/mcp's extras on high-level servers (spec section 4): session_id and intent, the ID line,
// and get_more_tools, against real servers and clients of both MCP SDKs.
import { PassThrough } from 'node:stream'
import { Client as Client1 } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport as Memory1 } from '@modelcontextprotocol/sdk/inMemory.js'
import { McpServer as McpServer1 } from '@modelcontextprotocol/sdk/server/mcp.js'
import { StdioServerTransport as Stdio1 } from '@modelcontextprotocol/sdk/server/stdio.js'
import { WebStandardStreamableHTTPServerTransport as Http1 } from '@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js'
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { Client as Client2 } from '@modelcontextprotocol/client'
import { McpServer as McpServer2, InMemoryTransport as Memory2, Server as Server2, WebStandardStreamableHTTPServerTransport as Http2, acceptedContent, inputRequired } from '@modelcontextprotocol/server'
import { StdioServerTransport as Stdio2 } from '@modelcontextprotocol/server/stdio'
import { afterEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import * as zm from 'zod/mini'
import * as z3 from 'zod/v3'
import { validateEvent } from '../../contract/src/validate.js'
import { LSquare, type LSquareEvent, type LSquareOptions, type ToolCallEvent, type TurnEvent } from '../src/index.js'
import { advertise } from '../src/mcp/extras.js'
import { instrument } from '../src/mcp/index.js'

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const tick = () => new Promise<void>((resolve) => setImmediate(() => setImmediate(resolve)))
const text = (value: string) => ({ content: [{ type: 'text' as const, text: value }] })
const ID = /^ses_[A-Za-z0-9_-]{22}$/
const SENT = 'ses_AbCdEfGhIjKlMnOpQrStUv'
const LINE = /^Task ID: (ses_[A-Za-z0-9_-]{22})\. Send it as session_id on later calls for this task, and pass it to any subagent working on it\.$/
const REPLY = "Noted. No other tools are available right now, and your request was passed on to the server's developers."
const OFF = { sessions: false, intent: false, missingTools: false }

/** The session ID a reply's last block gives, if it gives one. */
const issued = (reply: any): string | undefined => {
  const last = reply?.content?.at?.(-1)
  return last?.type === 'text' ? LINE.exec(last.text)?.[1] : undefined
}
/** A reply without the ID line at its end. */
const withoutLine = (reply: any) => (issued(reply) ? { ...reply, content: reply.content.slice(0, -1) } : reply)

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
  const client = new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test', fetch, logger: { info: (m) => logs.push(m), warn: (m) => logs.push(m) }, ...options })
  const calls = async () => {
    await client.flush()
    return events.filter((e): e is ToolCallEvent => e.kind === 'tool_call')
  }
  const turns = async () => {
    await client.flush()
    return events.filter((e): e is TurnEvent => e.kind === 'turn')
  }
  return { client, calls, turns, logs }
}

interface Connection {
  call(name: string, args?: Record<string, unknown>): Promise<any>
  list(cursor?: string): Promise<any>
}

interface Kit {
  name: string
  server(options?: Record<string, unknown>): any
  connect(server: any): Promise<Connection>
}

const caught = (error: { code?: unknown; message?: unknown }) => ({ error: { code: error.code, message: error.message } })

const kit1: Kit = {
  name: 'SDK 1.x',
  server: () => new McpServer1({ name: 'orders', version: '1.0.0' }),
  async connect(server) {
    const [a, b] = Memory1.createLinkedPair()
    await server.connect(a)
    const client = new Client1({ name: 'claude-code', version: '2.1' })
    await client.connect(b)
    return { call: (name, args = {}) => client.callTool({ name, arguments: args }).catch(caught), list: (cursor) => client.listTools(cursor ? { cursor } : {}) }
  },
}

const kit2: Kit = {
  name: 'SDK 2.x',
  server: (options) => new McpServer2({ name: 'orders', version: '1.0.0' }, options as never),
  async connect(server) {
    const [a, b] = Memory2.createLinkedPair()
    await server.connect(a)
    const client = new Client2({ name: 'claude-code', version: '2.1' })
    await client.connect(b)
    return { call: (name, args = {}) => client.callTool({ name, arguments: args }).catch(caught), list: (cursor) => client.listTools(cursor ? { cursor } : {}) }
  },
}

/** A tool that answers with the arguments it was given, so a test sees exactly what reached it. */
const echo = (seen: unknown[]) => async (args: unknown) => {
  seen.push(args)
  return text(JSON.stringify(args))
}

const tool = (listed: any, name: string) => listed.tools.find((entry: any) => entry.name === name)

/** A request context for calling a server's tools/call handler directly, as each SDK's transport would give it. */
const direct = (kit: Kit, sessionId?: string): Record<string, unknown> => ({
  ...(kit === kit1
    ? { signal: new AbortController().signal, sendNotification: async () => {}, sendRequest: async () => {}, requestId: 1 }
    : { mcpReq: { id: 1, signal: new AbortController().signal, envelope: {}, requestState: () => undefined } }),
  ...(sessionId === undefined ? {} : { sessionId }),
})

describe.each([kit1, kit2])('$name: session_id and intent', (kit) => {
  it('lists both on every tool and adds get_more_tools, on a copy of the definitions', async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client)
    const schema = z.object({ q: z.string() })
    server.registerTool('search', { description: 'Search orders', inputSchema: schema }, async () => text('ok'))
    server.registerTool('plain', {}, async () => text('ok'))
    const listed = await (await kit.connect(server)).list()
    expect(listed.tools.map((entry: any) => entry.name)).toEqual(['search', 'plain', 'get_more_tools'])
    const search = tool(listed, 'search')
    expect(Object.keys(search.inputSchema.properties)).toEqual(['q', 'session_id', 'intent'])
    expect(search.inputSchema.required).toEqual(['q'])
    expect(search.inputSchema.properties.session_id).toEqual({
      type: 'string',
      description:
        'The ID of the task this call belongs to. Leave it out on your first call. On every later call for the same task, send the ID an earlier result gave you, and pass it to any subagent working on that task.',
    })
    expect(search.inputSchema.properties.intent).toEqual({
      type: 'string',
      description: "In one sentence, why you're calling this tool and what the user is trying to do. No personal data or secrets.",
    })
    expect(Object.keys(tool(listed, 'plain').inputSchema.properties)).toEqual(['session_id', 'intent'])
    expect(tool(listed, 'get_more_tools')).toMatchObject({
      description: "Call this when none of the available tools fits what you need, or one fits only in part. It doesn't return new tools: it tells the server's developers what to add.",
      inputSchema: {
        type: 'object',
        properties: { need: { type: 'string', description: "What you needed to do and couldn't, in one or two sentences. No personal data or secrets." }, session_id: { type: 'string' } },
        required: ['need'],
      },
      annotations: { readOnlyHint: true, openWorldHint: false },
    })
    expect(tool(listed, 'get_more_tools').inputSchema.properties).not.toHaveProperty('intent')
    // The definition is the developer's, as registered.
    expect(server._registeredTools.search.inputSchema).toBe(schema)
    expect(Object.keys(schema.shape)).toEqual(['q'])
  })

  it('takes them out before the SDK checks the arguments, gives the first result a task ID, and groups later calls by it', async () => {
    const lsquare = setup()
    const seen: unknown[] = []
    const identified: unknown[] = []
    const server = instrument(kit.server(), lsquare.client, { identify: ({ args }) => void identified.push(args) })
    server.registerTool('search', { inputSchema: z.strictObject({ q: z.string() }) }, echo(seen))
    const connection = await kit.connect(server)
    const first = await connection.call('search', { q: 'late', intent: 'Find the late orders' })
    const id = issued(first)
    expect(id).toMatch(ID)
    expect(withoutLine(first)).toEqual(text('{"q":"late"}'))
    const second = await connection.call('search', { q: 'early', session_id: id, intent: 'Compare with early ones' })
    expect(second).toEqual(text('{"q":"early"}'))
    expect(seen).toEqual([{ q: 'late' }, { q: 'early' }])
    expect(identified).toEqual([{ q: 'late' }, { q: 'early' }])
    const [one, two] = await lsquare.calls()
    expect(one).toMatchObject({ sessionId: id, sessionSource: 'issued', intent: 'Find the late orders', input: { q: 'late' }, result: text('{"q":"late"}') })
    expect(two).toMatchObject({ sessionId: id, sessionSource: 'sent', intent: 'Compare with early ones', input: { q: 'early' } })
  })

  it('removes a malformed session_id and any intent that is not a non-empty string, and ignores them', async () => {
    const lsquare = setup()
    const seen: unknown[] = []
    const server = instrument(kit.server(), lsquare.client)
    server.registerTool('search', { inputSchema: z.strictObject({ q: z.string() }) }, echo(seen))
    const connection = await kit.connect(server)
    const reply = await connection.call('search', { q: 'a', session_id: 'ses_short', intent: 42 })
    await connection.call('search', { q: 'b', session_id: 7, intent: '' })
    expect(seen).toEqual([{ q: 'a' }, { q: 'b' }])
    const [one, two] = await lsquare.calls()
    expect(one).toMatchObject({ sessionId: issued(reply), sessionSource: 'issued' })
    expect(one).not.toHaveProperty('intent')
    expect(two).not.toHaveProperty('intent')
  })

  it('records intent whether or not inputs are, redacted and cut at 500 characters', async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client, { inputs: false })
    server.registerTool('search', { inputSchema: z.object({ q: z.string() }) }, async () => text('ok'))
    const connection = await kit.connect(server)
    await connection.call('search', { q: 'a', intent: `Find orders for ana@example.com ${'x'.repeat(600)}` })
    const [event] = await lsquare.calls()
    expect(event).not.toHaveProperty('input')
    expect(event!.intent).toMatch(/^Find orders for \[EMAIL_1\] x+…\[\+\d+ chars\]$/)
    expect(event!.truncated).toContain('intent')
  })

  it('sends neither the session nor the intent in limited diagnostics', async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client, {
      identify: () => {
        throw new Error('lookup failed')
      },
    })
    server.registerTool('search', { inputSchema: z.object({}) }, async () => text('ok'))
    const reply = await (await kit.connect(server)).call('search', { intent: 'Find orders' })
    expect(issued(reply)).toMatch(ID)
    const [event] = await lsquare.calls()
    expect(event).toMatchObject({ identity: 'failed' })
    expect(event).not.toHaveProperty('sessionId')
    expect(event).not.toHaveProperty('sessionSource')
    expect(event).not.toHaveProperty('intent')
  })

  it("leaves the result unchanged when the call is grouped by a capture()'s conversationId", async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client)
    server.registerTool('ask', { inputSchema: z.object({ q: z.string() }) }, async () => {
      lsquare.client.capture({ conversationId: 'conv-1', question: 'Where is my order?' })
      return text('answered')
    })
    expect(await (await kit.connect(server)).call('ask', { q: 'a' })).toEqual(text('answered'))
    const [event] = await lsquare.calls()
    expect(event).toMatchObject({ conversationId: 'conv-1' })
    expect(event).not.toHaveProperty('sessionId')
    expect(event).not.toHaveProperty('sessionSource')
  })

  it('gives no ID line to results the SDK made, thrown errors or unknown tools', async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client)
    server.registerTool('search', { inputSchema: z.object({ q: z.string() }) }, async () => text('ok'))
    server.registerTool('boom', { inputSchema: z.object({}) }, async () => {
      throw new Error('broke')
    })
    server.registerTool('typed', { inputSchema: z.object({}), outputSchema: z.object({ n: z.number() }) }, async () => text('no structured content') as never)
    const connection = await kit.connect(server)
    const replies = [await connection.call('search', { q: 5 }), await connection.call('boom'), await connection.call('typed'), await connection.call('nope')]
    for (const reply of replies) expect(issued(reply)).toBeUndefined()
    const events = await lsquare.calls()
    expect(events.map((event) => event.error?.category)).toEqual(['protocol', 'thrown', 'protocol', 'protocol'])
    for (const event of events) expect(event).not.toHaveProperty('sessionId')
  })

  it('gives an isError result of the tool its own the ID line', async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client)
    server.registerTool('empty', { inputSchema: z.object({}) }, async () => ({ ...text('Nothing found'), isError: true }))
    const reply = await (await kit.connect(server)).call('empty')
    expect(withoutLine(reply)).toEqual({ ...text('Nothing found'), isError: true })
    const [event] = await lsquare.calls()
    expect(event).toMatchObject({ sessionId: issued(reply), sessionSource: 'issued', error: { category: 'tool_error', message: 'Nothing found' } })
  })

  it('leaves the inputs a tool declares as its own, and logs that once', async () => {
    const lsquare = setup()
    const seen: unknown[] = []
    const server = instrument(kit.server(), lsquare.client)
    server.registerTool('resume', { inputSchema: z.object({ session_id: z.string().optional() }) }, echo(seen))
    server.registerTool('explain', { inputSchema: z.object({ intent: z.string() }) }, echo(seen))
    const connection = await kit.connect(server)
    const listed = await connection.list()
    expect(tool(listed, 'resume').inputSchema.properties.session_id).toEqual({ type: 'string' })
    expect(Object.keys(tool(listed, 'resume').inputSchema.properties)).toEqual(['session_id', 'intent'])
    expect(tool(listed, 'explain').inputSchema.properties.intent).toEqual({ type: 'string' })
    expect(Object.keys(tool(listed, 'explain').inputSchema.properties)).toEqual(['intent', 'session_id'])
    const resumed = await connection.call('resume', { session_id: 'their-own', intent: 'Pick up where we left' })
    const explained = await connection.call('explain', { intent: 'theirs', session_id: SENT })
    expect(seen).toEqual([{ session_id: 'their-own' }, { intent: 'theirs' }])
    // Never given an ID line, and its own session_id is never read.
    expect(resumed).toEqual(text('{"session_id":"their-own"}'))
    expect(explained).toEqual(text('{"intent":"theirs"}'))
    await connection.list()
    const [resume, explain] = await lsquare.calls()
    await tick()
    expect(resume).not.toHaveProperty('sessionId')
    expect(resume).toMatchObject({ intent: 'Pick up where we left' })
    expect(explain).toMatchObject({ sessionId: SENT, sessionSource: 'sent' })
    expect(explain).not.toHaveProperty('intent')
    expect(lsquare.logs.filter((line) => line.includes('has its own'))).toEqual([
      '[lsquare] Tool "resume" has its own session_id input: it is left as it is.',
      '[lsquare] Tool "explain" has its own intent input: it is left as it is.',
    ])
  })

  it('turns each extra off by itself', async () => {
    for (const off of ['sessions', 'intent', 'missingTools'] as const) {
      const lsquare = setup()
      const seen: unknown[] = []
      const server = instrument(kit.server(), lsquare.client, { extras: { [off]: false } })
      server.registerTool('search', { inputSchema: z.object({ q: z.string() }) }, echo(seen))
      const connection = await kit.connect(server)
      const listed = await connection.list()
      const reply = await connection.call('search', { q: 'a', session_id: SENT, intent: 'why' })
      const missing = await connection.call('get_more_tools', { need: 'refunds' })
      const [event] = await lsquare.calls()
      const properties = Object.keys(tool(listed, 'search').inputSchema.properties)
      if (off === 'sessions') {
        expect(properties).toEqual(['q', 'intent'])
        expect(tool(listed, 'get_more_tools').inputSchema.properties).not.toHaveProperty('session_id')
        expect(seen).toEqual([{ q: 'a' }])
        expect(issued(reply)).toBeUndefined()
        expect(event).not.toHaveProperty('sessionId')
        expect(event).toMatchObject({ intent: 'why', input: { q: 'a' } })
      } else if (off === 'intent') {
        expect(properties).toEqual(['q', 'session_id'])
        expect(event).toMatchObject({ sessionId: SENT, sessionSource: 'sent', input: { q: 'a' } })
        expect(event).not.toHaveProperty('intent')
      } else {
        expect(properties).toEqual(['q', 'session_id', 'intent'])
        expect(tool(listed, 'get_more_tools')).toBeUndefined()
        expect(JSON.stringify(missing)).toMatch(/get_more_tools not found/)
      }
    }
  })

  it("reads each instance's own definitions at every call, without a listing first", async () => {
    const lsquare = setup()
    const seen: unknown[] = []
    // Two instances of one server, as a server built per request has: same name, different tools.
    const theirs = instrument(kit.server(), lsquare.client)
    theirs.registerTool('t', { inputSchema: z.object({ session_id: z.string() }) }, echo(seen))
    const ours = instrument(kit.server(), lsquare.client)
    ours.registerTool('t', { inputSchema: z.object({}) }, echo(seen))
    await (await kit.connect(theirs)).call('t', { session_id: SENT })
    await (await kit.connect(ours)).call('t', { session_id: SENT })
    expect(seen).toEqual([{ session_id: SENT }, {}])
  })

  it('changes no request, listing or result in place, frozen ones included', async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client)
    const result = Object.freeze({ content: Object.freeze([Object.freeze({ type: 'text', text: 'kept' })]) })
    const seen: unknown[] = []
    server.registerTool('t', { inputSchema: z.object({ q: z.string() }) }, (args: unknown) => {
      seen.push(args)
      return result
    })
    const deepFreeze = (value: any): any => {
      for (const key of Object.keys(value)) if (typeof value[key] === 'object' && value[key] !== null) deepFreeze(value[key])
      return Object.freeze(value)
    }
    const request = deepFreeze({ method: 'tools/call', params: { name: 't', arguments: { q: 'a', session_id: SENT, intent: 'why' } } })
    const before = JSON.stringify(request)
    const reply: any = await server.server._requestHandlers.get('tools/call')(request, direct(kit))
    expect(JSON.stringify(request)).toBe(before)
    expect(seen).toEqual([{ q: 'a' }])
    expect(reply).toEqual(result)
    // One that isn't frozen either: the wrapper passes a copy on, and this one stays as it was.
    const held = { method: 'tools/call', params: { name: 't', arguments: { q: 'c', session_id: SENT, intent: 'why' } } }
    await server.server._requestHandlers.get('tools/call')(held, direct(kit))
    expect(held.params.arguments).toEqual({ q: 'c', session_id: SENT, intent: 'why' })
    // Without a session_id, the ID line goes on a copy (SDK 1.x hands the wrapper the tool's frozen result itself).
    const lined: any = await server.server._requestHandlers.get('tools/call')(deepFreeze({ method: 'tools/call', params: { name: 't', arguments: { q: 'b' } } }), direct(kit))
    expect(issued(lined)).toMatch(ID)
    expect(withoutLine(lined)).toEqual(result)
    expect(result.content).toHaveLength(1)

    // A frozen listing: the copy has the extras, and the listing itself is as it was.
    const frozen: any[] = []
    const fresh = kit.server()
    fresh.registerTool('t', {}, async () => text('ok'))
    const handlers = fresh.server._requestHandlers
    const whole = handlers.get('tools/list')
    handlers.set('tools/list', async (...args: unknown[]) => {
      const listed = deepFreeze(JSON.parse(JSON.stringify(await whole(...args))))
      frozen.push(listed)
      return listed
    })
    const listed = await (await kit.connect(instrument(fresh, setup().client))).list()
    expect(Object.keys(listed.tools[0].inputSchema.properties)).toEqual(['session_id', 'intent'])
    expect(frozen[0].tools).toHaveLength(1)
    expect(frozen[0].tools[0].inputSchema.properties).toEqual({})
  })

  it('leaves a schema alone when reading it throws, never taking the failure for an absent part', async () => {
    const run = async (instrumented: boolean) => {
      const seen: unknown[] = []
      // A loose object, which keeps every key, whose catchall can't be read: zod itself never reads it again.
      const loose = z.looseObject({})
      const def = Object.create(loose._zod.def, {
        catchall: {
          get: () => {
            throw new Error('no catchall')
          },
        },
      })
      const schema = Object.create(loose, { _zod: { value: Object.create(loose._zod, { def: { value: def } }) } })
      const plain = kit.server()
      const server = instrumented ? instrument(plain, setup().client) : plain
      server.registerTool('t', { inputSchema: schema }, echo(seen))
      await (await kit.connect(server)).call('t', { session_id: 'business', intent: 'business' })
      return seen
    }
    const without = await run(false)
    expect(without).toEqual([{ session_id: 'business', intent: 'business' }])
    expect(await run(true)).toEqual(without)
  })

  it("groups a call by a capture()'s conversationId ahead of its protocol session", async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client)
    server.registerTool('ask', { inputSchema: z.object({}) }, async () => {
      lsquare.client.capture({ conversationId: 'conversation-1' })
      return text('answered')
    })
    server.registerTool('plain', { inputSchema: z.object({}) }, async () => text('plain'))
    const handler = server.server._requestHandlers.get('tools/call')
    expect(await handler({ method: 'tools/call', params: { name: 'ask', arguments: {} } }, direct(kit, 'transport-id'))).toEqual(text('answered'))
    expect(await handler({ method: 'tools/call', params: { name: 'plain', arguments: {} } }, direct(kit, 'transport-id'))).toEqual(text('plain'))
    const [ask, plain] = await lsquare.calls()
    expect(ask).toMatchObject({ conversationId: 'conversation-1' })
    expect(ask).not.toHaveProperty('sessionId')
    expect(plain).toMatchObject({ sessionId: 'transport-id', sessionSource: 'protocol' })
  })

  it('drops the extras from a listing still pending when the tools/call handler is replaced, and groups no later call', async () => {
    const lsquare = setup()
    const server = kit.server()
    server.registerTool('t', { inputSchema: z.object({}) }, async () => text('unused'))
    const handlers = server.server._requestHandlers
    const whole = handlers.get('tools/list')
    let release!: () => void
    const gate = new Promise<void>((resolve) => (release = resolve))
    handlers.set('tools/list', async (...args: unknown[]) => {
      await gate
      return whole(...args)
    })
    instrument(server, lsquare.client)
    const pending = handlers.get('tools/list')({ method: 'tools/list', params: {} }, {})
    const own = async () => text('custom')
    if (kit === kit1) server.server.setRequestHandler(CallToolRequestSchema, own)
    else server.server.setRequestHandler('tools/call', own)
    release()
    const listed = await pending
    expect(listed.tools.map((entry: any) => entry.name)).toEqual(['t'])
    expect(listed.tools[0].inputSchema.properties ?? {}).toEqual({})
    await handlers.get('tools/call')({ method: 'tools/call', params: { name: 't', arguments: {} } }, direct(kit, 'transport-id'))
    const [event] = await lsquare.calls()
    expect(event).not.toHaveProperty('sessionId')
  })

  it('treats a server whose tools/call handler was replaced after instrument() as low-level: no extras', async () => {
    const lsquare = setup()
    const seen: unknown[] = []
    const server = instrument(kit.server(), lsquare.client)
    server.registerTool('t', { inputSchema: z.object({}) }, async () => text('unused'))
    const own = async (request: any) => {
      seen.push(request.params.arguments)
      return text('custom')
    }
    if (kit === kit1) server.server.setRequestHandler(CallToolRequestSchema, own)
    else server.server.setRequestHandler('tools/call', own)
    const connection = await kit.connect(server)
    const listed = await connection.list()
    expect(listed.tools.map((entry: any) => entry.name)).toEqual(['t'])
    expect(listed.tools[0].inputSchema.properties ?? {}).toEqual({})
    expect(await connection.call('t', { session_id: SENT, intent: 'why' })).toEqual(text('custom'))
    expect(seen).toEqual([{ session_id: SENT, intent: 'why' }])
    const [event] = await lsquare.calls()
    expect(event).not.toHaveProperty('sessionId')
    expect(event).not.toHaveProperty('intent')
  })
})

/**
 * zod/compile's global mode, on only while `make` builds a schema: imported once, its
 * post-processor put back as it found it. It wraps each schema's run, keeping the one it wrapped.
 */
const zodConfig = (globalThis as unknown as { __zod_globalConfig: { postProcessor?: unknown } }).__zod_globalConfig
const before = zodConfig.postProcessor
await import('zod/compile')
const compileAll = zodConfig.postProcessor
zodConfig.postProcessor = before
function compiledAll<T>(make: () => T): T {
  zodConfig.postProcessor = compileAll
  try {
    return make()
  } finally {
    zodConfig.postProcessor = before
  }
}

/** Which forms of input schema get the extras: the rest behave exactly as without the SDK. */
describe('input schemas', () => {
  const base = { q: z.string() }
  const forms4: Array<[string, () => unknown, boolean]> = [
    ['a zod 4 object', () => z.object(base), true],
    ['a refined one', () => z.object(base).refine(() => true), true],
    ['a superRefined one', () => z.object(base).superRefine(() => {}), true],
    ['a zod mini object', () => zm.object({ q: zm.string() }), true],
    ['a refined zod mini object', () => zm.object({ q: zm.string() }).check(zm.refine(() => true)), true],
    ['a strict object', () => z.strictObject(base), true],
    ['a strict zod mini object', () => zm.strictObject({ q: zm.string() }), true],
    ['a raw shape', () => base, true],
    ['a preprocess', () => z.preprocess((value) => value, z.object(base)), false],
    ['a transform', () => z.object(base).transform((value) => value), false],
    ['a lazy object', () => z.lazy(() => z.object(base)), false],
    ['a union', () => z.union([z.object(base), z.object({ r: z.number() })]), false],
    ['an intersection', () => z.intersection(z.object(base), z.object({ r: z.string().optional() })), false],
    ['a record', () => z.record(z.string(), z.string()), false],
    ['a loose object', () => z.looseObject(base), false],
    ['an object with a catchall', () => z.object(base).catchall(z.string()), false],
    ['a refined strict object', () => z.strictObject(base).refine(() => true), false],
    // Codex's fifth review: a strict object's checks are told by the run zod keeps for it.
    ['a strict object zod/compile compiles', () => compiledAll(() => z.strictObject(base)), true],
    ['a refined strict object zod/compile compiles', () => compiledAll(() => z.strictObject(base).refine(() => true)), false],
    // A clone whose run falls back to another schema's, which may run checks it can't show.
    ['a strict object z.compile() made', () => z.compile(z.strictObject(base)), false],
  ]
  const forms3: Array<[string, () => unknown, boolean]> = [
    ['a zod 3 object', () => z3.object({ q: z3.string() }), true],
    ['a refined zod 3 object', () => z3.object({ q: z3.string() }).refine(() => true), true],
    ['a twice-refined zod 3 object', () => z3.object({ q: z3.string() }).refine(() => true).superRefine(() => {}), true],
    ['a strict zod 3 object', () => z3.object({ q: z3.string() }).strict(), true],
    ['a zod 3 raw shape', () => ({ q: z3.string() }), true],
    ['a zod 3 passthrough object', () => z3.object({ q: z3.string() }).passthrough(), false],
    ['a refined strict zod 3 object', () => z3.object({ q: z3.string() }).strict().refine(() => true), false],
    ['a zod 3 transform', () => z3.object({ q: z3.string() }).transform((value) => value), false],
    ['a zod 3 preprocess', () => z3.preprocess((value) => value, z3.object({ q: z3.string() })), false],
    ['a zod 3 catchall', () => z3.object({ q: z3.string() }).catchall(z3.string()), false],
  ]
  /** A schema from another library: any Standard Schema, which SDK 2.x accepts. */
  const other = () => ({
    '~standard': {
      version: 1,
      vendor: 'other',
      validate: (value: unknown) => ({ value }),
      jsonSchema: { input: () => ({ type: 'object', properties: { q: { type: 'string' } } }), output: () => ({ type: 'object' }) },
    },
  })
  const cases: Array<[Kit, string, () => unknown, boolean]> = [
    ...forms4.map(([name, make, gets]) => [kit1, name, make, gets] as [Kit, string, () => unknown, boolean]),
    ...forms3.map(([name, make, gets]) => [kit1, name, make, gets] as [Kit, string, () => unknown, boolean]),
    ...forms4.map(([name, make, gets]) => [kit2, name, make, gets] as [Kit, string, () => unknown, boolean]),
    [kit2, 'a schema from another library', other, false],
  ]

  async function run(kit: Kit, make: () => unknown, instrumented: boolean, args: Record<string, unknown>) {
    const lsquare = setup()
    const seen: unknown[] = []
    const plain = kit.server()
    const server = instrumented ? instrument(plain, lsquare.client) : plain
    server.registerTool('t', { inputSchema: make() }, echo(seen))
    const connection = await kit.connect(server)
    const listed = await connection.list()
    const reply = await connection.call('t', args)
    return { listed: tool(listed, 't').inputSchema, reply, seen, events: instrumented ? await lsquare.calls() : [] }
  }

  it.each(cases)('%s, %s: given the extras %s', async (kit, _name, make, gets) => {
    const args = { q: 'a', session_id: SENT, intent: 'why' }
    const without = await run(kit, make, false, args)
    const withSdk = await run(kit, make, true, args)
    const [event] = withSdk.events
    if (gets) {
      expect(withSdk.seen).toEqual([{ q: 'a' }])
      expect(withSdk.reply).toEqual(text('{"q":"a"}'))
      expect(event).toMatchObject({ sessionId: SENT, sessionSource: 'sent', intent: 'why' })
      expect(withSdk.listed).toEqual({ ...without.listed, properties: { ...without.listed.properties, session_id: expect.any(Object), intent: expect.any(Object) } })
    } else {
      // Exactly as without the SDK: the listing, what reached the tool and the reply.
      expect(withSdk.listed).toEqual(without.listed)
      expect(withSdk.seen).toEqual(without.seen)
      expect(withoutLine(withSdk.reply)).toEqual(without.reply)
      expect(issued(withSdk.reply)).toBeUndefined()
      expect(event).not.toHaveProperty('sessionId')
      expect(event).not.toHaveProperty('intent')
    }
  })

  it('gives a tool without an input schema the extras: it is never given arguments', async () => {
    for (const kit of [kit1, kit2]) {
      const lsquare = setup()
      const server = instrument(kit.server(), lsquare.client)
      server.registerTool('t', {}, async () => text('ok'))
      const connection = await kit.connect(server)
      expect(Object.keys(tool(await connection.list(), 't').inputSchema.properties)).toEqual(['session_id', 'intent'])
      expect(await connection.call('t', { session_id: SENT, intent: 'why' })).toEqual(text('ok'))
      expect((await lsquare.calls())[0]).toMatchObject({ sessionId: SENT, intent: 'why', input: {} })
    }
  })

  it('passes session_id on to a preprocess that reads it, and to a refinement that reads it from a refusal', async () => {
    for (const kit of [kit1, kit2]) {
      const seen: unknown[] = []
      const read = z.preprocess((value: any) => ({ q: value?.session_id }), z.object({ q: z.string() }))
      const lsquare = setup()
      const server = instrument(kit.server(), lsquare.client)
      server.registerTool('read', { inputSchema: read }, echo(seen))
      const refused: unknown[] = []
      const strict = z.strictObject({ q: z.string() }).superRefine((_value, ctx) => void refused.push((ctx as any).issues.flatMap((issue: any) => issue.keys ?? [])), { when: () => true } as never)
      server.registerTool('strict', { inputSchema: strict }, echo(seen))
      const connection = await kit.connect(server)
      expect(await connection.call('read', { session_id: SENT })).toEqual(text(JSON.stringify({ q: SENT })))
      const reply = await connection.call('strict', { q: 'a', session_id: SENT })
      expect(JSON.stringify(reply)).toMatch(/session_id/)
      expect(refused).toEqual([['session_id']])
    }
  })

  it('reads a zod 3 shape changed after registration as it is now, and leaves one changed after its first check alone', async () => {
    const lsquare = setup()
    const seen: unknown[] = []
    const server = instrument(kit1.server(), lsquare.client)
    const early: Record<string, unknown> = { q: z3.string() }
    server.registerTool('early', { inputSchema: z3.object(early as never) as never }, echo(seen))
    early.session_id = z3.string()
    const late: Record<string, unknown> = { q: z3.string() }
    server.registerTool('late', { inputSchema: z3.object(late as never) as never }, echo(seen))
    const connection = await kit1.connect(server)
    await connection.call('late', { q: 'first' })
    late.session_id = z3.string().optional()
    const listed = await connection.list()
    // Changed before any check: session_id is the tool's own now.
    expect(await connection.call('early', { q: 'a', session_id: 'theirs' })).toEqual(text('{"q":"a","session_id":"theirs"}'))
    expect(Object.keys(tool(listed, 'early').inputSchema.properties)).toEqual(['q', 'session_id', 'intent'])
    // Changed after one: zod still checks the keys it first saw, so the tool is left alone.
    expect(Object.keys(tool(listed, 'late').inputSchema.properties)).toEqual(['q', 'session_id'])
    await connection.call('late', { q: 'b', session_id: SENT })
    const events = await lsquare.calls()
    await tick()
    expect(events[2]).not.toHaveProperty('sessionId')
    expect(lsquare.logs).toContain('[lsquare] Tool "late" gets no session_id or intent: its input schema can\'t be read safely (its shape changed after it was first used), so any input could be its own.')
  })

  it('lists nothing more for a recursive schema, but still takes the inputs out of its calls', async () => {
    for (const kit of [kit1, kit2]) {
      const lsquare = setup()
      const seen: unknown[] = []
      const Tree: any = z.strictObject({ name: z.string(), children: z.array(z.lazy(() => Tree)).optional() })
      const server = instrument(kit.server(), lsquare.client)
      server.registerTool('tree', { inputSchema: Tree }, echo(seen))
      const connection = await kit.connect(server)
      const listed = tool(await connection.list(), 'tree').inputSchema
      expect(Object.keys(listed.properties)).toEqual(['name', 'children'])
      expect(await connection.call('tree', { name: 'root', session_id: SENT, intent: 'why' })).toEqual(text('{"name":"root"}'))
      expect((await lsquare.calls())[0]).toMatchObject({ sessionId: SENT, intent: 'why' })
    }
  })

  it("lists them on a zod 3 schema whose listing refers to its own properties, as reused schemas do", async () => {
    const lsquare = setup()
    const id = z3.string()
    const Node: any = z3.lazy(() => z3.object({ name: z3.string(), children: z3.array(Node).optional() }))
    const server = instrument(kit1.server(), lsquare.client)
    server.registerTool('pair', { inputSchema: z3.object({ from: id, to: id }) as never }, async () => text('ok'))
    server.registerTool('tree', { inputSchema: z3.object({ tree: Node }) as never }, async () => text('ok'))
    const listed = await (await kit1.connect(server)).list()
    expect(tool(listed, 'pair').inputSchema.properties.to).toEqual({ $ref: '#/properties/from' })
    expect(Object.keys(tool(listed, 'pair').inputSchema.properties)).toEqual(['from', 'to', 'session_id', 'intent'])
    expect(Object.keys(tool(listed, 'tree').inputSchema.properties)).toEqual(['tree', 'session_id', 'intent'])
    // The nested level that refers back is the property, not the root: it gains nothing.
    expect(Object.keys(tool(listed, 'tree').inputSchema.properties.tree.properties)).toEqual(['name', 'children'])
  })

  it("lists nothing more where the listed schema can't take it, and still takes the inputs out of calls", async () => {
    const limits: Array<[string, Record<string, unknown>]> = [
      ['a property-count limit', { maxProperties: 3 }],
      ['const', { const: { q: 'a' } }],
      ['enum', { enum: [{ q: 'a' }] }],
      ['dependentRequired', { dependentRequired: { q: ['q'] } }],
      ['a required naming session_id', { required: ['q', 'session_id'] }],
    ]
    for (const kit of [kit1, kit2]) {
      for (const [label, meta] of limits) {
        const lsquare = setup()
        const seen: unknown[] = []
        const server = instrument(kit.server(), lsquare.client)
        server.registerTool('t', { inputSchema: z.strictObject({ q: z.string() }).meta(meta) }, echo(seen))
        const connection = await kit.connect(server)
        const properties = Object.keys(tool(await connection.list(), 't').inputSchema.properties)
        expect(properties, label).toEqual(label === 'a required naming session_id' ? ['q', 'intent'] : ['q'])
        await connection.call('t', { q: 'a', session_id: SENT, intent: 'why' })
        expect(seen, label).toEqual([{ q: 'a' }])
        expect((await lsquare.calls())[0], label).toMatchObject({ sessionId: SENT, intent: 'why' })
      }
    }
  })
})

describe.each([kit1, kit2])('$name: get_more_tools', (kit) => {
  it('is answered by the wrapper, which records the need under the inputs option', async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client)
    server.registerTool('search', { inputSchema: z.object({ q: z.string() }) }, async () => text('ok'))
    const connection = await kit.connect(server)
    const first = await connection.call('get_more_tools', { need: 'Cancel an order' })
    expect(withoutLine(first)).toEqual(text(REPLY))
    expect(await connection.call('get_more_tools', { need: 'Refund it', session_id: issued(first) })).toEqual(text(REPLY))
    const [one, two] = await lsquare.calls()
    expect(one).toMatchObject({ toolName: 'get_more_tools', extra: 'missing_tools', status: 'completed', input: { need: 'Cancel an order' }, sessionSource: 'issued', result: text(REPLY) })
    expect(two).toMatchObject({ extra: 'missing_tools', input: { need: 'Refund it' }, sessionId: issued(first), sessionSource: 'sent' })

    const quiet = setup()
    const hidden = instrument(kit.server(), quiet.client, { inputs: (name) => name !== 'get_more_tools' })
    hidden.registerTool('search', {}, async () => text('ok'))
    await (await kit.connect(hidden)).call('get_more_tools', { need: 'Cancel an order' })
    expect((await quiet.calls())[0]).not.toHaveProperty('input')
  })

  it('answers a call without a string need with an isError reply, recorded as protocol', async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client)
    server.registerTool('search', {}, async () => text('ok'))
    const reply = await (await kit.connect(server)).call('get_more_tools', { need: 3 })
    expect(reply).toMatchObject({ isError: true, content: [{ type: 'text', text: expect.stringContaining('need is required') }] })
    expect(issued(reply)).toBeUndefined()
    expect((await lsquare.calls())[0]).toMatchObject({ extra: 'missing_tools', status: 'error', error: { category: 'protocol' } })
  })

  it('is there only while the server has an enabled tool of its own', async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client)
    const search = server.registerTool('search', {}, async () => text('ok'))
    const connection = await kit.connect(server)
    expect((await connection.list()).tools.map((entry: any) => entry.name)).toEqual(['search', 'get_more_tools'])
    search.disable()
    expect((await connection.list()).tools).toEqual([])
    expect(JSON.stringify(await connection.call('get_more_tools', { need: 'x' }))).toMatch(/get_more_tools not found/)
    search.enable()
    search.remove()
    expect((await connection.list()).tools).toEqual([])
  })

  it('gives way to a tool of that name the developer registers, even a disabled one', async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client)
    server.registerTool('search', {}, async () => text('ok'))
    const connection = await kit.connect(server)
    expect(tool(await connection.list(), 'get_more_tools')).toBeDefined()
    const theirs = server.registerTool('get_more_tools', { description: 'Their own' }, async () => text('theirs'))
    const listed = await connection.list()
    expect(listed.tools.filter((entry: any) => entry.name === 'get_more_tools')).toMatchObject([{ description: 'Their own' }])
    expect(withoutLine(await connection.call('get_more_tools', { need: 'x' }))).toEqual(text('theirs'))
    theirs.disable()
    expect((await connection.list()).tools.map((entry: any) => entry.name)).toEqual(['search'])
    await tick()
    expect(lsquare.logs.filter((line) => line.includes('get_more_tools'))).toEqual(["[lsquare] The server has its own get_more_tools tool: it is left as it is, and LSquare's isn't added."])
  })

  it('is added to the last page of a paged list only', async () => {
    const lsquare = setup()
    const plain = kit.server()
    plain.registerTool('a', {}, async () => text('a'))
    plain.registerTool('b', {}, async () => text('b'))
    const handlers = plain.server._requestHandlers
    const whole = handlers.get('tools/list')
    handlers.set('tools/list', async (request: any, extra: unknown) => {
      const { tools } = await whole(request, extra)
      return request.params?.cursor ? { tools: tools.slice(1) } : { tools: tools.slice(0, 1), nextCursor: 'page-2' }
    })
    instrument(plain, lsquare.client)
    // SDK 2.x's client follows nextCursor itself: the pages are read from the handler.
    const page = (cursor?: string) => handlers.get('tools/list')({ method: 'tools/list', params: cursor ? { cursor } : {} }, {})
    const first = await page()
    expect(first.tools.map((entry: any) => entry.name)).toEqual(['a'])
    expect(first.nextCursor).toBe('page-2')
    expect((await page('page-2')).tools.map((entry: any) => entry.name)).toEqual(['b', 'get_more_tools'])
  })
})

it('SDK 2.x: get_more_tools is absent with only the tools capability declared', async () => {
  const lsquare = setup()
  const server = instrument(kit2.server({ capabilities: { tools: {} } }), lsquare.client)
  const connection = await kit2.connect(server)
  expect((await connection.list()).tools).toEqual([])
  expect(JSON.stringify(await connection.call('get_more_tools', { need: 'x' }))).toMatch(/get_more_tools not found/)
})

const transports = [
  { kit: kit1, http: (options: object) => new Http1(options), stdio: (stdin: PassThrough, stdout: PassThrough) => new Stdio1(stdin, stdout) },
  { kit: kit2, http: (options: object) => new Http2(options), stdio: (stdin: PassThrough, stdout: PassThrough) => new Stdio2(stdin, stdout) },
]

describe.each(transports)('$kit.name: protocol sessions', ({ kit, http, stdio }) => {
  const ask = (lsquare: ReturnType<typeof setup>) => {
    const server = instrument(kit.server(), lsquare.client)
    server.registerTool('ask', { inputSchema: z.object({}) }, async () => text('ok'))
    return server
  }
  const post = (transport: { handleRequest(request: Request): Promise<Response> }, body: unknown, headers: Record<string, string> = {}) =>
    transport.handleRequest(
      new Request('http://localhost/mcp', {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: 'application/json, text/event-stream', 'mcp-protocol-version': '2025-06-18', ...headers },
        body: JSON.stringify(body),
      }),
    )
  const initialize = { jsonrpc: '2.0', id: 0, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'old', version: '1' } } }
  const callAsk = { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'ask', arguments: {} } }

  it("groups an older client's calls by the session a stateful HTTP server issued, leaving results unchanged", async () => {
    const lsquare = setup()
    const transport: any = http({ sessionIdGenerator: () => 'issued-by-server', enableJsonResponse: true })
    await ask(lsquare).connect(transport)
    await post(transport, initialize)
    const response = await post(transport, callAsk, { 'mcp-session-id': 'issued-by-server' })
    expect(((await response.json()) as any).result).toEqual(text('ok'))
    expect((await lsquare.calls())[0]).toMatchObject({ sessionId: 'issued-by-server', sessionSource: 'protocol' })
  })

  it('sends no session ID from a stateful HTTP server with sessions turned off', async () => {
    const lsquare = setup()
    const server = instrument(kit.server(), lsquare.client, { extras: { sessions: false } })
    server.registerTool('ask', { inputSchema: z.object({}) }, async () => text('ok'))
    const transport: any = http({ sessionIdGenerator: () => 'issued-by-server', enableJsonResponse: true })
    await server.connect(transport)
    await post(transport, initialize)
    const response = await post(transport, callAsk, { 'mcp-session-id': 'issued-by-server' })
    expect(((await response.json()) as any).result).toEqual(text('ok'))
    const calls = await lsquare.calls()
    expect(calls).toHaveLength(1)
    expect(calls[0]).not.toHaveProperty('sessionId')
    expect(calls[0]).not.toHaveProperty('sessionSource')
  })

  it('never takes a session from a header a stateless HTTP server did not issue: it gives an ID instead', async () => {
    const lsquare = setup()
    // No sessionIdGenerator: a stateless server, which takes any Mcp-Session-Id header.
    const transport: any = http({ enableJsonResponse: true })
    await ask(lsquare).connect(transport)
    const response = await post(transport, callAsk, { 'mcp-session-id': 'forged' })
    const id = issued(((await response.json()) as any).result)
    expect(id).toMatch(ID)
    expect((await lsquare.calls())[0]).toMatchObject({ sessionId: id, sessionSource: 'issued' })
  })

  it("groups a stdio connection's calls by one ID made for it, and another connection's by another", async () => {
    const lsquare = setup()
    const connect = async () => {
      const stdin = new PassThrough()
      const stdout = new PassThrough()
      const lines: any[] = []
      stdout.on('data', (chunk) => {
        for (const line of String(chunk).split('\n')) if (line.trim()) lines.push(JSON.parse(line))
      })
      await ask(lsquare).connect(stdio(stdin, stdout))
      const send = (message: unknown) => stdin.write(`${JSON.stringify(message)}\n`)
      send(initialize)
      send({ jsonrpc: '2.0', method: 'notifications/initialized' })
      send(callAsk)
      send({ ...callAsk, id: 2 })
      for (let wait = 0; lines.length < 3 && wait < 100; wait++) await sleep(10)
      return lines.filter((line) => line.id > 0).map((line) => line.result)
    }
    expect(await connect()).toEqual([text('ok'), text('ok')])
    expect(await connect()).toEqual([text('ok'), text('ok')])
    const [one, two, three, four] = await lsquare.calls()
    expect(one).toMatchObject({ sessionSource: 'protocol', sessionId: expect.stringMatching(/^[0-9a-f-]{36}$/) })
    expect(two!.sessionId).toBe(one!.sessionId)
    expect(three).toMatchObject({ sessionSource: 'protocol' })
    expect(four!.sessionId).toBe(three!.sessionId)
    expect(three!.sessionId).not.toBe(one!.sessionId)
  })
})

describe('protocol versions', () => {
  const ask = (lsquare: ReturnType<typeof setup>) => {
    const server = instrument(kit1.server(), lsquare.client)
    server.registerTool('ask', { inputSchema: z.object({}) }, async () => text('ok'))
    return server
  }

  it('gives an ID to a client on 2026-07-28, which has no sessions, whatever the transport', async () => {
    const lsquare = setup()
    const server = ask(lsquare)
    const extra = { sessionId: 'transport-session', signal: new AbortController().signal, sendNotification: async () => {}, sendRequest: async () => {}, requestId: 1 }
    const modern = { method: 'tools/call', params: { name: 'ask', arguments: {}, _meta: { 'io.modelcontextprotocol/protocolVersion': '2026-07-28' } } }
    const reply: any = await server.server._requestHandlers.get('tools/call')(modern, extra)
    expect(issued(reply)).toMatch(ID)
    const older = { method: 'tools/call', params: { name: 'ask', arguments: {}, _meta: { 'io.modelcontextprotocol/protocolVersion': 'draft' } } }
    expect(await server.server._requestHandlers.get('tools/call')(older, extra)).toEqual(text('ok'))
    const [one, two] = await lsquare.calls()
    expect(one).toMatchObject({ sessionSource: 'issued' })
    expect(two).toMatchObject({ sessionId: 'transport-session', sessionSource: 'protocol' })
  })
})

describe('rounds and tasks', () => {
  it('gives no ID line to an input_required round, and labels a continuation that brings only requestState', async () => {
    const lsquare = setup()
    const server = instrument(kit2.server(), lsquare.client)
    // A 2026-07-28 instance hands input_required back to the client, instead of running the rounds itself.
    server.server._negotiatedProtocolVersion = '2026-07-28'
    let rounds = 0
    // The round carries content too, so only the rule keeps the line off it.
    const round = { resultType: 'input_required', requestState: 'opaque', content: [] }
    server.registerTool('ask', { inputSchema: z.object({}) }, async () => (rounds++ === 0 ? round : text('done')) as never)
    const call = (requestState: string | undefined) =>
      server.server._requestHandlers.get('tools/call')(
        { method: 'tools/call', params: { name: 'ask', arguments: {} } },
        { mcpReq: { id: 1, signal: new AbortController().signal, envelope: { 'io.modelcontextprotocol/protocolVersion': '2026-07-28' }, requestState: () => requestState } },
      )
    expect(await call(undefined)).toEqual(round)
    const final = await call('opaque')
    expect(issued(final)).toMatch(ID)
    const [first, second] = await lsquare.calls()
    expect(first).toMatchObject({ round: 'input_required' })
    expect(first).not.toHaveProperty('sessionId')
    expect(second).toMatchObject({ round: 'continuation', sessionSource: 'issued' })
  })

  it("treats the rounds SDK 2.x's legacy shim runs for an older client as one call, whose final result gets the ID", async () => {
    const lsquare = setup()
    const server = instrument(kit2.server(), lsquare.client)
    let rounds = 0
    server.registerTool('deploy', { inputSchema: z.object({ env: z.string() }) }, async ({ env }: any, ctx: any) => {
      rounds++
      const confirmed = acceptedContent(ctx.mcpReq.inputResponses, 'confirm') as { confirm?: boolean } | undefined
      if (confirmed?.confirm !== true) {
        return inputRequired({ inputRequests: { confirm: inputRequired.elicit({ message: `Deploy to ${env}?`, requestedSchema: z.object({ confirm: z.boolean() }) }) } }) as never
      }
      return text(`deployed to ${env}`)
    })
    const [a, b] = Memory2.createLinkedPair()
    await server.connect(a)
    // In memory the pair negotiates 2025-11-25, so the server's shim runs the rounds itself.
    const client = new Client2({ name: 'older', version: '1' }, { capabilities: { elicitation: { form: {} } } })
    client.setRequestHandler('elicitation/create', (async () => ({ action: 'accept', content: { confirm: true } })) as never)
    await client.connect(b)
    const reply = await client.callTool({ name: 'deploy', arguments: { env: 'prod' } })
    expect(rounds).toBe(2)
    expect(withoutLine(reply)).toEqual(text('deployed to prod'))
    const events = await lsquare.calls()
    expect(events).toHaveLength(1)
    expect(events[0]).toMatchObject({ sessionId: issued(reply), sessionSource: 'issued', status: 'completed' })
    expect(events[0]).not.toHaveProperty('round')
  })

  it("keeps a capture() in a later round of the legacy shim inside the call, and one left over from an earlier round out of it", async () => {
    const lsquare = setup()
    const server = instrument(kit2.server(), lsquare.client)
    server.registerTool('deploy', { inputSchema: z.object({ env: z.string() }) }, async ({ env }: any, ctx: any) => {
      const confirmed = acceptedContent(ctx.mcpReq.inputResponses, 'confirm') as { confirm?: boolean } | undefined
      if (confirmed?.confirm !== true) {
        // Lands while the client is still answering, after this round has returned.
        setTimeout(() => lsquare.client.capture({ question: 'left over' }), 0)
        return inputRequired({ inputRequests: { confirm: inputRequired.elicit({ message: `Deploy to ${env}?`, requestedSchema: z.object({ confirm: z.boolean() }) }) } }) as never
      }
      lsquare.client.capture({ conversationId: 'second-round', question: 'Deploy it?' })
      return text(`deployed to ${env}`)
    })
    const [a, b] = Memory2.createLinkedPair()
    await server.connect(a)
    const client = new Client2({ name: 'older', version: '1' }, { capabilities: { elicitation: { form: {} } } })
    client.setRequestHandler('elicitation/create', (async () => {
      await sleep(20)
      return { action: 'accept', content: { confirm: true } }
    }) as never)
    await client.connect(b)
    // Grouped by the second round's conversation: no ID line.
    expect(await client.callTool({ name: 'deploy', arguments: { env: 'prod' } })).toEqual(text('deployed to prod'))
    const events = await lsquare.calls()
    expect(events).toHaveLength(1)
    expect(events[0]).toMatchObject({ conversationId: 'second-round', agent: { question: 'Deploy it?' } })
    expect(events[0]).not.toHaveProperty('sessionId')
    expect((await lsquare.turns()).map((turn) => [turn.question, turn.callId])).toEqual([['left over', events[0]!.callId]])
  })

  it('gives no ID line to a task acknowledgement', async () => {
    const lsquare = setup()
    const server = instrument(kit1.server(), lsquare.client)
    const task = { taskId: 'task_1', status: 'working', ttl: 1_000, createdAt: new Date().toISOString(), lastUpdatedAt: new Date().toISOString() }
    server.experimental.tasks.registerToolTask('job', { inputSchema: {} }, {
      // With content too, so only the rule keeps the line off it.
      createTask: async () => ({ task, content: [] }) as never,
      getTask: async () => task,
      getTaskResult: async () => text('done'),
    })
    const extra = { signal: new AbortController().signal, sendNotification: async () => {}, sendRequest: async () => {}, requestId: 1, taskStore: {} }
    const reply = await server.server._requestHandlers.get('tools/call')({ method: 'tools/call', params: { name: 'job', arguments: {}, task: { ttl: 1_000 } } }, extra)
    expect(reply).toMatchObject({ task })
    expect(issued(reply)).toBeUndefined()
    const [event] = await lsquare.calls()
    expect(event).toMatchObject({ task: { id: 'task_1' } })
    expect(event).not.toHaveProperty('sessionId')
  })
})

describe('dry run', () => {
  it('says in dry run what the extras can change, counting the tools registered so far', async () => {
    const lsquare = setup({ apiKey: undefined, dryRun: true })
    const server = kit1.server()
    server.registerTool('search', { inputSchema: z.object({ q: z.string() }) }, async () => text('ok'))
    server.registerTool('resume', { inputSchema: z.object({ session_id: z.string() }) }, async () => text('ok'))
    server.registerTool('mixed', { inputSchema: z.union([z.object({ a: z.string() }), z.object({ b: z.string() })]) }, async () => text('ok'))
    instrument(server, lsquare.client, { extras: { missingTools: false } })
    await tick()
    const lines = lsquare.logs.filter((line) => line.startsWith('[lsquare dry-run]')).map((line) => JSON.parse(line.replace('[lsquare dry-run] ', '')))
    expect(lines).toEqual([
      {
        server: 'orders',
        level: 'high',
        records: 'every tools/call',
        extras: {
          session_id: '1 of the 3 tools registered so far',
          intent: '2 of the 3 tools registered so far',
          later: "tools registered later follow the same rules, and a tool whose listed schema can't take an input is listed as it is",
          idLine: "added to a task's first result when its call can't be grouped by its protocol session or a conversationId",
        },
      },
    ])
    expect(lsquare.logs.filter((line) => line.includes('"mixed"'))).toEqual([
      '[lsquare] Tool "mixed" gets no session_id or intent: its input schema can\'t be read safely (it is a zod union, not a plain object), so any input could be its own.',
    ])
  })
})

describe('the listing check', () => {
  const schema = (more: Record<string, unknown>) => ({ type: 'object', properties: { q: { type: 'string' } }, ...more })

  it('reads at most its budget of entries, however wide or deep the schema', () => {
    let reads = 0
    const examples: Record<string, unknown> = {}
    for (let index = 0; index < 20_001; index++) Object.defineProperty(examples, `k${index}`, { enumerable: true, get: () => (reads++, 1) })
    expect(advertise(schema({ examples }), ['session_id'])).toBeUndefined()
    expect(reads).toBeLessThanOrEqual(10_000)
    expect(advertise(schema({ examples: new Array(20_001).fill(1) }), ['session_id'])).toBeUndefined()
    const deep = (levels: number): unknown => (levels === 0 ? {} : { nested: deep(levels - 1) })
    expect(advertise(schema({ default: deep(70) }), ['session_id'])).toBeUndefined()
    expect(advertise(schema({ default: deep(10) }), ['session_id'])).toBeDefined()
    expect(advertise(schema({ examples: new Array(100).fill({ a: 1 }) }), ['session_id'])).toBeDefined()
  })

  it('refuses a reference that could reach the root or an input being added', () => {
    for (const ref of ['#', '#/', '#/properties/session_id', '#/properties/session_id/x', '#/properties/session%5Fid', 'other.json#/properties/q', '#anchor']) {
      expect(advertise(schema({ $defs: { x: { $ref: ref } } }), ['session_id']), ref).toBeUndefined()
    }
    for (const ref of ['#/$defs/y', '#/definitions/y', '#/properties/q', '#/properties/q/items']) {
      expect(advertise(schema({ $defs: { x: { $ref: ref }, y: {} }, definitions: { y: {} } }), ['session_id']), ref).toBeDefined()
    }
    expect(advertise(schema({ $defs: { x: { $dynamicRef: '#meta' } } }), ['session_id'])).toBeUndefined()
    expect(advertise(schema({ $defs: { x: { $recursiveRef: '#' } } }), ['session_id'])).toBeUndefined()
  })
})
