// Cloudflare's createMcpHandler from agents/mcp/server: a stateless SDK 2.x server built per
// request by a factory that instruments it with the request's waitUntil, as section 1 shows.
import { createExecutionContext, env, waitOnExecutionContext } from 'cloudflare:test'
import { Client, StreamableHTTPClientTransport } from '@modelcontextprotocol/client'
import { Client as Client1 } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport as ClientHttp1 } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { McpServer } from '@modelcontextprotocol/server'
import { createMcpHandler } from 'agents/mcp/server'
import { LSquare, type LSquareOptions } from 'lsquare'
import { instrument, type InstrumentOptions } from 'lsquare/mcp'
import { describe, expect, it, vi } from 'vitest'
import { z } from 'zod'
import { Ingestion, issued, text, until } from '../shared/ingestion.js'

interface Served {
  ctx: ExecutionContext
  /** What the request's waitUntil was handed. */
  registered: Promise<unknown>[]
  method: string | undefined
}

function setup(
  options: (ctx: ExecutionContext, registered: Promise<unknown>[]) => InstrumentOptions = (ctx, registered) => ({
    waitUntil: (promise) => {
      registered.push(promise)
      ctx.waitUntil(promise)
    },
  }),
  client: Pick<LSquareOptions, 'flushTimeoutMs' | 'onError'> = {},
) {
  const ingestion = new Ingestion()
  const logs: string[] = []
  const lsquare = new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test', fetch: ingestion.fetch, logger: { info: (line) => logs.push(line), warn: (line) => logs.push(line) }, ...client })
  const served: Served[] = []
  const fetch = async (url: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const ctx = createExecutionContext()
    const registered: Promise<unknown>[] = []
    const request = new Request(url, init)
    const body = request.method === 'POST' ? await request.clone().json().catch(() => undefined) : undefined
    const handler = createMcpHandler(() => {
      const server = new McpServer({ name: 'orders', version: '1.0.0' })
      server.registerTool('search', { inputSchema: z.object({ q: z.string() }) }, async ({ q }) => {
        lsquare.capture({ question: `Find ${q}` })
        return text(`3 orders for ${q}`)
      })
      server.registerTool('lookup', { inputSchema: z.object({ tenant: z.string().meta({ 'x-mcp-header': 'Tenant' }) }) }, async ({ tenant }) => text(`for ${tenant}`))
      // A common pattern: capture(), then await flush(), inside the tool.
      server.registerTool('noted', { inputSchema: z.object({ q: z.string() }) }, async ({ q }) => {
        lsquare.capture({ question: `Find ${q}` })
        await lsquare.flush()
        return text(`noted ${q}`)
      })
      // A second capture() inside the call is a turn of its own, sent first.
      server.registerTool('turns', { inputSchema: z.object({ q: z.string() }) }, async ({ q }) => {
        lsquare.capture({ question: `Find ${q}` })
        lsquare.capture({ conversationId: 'c1', turnId: 2, question: `Then ${q}` })
        await until(() => ingestion.batches.length === 1)
        return text(`turns ${q}`)
      })
      return instrument(server, lsquare, options(ctx, registered))
    })
    const response = await handler(request, env, ctx)
    served.push({ ctx, registered, method: (body as { method?: string } | undefined)?.method })
    return response
  }
  return { ingestion, logs, lsquare, served, fetch }
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

/** A tools/call of the 2026-07-28 protocol, as a client sends it, with the headers given. */
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

describe("agents' createMcpHandler", () => {
  // Each request's server closes once it has replied: only a stdio server's close flushes.
  it("never flushes when a request's server closes, so the slow ingestion meanwhile gets no pending_after_flush reports", async () => {
    const codes: string[] = []
    const { ingestion, lsquare, served, fetch } = setup(undefined, { flushTimeoutMs: 50, onError: (error) => void codes.push(error.code) })
    const flush = vi.spyOn(lsquare, 'flush')
    const client = await connect(fetch)
    ingestion.hold()
    for (const q of ['a', 'b', 'c']) await client.callTool({ name: 'search', arguments: { q } })
    expect(flush).not.toHaveBeenCalled()
    ingestion.release()
    for (const request of served) await waitOnExecutionContext(request.ctx)
    expect(flush).not.toHaveBeenCalled()
    expect(codes).toEqual([])
    expect(ingestion.events('tool_call')).toHaveLength(3)
  })

  it("hands each call's sending to its request's waitUntil: the reply comes first, the event once the ingestion answers", async () => {
    const { ingestion, served, fetch } = setup()
    const client = await connect(fetch)
    ingestion.hold()
    const reply = await client.callTool({ name: 'search', arguments: { q: 'late' } })
    expect(reply.content?.[0]).toEqual({ type: 'text', text: '3 orders for late' })
    const call = served.find((request) => request.method === 'tools/call')!
    expect(call.registered).toHaveLength(1)
    let settled = false
    void call.registered[0]!.then(() => (settled = true))
    await until(() => ingestion.batches.length > 0)
    expect(ingestion.batches[0]!.answered).toBe(false)
    expect(settled).toBe(false)
    ingestion.release()
    await waitOnExecutionContext(call.ctx)
    expect(settled).toBe(true)
    expect(ingestion.batches[0]!.answered).toBe(true)
    const [event] = ingestion.events('tool_call')
    expect(event).toMatchObject({ toolName: 'search', sdk: { runtime: 'workers' }, input: { q: 'late' }, agent: { question: 'Find late' }, sessionSource: 'issued', sessionId: issued(reply) })
    // Only the call hands anything over: listing and initializing send nothing.
    expect(served.filter((request) => request.registered.length > 0)).toEqual([call])
  })

  it("still hands the call's sending to waitUntil when the tool called flush(): the reply waits at most flushTimeoutMs, the event once the ingestion answers", async () => {
    const { ingestion, served, fetch } = setup(undefined, { flushTimeoutMs: 50 })
    const client = await connect(fetch)
    ingestion.hold()
    const reply = await client.callTool({ name: 'noted', arguments: { q: 'late' } })
    expect(reply.content?.[0]).toEqual({ type: 'text', text: 'noted late' })
    const call = served.find((request) => request.method === 'tools/call')!
    await until(() => ingestion.batches.length > 0)
    expect(ingestion.batches[0]!.answered).toBe(false)
    expect(call.registered).toHaveLength(1)
    let settled = false
    void call.registered[0]!.then(() => (settled = true))
    ingestion.release()
    await waitOnExecutionContext(call.ctx)
    expect(settled).toBe(true)
    expect(ingestion.events('tool_call')).toMatchObject([{ toolName: 'noted', agent: { question: 'Find late' } }])
  })

  it("gives identify its second with flushTimeoutMs 0, though the tool called flush(): the reply doesn't wait for it, and the event is whole", async () => {
    const { ingestion, served, fetch } = setup(
      (ctx, registered) => ({
        waitUntil: (promise) => {
          registered.push(promise)
          ctx.waitUntil(promise)
        },
        // An ID lookup that takes a moment: longer than the tool, well within its second.
        identify: async () => {
          await new Promise((resolve) => setTimeout(resolve, 20))
          return { orgId: 'org_1', userId: 'u-1' }
        },
      }),
      { flushTimeoutMs: 0 },
    )
    const client = await connect(fetch)
    expect((await client.callTool({ name: 'noted', arguments: { q: 'late' } })).content?.[0]).toEqual({ type: 'text', text: 'noted late' })
    const call = served.find((request) => request.method === 'tools/call')!
    expect(call.registered).toHaveLength(1)
    await waitOnExecutionContext(call.ctx)
    expect(ingestion.events('tool_call')).toMatchObject([{ toolName: 'noted', orgId: 'org_1', userId: 'u-1', input: { q: 'late' }, agent: { question: 'Find late' } }])
  })

  it('keeps the request alive for the turns captured inside the call, as for its event', async () => {
    const { ingestion, lsquare, served, fetch } = setup()
    const client = await connect(fetch)
    ingestion.hold()
    await client.callTool({ name: 'turns', arguments: { q: 'late' } })
    const call = served.find((request) => request.method === 'tools/call')!
    expect(call.registered).toHaveLength(1)
    await until(() => ingestion.batches.length === 2)
    const [turn, made] = ingestion.batches
    expect([turn!.events.map((event) => event.kind), made!.events.map((event) => event.kind)]).toEqual([['turn'], ['tool_call']])
    let settled = false
    void call.registered[0]!.then(() => (settled = true))
    ingestion.releaseWhere((batch) => batch === made)
    // The call's event is sent: only the turn is left. Its wait would have ended by the next watch.
    const left = async () => (await lsquare.flush({ timeoutMs: 0 })).pending
    for (let tries = 0; tries < 200 && (await left()) !== 1; tries++) await new Promise((resolve) => setTimeout(resolve, 10))
    expect(await left()).toBe(1)
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(settled).toBe(false)
    ingestion.release()
    await waitOnExecutionContext(call.ctx)
    expect(settled).toBe(true)
    expect(turn!.answered).toBe(true)
  })

  it('adds the extras on each per-request server', async () => {
    const { ingestion, fetch } = setup()
    const client = await connect(fetch)
    const listed = await client.listTools()
    expect(Object.keys(listed.tools.find((tool) => tool.name === 'search')!.inputSchema.properties ?? {})).toEqual(['q', 'session_id', 'intent'])
    expect(listed.tools.map((tool) => tool.name)).toContain('get_more_tools')
    const reply = await client.callTool({ name: 'search', arguments: { q: 'a', intent: 'Find late orders' } })
    const id = issued(reply)
    expect(id).toMatch(/^ses_/)
    await client.callTool({ name: 'search', arguments: { q: 'b', session_id: id } })
    await until(() => ingestion.events('tool_call').length === 2)
    const [one, two] = ingestion.events('tool_call')
    expect(one).toMatchObject({ intent: 'Find late orders', sessionSource: 'issued' })
    expect(two).toMatchObject({ sessionId: id, sessionSource: 'sent' })
  })

  it("names a client on 2026-07-28; one on 2025-11-25 only gets its protocol version, from its request's header", async () => {
    const { ingestion, lsquare, served, fetch } = setup()
    await (await connect(fetch, { versionNegotiation: { mode: { pin: '2026-07-28' } } })).callTool({ name: 'search', arguments: { q: 'new' } })
    await (await connectOlder(fetch)).callTool({ name: 'search', arguments: { q: 'old' } })
    for (const request of served) await waitOnExecutionContext(request.ctx)
    await lsquare.flush()
    const [modern, older] = ingestion.events('tool_call')
    expect(modern).toMatchObject({ input: { q: 'new' }, client: { name: 'claude-code', version: '2.1' }, protocolVersion: '2026-07-28' })
    expect(older).toMatchObject({ input: { q: 'old' }, protocolVersion: '2025-11-25', sessionSource: 'issued' })
    expect(older).not.toHaveProperty('client')
  })

  it("makes no event for a call the SDK's HTTP layer refuses before dispatch", async () => {
    const { ingestion, lsquare, served, fetch } = setup()
    const refused = await fetch(modernCall('lookup', { tenant: 'acme' }))
    expect(refused.status).toBe(400)
    const accepted = await fetch(modernCall('lookup', { tenant: 'acme' }, { 'mcp-param-tenant': 'acme' }))
    expect(accepted.status).toBe(200)
    for (const request of served) await waitOnExecutionContext(request.ctx)
    await lsquare.flush()
    expect(ingestion.events('tool_call').map((event) => event.toolName)).toEqual(['lookup'])
  })

  it('warns once when given neither waitUntil nor flushBeforeReply, and not with waitUntil', async () => {
    const plain = setup(() => ({}))
    const client = await connect(plain.fetch)
    await client.callTool({ name: 'search', arguments: { q: 'a' } })
    await client.callTool({ name: 'search', arguments: { q: 'b' } })
    await new Promise((resolve) => setTimeout(resolve, 10))
    expect(plain.logs.filter((line) => line.includes('This looks like Cloudflare Workers'))).toHaveLength(1)
    const handed = setup()
    await (await connect(handed.fetch)).callTool({ name: 'search', arguments: { q: 'c' } })
    await new Promise((resolve) => setTimeout(resolve, 10))
    expect(handed.logs.filter((line) => line.includes('This looks like Cloudflare Workers'))).toHaveLength(0)
  })
})

describe('the core on Workers', () => {
  it('sends a capture() outside any call as a turn', async () => {
    const ingestion = new Ingestion()
    const lsquare = new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test', fetch: ingestion.fetch, logger: { info() {}, warn() {} } })
    lsquare.capture({ conversationId: 'c1', turnId: 1, question: 'Where is my order?', answer: 'On its way.' })
    await lsquare.flush()
    expect(ingestion.events('turn')).toMatchObject([{ kind: 'turn', conversationId: 'c1', question: 'Where is my order?', sdk: { name: 'lsquare', runtime: 'workers' } }])
  })
})
