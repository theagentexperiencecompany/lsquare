// lsquare/mcp with MCP SDK 2.x, packed and installed with the SDK's pinned version, on this Node version.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { Client } from '@modelcontextprotocol/client'
import { InMemoryTransport, McpServer, Server } from '@modelcontextprotocol/server'
import { LSquare } from 'lsquare'
import { instrument } from 'lsquare/mcp'
import { z } from 'zod'
import { ingestion, runtime, text } from './ingestion.mjs'

async function connect(server) {
  const [a, b] = InMemoryTransport.createLinkedPair()
  await server.connect(a)
  const client = new Client({ name: 'claude-code', version: '2.1' })
  await client.connect(b)
  return client
}

const client = (fake) => new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test', fetch: fake.fetch, logger: { info() {}, warn() {} } })

test('a high-level call is recorded, with the extras', async () => {
  const fake = ingestion()
  const lsquare = client(fake)
  const server = new McpServer({ name: 'orders', version: '1.0.0' })
  server.registerTool('search', { inputSchema: z.object({ q: z.string() }) }, async ({ q }) => {
    lsquare.capture({ question: `Find ${q}` })
    return text(`3 orders for ${q}`)
  })
  instrument(server, lsquare)
  const reply = await (await connect(server)).callTool({ name: 'search', arguments: { q: 'late', intent: 'Find late orders' } })
  assert.equal(reply.content[0].text, '3 orders for late')
  assert.match(reply.content.at(-1).text, /^Task ID: ses_/)
  await lsquare.flush()
  const [event] = fake.events
  assert.equal(event.toolName, 'search')
  assert.deepEqual(event.input, { q: 'late' })
  assert.equal(event.intent, 'Find late orders')
  assert.equal(event.agent.question, 'Find late')
  assert.equal(event.sdk.runtime, runtime)
})

test('a low-level call is recorded', async () => {
  const fake = ingestion()
  const lsquare = client(fake)
  const server = new Server({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
  server.setRequestHandler('tools/call', async (request) => text(`ran ${request.params.name}`))
  instrument(server, lsquare)
  await (await connect(server)).callTool({ name: 'search', arguments: { q: 'x' } })
  await lsquare.flush()
  assert.equal(fake.events[0].toolName, 'search')
  assert.deepEqual(fake.events[0].input, { q: 'x' })
})
