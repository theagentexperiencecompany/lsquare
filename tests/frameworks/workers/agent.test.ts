// Cloudflare's McpAgent: an SDK 1.x server in a Durable Object per session, instrumented in init().
import { SELF } from 'cloudflare:test'
import { createHash } from 'node:crypto'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { afterEach, describe, expect, it } from 'vitest'
import { text, until } from '../shared/ingestion.js'
import { ingestion, logs } from './worker.js'

const hashed = (id: string | undefined): string => createHash('sha256').update(id ?? '').digest('hex')

const WARNING = /This looks like Cloudflare Workers \(unless the server runs in a Durable Object\)/

async function session(path: string) {
  const client = new Client({ name: 'claude-code', version: '2.1' })
  const transport = new StreamableHTTPClientTransport(new URL(`https://example.com${path}`), { fetch: (url, init) => SELF.fetch(new Request(url, init)) })
  await client.connect(transport)
  return client
}

/** The session McpAgent issued the client: its transport keeps it. */
const issuedTo = (client: Client) => client.transport?.sessionId

afterEach(() => {
  ingestion.release()
  ingestion.clear()
})

describe('McpAgent', () => {
  it('records each call in its Durable Object, grouped by the session McpAgent issued, results unchanged', async () => {
    const first = await session('/orders/mcp')
    const second = await session('/orders/mcp')
    expect(await first.callTool({ name: 'search', arguments: { q: 'late' } })).toEqual(text('3 orders for late'))
    expect(await first.callTool({ name: 'search', arguments: { q: 'early' } })).toEqual(text('3 orders for early'))
    expect(await second.callTool({ name: 'search', arguments: { q: 'lost' } })).toEqual(text('3 orders for lost'))
    await until(() => ingestion.events('tool_call').length === 3)
    const [late, early, lost] = ingestion.events('tool_call')
    expect(late).toMatchObject({
      toolName: 'search',
      sdk: { name: 'lsquare', runtime: 'workers' },
      server: { name: 'orders' },
      client: { name: 'claude-code' },
      sessionSource: 'protocol',
      input: { q: 'late' },
      agent: { question: 'Find late' },
    })
    // Grouped by the session McpAgent issued each client, which is its object's.
    expect(issuedTo(first)).toBeTruthy()
    expect(late!.sessionId).toBe(hashed(issuedTo(first)))
    expect(early!.sessionId).toBe(hashed(issuedTo(first)))
    expect(lost!.sessionId).toBe(hashed(issuedTo(second)))
    expect(issuedTo(second)).not.toBe(issuedTo(first))
  })

  it("keeps each session's state in an object of its own", async () => {
    const first = await session('/orders/mcp')
    const second = await session('/orders/mcp')
    expect(await first.callTool({ name: 'count' })).toEqual(text('1'))
    expect(await first.callTool({ name: 'count' })).toEqual(text('2'))
    expect(await second.callTool({ name: 'count' })).toEqual(text('1'))
    await until(() => ingestion.events('tool_call').length === 3)
    expect(ingestion.events('tool_call').map((event) => event.sessionId)).toEqual([hashed(issuedTo(first)), hashed(issuedTo(first)), hashed(issuedTo(second))])
  })

  it('sends without waitUntil: the object stays active while the ingestion answers', async () => {
    const client = await session('/orders/mcp')
    ingestion.hold()
    expect(await client.callTool({ name: 'search', arguments: { q: 'held' } })).toEqual(text('3 orders for held'))
    await until(() => ingestion.batches.length > 0)
    expect(ingestion.batches[0]!.answered).toBe(false)
    ingestion.release()
    await until(() => ingestion.batches[0]!.answered)
  })

  it("warns once when given neither option, and not when given the object's own waitUntil", async () => {
    await (await session('/orders/mcp')).callTool({ name: 'search', arguments: { q: 'a' } })
    await (await session('/quiet/mcp')).callTool({ name: 'search', arguments: { q: 'b' } })
    await until(() => ingestion.events('tool_call').length === 2)
    expect(logs.filter((line) => line.startsWith('orders:') && WARNING.test(line))).toHaveLength(1)
    expect(logs.filter((line) => line.startsWith('quiet:') && WARNING.test(line))).toHaveLength(0)
  })
})
