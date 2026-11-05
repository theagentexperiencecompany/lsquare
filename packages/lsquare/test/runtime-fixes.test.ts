// TypeScript's side of the runtime and parity fixes of the deep review's last Python findings, 31
// October 2026 (packages/python/tests/runtime_fixes_test.py). shutdown()'s count is a fix in both
// SDKs; the rest pin what TypeScript already does, and Python now does too.
import { createServer, type Server, type Socket } from 'node:net'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { INTERNAL } from '../src/client.js'
import { LSquare, type LSquareError, type TurnEvent } from '../src/index.js'

const input = { conversationId: 'conv1', orgId: 'org_1', toolName: 'ask_docs', question: 'q' }
/** onError and loggers run in a later event-loop turn (setImmediate): let them run. */
const tick = () => new Promise<void>((resolve) => setImmediate(() => setImmediate(resolve)))
const ids = (init?: RequestInit) => (JSON.parse(String(init?.body)).events as TurnEvent[]).map((event) => event.eventId)
const ack = (init?: RequestInit) => new Response(JSON.stringify({ accepted: ids(init) }), { status: 200 })

/** A client whose fetch answers with `answer`, recording the URLs it was given and the reports. */
function setup(answer: (init: RequestInit | undefined, n: number) => Response | Promise<Response>, endpoint = 'https://ingest.test/') {
  const urls: string[] = []
  const errors: LSquareError[] = []
  const warnings: string[] = []
  const fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    urls.push(String(url))
    return answer(init, urls.length)
  }) as typeof globalThis.fetch
  const client = new LSquare({ apiKey: 'lsq_test', endpoint, fetch, onError: (error) => errors.push(error), logger: { info() {}, warn: (line) => warnings.push(line) } })
  return { client, urls, errors, warnings }
}

afterEach(() => {
  vi.useRealTimers()
})

describe('py-runtime-16: shutdown() counts what was outstanding when it was called', () => {
  /** Each request is answered when the test says so, in the order they came. */
  function held() {
    const answers: Array<() => void> = []
    const bodies: TurnEvent[][] = []
    let entered!: () => void
    const first = new Promise<void>((resolve) => (entered = resolve))
    const fetch = (async (_url: unknown, init?: RequestInit) => {
      const events = JSON.parse(String(init?.body)).events as TurnEvent[]
      bodies.push(events)
      entered()
      await new Promise<void>((resolve) => answers.push(resolve))
      return ack(init)
    }) as typeof globalThis.fetch
    return { fetch, answers, bodies, first }
  }

  it('counts an event outstanding when it was called, though it was sent while it waited for a call', async () => {
    const { fetch, answers, bodies, first } = held()
    const client = new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test/', fetch, onError() {} })
    let built!: () => void
    client[INTERNAL].track('call', new Promise<void>((resolve) => (built = resolve)))
    client.capture(input)
    await first
    // Up to its wait for the call, synchronously: what is outstanding is read before it.
    const shutdown = client.shutdown({ timeoutMs: 5_000 })
    // Acknowledged while it waits for the call, which then ends with no event recorded.
    answers[0]!()
    await client[INTERNAL].settled(bodies[0]![0]!.eventId, 5_000)
    built()
    expect(await shutdown).toEqual({ sent: 1, skipped: 0, failed: 1, pending: 0 })
  })

  it('still waits for a capture made while it waited for a call, and counts it (a guard)', async () => {
    const { fetch, answers, bodies, first } = held()
    const errors: LSquareError[] = []
    const client = new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test/', fetch, onError: (error) => errors.push(error) })
    let built!: () => void
    client[INTERNAL].track('call', new Promise<void>((resolve) => (built = resolve)))
    const shutdown = client.shutdown({ timeoutMs: 5_000 })
    client.capture({ ...input, conversationId: 'meanwhile' })
    await first
    built()
    // Closed, and waiting for it: answered now.
    await tick()
    answers[0]!()
    expect(await shutdown).toEqual({ sent: 1, skipped: 0, failed: 1, pending: 0 })
    expect(bodies.map((events) => events.map((event) => event.conversationId))).toEqual([['meanwhile']])
    expect(errors.map((error) => error.code)).toEqual([])
  })
})

describe('py-runtime-18: a reply that ends before its Content-Length', () => {
  let server: Server | undefined

  afterEach(() => {
    server?.close()
    server = undefined
  })

  it('is a network error, retried, through the default fetch', async () => {
    let requests = 0
    server = createServer((socket: Socket) => {
      let received = ''
      socket.on('data', (chunk) => {
        received += chunk.toString('latin1')
        const end = received.indexOf('\r\n\r\n')
        const length = Number(/content-length:\s*(\d+)/i.exec(received)?.[1] ?? 0)
        if (end < 0 || received.length - end - 4 < length) return
        requests++
        const events = JSON.parse(received.slice(end + 4)).events as TurnEvent[]
        const body = JSON.stringify({ accepted: events.map((event) => event.eventId) })
        // The first reply says 100 bytes and sends 13, then closes.
        socket.end(requests === 1 ? 'HTTP/1.1 200 OK\r\ncontent-length: 100\r\n\r\n{"accepted":[' : `HTTP/1.1 200 OK\r\ncontent-length: ${body.length}\r\n\r\n${body}`)
      })
    })
    await new Promise<void>((resolve) => server!.listen(0, '127.0.0.1', resolve))
    const { port } = server.address() as { port: number }
    const errors: LSquareError[] = []
    const client = new LSquare({ apiKey: 'lsq_test', endpoint: `http://127.0.0.1:${port}`, onError: (error) => errors.push(error), logger: { info() {}, warn() {} } })
    client.capture(input)
    expect(await client.flush({ timeoutMs: 5_000 })).toMatchObject({ sent: 1, failed: 0 })
    expect(requests).toBe(2)
    expect(errors).toEqual([])
    await client.shutdown({ timeoutMs: 0 })
  })
})

describe('py-runtime-19: byte order marks in an acknowledgement', () => {
  const bytes = (prefix: number[], init?: RequestInit) =>
    new Response(new Uint8Array([...prefix, ...new TextEncoder().encode(JSON.stringify({ accepted: ids(init) }))]), { status: 200 })

  it('drops one leading byte order mark, as TextDecoder does', async () => {
    const { client, errors } = setup((init) => bytes([0xef, 0xbb, 0xbf], init))
    client.capture(input)
    expect(await client.flush()).toMatchObject({ sent: 1, failed: 0 })
    await tick()
    expect(errors).toEqual([])
  })

  it.each([
    ['two', [0xef, 0xbb, 0xbf, 0xef, 0xbb, 0xbf]],
    ['a partial one', [0xef, 0xbb]],
  ])('fails one preceded by %s: not JSON', async (_name, prefix) => {
    const { client, errors } = setup((init) => bytes(prefix, init))
    client.capture(input)
    expect(await client.flush()).toMatchObject({ sent: 0, failed: 1 })
    await tick()
    expect(errors.map((error) => error.code)).toEqual(['send_failed'])
  })
})

describe('parity-14: a Retry-After too large for a number', () => {
  it.each(['9'.repeat(400), '9'.repeat(5_000)])('waits the 30 s cap, then retries (%#)', async (retryAfter) => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'performance', 'Date'] })
    const { client, urls, errors } = setup((init, n) => (n === 1 ? new Response('', { status: 503, headers: { 'retry-after': retryAfter } }) : ack(init)))
    client.capture(input)
    await vi.advanceTimersByTimeAsync(10)
    expect(urls).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(29_980)
    expect(urls).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(20)
    expect(urls).toHaveLength(2)
    vi.useRealTimers()
    expect(await client.flush()).toMatchObject({ failed: 0, pending: 0 })
    await tick()
    expect(errors).toEqual([])
  })
})

describe('parity-15: NaN and the infinities in an acknowledgement', () => {
  it.each(['NaN', 'Infinity', '-Infinity'])('fails one holding %s: not JSON', async (constant) => {
    const { client, errors } = setup((init) => new Response(`{"accepted":${JSON.stringify(ids(init))},"extra":${constant}}`, { status: 200 }))
    client.capture(input)
    expect(await client.flush()).toMatchObject({ sent: 0, failed: 1 })
    await tick()
    expect(errors.map((error) => error.code)).toEqual(['send_failed'])
  })

  it('accepts one holding a number too large for a double, which is JSON (a guard)', async () => {
    const { client, errors } = setup((init) => new Response(`{"accepted":${JSON.stringify(ids(init))},"extra":1e400}`, { status: 200 }))
    client.capture(input)
    expect(await client.flush()).toMatchObject({ sent: 1, failed: 0 })
    await tick()
    expect(errors).toEqual([])
  })
})

/**
 * What the client makes of each endpoint: the URL it posts to, or `invalid` (not a valid URL) or
 * `https` (must use https). Python's test has the same table, and makes the same of each, but for
 * the internationalized hosts, marked here, which it refuses.
 */
const ENDPOINTS: Array<[string, string, 'international'?]> = [
  ['https://ingest.example.com', 'https://ingest.example.com/v1/turns'],
  ['https://ingest.example/base/', 'https://ingest.example/base/v1/turns'],
  ['https://ingest.example/é a"^%41', 'https://ingest.example/%C3%A9%20a%22%5E%41/v1/turns'],
  ['https://ingest.example/日本', 'https://ingest.example/%E6%97%A5%E6%9C%AC/v1/turns'],
  ['https://ingest.example/a\\b', 'https://ingest.example/a/b/v1/turns'],
  ['https://ingest.example\\x', 'https://ingest.example/x/v1/turns'],
  ['https://ingest.example\\@elsewhere.example/', 'https://ingest.example/@elsewhere.example/v1/turns'],
  ['https://ingest.example/x|[]{}`<>\u007f', 'https://ingest.example/x|[]%7B%7D%60%3C%3E%7F/v1/turns'],
  ["https://ingest.example/~!$&()*+,;=:@'", "https://ingest.example/~!$&()*+,;=:@'/v1/turns"],
  ['https://ingest.example/a%zz/%41/', 'https://ingest.example/a%zz/%41/v1/turns'],
  ['https://ingest.example/\ud800', 'https://ingest.example/%EF%BF%BD/v1/turns'],
  ['https://ingest.example/a?b#c', 'https://ingest.example/a/v1/turns'],
  ['https://ingest.example/a/../b', 'https://ingest.example/b/v1/turns'],
  ['https://ingest.example/a/./b/.', 'https://ingest.example/a/b/v1/turns'],
  ['https://ingest.example/a/%2e%2E/b', 'https://ingest.example/b/v1/turns'],
  ['https://ingest.example/../..', 'https://ingest.example/v1/turns'],
  [' \u0000https://ingest.example/x\u001f ', 'https://ingest.example/x/v1/turns'],
  [' https://ingest.example/', 'invalid'],
  ['https://ingest.example/ ', 'https://ingest.example/%C2%A0/v1/turns'],
  ['https://ingest.ex\tam\nple/x\r', 'https://ingest.example/x/v1/turns'],
  ['https://INGEST.Example/', 'https://ingest.example/v1/turns'],
  ['https://ingest%2Eexample/', 'https://ingest.example/v1/turns'],
  ['https://ingest%41.example/', 'https://ingesta.example/v1/turns'],
  ['https://user:secret@ingest.example/', 'https://ingest.example/v1/turns'],
  ['https://ingest example/', 'invalid'],
  ['https://ingest^example/', 'invalid'],
  ['https://ingest|example/', 'invalid'],
  ['https://ingest<example/', 'invalid'],
  ['https://ingest%20example/', 'invalid'],
  ['https://ingest%00example/', 'invalid'],
  ['https://ingest%zz/', 'invalid'],
  ['https://%ff.example/', 'invalid'],
  ['https://%c0%af.example/', 'invalid'],
  ['https://�.example/', 'invalid'],
  ['https://user@/x', 'invalid'],
  ['https://bücher.example/', 'https://xn--bcher-kva.example/v1/turns', 'international'],
  ['https://xn--bcher-kva.example/', 'https://xn--bcher-kva.example/v1/turns', 'international'],
  ['https://a‍b.example/', 'invalid', 'international'],
  ['https://a b.example/', 'invalid', 'international'],
  ['http://bücher.example/', 'https', 'international'],
  ['https://a b\u00e9.example/', 'invalid'],
  ['https://bücher.example:99999/', 'invalid'],
  ['https://a<\u0338b.example/', 'https://xn--ab-tjv.example/v1/turns', 'international'],
  ['http://127.1:8787/', 'http://127.0.0.1:8787/v1/turns'],
  ['http://0x7f.0.0.1/', 'http://127.0.0.1/v1/turns'],
  ['http://0177.0.0.1/', 'http://127.0.0.1/v1/turns'],
  ['http://2130706433/', 'http://127.0.0.1/v1/turns'],
  ['https://0x/', 'https://0.0.0.0/v1/turns'],
  ['https://127.0.0.999/', 'invalid'],
  ['https://1.2.3.4.5/', 'invalid'],
  ['https://example.123/', 'invalid'],
  ['https://09/', 'invalid'],
  ['https://4294967296/', 'invalid'],
  ['http://[0:0::1]:8787/', 'http://[::1]:8787/v1/turns'],
  ['https://[::FFFF:1.2.3.4]/', 'https://[::ffff:102:304]/v1/turns'],
  ['https://[1:0:0:2:0:0:0:3]/', 'https://[1:0:0:2::3]/v1/turns'],
  ['https://[fe80::1%25eth0]/', 'invalid'],
  ['https://[1::2::3]/', 'invalid'],
  ['https://[::1.2.3.04]/', 'invalid'],
  ['http://localhost./', 'https'],
  ['https://ingest.example:443/', 'https://ingest.example/v1/turns'],
  ['https://ingest.example:0443/', 'https://ingest.example/v1/turns'],
  ['https://ingest.example:8443/', 'https://ingest.example:8443/v1/turns'],
  ['https://ingest.example:0/', 'https://ingest.example:0/v1/turns'],
  ['http://localhost:00/', 'http://localhost:0/v1/turns'],
  ['https://ingest.example:65536/', 'invalid'],
  ['https://ingest.example:8a/', 'invalid'],
  ['HTTPS://ingest.example/', 'https://ingest.example/v1/turns'],
  ['https:ingest.example/x', 'https://ingest.example/x/v1/turns'],
  ['https:\\\\\\ingest.example/x', 'https://ingest.example/x/v1/turns'],
  ['http://ingest.example/', 'https'],
  ['mailto:x', 'https'],
  ['ftp://%zz/', 'invalid'],
  ['not a url', 'invalid'],
]

describe('parity-16: an endpoint, read with new URL', () => {
  it.each(ENDPOINTS)('reads %j as %s', async (endpoint, expected) => {
    const { client, urls, warnings } = setup((init) => ack(init), endpoint)
    client.capture(input)
    await client.flush()
    await tick()
    const warning = warnings.join(' ')
    const outcome = warning.includes('not a valid URL') ? 'invalid' : warning.includes('must use https') ? 'https' : urls[0]
    expect(outcome).toBe(expected)
  })

  it('fails an event posted to port 0 as a network error: fetch tries that port, and cannot connect', async () => {
    // Python's Exchange took 0 for no port and posted to 80 (or 443) instead.
    const errors: LSquareError[] = []
    const client = new LSquare({ apiKey: 'lsq_test', endpoint: 'http://127.0.0.1:0/', maxAttempts: 1, onError: (error) => errors.push(error), logger: { info() {}, warn() {} } })
    client.capture(input)
    expect(await client.flush({ timeoutMs: 5_000 })).toMatchObject({ sent: 0, failed: 1 })
    await tick()
    expect(errors.map((error) => [error.code, error.message])).toEqual([['send_failed', 'Network error after 1 attempts.']])
    await client.shutdown({ timeoutMs: 0 })
  })
})
