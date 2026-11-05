// The Worker the Workers tests run: SDK 1.x servers in Cloudflare's McpAgent, a Durable Object per
// session, instrumented in init() as the README shows; and, under /core/, the core alone (core.ts).
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { McpAgent } from 'agents/mcp'
import { LSquare } from 'lsquare'
import { instrument } from 'lsquare/mcp'
import { z } from 'zod'
import { Ingestion, text } from '../shared/ingestion.js'
import { core } from './core.js'

export const ingestion = new Ingestion()
export const logs: string[] = []

const client = (name: string) =>
  new LSquare({
    apiKey: 'lsq_test',
    endpoint: 'https://ingest.test',
    fetch: ingestion.fetch,
    logger: { info: (line) => logs.push(`${name}: ${line}`), warn: (line) => logs.push(`${name}: ${line}`) },
  })

/** One client per agent class: each says once what it warns of. */
export const clients = { orders: client('orders'), quiet: client('quiet') }

function register(server: McpServer, lsquare: LSquare): void {
  server.registerTool('search', { inputSchema: { q: z.string() } }, async ({ q }) => {
    lsquare.capture({ question: `Find ${q}` })
    return text(`3 orders for ${q}`)
  })
}

/** Passes neither waitUntil nor flushBeforeReply: a Durable Object needs neither. */
export class OrdersAgent extends McpAgent {
  server = new McpServer({ name: 'orders', version: '1.0.0' })
  /** This object's own state: each session's object counts its own calls. */
  counted = 0
  async init() {
    register(this.server, clients.orders)
    this.server.registerTool('count', {}, async () => text(String(++this.counted)))
    instrument(this.server, clients.orders)
  }
}

/** Passes the object's own waitUntil, which silences the serverless warning. */
export class QuietAgent extends McpAgent {
  server = new McpServer({ name: 'quiet', version: '1.0.0' })
  async init() {
    register(this.server, clients.quiet)
    instrument(this.server, clients.quiet, { waitUntil: this.ctx.waitUntil.bind(this.ctx) })
  }
}

const orders = OrdersAgent.serve('/orders/mcp', { binding: 'ORDERS' })
const quiet = QuietAgent.serve('/quiet/mcp', { binding: 'QUIET' })

export default {
  fetch(request: Request, env: unknown, ctx: ExecutionContext): Promise<Response> | Response {
    const path = new URL(request.url).pathname
    if (path === '/orders/mcp') return orders.fetch(request, env, ctx)
    if (path === '/quiet/mcp') return quiet.fetch(request, env, ctx)
    if (path.startsWith('/core/')) return core(request, ctx)
    return new Response('Not Found', { status: 404 })
  },
}
