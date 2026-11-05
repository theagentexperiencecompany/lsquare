// A stateless Worker with module-level clients shared by every request, built without a fetch
// option, so they send through Workers' own fetch to ./ingest.js. Each request runs in its tenant,
// in an app AsyncLocalStorage, and what the app's callbacks see of it is recorded.
import { AsyncLocalStorage } from 'node:async_hooks'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { LSquare, type LSquareError, type LSquareEvent, type LSquareOptions } from 'lsquare'
import { instrument } from 'lsquare/mcp'

const tenant = new AsyncLocalStorage<string>()
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

/** What the app's own callbacks saw: the tenant they ran in, and the conversation they were about. */
export const seen: Array<{ by: string; tenant: string | undefined; about: string }> = []
/** What a custom logger was given, with the tenant it ran in. */
export const lines: Array<{ tenant: string | undefined; line: string }> = []
/**
 * Calls the test holds unbuilt: each finishes once the test takes its ID out. The call's own
 * request checks, on its own timers, so it finishes in that request.
 */
export const holding = new Set<string>()
const released = async (id: string) => {
  while (holding.has(id)) await sleep(5)
}

/** The client's internal API, as the wrapper reaches it: here, how it tracks a call it is still building. */
const internalOf = (client: LSquare) => (client as unknown as Record<symbol, { track(callId: string, finishing: Promise<void>): void }>)[Symbol.for('lsquare.client.internal/1')]!

const stuckHook = (event: LSquareEvent) => (event.conversationId === 'stuck' ? new Promise<null>(() => {}) : sleep(10).then(() => event))

/**
 * An async onError that records where it started, then awaits a timer of its request: once that
 * request has ended, it never settles.
 */
const slowOnError = async (error: LSquareError) => {
  seen.push({ by: `onError ${error.code}`, tenant: tenant.getStore(), about: `suppressed ${error.suppressed ?? 0}` })
  await sleep(60_000)
}

const make = (options: LSquareOptions = {}) =>
  new LSquare({
    apiKey: 'lsq_test',
    endpoint: 'https://ingest.test',
    logger: { info() {}, warn() {} },
    onError: (error, event) => void seen.push({ by: `onError ${error.code}`, tenant: tenant.getStore(), about: String(event?.conversationId) }),
    ...options,
  })

const clients: Record<string, LSquare> = {
  plain: make(),
  /** One request slot: more requests at once than slots. */
  narrow: make({ maxConcurrentRequests: 1 }),
  /** One hook slot, and a hook that takes 100 ms. */
  hooked: make({ maxConcurrentHooks: 1, beforeSend: async (event) => (await sleep(100), event) }),
  /** A custom logger and no onError: reports go to the logger. */
  logged: make({
    onError: undefined,
    logger: {
      info: (line) => void lines.push({ tenant: tenant.getStore(), line }),
      warn: (line) => void lines.push({ tenant: tenant.getStore(), line }),
    },
  }),
  /** One hook slot, a short hook deadline, and a hook for "stuck" that never settles. */
  stuck: make({ maxConcurrentHooks: 1, beforeSendTimeoutMs: 300, beforeSend: stuckHook }),
  /** The same, with room for one event: a stuck hook's event fills it. */
  pinned: make({ maxConcurrentHooks: 1, maxQueueEvents: 1, beforeSendTimeoutMs: 300, beforeSend: stuckHook }),
  /** Room for no event at all: each is refused as it's queued. */
  full: make({ maxQueueBytes: 64 }),
  /** Two with a slow async onError: one test lets one's requests end, and keeps the other's alive. */
  gone: make({ onError: slowOnError }),
  alive: make({ onError: slowOnError }),
  /** A short request timeout, so a lost request is past its deadline soon. */
  short: make({ maxConcurrentRequests: 1, requestTimeoutMs: 300 }),
  /** Its own, for a call still unbuilt in one request while another flushes. */
  tracked: make(),
  /**
   * Every callback of the app records the tenant it runs in. Two hook slots: one is kept by the hook
   * that ignores its deadline, and the three events share the other, so one starts after another's.
   */
  tenanted: make({
    maxConcurrentRequests: 1,
    maxConcurrentHooks: 2,
    beforeSendTimeoutMs: 400,
    beforeSend: async (event, { signal }) => {
      seen.push({ by: 'beforeSend', tenant: tenant.getStore(), about: String(event.conversationId) })
      if (event.conversationId === 'slow') {
        // Ignores its deadline, but listens for the abort: which tenant does the listener run in?
        signal.addEventListener('abort', () => seen.push({ by: 'abort', tenant: tenant.getStore(), about: 'slow' }))
        await sleep(800)
      } else {
        await sleep(50)
      }
      return event
    },
    fetch: (url, init) => {
      const events = JSON.parse(String(init?.body)).events as Array<{ conversationId: string }>
      seen.push({ by: 'fetch', tenant: tenant.getStore(), about: events[0]!.conversationId })
      return fetch(url, init)
    },
  }),
}

export async function core(request: Request, ctx: ExecutionContext): Promise<Response> {
  const url = new URL(request.url)
  const client = clients[url.searchParams.get('client') ?? 'plain']!
  const id = url.searchParams.get('id') ?? ''
  return tenant.run(`tenant-${id}`, async () => {
    if (url.pathname === '/core/turn') {
      client.capture({ conversationId: id, turnId: 1, question: `q ${id}` })
      // Without a wait, the request ends with its reply: its event is left to the others.
      if (url.searchParams.has('wait')) ctx.waitUntil(client.flush({ timeoutMs: Number(url.searchParams.get('wait')) }))
      // Holding its reply, the request stays alive that long.
      await sleep(Number(url.searchParams.get('hold') ?? 0))
      return new Response(`ok ${id}`)
    }
    if (url.pathname === '/core/call') {
      // A server built per request and instrumented with its waitUntil, called once in it: the
      // reply goes out at once, and the request lives as long as what was handed over.
      const server = new McpServer({ name: 'orders', version: '1.0.0' })
      server.registerTool('search', {}, async () => ({ content: [{ type: 'text' as const, text: '3 orders' }] }))
      instrument(server, client, { waitUntil: ctx.waitUntil.bind(ctx), extras: { sessions: false, intent: false, missingTools: false } })
      const [near, far] = InMemoryTransport.createLinkedPair()
      await server.connect(near)
      const mcp = new Client({ name: 'claude-code', version: '2.1' })
      await mcp.connect(far)
      await mcp.callTool({ name: 'search', arguments: {} })
      return new Response(`ok ${id}`)
    }
    if (url.pathname === '/core/unbuilt') {
      // A call that has replied and whose event is still being built, tracked as the wrapper
      // tracks one, in this request, until the test lets go of it (see holding): none of the
      // wrapper's deadlines (identify's second) ends it sooner. The request lives as long.
      const finishing = released(id)
      internalOf(client).track(`call-${id}`, finishing)
      ctx.waitUntil(finishing)
      return new Response(`ok ${id}`)
    }
    if (url.pathname === '/core/report') {
      // Each refused as it's captured, so reported at once: an orgId that isn't a string.
      for (let i = 0; i < Number(url.searchParams.get('count') ?? 1); i++) {
        client.capture({ conversationId: id, orgId: 7 as unknown as string, question: `q ${id}` })
      }
      // Their handlers have started once it resolves.
      await client.flush({ timeoutMs: 0 })
      await sleep(Number(url.searchParams.get('hold') ?? 0))
      return new Response(`ok ${id}`)
    }
    if (url.pathname === '/core/flush') {
      const started = Date.now()
      const result = await client.flush({ timeoutMs: Number(url.searchParams.get('wait') ?? 2000) })
      const ms = Date.now() - started
      // Alive a little longer, so what the flush reported gets its turn here.
      await sleep(Number(url.searchParams.get('linger') ?? 0))
      return Response.json({ ...result, ms })
    }
    return new Response('Not Found', { status: 404 })
  })
}
