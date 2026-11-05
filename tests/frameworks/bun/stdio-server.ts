// A stdio server in dry run with the default logger, which must keep stdout for MCP messages.
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { LSquare } from 'lsquare'
import { instrument } from 'lsquare/mcp'
import { z } from 'zod'

const lsquare = new LSquare({ dryRun: true })
const server = new McpServer({ name: 'orders', version: '1.0.0' })
server.registerTool('search', { inputSchema: { q: z.string() } }, async ({ q }) => ({ content: [{ type: 'text', text: `3 orders for ${q}` }] }))
instrument(server, lsquare)
await server.connect(new StdioServerTransport())
