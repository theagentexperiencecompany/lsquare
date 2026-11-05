// The built package on Bun: both MCP SDKs, high- and low-level, the extras, and a stdio server.
import { describe, expect, it } from 'bun:test'
import { spawn } from 'node:child_process'
import { Client as Client1 } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport as Memory1 } from '@modelcontextprotocol/sdk/inMemory.js'
import { McpServer as McpServer1 } from '@modelcontextprotocol/sdk/server/mcp.js'
import { Server as Server1 } from '@modelcontextprotocol/sdk/server/index.js'
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { Client as Client2 } from '@modelcontextprotocol/client'
import { McpServer as McpServer2, InMemoryTransport as Memory2, Server as Server2 } from '@modelcontextprotocol/server'
import { LSquare } from 'lsquare'
import { instrument } from 'lsquare/mcp'
import { z } from 'zod'
import { Ingestion, issued, text } from '../shared/ingestion.js'

function setup() {
  const ingestion = new Ingestion()
  const lsquare = new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test', fetch: ingestion.fetch, logger: { info() {}, warn() {} } })
  return { ingestion, lsquare }
}

const RUNTIME = /^bun\/\d+\.\d+$/
const ON = { sessions: true, intent: true, missingTools: true }

describe('SDK 1.x on Bun', () => {
  it('records a high-level call, with the extras', async () => {
    const { ingestion, lsquare } = setup()
    const server = new McpServer1({ name: 'orders', version: '1.0.0' })
    server.registerTool('search', { inputSchema: { q: z.string() } }, async ({ q }) => text(`3 orders for ${q}`))
    instrument(server, lsquare)
    const [a, b] = Memory1.createLinkedPair()
    await server.connect(a)
    const client = new Client1({ name: 'claude-code', version: '2.1' })
    await client.connect(b)
    const reply = await client.callTool({ name: 'search', arguments: { q: 'late', intent: 'Find late orders' } })
    expect(issued(reply)).toMatch(/^ses_/)
    await lsquare.flush()
    expect(ingestion.events('tool_call')[0]).toMatchObject({ toolName: 'search', input: { q: 'late' }, intent: 'Find late orders', sessionSource: 'issued' })
    expect(ingestion.events('tool_call')[0]!.sdk.runtime).toMatch(RUNTIME)
  })

  it('records a low-level call, with the extras passed as true', async () => {
    const { ingestion, lsquare } = setup()
    const seen: unknown[] = []
    const server = new Server1({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
    server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{ name: 'search', inputSchema: { type: 'object' as const, properties: { q: { type: 'string' } } } }] }))
    server.setRequestHandler(CallToolRequestSchema, async (request) => {
      seen.push(request.params.arguments)
      return text('ok')
    })
    instrument(server, lsquare, { extras: ON })
    const [a, b] = Memory1.createLinkedPair()
    await server.connect(a)
    const client = new Client1({ name: 'claude-code', version: '2.1' })
    await client.connect(b)
    await client.callTool({ name: 'search', arguments: { q: 'a', session_id: 'ses_AbCdEfGhIjKlMnOpQrStUv' } })
    expect(seen).toEqual([{ q: 'a' }])
    await lsquare.flush()
    expect(ingestion.events('tool_call')[0]).toMatchObject({ sessionSource: 'sent' })
  })
})

describe('SDK 2.x on Bun', () => {
  it('records a high-level call, with the extras', async () => {
    const { ingestion, lsquare } = setup()
    const server = new McpServer2({ name: 'orders', version: '1.0.0' })
    server.registerTool('search', { inputSchema: z.object({ q: z.string() }) }, async ({ q }) => text(`3 orders for ${q}`))
    instrument(server, lsquare)
    const [a, b] = Memory2.createLinkedPair()
    await server.connect(a)
    const client = new Client2({ name: 'claude-code', version: '2.1' })
    await client.connect(b)
    const reply = await client.callTool({ name: 'search', arguments: { q: 'late' } })
    expect(issued(reply)).toMatch(/^ses_/)
    await lsquare.flush()
    expect(ingestion.events('tool_call')[0]).toMatchObject({ toolName: 'search', sessionSource: 'issued' })
    expect(ingestion.events('tool_call')[0]!.sdk.runtime).toMatch(RUNTIME)
  })

  it('records a low-level call, with the extras passed as true', async () => {
    const { ingestion, lsquare } = setup()
    const seen: unknown[] = []
    const server = new Server2({ name: 'orders', version: '1.0.0' }, { capabilities: { tools: {} } })
    server.setRequestHandler('tools/list', async () => ({ tools: [{ name: 'search', inputSchema: { type: 'object' as const, properties: { q: { type: 'string' } } } }] }))
    server.setRequestHandler('tools/call', async (request: any) => {
      seen.push(request.params.arguments)
      return text('ok')
    })
    instrument(server, lsquare, { extras: ON })
    const [a, b] = Memory2.createLinkedPair()
    await server.connect(a)
    const client = new Client2({ name: 'claude-code', version: '2.1' })
    await client.connect(b)
    await client.callTool({ name: 'search', arguments: { q: 'a', intent: 'why' } })
    expect(seen).toEqual([{ q: 'a' }])
    await lsquare.flush()
    expect(ingestion.events('tool_call')[0]).toMatchObject({ intent: 'why' })
  })
})

describe('a stdio server on Bun', () => {
  // Longer than its own 10-second wait: Bun's default is 5.
  it('in dry run with the default logger, keeps stdout for MCP messages', async () => {
    const child = spawn(process.execPath, ['run', new URL('./stdio-server.ts', import.meta.url).pathname], { stdio: ['pipe', 'pipe', 'pipe'] })
    let stdout = ''
    let stderr = ''
    child.stdout.on('data', (chunk) => (stdout += chunk))
    child.stderr.on('data', (chunk) => (stderr += chunk))
    const send = (message: unknown) => child.stdin.write(`${JSON.stringify(message)}\n`)
    send({ jsonrpc: '2.0', id: 0, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'c', version: '1' } } })
    send({ jsonrpc: '2.0', method: 'notifications/initialized' })
    send({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'search', arguments: { q: 'late' } } })
    try {
      const deadline = Date.now() + 10_000
      while (!(stdout.includes('"id":1') && stderr.includes('"kind":"tool_call"')) && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 20))
    } finally {
      child.kill()
    }
    const lines = stdout.split('\n').filter((line) => line.trim() !== '')
    expect(lines.length).toBeGreaterThanOrEqual(2)
    for (const line of lines) expect(JSON.parse(line).jsonrpc).toBe('2.0')
    expect(stderr).toContain('[lsquare dry-run]')
    expect(stderr).toContain('"kind":"tool_call"')
  }, 20_000)
})
