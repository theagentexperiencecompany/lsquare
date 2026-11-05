// A stdio server's client shuts it by closing its stdin, then waits for it to exit (ts-mcp-11). The
// built package, in a process of its own on each MCP SDK: what's queued must be flushed, and the
// process kept alive for it, though an event's retry waits on a timer that doesn't keep a process
// alive. SDK 1.x's transport never hears its stdin end, so its server never closes.
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const SERVER = fileURLToPath(new URL('./stdio-server.mjs', import.meta.url))

/**
 * Starts the server, opens its connection as a stdio client does, then, for `mode`: makes a call
 * ('call'), makes none ('capture': the server sends a turn of its own), or starts one that's still
 * running ('running'). Then it closes the server's stdin, and gives what the server said once it
 * has exited, within 10 seconds.
 */
async function shut(sdk: '1' | '2', mode: 'call' | 'capture' | 'running'): Promise<string[]> {
  const child = spawn(process.execPath, [SERVER, sdk, mode], { stdio: ['pipe', 'pipe', 'pipe'] })
  const said: string[] = []
  const replies = new Set<number>()
  const lines = (stream: NodeJS.ReadableStream, each: (line: string) => void) => {
    let buffered = ''
    stream.on('data', (chunk) => {
      buffered += String(chunk)
      let end: number
      while ((end = buffered.indexOf('\n')) >= 0) {
        const line = buffered.slice(0, end)
        buffered = buffered.slice(end + 1)
        if (line.trim()) each(line)
      }
    })
  }
  lines(child.stderr, (line) => said.push(line))
  lines(child.stdout, (line) => {
    const id = JSON.parse(line).id
    if (typeof id === 'number') replies.add(id)
  })
  const exited = new Promise<number | null>((resolve) => child.on('exit', resolve))
  const until = async (check: () => boolean) => {
    for (let i = 0; i < 1_000 && !check(); i++) await new Promise((resolve) => setTimeout(resolve, 10))
    expect(check()).toBe(true)
  }
  const send = (message: unknown) => child.stdin.write(`${JSON.stringify(message)}\n`)
  try {
    await until(() => said.includes('connected'))
    send({ jsonrpc: '2.0', id: 0, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'claude-code', version: '2.1' } } })
    await until(() => replies.has(0))
    send({ jsonrpc: '2.0', method: 'notifications/initialized' })
    if (mode !== 'capture') send({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'search', arguments: {} } })
    if (mode === 'call') await until(() => replies.has(1))
    if (mode === 'running') await until(() => said.includes('entered'))
    child.stdin.end()
    const bounded = new Promise<'timed out'>((resolve) => setTimeout(() => resolve('timed out'), 10_000).unref())
    expect(await Promise.race([exited, bounded])).toBe(0)
  } finally {
    child.kill()
  }
  return said
}

describe.each(['1', '2'] as const)('a stdio server on SDK %s.x whose client closes its stdin', (sdk) => {
  it.each([
    ['after a call', 'call', 'tool_call'],
    ['on a connection that made no tools/call', 'capture', 'turn'],
    ['while a call still runs', 'running', 'tool_call'],
  ] as const)('stays alive %s until its event has been sent, by the retry its first, failed request needed', async (_name, mode, kind) => {
    const said = await shut(sdk, mode)
    expect(said.filter((line) => line.startsWith('attempt') || line.startsWith('exit') || line.startsWith('error'))).toEqual([
      `attempt 1: ${kind}`,
      `attempt 2: ${kind}`,
      'exit after 2 attempts',
    ])
  }, 15_000)
})
