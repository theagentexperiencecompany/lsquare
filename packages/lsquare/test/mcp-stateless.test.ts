// A client on a 2025 protocol version calling a stateless HTTP server, where each request is served
// by a fresh server that never saw the client's initialize: SDK 1.x's transport without a
// sessionIdGenerator, and SDK 2.x's createMcpHandler, whose legacy fallback is stateless. Then a
// stateful server, and the order the protocol version's sources are read in.
import { Client as Client1 } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport as ClientHttp1 } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { McpServer as McpServer1 } from '@modelcontextprotocol/sdk/server/mcp.js'
import { WebStandardStreamableHTTPServerTransport as Http1 } from '@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js'
import { McpServer as McpServer2, WebStandardStreamableHTTPServerTransport as Http2, createMcpHandler } from '@modelcontextprotocol/server'
import { afterEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { validateEvent } from '../../contract/src/validate.js'
import { LSquare, type LSquareEvent, type ToolCallEvent } from '../src/index.js'
import { instrument } from '../src/mcp/index.js'
import { createHash } from 'node:crypto'

const hashed = (id: string): string => createHash('sha256').update(id).digest('hex')
const text = (value: string) => ({ content: [{ type: 'text' as const, text: value }] })
const OFF = { sessions: false, intent: false, missingTools: false }
const LINE = /^Task ID: (ses_[A-Za-z0-9_-]{22})\. Send it as session_id on later calls for this task, and pass it to any subagent working on it\.$/

const rejected: string[] = []
afterEach(() => {
  expect(rejected).toEqual([])
  rejected.length = 0
})

function setup() {
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
  const client = new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test', fetch, logger: { info() {}, warn() {} } })
  const calls = async () => {
    await client.flush()
    return events.filter((e): e is ToolCallEvent => e.kind === 'tool_call')
  }
  return { client, calls }
}

type Serve = (request: Request) => Promise<Response>

/** A server built per request, as a stateless server is: `build` registers its tools and instruments it. */
const sdks: { name: string; serve: (build: (server: any) => void) => Serve }[] = [
  {
    name: 'SDK 1.x',
    serve: (build) => async (request) => {
      const server = new McpServer1({ name: 'orders', version: '1.0.0' })
      build(server)
      const transport = new Http1({ enableJsonResponse: true })
      await server.connect(transport)
      return transport.handleRequest(request)
    },
  },
  {
    name: 'SDK 2.x',
    serve: (build) => {
      const handler = createMcpHandler(() => {
        const server = new McpServer2({ name: 'orders', version: '1.0.0' })
        build(server)
        return server
      })
      return (request) => handler.fetch(request)
    },
  },
]

/** SDK 1.x's client, which speaks 2025-11-25, as most agents still do. */
async function connect(serve: Serve) {
  const client = new Client1({ name: 'cursor', version: '1.7' })
  const fetch = (url: string | URL, init?: RequestInit) => serve(new Request(url, init))
  await client.connect(new ClientHttp1(new URL('https://example.com/mcp'), { fetch }))
  return client
}

describe.each(sdks)('$name, stateless, a client on 2025-11-25', ({ serve }) => {
  it("records the protocol version the request's MCP-Protocol-Version header gives, and no client, which only initialize names", async () => {
    const lsquare = setup()
    const handler = serve((server) => {
      server.registerTool('ask', { inputSchema: z.object({}) }, async () => text('ok'))
      instrument(server, lsquare.client, { extras: OFF })
    })
    const client = await connect(handler)
    expect(await client.callTool({ name: 'ask', arguments: {} })).toEqual(text('ok'))
    const [call] = await lsquare.calls()
    expect(call).toMatchObject({ toolName: 'ask', status: 'completed', protocolVersion: '2025-11-25', server: { name: 'orders' } })
    expect(call).not.toHaveProperty('client')
  })

  it('still gives the client an ID with the defaults, which it can send back: an older version has no session there', async () => {
    const lsquare = setup()
    const handler = serve((server) => {
      server.registerTool('ask', { inputSchema: z.object({}) }, async () => text('ok'))
      instrument(server, lsquare.client)
    })
    const client = await connect(handler)
    const first: any = await client.callTool({ name: 'ask', arguments: {} })
    expect(first.content[0]).toEqual({ type: 'text', text: 'ok' })
    const id = LINE.exec(first.content[1]?.text)?.[1]
    expect(id).toMatch(/^ses_/)
    expect(await client.callTool({ name: 'ask', arguments: { session_id: id } })).toEqual(text('ok'))
    const [one, two] = await lsquare.calls()
    expect(one).toMatchObject({ protocolVersion: '2025-11-25', sessionId: id, sessionSource: 'issued' })
    expect(two).toMatchObject({ protocolVersion: '2025-11-25', sessionId: id, sessionSource: 'sent' })
  })
})

const transports: { name: string; server: () => any; http: (options: object) => unknown }[] = [
  { name: 'SDK 1.x', server: () => new McpServer1({ name: 'orders', version: '1.0.0' }), http: (options) => new Http1(options) },
  { name: 'SDK 2.x', server: () => new McpServer2({ name: 'orders', version: '1.0.0' }), http: (options) => new Http2(options) },
]

describe.each(transports)('$name, stateful, a client on 2025-11-25', ({ server: make, http }) => {
  const post = (transport: { handleRequest(request: Request): Promise<Response> }, body: unknown, headers: Record<string, string>) =>
    transport.handleRequest(
      new Request('http://localhost/mcp', {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: 'application/json, text/event-stream', ...headers },
        body: JSON.stringify(body),
      }),
    )
  const initialize = { jsonrpc: '2.0', id: 0, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'cursor', version: '1.7' } } }
  const callAsk = { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'ask', arguments: {} } }

  it("takes the version a request's header gives over the one initialize negotiated, and the one negotiated without a header", async () => {
    const lsquare = setup()
    const server = make()
    server.registerTool('ask', { inputSchema: z.object({}) }, async () => text('ok'))
    instrument(server, lsquare.client)
    const transport: any = http({ sessionIdGenerator: () => 'issued-by-server', enableJsonResponse: true })
    await server.connect(transport)
    await post(transport, initialize, {})
    // The transports check that a header names a version they support, not the one negotiated.
    const declared = await post(transport, callAsk, { 'mcp-session-id': 'issued-by-server', 'mcp-protocol-version': '2025-03-26' })
    expect(((await declared.json()) as any).result).toEqual(text('ok'))
    await post(transport, { ...callAsk, id: 2 }, { 'mcp-session-id': 'issued-by-server' })
    const [one, two] = await lsquare.calls()
    expect(one).toMatchObject({ protocolVersion: '2025-03-26', client: { name: 'cursor', version: '1.7' }, sessionId: hashed('issued-by-server'), sessionSource: 'protocol' })
    expect(two).toMatchObject({ protocolVersion: '2025-11-25', client: { name: 'cursor', version: '1.7' }, sessionId: hashed('issued-by-server'), sessionSource: 'protocol' })
  })
})

describe('where the protocol version comes from', () => {
  const extra = (more: Record<string, unknown> = {}) => ({ signal: new AbortController().signal, sendNotification: async () => {}, sendRequest: async () => {}, requestId: 1, ...more })
  const ask = (meta?: Record<string, unknown>) => ({ method: 'tools/call', params: { name: 'ask', arguments: {}, ...(meta ? { _meta: meta } : {}) } })
  const throwing = new Proxy({}, { get: () => { throw new Error('unreadable') } })

  it('reads the envelope, then _meta, then the header, then what initialize negotiated, and only a header given as one string', async () => {
    const lsquare = setup()
    const server = instrument(new McpServer1({ name: 'orders', version: '1.0.0' }), lsquare.client, { extras: OFF })
    server.registerTool('ask', { inputSchema: z.object({}) }, async () => text('ok'))
    const handlers = (server.server as any)._requestHandlers
    await handlers.get('initialize')({ method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'cursor', version: '1.7' } } }, extra())
    const call = (request: unknown, more: Record<string, unknown>) => handlers.get('tools/call')(request, extra(more))
    const plain = { requestInfo: { headers: { 'mcp-protocol-version': '2025-03-26' } } }
    const web = { http: { req: new Request('http://localhost/mcp', { headers: { 'mcp-protocol-version': '2025-03-26' } }) } }
    await call(ask({ 'io.modelcontextprotocol/protocolVersion': 'DRAFT-2026-v1' }), { ...plain, mcpReq: { envelope: { 'io.modelcontextprotocol/protocolVersion': '2026-07-28' } } })
    await call(ask({ 'io.modelcontextprotocol/protocolVersion': '2026-07-28' }), plain)
    await call(ask(), plain)
    await call(ask(), web)
    await call(ask(), {})
    await call(ask(), { requestInfo: { headers: { 'mcp-protocol-version': ['2025-03-26', '2024-11-05'] } } })
    await call(ask(), { requestInfo: { headers: { get: () => { throw new Error('unreadable') } } } })
    await call(ask(), { requestInfo: throwing, http: throwing })
    expect((await lsquare.calls()).map((event) => event.protocolVersion)).toEqual([
      '2026-07-28',
      '2026-07-28',
      '2025-03-26',
      '2025-03-26',
      '2025-06-18',
      '2025-06-18',
      '2025-06-18',
      '2025-06-18',
    ])
  })
})
