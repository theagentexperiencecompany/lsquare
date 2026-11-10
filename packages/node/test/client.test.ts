import { AsyncLocalStorage } from 'node:async_hooks'
import { readFileSync } from 'node:fs'
import { Readable } from 'node:stream'
import { inspect } from 'node:util'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { LSquare, SDK_VERSION, type BeforeSend, type CaptureInput, type LSquareError, type LSquareOptions, type TurnEvent } from '../src/index.js'

const input = (over: Partial<CaptureInput> = {}): CaptureInput => ({
  conversationId: 'conv1',
  orgId: 'org_1',
  toolName: 'ask_docs',
  question: 'How many orders shipped last week?',
  ...over,
})

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
/** onError and loggers run in a later event-loop turn (setImmediate): let them run. */
const tick = () => new Promise<void>((resolve) => setImmediate(() => setImmediate(resolve)))
const busyWait = (ms: number) => {
  const end = performance.now() + ms
  while (performance.now() < end) {}
}
/** Polls until `check()` holds, for up to a second. */
const until = async (check: () => boolean) => {
  for (let i = 0; i < 200 && !check(); i++) await sleep(5)
}

interface Call {
  url: string
  init: RequestInit
  events: TurnEvent[]
}

type Handler = (call: Call, signal: AbortSignal | undefined, n: number) => Response | Promise<Response>

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

/** Acknowledges every event it receives, as the ingestion service does. */
const ack: Handler = ({ events }) => json({ accepted: events.map((e) => e.eventId), duplicates: [], rejected: [] })

/** Never answers; rejects when the SDK aborts the request. */
const hanging: Handler = (_call, signal) =>
  new Promise((_, reject) => signal?.addEventListener('abort', () => reject(new Error('aborted'))))

function setup(handler: Handler = ack, options: LSquareOptions = {}) {
  const calls: Call[] = []
  const errors: Array<{ error: LSquareError; event: TurnEvent | undefined }> = []
  const logs = { info: [] as string[], warn: [] as string[] }
  const fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    const call: Call = { url: String(url), init: init ?? {}, events: JSON.parse(String(init?.body)).events }
    calls.push(call)
    return handler(call, init?.signal ?? undefined, calls.length)
  }) as typeof globalThis.fetch
  const client = new LSquare({
    apiKey: 'lsq_test',
    endpoint: 'https://ingest.test/',
    fetch,
    onError: (error, event) => errors.push({ error, event }),
    logger: { info: (m) => logs.info.push(m), warn: (m) => logs.warn.push(m) },
    ...options,
  })
  return { client, calls, errors, logs }
}

const codes = (errors: Array<{ error: LSquareError }>) => errors.map((e) => e.error.code)

/** Collects unhandled rejections while `run` executes. */
async function unhandledDuring(run: () => Promise<void>): Promise<unknown[]> {
  const seen: unknown[] = []
  const listener = (reason: unknown) => seen.push(reason)
  process.on('unhandledRejection', listener)
  try {
    await run()
    await sleep(20)
  } finally {
    process.off('unhandledRejection', listener)
  }
  return seen
}

afterEach(() => {
  delete process.env.LSQUARE_DRY_RUN
  delete process.env.LSQUARE_ENDPOINT
})

describe('modes and configuration', () => {
  it('is disabled without an apiKey, with one warning', async () => {
    const { client, calls, logs } = setup(ack, { apiKey: undefined })
    client.capture(input())
    expect(await client.flush()).toEqual({ sent: 0, skipped: 0, failed: 0, pending: 0 })
    await tick()
    expect(calls).toHaveLength(0)
    expect(logs.warn).toEqual(['[lsquare] No apiKey: capture is disabled.'])
  })

  it('sends to https://ingest.lsquare.io without an endpoint option or LSQUARE_ENDPOINT', async () => {
    const { client, calls, logs } = setup(ack, { endpoint: undefined })
    client.capture(input())
    expect(await client.flush()).toMatchObject({ sent: 1 })
    await tick()
    expect(calls[0]?.url).toBe('https://ingest.lsquare.io/v1/turns')
    expect(logs.warn).toEqual([])
  })

  it.each([
    ['http://ingest.test', '[lsquare] The endpoint must use https (http only for localhost): capture is disabled.'],
    ['not a url', '[lsquare] The endpoint is not a valid URL: capture is disabled.'],
  ])('refuses endpoint %j', async (endpoint, warning) => {
    const { client, calls, logs } = setup(ack, { endpoint })
    client.capture(input())
    await client.flush()
    await tick()
    expect(calls).toHaveLength(0)
    expect(logs.warn).toEqual([warning])
  })

  it('accepts http only for localhost, and LSQUARE_ENDPOINT', async () => {
    process.env.LSQUARE_ENDPOINT = 'http://127.0.0.1:8787/base/'
    const { client, calls } = setup(ack, { endpoint: undefined })
    client.capture(input())
    expect(await client.flush()).toMatchObject({ sent: 1 })
    await tick()
    expect(calls[0]?.url).toBe('http://127.0.0.1:8787/base/v1/turns')
  })

  it('never throws on bad options or input', async () => {
    expect(() => new LSquare(null as unknown as LSquareOptions)).not.toThrow()
    const { client, errors } = setup(ack, { flushTimeoutMs: Number.NaN, maxConcurrentHooks: -1 })
    const hostile = new Proxy({}, { get: () => { throw new Error('trap') }, ownKeys: () => { throw new Error('trap') } })
    for (const bad of [null, 42, 'x', [], hostile]) {
      expect(() => client.capture(bad as unknown as CaptureInput)).not.toThrow()
    }
    await expect(client.flush()).resolves.toBeDefined()
    expect(codes(errors).every((c) => c === 'missing_required' || c === 'internal_error')).toBe(true)
  })

  it('reads options from a config class or a prototype of defaults', async () => {
    const urls: string[] = []
    const fetch = (async (url: string | URL | Request, init?: RequestInit) => {
      urls.push(String(url))
      return ack({ url: String(url), init: init ?? {}, events: JSON.parse(String(init?.body)).events }, undefined, urls.length)
    }) as typeof globalThis.fetch
    const logger = { info() {}, warn() {} }
    class Config {
      get apiKey() {
        return 'lsq_test'
      }
      get endpoint() {
        return 'https://config.test'
      }
      get fetch() {
        return fetch
      }
      get logger() {
        return logger
      }
    }
    for (const options of [new Config(), Object.create({ apiKey: 'lsq_test', endpoint: 'https://defaults.test', fetch, logger })]) {
      const client = new LSquare(options)
      client.capture(input())
      expect(await client.flush()).toMatchObject({ sent: 1 })
    }
    expect(urls).toEqual(['https://config.test/v1/turns', 'https://defaults.test/v1/turns'])
  })

  // Another dependency's prototype pollution must not redirect events and the key, or turn redaction off.
  it.each<[string, Record<string, unknown>, LSquareOptions]>([
    ['endpoint', { endpoint: 'https://attacker.test' }, {}],
    ['LSQUARE_ENDPOINT', { LSQUARE_ENDPOINT: 'https://attacker.test' }, {}],
    ['redact', { redact: false }, {}],
    ['a pattern in redact', { email: false }, { redact: { ip: false } }],
    ['dryRun', { dryRun: true }, {}],
    ['LSQUARE_DRY_RUN', { LSQUARE_DRY_RUN: '1' }, {}],
  ])('ignores %s inherited from a polluted Object.prototype', async (_name, pollution, options) => {
    const sent: Array<{ url: string; question: string | undefined }> = []
    const fetch = (async (url: string | URL | Request, init?: RequestInit) => {
      const call = { url: String(url), init: init ?? {}, events: JSON.parse(String(init?.body)).events as TurnEvent[] }
      sent.push({ url: call.url, question: call.events[0]?.question })
      return ack(call, undefined, sent.length)
    }) as typeof globalThis.fetch
    const proto = Object.prototype as Record<string, unknown>
    Object.assign(proto, pollution)
    let client: LSquare
    try {
      // Options and the environment are read once, here.
      client = new LSquare({ apiKey: 'lsq_test', fetch, logger: { info() {}, warn() {} }, ...options })
    } finally {
      for (const key of Object.keys(pollution)) delete proto[key]
    }
    client.capture(input({ question: 'Ask jane@acme.io' }))
    expect(await client.flush()).toMatchObject({ sent: 1 })
    expect(sent).toEqual([{ url: 'https://ingest.lsquare.io/v1/turns', question: 'Ask [EMAIL_1]' }])
  })

  it('dry run logs the shape only, sends nothing, and needs no key or endpoint', async () => {
    process.env.LSQUARE_DRY_RUN = '1'
    const { client, calls, logs } = setup(ack, { apiKey: undefined, endpoint: undefined })
    client.capture(
      input({
        turnId: 't1',
        question: 'Did jane@acme.io see it?',
        toolCalls: [{ tool: 'search_docs', args: { q: 'secret question' }, result: 'rows', sk_live_abcdefghijklmnop: 1, jane_doe: 2 }],
        verification: new Map(),
      }),
    )
    expect(await client.flush()).toEqual({ sent: 0, skipped: 1, failed: 0, pending: 0 })
    await tick()
    expect(calls).toHaveLength(0)
    expect(logs.info).toHaveLength(1)
    const line = logs.info[0] as string
    for (const secret of ['jane', 'secret question', 'sk_live']) expect(line).not.toContain(secret)
    const summary = JSON.parse(line.replace('[lsquare dry-run] ', ''))
    expect(summary).toMatchObject({
      eventId: 'from turnId',
      redactions: { email: 1 },
      dropped: { 'toolCalls[0]': 'missing_name', verification: 'not_json' },
      ignoredToolCallFields: { tool: 1, args: 1, result: 1, '(other)': 2 },
    })
  })

  it('dry run never logs identifiers, only where the event ID came from', async () => {
    process.env.LSQUARE_DRY_RUN = '1'
    const { client, logs } = setup(ack, { apiKey: undefined, endpoint: undefined })
    const conversationId = 'jane.doe@acme.io'
    client.capture(input({ conversationId, turnId: 'turn-of-jane' }))
    client.capture(input({ conversationId, turnId: 'x'.repeat(600) }))
    client.capture(input({ conversationId }))
    await client.flush()
    await tick()
    expect(logs.info).toHaveLength(3)
    for (const line of logs.info) {
      expect(line).not.toContain('jane')
      expect(line).not.toContain('turn-of')
    }
    expect(logs.info.map((line) => JSON.parse(line.replace('[lsquare dry-run] ', '')).eventId)).toEqual([
      'from turnId',
      'from turnId, hashed',
      'random: no turnId',
    ])
  })

  it('redacts an LSquare key that ends up in captured text', async () => {
    const key = `lsq_${'K'.repeat(43)}`
    const { client, calls } = setup()
    client.capture(input({ question: `my key is ${key}`, error: new Error(`401 with ${key}`) }))
    await client.flush()
    await tick()
    expect(JSON.stringify(calls[0]?.events)).not.toContain(key)
    expect(calls[0]?.events[0]?.question).toBe('my key is [SECRET_1]')
  })

  it('redacts values under secret key names, in the request and in the copy given to onError', async () => {
    const { client, calls, errors } = setup(() => new Response('', { status: 400 }))
    client.capture(
      input({
        toolCalls: [{ name: 'connect', input: { host: 'db.internal', password: 'hunter2-no-format' } }],
        metadata: { client_secret: 'opaque-value' },
      }),
    )
    await client.flush()
    await tick()
    expect(calls[0]?.events[0]).toMatchObject({
      toolCalls: [{ name: 'connect', input: { host: 'db.internal', password: '[SECRET_1]' } }],
      metadata: { client_secret: '[SECRET_2]' },
      redactions: { secret: 2 },
    })
    for (const seen of [calls[0]?.events, errors[0]?.event]) {
      expect(JSON.stringify(seen)).not.toContain('hunter2-no-format')
      expect(JSON.stringify(seen)).not.toContain('opaque-value')
    }
  })
})

describe('printing a client', () => {
  it('never shows the key or queued content, however it is printed or serialised', async () => {
    const key = `lsq_${'S'.repeat(43)}`
    const client = new LSquare({
      apiKey: key,
      endpoint: 'https://x.test',
      fetch: () => new Promise(() => {}),
      logger: { info() {}, warn() {} },
      onError() {},
    })
    client.capture({ ...input(), question: 'secret question for bob@example.com' })
    await tick()
    const printed = [
      inspect(client, { depth: 10, showHidden: true }),
      JSON.stringify(client),
      String(client),
      JSON.stringify(Object.entries(client)),
      Object.getOwnPropertyNames(client).join(','),
    ].join('\n')
    expect(printed).not.toContain(key)
    expect(printed).not.toContain('secret question')
    expect(printed).not.toContain('bob@example.com')
    expect(Object.getOwnPropertyNames(client)).toEqual([])
    await client.shutdown()
  })
})

describe('sending', () => {
  it('posts one redacted event with the SDK fields, refusing redirects', async () => {
    const { client, calls } = setup()
    client.capture(input({ turnId: 42, question: 'Ask jane@acme.io', usage: { inputTokens: 10, outputTokens: 5, costUsd: 0.01 } }))
    expect(await client.flush()).toEqual({ sent: 1, skipped: 0, failed: 0, pending: 0 })
    await tick()
    const call = calls[0] as Call
    expect(call.url).toBe('https://ingest.test/v1/turns')
    expect(call.init.redirect).toBe('error')
    expect((call.init.headers as Record<string, string>).authorization).toBe('Bearer lsq_test')
    expect(call.events[0]).toMatchObject({
      eventId: 'conv1/t/42',
      turnId: '42',
      source: 'live',
      schemaVersion: 1,
      sdk: { name: '@lsquare/node', version: '0.1.3' },
      status: 'completed',
      question: 'Ask [EMAIL_1]',
      redactions: { email: 1 },
      usage: { inputTokens: 10, outputTokens: 5, costUsd: 0.01 },
    })
  })

  it('uses a random eventId without a turnId', async () => {
    const { client, calls } = setup()
    client.capture(input())
    client.capture(input())
    await client.flush()
    await tick()
    const [a, b] = calls.map((c) => c.events[0]?.eventId)
    expect(a).toMatch(/^[0-9a-f-]{36}$/)
    expect(a).not.toBe(b)
  })

  it('hashes an eventId longer than 512 bytes, keeping the turnId', async () => {
    const { client, calls } = setup()
    client.capture({ ...input(), turnId: 'x'.repeat(600) })
    client.capture({ ...input(), turnId: 'x'.repeat(600) })
    await client.flush()
    await tick()
    const [a, b] = calls.map((c) => c.events[0])
    expect(a?.eventId).toMatch(/^h\/[0-9a-f]{64}$/)
    expect(b?.eventId).toBe(a?.eventId)
    expect(a?.turnId).toHaveLength(600)
    expect(a?.dropped).toBeUndefined()
  })

  it('retries 5xx and network errors, then succeeds', async () => {
    const { client, calls } = setup((call, signal, n) => {
      if (n === 1) return new Response('', { status: 503 })
      if (n === 2) throw new Error('ECONNRESET')
      return ack(call, signal, n)
    })
    client.capture(input())
    expect(await client.flush({ timeoutMs: 3000 })).toMatchObject({ sent: 1 })
    await tick()
    expect(calls).toHaveLength(3)
  })

  it('honours Retry-After on 429', async () => {
    const { client, calls } = setup((call, signal, n) =>
      n === 1 ? new Response('', { status: 429, headers: { 'retry-after': '0' } }) : ack(call, signal, n),
    )
    client.capture(input())
    expect(await client.flush()).toMatchObject({ sent: 1 })
    await tick()
    expect(calls).toHaveLength(2)
  })

  it('gives up after maxAttempts', async () => {
    const { client, calls, errors } = setup(() => new Response('', { status: 500 }), { maxAttempts: 2 })
    client.capture(input())
    expect(await client.flush({ timeoutMs: 3000 })).toMatchObject({ failed: 1 })
    await tick()
    expect(calls).toHaveLength(2)
    expect(errors[0]?.error).toMatchObject({ code: 'send_failed', status: 500 })
  })

  it('does not retry other 4xx, and reports a copy of the redacted event', async () => {
    const { client, calls, errors } = setup(() => new Response('', { status: 400 }))
    client.capture(input({ question: 'Ask jane@acme.io' }))
    expect(await client.flush()).toMatchObject({ failed: 1 })
    await tick()
    expect(calls).toHaveLength(1)
    expect(errors[0]?.error).toMatchObject({ code: 'send_failed', status: 400 })
    expect(errors[0]?.event?.question).toBe('Ask [EMAIL_1]')
  })

  it.each([
    ['a body that is not JSON', () => new Response('not JSON'), 'send_failed'],
    ['no acknowledgement', () => json({ accepted: [], duplicates: [], rejected: [] }), 'send_failed'],
    ['a contradictory acknowledgement', ({ events }: Call) => json({ accepted: [events[0]?.eventId], rejected: [{ eventId: events[0]?.eventId }] }), 'send_failed'],
    ['a body over 64 KB', () => new Response('x'.repeat(70 * 1024)), 'send_failed'],
  ])('a 200 with %s is a failure, not a success', async (_name, handler, code) => {
    const { client, calls, errors } = setup(handler as Handler)
    client.capture(input())
    expect(await client.flush()).toMatchObject({ sent: 0, failed: 1 })
    await tick()
    expect(calls).toHaveLength(1)
    expect(codes(errors)).toEqual([code])
  })

  it('reports rejection reasons only from the known list', async () => {
    const reasons = ['missing_field', 'jane@acme.io']
    const messages = []
    for (const reason of reasons) {
      const { client, errors } = setup(({ events }) => json({ accepted: [], rejected: [{ eventId: events[0]?.eventId, reason }] }))
      client.capture(input())
      await client.flush()
      await tick()
      messages.push(errors[0]?.error.message)
    }
    expect(messages).toEqual(['LSquare rejected the event: missing_field.', 'LSquare rejected the event: unspecified.'])
  })

  it('times out a response whose body stalls, and retries', async () => {
    const stalled: Handler = (_call, signal) =>
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode('{"accepted":'))
            signal?.addEventListener('abort', () => controller.error(new Error('aborted')))
          },
        }),
      )
    const { client, calls, errors } = setup(stalled, { requestTimeoutMs: 30, maxAttempts: 2 })
    client.capture(input())
    expect(await client.flush({ timeoutMs: 2000 })).toMatchObject({ failed: 1 })
    await tick()
    expect(calls).toHaveLength(2)
    expect(errors[0]?.error).toMatchObject({ code: 'send_failed', message: 'Network error after 2 attempts.' })
  })

  it("doesn't wait for a retry due after the deadline, in that flush or in later ones", async () => {
    // As the ingestion answers when rate-limited: one 429 with Retry-After: 10, then healthy.
    const { client, calls } = setup((call, signal, n) =>
      n === 1 ? new Response('', { status: 429, headers: { 'retry-after': '10' } }) : ack(call, signal, n),
    )
    client.capture(input({ question: 'rate limited' }))
    let started = performance.now()
    expect(await client.flush()).toMatchObject({ sent: 0, pending: 1 })
    expect(performance.now() - started).toBeLessThan(500)
    client.capture(input({ question: 'next request' }))
    started = performance.now()
    expect(await client.flush()).toMatchObject({ sent: 1, pending: 1 })
    expect(performance.now() - started).toBeLessThan(500)
    expect(calls).toHaveLength(2)
    await client.shutdown({ timeoutMs: 0 })
  })

  it('a timeout of 0 returns at once, without reporting pending events', async () => {
    const { client, errors } = setup(hanging)
    client.capture(input())
    const started = performance.now()
    expect(await client.flush({ timeoutMs: 0 })).toEqual({ sent: 0, skipped: 0, failed: 0, pending: 1 })
    expect(performance.now() - started).toBeLessThan(50)
    await tick()
    expect(errors).toEqual([])
    await client.shutdown({ timeoutMs: 0 })
  })

  it('flushTimeoutMs: 0 means flush() never waits; beforeSend keeps its default deadline', async () => {
    const hook: BeforeSend = async (event) => {
      await sleep(100)
      return event
    }
    const { client, calls } = setup(ack, { flushTimeoutMs: 0, beforeSend: hook })
    client.capture(input())
    const started = performance.now()
    expect(await client.flush()).toMatchObject({ pending: 1 })
    expect(performance.now() - started).toBeLessThan(50)
    await until(() => calls.length > 0)
    expect(calls).toHaveLength(1)
    await client.shutdown()
  })

  it('shutdown() still waits for requests in flight when flushTimeoutMs is 0', async () => {
    const slow: Handler = async (call, signal, n) => {
      await sleep(50)
      return ack(call, signal, n)
    }
    const { client, calls } = setup(slow, { flushTimeoutMs: 0 })
    client.capture(input())
    await tick()
    expect(await client.shutdown()).toMatchObject({ sent: 1, failed: 0 })
    expect(calls).toHaveLength(1)
  })

  it('reads a web stream body that has getReader() but no async iterator', async () => {
    const readerOnly = (async (_url: unknown, init?: RequestInit) => {
      const event = (JSON.parse(String(init?.body)) as { events: TurnEvent[] }).events[0] as TurnEvent
      const stream = new Response(JSON.stringify({ accepted: [event.eventId] })).body as ReadableStream<Uint8Array>
      return { status: 200, headers: new Headers(), body: { getReader: () => stream.getReader() } }
    }) as unknown as typeof globalThis.fetch
    const { client } = setup(ack, { fetch: readerOnly })
    client.capture(input())
    expect(await client.flush()).toMatchObject({ sent: 1, failed: 0 })
  })

  it('treats a fractional limit as at least 1, never as 0', async () => {
    const { client, calls } = setup(ack, { maxConcurrentRequests: 0.5, maxConcurrentHooks: 0.5, beforeSend: (event) => event })
    client.capture(input())
    expect(await client.flush()).toMatchObject({ sent: 1 })
    expect(calls).toHaveLength(1)
  })

  it('keeps at most maxConcurrentRequests requests in flight; the others wait their turn', async () => {
    const answers: Array<() => void> = []
    let inFlight = 0
    let peak = 0
    const { client, calls, errors } = setup(
      (call, signal, n) =>
        new Promise<Response>((resolve) => {
          peak = Math.max(peak, ++inFlight)
          answers.push(() => {
            inFlight--
            resolve(ack(call, signal, n) as Response)
          })
        }),
      { maxConcurrentRequests: 2 },
    )
    for (let i = 0; i < 5; i++) client.capture(input({ turnId: i }))
    await tick()
    expect(calls).toHaveLength(2)
    for (let answered = 0; answered < 5; answered++) {
      await until(() => answers.length > 0)
      answers.shift()?.()
    }
    expect(await client.flush()).toMatchObject({ failed: 0, pending: 0 })
    expect(errors).toEqual([])
    expect(peak).toBe(2)
    expect(calls.map((c) => c.events[0]?.turnId)).toEqual(['0', '1', '2', '3', '4'])
  })

  it('reads and discards the Node stream bodies fetch polyfills return, without retrying', async () => {
    const posted: string[] = []
    const polyfill = (async (_url: unknown, init?: RequestInit) => {
      const event = (JSON.parse(String(init?.body)) as { events: TurnEvent[] }).events[0] as TurnEvent
      posted.push(event.question)
      const accepted = event.question === 'accepted'
      const body = Readable.from([accepted ? JSON.stringify({ accepted: [event.eventId] }) : '{"error":"invalid_events"}'])
      return { status: accepted ? 200 : 400, headers: new Headers(), body }
    }) as unknown as typeof globalThis.fetch
    const { client } = setup(ack, { fetch: polyfill })
    client.capture(input({ question: 'accepted' }))
    client.capture(input({ question: 'refused' }))
    expect(await client.flush()).toMatchObject({ sent: 1, failed: 1 })
    expect(posted).toEqual(['accepted', 'refused'])
  })

  it('returns at the flush timeout and reports what is still pending', async () => {
    const { client, errors } = setup(hanging)
    client.capture(input())
    const started = Date.now()
    expect(await client.flush({ timeoutMs: 50 })).toEqual({ sent: 0, skipped: 0, failed: 0, pending: 1 })
    await tick()
    expect(Date.now() - started).toBeLessThan(500)
    expect(codes(errors)).toEqual(['pending_after_flush'])
  })

  it('shutdown is terminal: no request goes out after it resolves', async () => {
    const { client, calls, errors } = setup(() => new Response('', { status: 429, headers: { 'retry-after': '1' } }))
    client.capture(input())
    await sleep(10)
    expect(await client.shutdown({ timeoutMs: 50 })).toEqual({ sent: 0, skipped: 0, failed: 1, pending: 0 })
    const before = calls.length
    await sleep(1100)
    expect(calls.length).toBe(before)
    expect(codes(errors)).toEqual(['shutdown'])
  })
})

describe('diagnostics', () => {
  it('onError receives a copy: changing it never changes what is sent', async () => {
    const { client, calls } = setup(
      async (call, signal, n) => {
        if (n === 1) {
          await sleep(80)
          return new Response('', { status: 503 })
        }
        return ack(call, signal, n)
      },
      {
        onError: (_error, event) => {
          if (event) event.question = 'leak jane@acme.io'
        },
      },
    )
    client.capture(input({ question: 'Ask jane@acme.io' }))
    await client.flush({ timeoutMs: 20 })
    await tick()
    expect(await client.flush({ timeoutMs: 2000 })).toMatchObject({ sent: 1 })
    await tick()
    expect(calls.map((c) => c.events[0]?.question)).toEqual(['Ask [EMAIL_1]', 'Ask [EMAIL_1]'])
  })

  it('async onError and loggers that reject never cause unhandled rejections', async () => {
    const seen = await unhandledDuring(async () => {
      const failing = { onError: async () => { throw new Error('boom') } }
      const a = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', ...failing })
      a.capture({ ...input(), orgId: '' })
      const thenable = { then: () => { throw new Error('boom') } }
      const b = new LSquare({ dryRun: true, logger: { info: async () => { throw new Error('boom') }, warn: () => thenable } })
      b.capture(input())
      await b.flush()
    })
    expect(seen).toEqual([])
  })

  it('a throwing onError never breaks capture or flush', async () => {
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch: hanging as unknown as typeof fetch, onError: () => { throw new Error('x') } })
    expect(() => client.capture(null as unknown as CaptureInput)).not.toThrow()
    await expect(client.flush({ timeoutMs: 10 })).resolves.toBeDefined()
  })

  it('the default handler logs codes only, at most once a minute', async () => {
    const warn: string[] = []
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', logger: { info: () => {}, warn: (m) => warn.push(m) } })
    client.capture({ ...input(), orgId: '' })
    client.capture({ ...input(), conversationId: 'a/b' })
    await tick()
    expect(warn).toEqual(['[lsquare] missing_required: Required fields are missing or not strings; the event was dropped.'])
  })
})

describe('async context', () => {
  it("runs each event's beforeSend and reports in the context of its capture()", async () => {
    // The app's request context, as tracing, logging or tenant code keeps it.
    const request = new AsyncLocalStorage<string>()
    const seen: string[] = []
    const { client } = setup(hanging, {
      maxConcurrentHooks: 1,
      beforeSend: async (event) => {
        seen.push(`hook for ${event.orgId} in ${request.getStore()}`)
        await sleep(10)
        return event
      },
      onError: (error, event) => seen.push(`${error.code} for ${event?.orgId} in ${request.getStore()}`),
    })
    for (const org of ['org_A', 'org_B', 'org_C']) request.run(org, () => client.capture(input({ orgId: org, turnId: org })))
    // One request's flush reports every pending event, and one hook's end starts the next.
    await request.run('org_A', () => client.flush({ timeoutMs: 200 }))
    await tick()
    expect(seen.sort()).toEqual([
      'hook for org_A in org_A',
      'hook for org_B in org_B',
      'hook for org_C in org_C',
      'pending_after_flush for org_A in org_A',
      'pending_after_flush for org_B in org_B',
      'pending_after_flush for org_C in org_C',
    ])
    await client.shutdown({ timeoutMs: 0 })
    await tick()
  })
})

describe('async context of requests', () => {
  it('sends each event in the context of its capture(), whichever request freed the slot', async () => {
    const request = new AsyncLocalStorage<string>()
    const seen: string[] = []
    // As an instrumented fetch (OpenTelemetry, a tenant-aware proxy) would see it.
    const instrumented: Handler = async (call, signal, n) => {
      seen.push(`${call.events[0]?.orgId} sent in ${request.getStore()}`)
      await sleep(10)
      return ack(call, signal, n)
    }
    const { client } = setup(instrumented, { maxConcurrentRequests: 1 })
    for (const org of ['org_A', 'org_B']) request.run(org, () => client.capture(input({ orgId: org, turnId: org })))
    expect(await client.flush()).toMatchObject({ sent: 2 })
    expect(seen).toEqual(['org_A sent in org_A', 'org_B sent in org_B'])
  })
})

describe('AsyncLocalStorage', () => {
  // Before Node 24, an AsyncLocalStorage in use costs every promise in the app's process.
  it('is not used for the default logger, which cannot call back into the SDK', async () => {
    // Earlier tests' reports may still be on their way to their onError: let them land first.
    await tick()
    // The test runner uses AsyncLocalStorage too: count only the SDK's calls.
    const original = AsyncLocalStorage.prototype.run
    let sdkRuns = 0
    AsyncLocalStorage.prototype.run = function (this: AsyncLocalStorage<unknown>, ...args: unknown[]) {
      if (new Error().stack?.includes('src/client.ts')) sdkRuns++
      return (original as (...rest: unknown[]) => unknown).apply(this, args)
    } as typeof original
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const info = vi.spyOn(console, 'info').mockImplementation(() => {})
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      void new LSquare({ apiKey: undefined }) // no key, one warning
      void new LSquare({ apiKey: undefined, logger: console }) // console passed explicitly is the default too
      const dry = new LSquare({ dryRun: true })
      dry.capture(input())
      await dry.flush()
      await tick()
      expect(warn).toHaveBeenCalledTimes(2)
      // The default writes its dry-run line to stderr (console.error), never stdout (console.info).
      expect(info).toHaveBeenCalledTimes(0)
      expect(error).toHaveBeenCalledTimes(1)
      expect(sdkRuns).toBe(0)
      // A custom logger is the app's code: it runs at a depth, so it can't feed itself.
      void new LSquare({ apiKey: undefined, logger: { info() {}, warn() {} } })
      await tick()
      expect(sdkRuns).toBe(1)
    } finally {
      AsyncLocalStorage.prototype.run = original
      warn.mockRestore()
      info.mockRestore()
      error.mockRestore()
    }
  })
})

describe('beforeSend', () => {
  it('sees raw data; its output is checked, redacted and its drops labelled', async () => {
    const seen: string[] = []
    const hook: BeforeSend = async (event) => {
      seen.push(event.question)
      return {
        ...event,
        conversationId: 'hijacked',
        question: `${event.question.replace('Jane Doe', '[NAME_1]')} (bob@acme.io)`,
        toolCalls: [{ tool: 'x' }],
        extra: 'not allowed',
      } as unknown as TurnEvent
    }
    const { client, calls } = setup(ack, { beforeSend: hook })
    client.capture(input({ question: 'Did Jane Doe write?', toolCalls: [{ name: 'q', input: null }] }))
    expect(await client.flush()).toMatchObject({ sent: 1 })
    await tick()
    expect(seen).toEqual(['Did Jane Doe write?'])
    const event = calls[0]?.events[0] as TurnEvent & { extra?: unknown }
    expect(event.question).toBe('Did [NAME_1] write? ([EMAIL_1])')
    expect(event.conversationId).toBe('conv1')
    expect(event.extra).toBeUndefined()
    expect(event.toolCalls).toBeUndefined()
    expect(event.dropped).toEqual({ 'beforeSend.toolCalls[0]': 'missing_name' })
  })

  it('drops the event when it returns null, throws or returns nothing', async () => {
    const hooks: BeforeSend[] = [() => null, () => { throw new Error('boom') }, async () => undefined as unknown as TurnEvent]
    const results = []
    for (const beforeSend of hooks) {
      const { client, calls, errors } = setup(ack, { beforeSend })
      client.capture(input())
      const flush = await client.flush()
      await tick()
      results.push({ flush, calls: calls.length, errors: codes(errors) })
    }
    expect(results).toEqual([
      { flush: { sent: 0, skipped: 1, failed: 0, pending: 0 }, calls: 0, errors: [] },
      { flush: { sent: 0, skipped: 0, failed: 1, pending: 0 }, calls: 0, errors: ['hook_failed'] },
      { flush: { sent: 0, skipped: 0, failed: 1, pending: 0 }, calls: 0, errors: ['hook_failed'] },
    ])
  })

  it('fails closed at the deadline; a late hook keeps its slot and its result is discarded', async () => {
    let releaseFirst: (event: TurnEvent) => void = () => {}
    const started: string[] = []
    const signals: AbortSignal[] = []
    const hook: BeforeSend = (event, { signal }) => {
      started.push(event.question)
      signals.push(signal)
      if (event.question === 'first') return new Promise<TurnEvent>((resolve) => (releaseFirst = resolve))
      return event
    }
    const { client, calls, errors } = setup(ack, { beforeSend: hook, beforeSendTimeoutMs: 30, maxConcurrentHooks: 1 })

    client.capture(input({ question: 'first' }))
    await sleep(60)
    expect(codes(errors)).toEqual(['hook_timeout'])
    expect(errors[0]?.event).toBeUndefined()
    expect(signals[0]?.aborted).toBe(true)

    client.capture(input({ question: 'second' }))
    await sleep(5)
    expect(started).toEqual(['first'])

    releaseFirst({ ...input({ question: 'first' }) } as unknown as TurnEvent)
    expect(await client.flush()).toMatchObject({ sent: 1 })
    await tick()
    expect(started).toEqual(['first', 'second'])
    expect(calls.map((c) => c.events[0]?.question)).toEqual(['second'])
  })

  it('a hook settling after its deadline is refused even when the timer has not run', async () => {
    let release: () => void = () => {}
    const hook: BeforeSend = (event) => new Promise<TurnEvent>((resolve) => (release = () => resolve(event)))
    const { client, calls, errors } = setup(ack, { beforeSend: hook, beforeSendTimeoutMs: 5 })
    client.capture(input())
    await Promise.resolve()
    busyWait(25) // blocks the event loop outside the hook: the deadline timer can't fire
    release()
    await tick()
    expect(codes(errors)).toEqual(['hook_timeout'])
    await client.flush()
    await tick()
    expect(calls).toHaveLength(0)
  })

  it('shutdown drops events waiting for the hook, and later captures', async () => {
    const { client, calls, errors } = setup(ack, { beforeSend: () => new Promise<TurnEvent>(() => {}) })
    client.capture(input())
    await sleep(0)
    expect(await client.shutdown()).toMatchObject({ failed: 1 })
    client.capture(input())
    await tick()
    expect(calls).toHaveLength(0)
    expect(codes(errors)).toEqual(['shutdown', 'shutdown'])
  })
})

describe('limits', () => {
  it('drops the oldest event when the queue is full', async () => {
    const { client, errors } = setup(hanging, { maxQueueEvents: 2 })
    client.capture(input({ question: 'a' }))
    client.capture(input({ question: 'b' }))
    client.capture(input({ question: 'c' }))
    await tick()
    expect(codes(errors)).toEqual(['queue_full'])
    expect(await client.flush({ timeoutMs: 20 })).toMatchObject({ pending: 2 })
    await tick()
  })

  it('counts bytes, not just events', async () => {
    const { client, errors } = setup(hanging, { maxQueueBytes: 600 })
    client.capture(input())
    client.capture(input())
    await tick()
    expect(codes(errors)).toEqual(['queue_full'])
  })

  it('stops early on an input too large to fit, without copying it', async () => {
    const { client, errors } = setup(ack)
    const huge = Array.from({ length: 1_000_000 }, () => 0)
    const started = performance.now()
    client.capture(input({ verification: huge }))
    expect(performance.now() - started).toBeLessThan(100)
    await tick()
    expect(codes(errors)).toEqual(['too_large'])
  })

  it('re-checks the memory budget when a hook makes an event bigger', async () => {
    const grow: BeforeSend = (event) => ({ ...event, answer: 'x'.repeat(3000) })
    const { client, calls, errors } = setup(ack, { beforeSend: grow, maxQueueBytes: 2000 })
    client.capture(input())
    expect(await client.flush()).toMatchObject({ failed: 1 })
    await tick()
    expect(calls).toHaveLength(0)
    expect(codes(errors)).toEqual(['queue_full'])
  })

  it('an expired hook keeps its bytes until it settles', async () => {
    let releaseFirst: () => void = () => {}
    const hook: BeforeSend = (event) =>
      event.question === 'first' ? new Promise<TurnEvent>((resolve) => (releaseFirst = () => resolve(event))) : event
    const { client, calls, errors } = setup(ack, { beforeSend: hook, beforeSendTimeoutMs: 20, maxQueueBytes: 450 })
    client.capture(input({ question: 'first' }))
    await sleep(40)
    client.capture(input({ question: 'second' }))
    await tick()
    expect(codes(errors)).toEqual(['hook_timeout', 'queue_full'])
    releaseFirst()
    await sleep(0)
    client.capture(input({ question: 'third' }))
    expect(await client.flush()).toMatchObject({ sent: 1 })
    await tick()
    expect(calls.map((c) => c.events[0]?.question)).toEqual(['third'])
  })
})

describe('version', () => {
  it("is package.json's, which every event reports as sdk.version", () => {
    const { version } = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')) as { version: string }
    expect(SDK_VERSION).toBe(version)
  })
})
