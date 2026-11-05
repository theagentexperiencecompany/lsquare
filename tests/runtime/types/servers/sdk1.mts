// instrument() returns the server it's given, as its own type: SDK 1.x's two kinds, from ESM.
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { LSquare } from 'lsquare'
import { instrument } from 'lsquare/mcp'
const lsquare = new LSquare()
const high: McpServer = instrument(new McpServer({ name: 'x', version: '1' }), lsquare)
const low: Server = instrument(new Server({ name: 'x', version: '1' }, { capabilities: { tools: {} } }), lsquare)
export { high, low }
