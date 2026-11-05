// capture() and beforeSend with values whose shape depends on the app, compiled by
// `npm run typecheck` (not run). The shapes are deliberately odd: this must compile anyway.
import { LSquare } from '../src/index.js'

declare const conversationId: string
declare const turnId: number
declare const orgId: string
declare const question: string
declare const answer: string | undefined
declare const toolCalls: Array<{ tool: string; args: object; result: string; isError: boolean }>
declare const inputTokens: number
declare const outputTokens: number
declare const costUsd: number
declare const latency: { totalMs: number; steps: { name: string; ms: number }[] }
declare const verification: Map<string, unknown>
declare const client: { name: string; version: string }
declare const apiKeyId: string | undefined
declare const err: unknown
declare function scrub(text: string, options: { signal: AbortSignal }): Promise<string>

const lsquare = new LSquare({ apiKey: process.env.LSQUARE_KEY })

export async function afterTurn(): Promise<void> {
  lsquare.capture({
    conversationId, turnId, orgId, toolName: 'ask_docs',
    question, answer, toolCalls,
    usage: { inputTokens, outputTokens, costUsd },
    latency, verification, client, apiKeyId,
    surface: 'mcp',
  })
  await lsquare.flush()
}

export async function onError(): Promise<void> {
  lsquare.capture({
    conversationId, turnId, orgId, toolName: 'ask_docs', question,
    status: 'error', error: err, client, apiKeyId, surface: 'mcp',
  })
  await lsquare.flush()
}

export const withScrubber = new LSquare({
  apiKey: process.env.LSQUARE_KEY,
  beforeSend: async (event, { signal }) => ({ ...event, question: await scrub(event.question, { signal }) }),
})
