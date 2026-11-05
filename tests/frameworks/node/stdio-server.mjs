// A stdio server in a process of its own, for stdio.test.ts: the built package on MCP SDK 1.x or
// 2.x (the first argument), with an ingestion whose first request fails with a network error, as a
// stdio server's often does as its client shuts it. What it sends, and its attempts as it exits,
// it says on stderr, one line each: stdout is the MCP connection's.
import { LSquare } from 'lsquare'
import { instrument } from 'lsquare/mcp'

const [sdk, mode] = process.argv.slice(2)
const { McpServer } = sdk === '1' ? await import('@modelcontextprotocol/sdk/server/mcp.js') : await import('@modelcontextprotocol/server')
const { StdioServerTransport } = sdk === '1' ? await import('@modelcontextprotocol/sdk/server/stdio.js') : await import('@modelcontextprotocol/server/stdio')

const say = (line) => process.stderr.write(`${line}\n`)
let attempts = 0
const fetch = async (_url, init) => {
  attempts++
  const batch = JSON.parse(String(init?.body)).events
  say(`attempt ${attempts}: ${batch.map((event) => event.kind).join(',')}`)
  if (attempts === 1) throw new TypeError('fetch failed')
  return new Response(JSON.stringify({ accepted: batch.map((event) => event.eventId), duplicates: [], rejected: [] }))
}
const lsquare = new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test', fetch, onError: (error) => say(`error ${error.code}`), logger: { info() {}, warn() {} } })
const server = instrument(new McpServer({ name: 'orders', version: '1.0.0' }), lsquare, { extras: { sessions: false, intent: false, missingTools: false } })
server.registerTool('search', {}, async () => {
  // 'running': still running when the client shuts the server, which it does once told this.
  if (mode === 'running') {
    say('entered')
    await new Promise((resolve) => setTimeout(resolve, 200))
  }
  return { content: [{ type: 'text', text: '3 orders' }] }
})
process.on('exit', () => say(`exit after ${attempts} attempts`))
await server.connect(new StdioServerTransport())
// 'capture': a connection that makes no tools/call, with a turn to send all the same.
if (mode === 'capture') lsquare.capture({ conversationId: 'c-1', question: 'Find late orders' })
say('connected')
