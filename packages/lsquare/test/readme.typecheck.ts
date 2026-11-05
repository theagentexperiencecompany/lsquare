// The lsquare/mcp examples of README.md and REFERENCE.md, compiled by `npm run typecheck` (not
// run), so their option names and signatures stay true. The framework examples (agents,
// mcp-handler) are left out: their packages aren't this package's dependencies, and
// tests/frameworks runs them for real.
import { AsyncLocalStorage } from 'node:async_hooks'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { z } from 'zod'
import { LSquare } from '../src/index.js'
import { instrument } from '../src/mcp/index.js'

declare const orgId: string
declare const userId: string
declare const conversationId: string
declare const metadata: Record<string, unknown>
declare const question: string
declare const answer: string
declare const toolCalls: Array<{ tool: string; args: object; result: string; isError: boolean }>
declare const inputTokens: number
declare const outputTokens: number
declare const costUsd: number
declare function handle(request: Request, env: unknown, ctx: ExecutionContext): Promise<Response>
interface ExecutionContext {
  waitUntil(promise: Promise<unknown>): void
}

const lsquare = new LSquare({ apiKey: process.env.LSQUARE_KEY })

const server = new McpServer({ name: 'orders', version: '1.0.0' })
server.registerTool('search_orders', { inputSchema: { q: z.string() } }, async () => {
  lsquare.capture({ question, answer, toolCalls, usage: { inputTokens, outputTokens, costUsd } })
  return { content: [{ type: 'text', text: lsquare.currentCallId() ?? '' }] }
})
instrument(server, lsquare)

instrument(server, lsquare, { extras: { sessions: false, intent: false, missingTools: false } })
instrument(server, lsquare, {
  identify: async ({ toolName, args, context, signal }) => {
    void toolName, args, context, signal
    return { orgId, userId, conversationId, metadata }
  },
})
instrument(server, lsquare, { inputs: false, results: (tool) => tool !== 'get_customer', flushBeforeReply: true })

const low = new Server({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
instrument(low, lsquare, { extras: { sessions: true, intent: true, missingTools: true } })

const requests = new AsyncLocalStorage<ExecutionContext>()
instrument(server, lsquare, { waitUntil: (promise) => requests.getStore()?.waitUntil(promise) })
export default {
  fetch: (request: Request, env: unknown, ctx: ExecutionContext) => requests.run(ctx, () => handle(request, env, ctx)),
}

process.on('SIGTERM', async () => {
  await lsquare.shutdown()
  process.exit(0)
})
