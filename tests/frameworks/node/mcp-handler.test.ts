// Vercel's mcp-handler: an SDK 2.x McpServer built per request and handed to the developer's
// callback, where it is instrumented with Vercel's waitUntil (here a stub that keeps the promises).
import { Client, StreamableHTTPClientTransport } from '@modelcontextprotocol/client'
import { Client as Client1 } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport as ClientHttp1 } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { LSquare } from 'lsquare'
import { instrument, type InstrumentOptions } from 'lsquare/mcp'
import { createMcpHandler } from 'mcp-handler'
import { afterEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { Ingestion, issued, text, until } from '../shared/ingestion.js'

afterEach(() => {
  delete process.env.VERCEL
})

function setup(options: (registered: Promise<unknown>[]) => InstrumentOptions = (registered) => ({ waitUntil: (promise) => void registered.push(promise) })) {
  const ingestion = new Ingestion()
  const logs: string[] = []
  const lsquare = new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test', fetch: ingestion.fetch, logger: { info: (line) => logs.push(line), warn: (line) => logs.push(line) } })
  const registered: Promise<unknown>[] = []
  const handler = createMcpHandler(
    (server) => {
      server.registerTool('search', { inputSchema: z.object({ q: z.string() }) }, async ({ q }) => {
        lsquare.capture({ question: `Find ${q}` })
        return text(`3 orders for ${q}`)
      })
      server.registerTool('lookup', { inputSchema: z.object({ tenant: z.string().meta({ 'x-mcp-header': 'Tenant' }) }) }, async ({ tenant }) => text(`for ${tenant}`))
      instrument(server, lsquare, options(registered))
    },
    { serverInfo: { name: 'orders', version: '1.0.0' } },
  )
  const fetch = (url: string | URL | Request, init?: RequestInit) => handler(new Request(url, init))
  return { ingestion, logs, lsquare, registered, fetch }
}

async function connect(fetch: (url: string | URL | Request, init?: RequestInit) => Promise<Response>, options: ConstructorParameters<typeof Client>[1] = {}) {
  const client = new Client({ name: 'claude-code', version: '2.1' }, options)
  await client.connect(new StreamableHTTPClientTransport(new URL('https://example.com/mcp'), { fetch }))
  return client
}

/** SDK 1.x's client, on protocol 2025-11-25, which the handler serves through its stateless legacy leg. */
async function connectOlder(fetch: (url: string | URL | Request, init?: RequestInit) => Promise<Response>) {
  const client = new Client1({ name: 'cursor', version: '1.7' })
  await client.connect(new ClientHttp1(new URL('https://example.com/mcp'), { fetch }))
  return client
}

function modernCall(name: string, args: Record<string, unknown>, headers: Record<string, string> = {}) {
  return new Request('https://example.com/mcp', {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json, text/event-stream', 'mcp-protocol-version': '2026-07-28', 'mcp-method': 'tools/call', 'mcp-name': name, ...headers },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'tools/call',
      params: { name, arguments: args, _meta: { 'io.modelcontextprotocol/protocolVersion': '2026-07-28', 'io.modelcontextprotocol/clientInfo': { name: 'claude-code', version: '2.1' }, 'io.modelcontextprotocol/clientCapabilities': {} } },
    }),
  })
}

describe("Vercel's mcp-handler", () => {
  it("hands each call's sending to waitUntil: the reply comes first, the event once the ingestion answers", async () => {
    const { ingestion, registered, fetch } = setup()
    const client = await connect(fetch)
    ingestion.hold()
    const reply = await client.callTool({ name: 'search', arguments: { q: 'late' } })
    expect(reply.content?.[0]).toEqual({ type: 'text', text: '3 orders for late' })
    expect(registered).toHaveLength(1)
    let settled = false
    void registered[0]!.then(() => (settled = true))
    await until(() => ingestion.batches.length > 0)
    expect(ingestion.batches[0]!.answered).toBe(false)
    expect(settled).toBe(false)
    ingestion.release()
    await registered[0]
    expect(settled).toBe(true)
    expect(ingestion.batches[0]!.answered).toBe(true)
    expect(ingestion.events('tool_call')[0]).toMatchObject({
      toolName: 'search',
      sdk: { runtime: expect.stringMatching(/^node\//) },
      server: { name: 'orders' },
      agent: { question: 'Find late' },
      sessionSource: 'issued',
      sessionId: issued(reply),
    })
  })

  it('adds the extras on each per-request server', async () => {
    const { ingestion, fetch, lsquare } = setup()
    const client = await connect(fetch)
    const listed = await client.listTools()
    expect(Object.keys(listed.tools.find((tool) => tool.name === 'search')!.inputSchema.properties ?? {})).toEqual(['q', 'session_id', 'intent'])
    expect(listed.tools.map((tool) => tool.name)).toContain('get_more_tools')
    const id = issued(await client.callTool({ name: 'search', arguments: { q: 'a', intent: 'Find late orders' } }))
    await client.callTool({ name: 'search', arguments: { q: 'b', session_id: id } })
    await lsquare.flush()
    expect(ingestion.events('tool_call')).toMatchObject([
      { intent: 'Find late orders', sessionSource: 'issued' },
      { sessionId: id, sessionSource: 'sent' },
    ])
  })

  it("names a client on 2026-07-28; one on 2025-11-25 only gets its protocol version, from its request's header", async () => {
    const { ingestion, lsquare, fetch } = setup()
    await (await connect(fetch, { versionNegotiation: { mode: { pin: '2026-07-28' } } })).callTool({ name: 'search', arguments: { q: 'new' } })
    await (await connectOlder(fetch)).callTool({ name: 'search', arguments: { q: 'old' } })
    await lsquare.flush()
    const [modern, older] = ingestion.events('tool_call')
    expect(modern).toMatchObject({ input: { q: 'new' }, client: { name: 'claude-code', version: '2.1' }, protocolVersion: '2026-07-28' })
    expect(older).toMatchObject({ input: { q: 'old' }, protocolVersion: '2025-11-25', sessionSource: 'issued' })
    expect(older).not.toHaveProperty('client')
  })

  it("makes no event for a call the SDK's HTTP layer refuses before dispatch", async () => {
    const { ingestion, lsquare, fetch } = setup()
    expect((await fetch(modernCall('lookup', { tenant: 'acme' }))).status).toBe(400)
    expect((await fetch(modernCall('lookup', { tenant: 'acme' }, { 'mcp-param-tenant': 'acme' }))).status).toBe(200)
    await lsquare.flush()
    expect(ingestion.events('tool_call').map((event) => event.toolName)).toEqual(['lookup'])
  })

  it('warns once on Vercel without waitUntil, and not with it', async () => {
    process.env.VERCEL = '1'
    const plain = setup(() => ({}))
    const client = await connect(plain.fetch)
    await client.callTool({ name: 'search', arguments: { q: 'a' } })
    await client.callTool({ name: 'search', arguments: { q: 'b' } })
    await new Promise((resolve) => setTimeout(resolve, 10))
    expect(plain.logs.filter((line) => line.includes('This looks like Vercel'))).toHaveLength(1)
    const handed = setup()
    await (await connect(handed.fetch)).callTool({ name: 'search', arguments: { q: 'c' } })
    await new Promise((resolve) => setTimeout(resolve, 10))
    expect(handed.logs.filter((line) => line.includes('This looks like Vercel'))).toHaveLength(0)
  })
})
