// And SDK 2.x's McpServer.
import { McpServer } from '@modelcontextprotocol/server'
import { LSquare } from 'lsquare'
import { instrument } from 'lsquare/mcp'
const server: McpServer = instrument(new McpServer({ name: 'x', version: '1' }), new LSquare())
export { server }
