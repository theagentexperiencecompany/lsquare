import { AsyncLocalStorage } from 'node:async_hooks'
import { Readable } from 'node:stream'
import { inspect } from 'node:util'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { type CallScope, INTERNAL } from '../src/client.js'
import { LSquare, type BeforeSend, type CaptureInput, type LSquareError, type LSquareEvent, type LSquareOptions, type TurnEvent } from '../src/index.js'
import { turnKey } from '../src/normalize.js'

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
  const errors: Array<{ error: LSquareError; event: LSquareEvent | undefined }> = []
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
  vi.unstubAllGlobals()
})

describe('modes and configuration', () => {
  const customer = { toolCalls: [{ name: 'lookup', input: { customerName: 'Jane Roe', address: '1 Main St' } }] }

  it('masks keys given as a Set, with no warning', async () => {
    const { client, calls, logs } = setup(ack, { redactKeys: new Set(['name', 'address']) })
    client.capture(input(customer))
    expect(await client.flush()).toMatchObject({ sent: 1 })
    await tick()
    expect(calls[0]!.events[0]).toMatchObject({ toolCalls: [{ input: { customerName: '[SECRET_1]', address: '[SECRET_2]' } }] })
    expect(logs.warn).toEqual([])
  })

  it("warns once about entries of redactKeys that aren't key names, and masks with the rest", async () => {
    const { client, calls, logs } = setup(ack, { redactKeys: ['name', 7] as unknown as string[] })
    client.capture(input(customer))
    expect(await client.flush()).toMatchObject({ sent: 1 })
    await tick()
    expect(calls[0]!.events[0]).toMatchObject({ toolCalls: [{ input: { customerName: '[SECRET_1]', address: '1 Main St' } }] })
    expect(logs.warn).toEqual(["[lsquare] Part of redactKeys isn't a key name: it is ignored."])
  })

  it.each([[{ name: true }], [42]])('is disabled by redactKeys that are no list of names, %j, and says so', async (redactKeys) => {
    const { client, calls, logs } = setup(ack, { redactKeys: redactKeys as unknown as string[] })
    client.capture(input(customer))
    expect(await client.flush()).toEqual({ sent: 0, skipped: 0, failed: 0, pending: 0 })
    await tick()
    expect(calls).toHaveLength(0)
    expect(logs.warn).toEqual(['[lsquare] redactKeys must be a list of key names: capture is disabled.'])
  })

  it.each([
    ['throws', { [Symbol.iterator]: () => { throw new Error('the configuration failed') } }],
    ["gives results that aren't objects", { [Symbol.iterator]: () => ({ next: () => 0 }) }],
  ])('is disabled by redactKeys whose iteration %s', async (_how, redactKeys) => {
    const { client, calls, logs } = setup(ack, { redactKeys: redactKeys as unknown as string[] })
    client.capture(input(customer))
    await client.flush()
    await tick()
    expect(calls).toHaveLength(0)
    expect(logs.warn).toEqual(['[lsquare] Invalid options: capture is disabled.'])
  })

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
    // Reported by the time flush() resolves: the four that aren't objects, and the one that can't be read.
    expect(codes(errors)).toEqual(['invalid_input', 'invalid_input', 'invalid_input', 'invalid_input', 'redaction_failed'])
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
      kind: 'turn',
      eventId: 'from turnId, hashed',
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
      'from turnId, hashed',
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
    expect(call.init.redirect).toBe('manual')
    expect((call.init.headers as Record<string, string>).authorization).toBe('Bearer lsq_test')
    expect(call.events[0]).toMatchObject({
      eventId: turnKey('org_1', 'conv1', '42'),
      turnId: '42',
      kind: 'turn',
      source: 'live',
      schemaVersion: 2,
      sdk: { name: 'lsquare', version: '0.2.0', language: 'js', runtime: `node/${process.versions.node.split('.')[0]}` },
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
    const [a, b] = calls.flatMap((c) => c.events.map((e) => e.eventId))
    expect(a).toMatch(/^[0-9a-f-]{36}$/)
    expect(b).toMatch(/^[0-9a-f-]{36}$/)
    expect(a).not.toBe(b)
  })

  it('hashes the turn into its eventId, keeping a turnId of any length', async () => {
    const { client, calls } = setup()
    client.capture({ ...input(), turnId: 'x'.repeat(600) })
    client.capture({ ...input(), turnId: 'x'.repeat(600) })
    await client.flush()
    await tick()
    const [a, b] = calls.flatMap((c) => c.events) as TurnEvent[]
    expect(a?.eventId).toMatch(/^k\/[0-9a-f]{64}$/)
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
    expect((errors[0]?.event as TurnEvent | undefined)?.question).toBe('Ask [EMAIL_1]')
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

  it('keeps at most maxConcurrentRequests requests in flight; the others wait, and go together', async () => {
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
    // A quiet server sends each event at once.
    client.capture(input({ turnId: 0 }))
    await tick()
    client.capture(input({ turnId: 1 }))
    await tick()
    expect(calls.map((c) => c.events.length)).toEqual([1, 1])
    // With both slots taken, the next events wait, and the first slot to free up sends them all.
    for (let i = 2; i < 5; i++) client.capture(input({ turnId: i }))
    await tick()
    expect(calls).toHaveLength(2)
    answers.shift()?.()
    await until(() => calls.length === 3)
    expect(calls[2]?.events.map((e) => (e as TurnEvent).turnId)).toEqual(['2', '3', '4'])
    while (answers.length > 0 || inFlight > 0) {
      await until(() => answers.length > 0)
      answers.shift()?.()
    }
    expect(await client.flush()).toMatchObject({ failed: 0, pending: 0 })
    expect(errors).toEqual([])
    expect(peak).toBe(2)
  })

  it('reads and discards the Node stream bodies fetch polyfills return, without retrying', async () => {
    const posted: string[] = []
    const polyfill = (async (_url: unknown, init?: RequestInit) => {
      const event = (JSON.parse(String(init?.body)) as { events: TurnEvent[] }).events[0] as TurnEvent
      posted.push((event as TurnEvent).question as string)
      const accepted = (event as TurnEvent).question === 'accepted'
      const body = Readable.from([accepted ? JSON.stringify({ accepted: [event.eventId] }) : '{"error":"invalid_events"}'])
      return { status: accepted ? 200 : 400, headers: new Headers(), body }
    }) as unknown as typeof globalThis.fetch
    const { client } = setup(ack, { fetch: polyfill })
    client.capture(input({ question: 'accepted' }))
    expect(await client.flush()).toMatchObject({ sent: 1 })
    client.capture(input({ question: 'refused' }))
    expect(await client.flush()).toMatchObject({ failed: 1 })
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

  describe('a timeoutMs that throws when read counts as not given', () => {
    const throwing = {
      get timeoutMs(): number {
        throw new Error('bad option')
      },
    }
    /** A request held until the test lets it answer. */
    const held = () => {
      let release = () => {}
      const gate = new Promise<void>((resolve) => (release = resolve))
      const handler: Handler = async (call, signal, n) => {
        await gate
        return ack(call, signal, n)
      }
      return { handler, release: () => release() }
    }

    it('shutdown() waits as by default, sends what is queued, and still closes the client', async () => {
      const { handler, release } = held()
      const { client, calls, errors } = setup(handler, { flushTimeoutMs: 2_000 })
      client.capture(input({ question: 'before' }))
      const closing = client.shutdown(throwing)
      await until(() => calls.length === 1)
      release()
      expect(await closing).toEqual({ sent: 1, skipped: 0, failed: 0, pending: 0 })
      client.capture(input({ question: 'after' }))
      await tick()
      expect(calls.flatMap((c) => c.events.map((e) => e.question))).toEqual(['before'])
      expect(codes(errors)).toEqual(['shutdown'])
    })

    it('shutdown() given a Proxy whose get trap throws: the same', async () => {
      const { handler, release } = held()
      const { client, calls } = setup(handler, { flushTimeoutMs: 2_000 })
      client.capture(input({ question: 'before' }))
      const options = new Proxy(
        { timeoutMs: 5 },
        {
          get: () => {
            throw new Error('bad option')
          },
        },
      )
      const closing = client.shutdown(options)
      await until(() => calls.length === 1)
      release()
      expect(await closing).toMatchObject({ sent: 1 })
      client.capture(input({ question: 'after' }))
      await tick()
      expect(calls.flatMap((c) => c.events.map((e) => e.question))).toEqual(['before'])
    })

    it('flush() waits its default, then counts and reports what is pending', async () => {
      const { client, errors } = setup(hanging, { flushTimeoutMs: 20 })
      client.capture(input())
      expect(await client.flush(throwing)).toEqual({ sent: 0, skipped: 0, failed: 0, pending: 1 })
      expect(codes(errors)).toEqual(['pending_after_flush'])
      await client.shutdown({ timeoutMs: 0 })
    })
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
          if (event) (event as TurnEvent).question = 'leak jane@acme.io'
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
    client.capture({ ...input(), orgId: 7 as unknown as string })
    client.capture({ ...input(), userId: 'a\u0000b' })
    await tick()
    expect(warn).toEqual(['[lsquare] invalid_id: An ID must be a non-empty string of at most 512 bytes, without NUL; the event was dropped.'])
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
  it('sends each request in the context of its first event, whichever request freed the slot', async () => {
    const request = new AsyncLocalStorage<string>()
    const seen: string[] = []
    // As an instrumented fetch (OpenTelemetry, a tenant-aware proxy) would see it.
    const instrumented: Handler = async (call, signal, n) => {
      seen.push(`${call.events.map((e) => e.orgId).join('+')} sent in ${request.getStore()}`)
      await sleep(10)
      return ack(call, signal, n)
    }
    const { client } = setup(instrumented, { maxConcurrentRequests: 1 })
    request.run('org_A', () => client.capture(input({ orgId: 'org_A', turnId: 'org_A' })))
    await tick()
    // Ready while A's request holds the only slot: sent once it frees, in B's own context.
    for (const org of ['org_B', 'org_C']) request.run(org, () => client.capture(input({ orgId: org, turnId: org })))
    expect(await client.flush()).toMatchObject({ sent: 3 })
    expect(seen).toEqual(['org_A sent in org_A', 'org_B+org_C sent in org_B'])
  })
})

describe('AsyncLocalStorage', () => {
  // Before Node 24, an AsyncLocalStorage in use costs every promise in the app's process.
  it('is not used for the default logger, which cannot call back into the SDK', async () => {
    // The test runner uses AsyncLocalStorage too: count only the SDK's calls. And of those, only
    // the ones that write to this test's own sinks (console, standard error, its custom logger):
    // a client an earlier test made may still deliver a report (a retry's, a flush's) to its own
    // onError or logger, in a run of its own, while this test counts. One does here, on purpose.
    const original = AsyncLocalStorage.prototype.run
    let wrote = false
    let sdkRuns = 0
    let otherRuns = 0
    AsyncLocalStorage.prototype.run = function (this: AsyncLocalStorage<unknown>, ...args: unknown[]) {
      if (!new Error().stack?.includes('src/client.ts')) return (original as (...rest: unknown[]) => unknown).apply(this, args)
      const outer = wrote
      wrote = false
      try {
        return (original as (...rest: unknown[]) => unknown).apply(this, args)
      } finally {
        if (wrote) sdkRuns++
        else otherRuns++
        wrote ||= outer
      }
    } as typeof original
    const mark = () => void (wrote = true)
    const warn = vi.spyOn(console, 'warn').mockImplementation(mark)
    const info = vi.spyOn(console, 'info').mockImplementation(mark)
    const stderr = vi.spyOn(process.stderr, 'write').mockImplementation(() => (mark(), true))
    // Another client's report, on its way to its onError (which writes to none of the sinks).
    const other = setup(hanging)
    try {
      other.client.capture(input())
      await other.client.flush({ timeoutMs: 1 })
      void new LSquare({ apiKey: undefined }) // a local setup without a key: one warning, on stderr
      void new LSquare({ apiKey: undefined, logger: console }) // console passed explicitly: no depth either
      const dry = new LSquare({ dryRun: true })
      dry.capture(input())
      await dry.flush()
      await tick()
      // The default logger writes both lines to standard error; only the explicit console uses console.
      // The dry run's first: its flush() wrote its own client's line, the other's came in its turn.
      expect(stderr.mock.calls.map(([line]) => String(line).slice(0, 18))).toEqual(['[lsquare dry-run] ', '[lsquare] No apiKe'])
      expect(warn).toHaveBeenCalledTimes(1)
      expect(info).toHaveBeenCalledTimes(0)
      // The other client's report reached its onError, in a run, while this test counted.
      expect(codes(other.errors)).toEqual(['pending_after_flush'])
      expect(otherRuns).toBeGreaterThan(0)
      expect(sdkRuns).toBe(0)
      // A custom logger is the app's code: it runs at a depth, so it can't feed itself.
      void new LSquare({ apiKey: undefined, logger: { info: mark, warn: mark } })
      await tick()
      expect(sdkRuns).toBe(1)
    } finally {
      AsyncLocalStorage.prototype.run = original
      warn.mockRestore()
      info.mockRestore()
      stderr.mockRestore()
      await other.client.shutdown({ timeoutMs: 0 })
      await tick()
    }
  })
})

describe('beforeSend', () => {
  it('sees raw data; its output is checked, redacted and its drops labelled', async () => {
    const seen: string[] = []
    const hook: BeforeSend = async (event) => {
      seen.push((event as TurnEvent).question as string)
      return {
        ...event,
        conversationId: 'hijacked',
        question: `${(event as TurnEvent).question?.replace('Jane Doe', '[NAME_1]')} (bob@acme.io)`,
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
    expect((event as TurnEvent).question).toBe('Did [NAME_1] write? ([EMAIL_1])')
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
      started.push((event as TurnEvent).question as string)
      signals.push(signal)
      if ((event as TurnEvent).question === 'first') return new Promise<LSquareEvent>((resolve) => (releaseFirst = resolve))
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
    const hook: BeforeSend = (event) => new Promise<LSquareEvent>((resolve) => (release = () => resolve(event)))
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
    const { client, calls, errors } = setup(ack, { beforeSend: () => new Promise<LSquareEvent>(() => {}) })
    client.capture(input())
    await sleep(0)
    expect(await client.shutdown()).toMatchObject({ failed: 1 })
    client.capture(input())
    await tick()
    expect(calls).toHaveLength(0)
    expect(codes(errors)).toEqual(['shutdown', 'shutdown'])
  })

  it.each([
    ['', ''],
    [' (Workers)', 'Cloudflare-Workers'],
  ])("shutdown aborts a hook still running, never one that has returned, even from the app's code chained on what it awaited%s", async (_, userAgent) => {
    if (userAgent) vi.stubGlobal('navigator', { userAgent })
    let open: () => void = () => {}
    const ready = new Promise<void>((resolve) => (open = resolve))
    const signals: Record<string, AbortSignal> = {}
    const fired: string[] = []
    const hook: BeforeSend = async (event, { signal }) => {
      const question = (event as TurnEvent).question as string
      signals[question] = signal
      signal.addEventListener('abort', () => fired.push(question))
      if (question === 'running') return new Promise<LSquareEvent>(() => {})
      await ready
      return event
    }
    const { client } = setup(ack, { beforeSend: hook })
    client.capture(input({ question: 'returned' }))
    client.capture(input({ question: 'running' }))
    await until(() => Object.keys(signals).length === 2)
    setTimeout(open, 5)
    // Resumed by the same promise right after the hook: it has returned, and its event isn't done yet.
    await ready
    await client.shutdown()
    await sleep(20)
    expect(signals.returned?.aborted).toBe(false)
    expect(signals.running?.aborted).toBe(true)
    expect(fired).toEqual(['running'])
  })

  it.each([
    ['', ''],
    [' (Workers)', 'Cloudflare-Workers'],
  ])("never aborts a hook whose promise has fulfilled, even from the app's code chained on it%s", async (_, userAgent) => {
    if (userAgent) vi.stubGlobal('navigator', { userAgent })
    let signal: AbortSignal | undefined
    let shutdown: Promise<unknown> | undefined
    const { client } = setup(ack, {
      beforeSend: (event, context) => {
        signal = context.signal
        // Fulfilled as it is returned: the app's code chained on it runs before the SDK takes its result.
        const kept = Promise.resolve(event)
        void kept.then(() => (shutdown = client.shutdown()))
        return kept
      },
    })
    client.capture(input({ question: 'kept' }))
    await until(() => shutdown !== undefined)
    await shutdown
    await sleep(20)
    expect(signal?.aborted).toBe(false)
  })

  it('settles a hook whose result is a proxy that throws when asked what it is, and frees its slot', async () => {
    const { client, calls } = setup(ack, {
      maxConcurrentHooks: 1,
      beforeSend: (event) =>
        // A working thenable whose prototype can't be read.
        new Proxy({ then: (resolve: (value: unknown) => void) => resolve(event) }, {
          getPrototypeOf() {
            throw new Error('no prototype')
          },
        }) as unknown as LSquareEvent,
    })
    client.capture(input({ question: 'first' }))
    client.capture(input({ question: 'second' }))
    const result = await client.flush({ timeoutMs: 2_000 })
    expect(result).toMatchObject({ sent: 2, failed: 0, pending: 0 })
    expect(calls.flatMap((call) => call.events.map((event) => (event as TurnEvent).question))).toEqual(['first', 'second'])
  })

  it("fails a hook whose promise can't be read or adopted (its constructor throws), and runs the next one", async () => {
    const { client, calls, errors } = setup(ack, {
      maxConcurrentHooks: 1,
      beforeSend: (event) => {
        if ((event as TurnEvent).question !== 'broken') return event
        const broken = Promise.resolve(event)
        Object.defineProperty(broken, 'constructor', {
          get() {
            throw new Error('no constructor')
          },
        })
        return broken
      },
    })
    client.capture(input({ question: 'broken' }))
    client.capture(input({ question: 'next' }))
    const result = await client.flush({ timeoutMs: 2_000 })
    expect(result).toMatchObject({ sent: 1, failed: 1, pending: 0 })
    expect(calls.flatMap((call) => call.events.map((event) => (event as TurnEvent).question))).toEqual(['next'])
    await until(() => errors.length > 0)
    expect(errors.map(({ error }) => error.code)).toEqual(['hook_failed'])
  })
})

describe('limits', () => {
  it('holds at most 100 of the turns captured inside a call that are still being sent, each until it is', async () => {
    let release = () => {}
    const answers = new Promise<void>((resolve) => (release = resolve))
    const { client } = setup(async (call, signal, n) => {
      await answers
      return ack(call, signal, n)
    })
    const scope: CallScope = { callId: 'call-1', returned: false, agent: undefined, flushRequested: false, turns: new Set(), invoked: false, thrown: undefined, toolResult: undefined, onToolEntry: undefined, released: false }
    client[INTERNAL].calls().run(scope, () => {
      client.capture(input({ question: 'the agent part' }))
      for (let turnId = 1; turnId <= 101; turnId++) client.capture(input({ turnId }))
    })
    // The 101st goes as usual, only not waited for with the call's event.
    expect(scope.turns.size).toBe(100)
    release()
    expect(await client.flush({ timeoutMs: 5_000 })).toMatchObject({ sent: 101 })
    expect(scope.turns.size).toBe(0)
  })

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

  it('shortens an input too large to fit, without copying it', async () => {
    const { client, calls, errors } = setup(ack)
    const huge = Array.from({ length: 1_000_000 }, () => 0)
    const started = performance.now()
    client.capture(input({ verification: huge }))
    expect(performance.now() - started).toBeLessThan(100)
    expect(await client.flush()).toMatchObject({ sent: 1 })
    expect(errors).toEqual([])
    expect((calls[0]?.events[0] as TurnEvent).verification).toHaveLength(100)
    expect(calls[0]?.events[0]?.truncated).toEqual(['verification'])
  })

  it('re-checks the memory budget when a hook makes an event bigger', async () => {
    // Prose: a run of base64 characters would be kept as its size only.
    const grow: BeforeSend = (event) => ({ ...event, answer: 'x '.repeat(1500) })
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
      (event as TurnEvent).question === 'first' ? new Promise<LSquareEvent>((resolve) => (releaseFirst = () => resolve(event))) : event
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

describe('what flush() and shutdown() reported, or waited for, reaches onError before they resolve', () => {
  // A host that waits on them (Workers' waitUntil) may end the request as they resolve, taking the
  // later turns of reports still to come with it.
  it('flush(): the failure of an event it waited for, and a report made before it', async () => {
    const { client, errors } = setup(() => json({}, 400))
    client.capture(input())
    client.capture(input({ orgId: 7 as unknown as string }))
    await client.flush()
    expect(codes(errors)).toEqual(['invalid_id', 'send_failed'])
  })

  it('flush(): what it found still pending', async () => {
    const { client, errors } = setup(hanging)
    client.capture(input())
    expect(await client.flush({ timeoutMs: 1 })).toMatchObject({ pending: 1 })
    expect(codes(errors)).toEqual(['pending_after_flush'])
    await client.shutdown({ timeoutMs: 0 })
  })

  it('shutdown(): what it dropped', async () => {
    const { client, errors } = setup(hanging)
    client.capture(input())
    expect(await client.shutdown({ timeoutMs: 0 })).toMatchObject({ failed: 1 })
    expect(codes(errors)).toEqual(['shutdown'])
  })

  it('flush() reports what is still pending though waiting for calls took all its time', async () => {
    // The clock moves only as the test says, so the wait for calls takes exactly all of it.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] })
    try {
      const { client, errors } = setup(hanging)
      // A call on an instrumented server whose event is never built.
      client[INTERNAL].track('call', new Promise<void>(() => {}))
      client.capture(input())
      const flushed = client.flush({ timeoutMs: 20 })
      await vi.advanceTimersByTimeAsync(20)
      expect(await flushed).toMatchObject({ pending: 2 })
      // The unbuilt call too, by the ID its event will have.
      expect(codes(errors)).toEqual(['pending_after_flush', 'pending_after_flush'])
      expect(errors[1]?.error.eventId).toBe('call')
      await client.shutdown({ timeoutMs: 0 })
    } finally {
      vi.useRealTimers()
    }
  })

  it('flush() reports each call still waiting for identify that it counts as pending, by its call ID', async () => {
    const { client, errors } = setup(hanging)
    // Two calls on an instrumented server whose events are never built (identify still running).
    client[INTERNAL].track('call-1', new Promise<void>(() => {}))
    client[INTERNAL].track('call-2', new Promise<void>(() => {}))
    client.capture(input())
    expect(await client.flush({ timeoutMs: 20 })).toMatchObject({ pending: 3 })
    expect(codes(errors)).toEqual(['pending_after_flush', 'pending_after_flush', 'pending_after_flush'])
    expect(errors.map((e) => e.error.eventId)).toEqual(expect.arrayContaining(['call-1', 'call-2']))
    await client.shutdown({ timeoutMs: 0 })
  })

  it('flush() reports an unbuilt call alone too, and with 0 reports nothing', async () => {
    const { client, errors } = setup(hanging)
    client[INTERNAL].track('call-1', new Promise<void>(() => {}))
    expect(await client.flush({ timeoutMs: 0 })).toMatchObject({ pending: 1 })
    await tick()
    expect(errors).toEqual([])
    expect(await client.flush({ timeoutMs: 20 })).toMatchObject({ pending: 1 })
    expect(errors.map((e) => [e.error.code, e.error.eventId])).toEqual([['pending_after_flush', 'call-1']])
    await client.shutdown({ timeoutMs: 0 })
  })

  it('reports an unbuilt call in the request it was tracked in, whichever request flushes', async () => {
    // The app's request context, as tracing, logging or tenant code keeps it.
    const request = new AsyncLocalStorage<string>()
    const seen: string[] = []
    const { client } = setup(hanging, { onError: (error) => void seen.push(`${error.code} for ${error.eventId} in ${request.getStore()}`) })
    request.run('A', () => client[INTERNAL].track('call-A', new Promise<void>(() => {})))
    expect(await request.run('B', () => client.flush({ timeoutMs: 20 }))).toMatchObject({ pending: 1 })
    expect(seen).toEqual(['pending_after_flush for call-A in A'])
    await client.shutdown({ timeoutMs: 0 })
  })

  it('reports an unbuilt call at the callback depth it was tracked at: past the limit, not at all', async () => {
    const { client, errors } = setup(hanging)
    const internal = client[INTERNAL]
    // Tracked from inside callbacks (identify making a call, say): one and two levels deep.
    internal.callback(() => internal.track('call-1', new Promise<void>(() => {})))
    internal.callback(() => internal.callback(() => internal.track('call-2', new Promise<void>(() => {}))))
    expect(await client.flush({ timeoutMs: 20 })).toMatchObject({ pending: 2 })
    expect(errors.map((e) => [e.error.code, e.error.eventId])).toEqual([['pending_after_flush', 'call-1']])
    await client.shutdown({ timeoutMs: 0 })
  })

  // What a delivered report held, its event copy and the request it was made in, goes with its
  // slot, wherever its own turn still is.
  const request = new AsyncLocalStorage<object>()
  /** Runs `run` in a request of its own: a WeakRef to that request's state, which nothing else holds. */
  const inRequest = (run: () => void) => {
    const state = { requestData: Buffer.alloc(1024) }
    request.run(state, run)
    return new WeakRef(state)
  }
  const pause = setTimeout
  /** How many of `refs` are still alive after ten collections, each in a later real turn. */
  const survivors = async (refs: Array<WeakRef<object>>) => {
    const gc = (globalThis as { gc?: () => void }).gc
    if (gc === undefined) throw new Error('run with --expose-gc')
    for (let i = 0; i < 10; i++) {
      await new Promise((resolve) => pause(resolve, 5))
      gc()
    }
    return refs.filter((ref) => ref.deref() !== undefined).length
  }
  /** Fails every event it receives (400), each reported with a copy of the event: a WeakRef to each copy. */
  const failing = () => {
    const copies: Array<WeakRef<object>> = []
    const { client } = setup(() => json({}, 400), { onError: (_error, event) => void (event && copies.push(new WeakRef(event))) })
    return { client, copies }
  }

  describe("under an app's fake timers, which hold every later turn (vitest's defaults)", () => {
    const realSetTimeout = setTimeout
    /** Whether `promise` settles within 2 s of real time. */
    const settles = (promise: Promise<unknown>) =>
      Promise.race([promise.then(() => true), new Promise<boolean>((resolve) => realSetTimeout(() => resolve(false), 2_000))])
    afterEach(() => vi.useRealTimers())

    it('flush() and shutdown() wait for no turn when nothing was reported', async () => {
      vi.useFakeTimers()
      const { client, calls } = setup()
      client.capture(input())
      expect(await settles(client.flush())).toBe(true)
      expect(calls).toHaveLength(1)
      client.capture(input({ question: 'second' }))
      expect(await settles(client.shutdown())).toBe(true)
    })

    it('shutdown() without an apiKey, as in an afterEach, waits for no turn, though its warning has one to come', async () => {
      vi.useFakeTimers()
      const warned: string[] = []
      const client = new LSquare({ logger: { info() {}, warn: (line) => void warned.push(line) } })
      expect(await settles(client.shutdown())).toBe(true)
      expect(warned).toEqual([])
    })

    it("a dry run's flush() and shutdown() write its lines themselves, and settle", async () => {
      vi.useFakeTimers()
      const lines: string[] = []
      const client = new LSquare({ dryRun: true, logger: { info: (line) => void lines.push(line), warn: (line) => void lines.push(line) } })
      client.capture(input())
      expect(await settles(client.flush())).toBe(true)
      expect(lines).toEqual([expect.stringMatching(/^\[lsquare dry-run\] /)])
      client.capture(input({ question: 'second' }))
      expect(await settles(client.shutdown())).toBe(true)
      expect(lines).toHaveLength(2)
      // Their own turns, should the clock run them after all, do nothing.
      vi.runAllTimers()
      expect(lines).toHaveLength(2)
    })

    it("flush() after the default logger's email-like userId warning writes it, and settles", async () => {
      // A fresh copy of the SDK: the warning is given once per copy.
      vi.resetModules()
      const { LSquare: Fresh } = await import('../src/index.js')
      const stderr = vi.spyOn(process.stderr, 'write').mockImplementation(() => true)
      try {
        vi.useFakeTimers()
        const fetch = (async (_url: string | URL | Request, init?: RequestInit) => {
          const events = JSON.parse(String(init?.body)).events as TurnEvent[]
          return json({ accepted: events.map((e) => e.eventId), duplicates: [], rejected: [] })
        }) as typeof globalThis.fetch
        const client = new Fresh({ apiKey: 'lsq_test', endpoint: 'https://ingest.test/', fetch })
        client.capture(input({ userId: 'jane@acme.io' }))
        const flushed = client.flush()
        expect(await settles(flushed)).toBe(true)
        expect(await flushed).toMatchObject({ sent: 1 })
        expect(stderr.mock.calls.map(([line]) => String(line))).toEqual([expect.stringContaining('A userId looks like an email address')])
      } finally {
        stderr.mockRestore()
      }
    })

    it('an onError that awaits flush() is run by flush(), and both settle', async () => {
      vi.useFakeTimers()
      const inner: Array<Promise<boolean>> = []
      const { client, errors } = setup(ack, {
        onError: async (error) => {
          errors.push({ error, event: undefined })
          inner.push(settles(client.flush()))
          await inner.at(-1)
        },
      })
      client.capture(input({ orgId: 7 as unknown as string }))
      expect(await settles(client.flush())).toBe(true)
      expect(codes(errors)).toEqual(['invalid_id'])
      expect(await Promise.all(inner)).toEqual([true])
    })

    // The count of turns still to come drops as each starts: on Workers, every later wait would
    // otherwise wait a turn, which the clock holds.
    it.each(['Node', 'Workers'])('on %s, a report and a log line that had their turn leave none to wait for', async (runtime) => {
      if (runtime === 'Workers') vi.stubGlobal('navigator', { userAgent: 'Cloudflare-Workers' })
      // A log line too, warning about redactKeys.
      const { client, errors, logs } = setup(ack, { redactKeys: ['name', 7] as unknown as string[] })
      client.capture(input({ conversationId: 7 as unknown as string }))
      await client.flush()
      expect(codes(errors)).toEqual(['invalid_id'])
      expect(logs.warn).toEqual(["[lsquare] Part of redactKeys isn't a key name: it is ignored."])
      vi.useFakeTimers()
      client.capture(input())
      expect(await settles(client.flush())).toBe(true)
      expect(await settles(client.shutdown())).toBe(true)
    })

    it('the reports flush() delivered keep nothing alive through their own turns, which it takes off the clock', async () => {
      vi.useFakeTimers()
      const { client, copies } = failing()
      const state = inRequest(() => {
        for (let turnId = 1; turnId <= 10; turnId++) client.capture(input({ turnId }))
      })
      expect(await settles(client.flush())).toBe(true)
      expect(copies).toHaveLength(10)
      // Their own turns are gone, so none runs a handler again.
      expect(vi.getTimerCount()).toBe(0)
      vi.runAllTimers()
      expect(copies).toHaveLength(10)
      expect(await survivors([...copies, state])).toBe(0)
    })

    describe('a client made on the real clock, ten minutes in, which the fake one sets back to 0', () => {
      /** Makes the client, and runs `first` with it, while performance.now() reads ten minutes; then fakes the timers. */
      const tenMinutesIn = async <T>(make: () => T, first?: (made: T) => Promise<unknown>): Promise<T> => {
        const uptime = vi.spyOn(performance, 'now').mockReturnValue(600_000)
        try {
          const made = make()
          await first?.(made)
          return made
        } finally {
          uptime.mockRestore()
          vi.useFakeTimers()
        }
      }

      it('still delivers a report, and a log line', async () => {
        const { client, errors, logs } = await tenMinutesIn(() => setup(ack, { dryRun: true }))
        client.capture(input())
        client.capture(input({ orgId: 7 as unknown as string }))
        expect(await settles(client.flush())).toBe(true)
        expect(codes(errors)).toEqual(['invalid_id'])
        expect(logs.info).toEqual([expect.stringMatching(/^\[lsquare dry-run\] /)])
      })

      it('the default logger still writes the next report, though it wrote one less than a minute before on that clock', async () => {
        const stderr = vi.spyOn(process.stderr, 'write').mockImplementation(() => true)
        try {
          const client = await tenMinutesIn(
            () => new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test/', fetch: hanging as unknown as typeof fetch }),
            async (made) => {
              made.capture(input({ orgId: 7 as unknown as string }))
              await made.flush()
            },
          )
          expect(stderr).toHaveBeenCalledTimes(1)
          client.capture(input({ conversationId: 7 as unknown as string }))
          expect(await settles(client.flush())).toBe(true)
          expect(stderr.mock.calls.map(([line]) => String(line).slice(0, 21))).toEqual(['[lsquare] invalid_id:', '[lsquare] invalid_id:'])
        } finally {
          stderr.mockRestore()
        }
      })
    })

    describe('a report whose turn the fake clock of an earlier test threw away', () => {
      // One client for these tests, as an app's module-level one, each test with fake timers that
      // the afterEach above takes away, and with them every turn they held. Made on the first
      // test's fake clock, whose performance.now() starts at 0, as each test's does: its rate limit
      // sees no time go back.
      let lsquare: ReturnType<typeof setup>
      beforeEach(() => vi.useFakeTimers())

      it('is reported in one test, with no flush()', () => {
        lsquare = setup()
        lsquare.client.capture(input({ orgId: 7 as unknown as string }))
        expect(lsquare.errors).toEqual([])
      })

      it('reaches onError at the next flush(), which settles', async () => {
        expect(await settles(lsquare.client.flush())).toBe(true)
        expect(codes(lsquare.errors)).toEqual(['invalid_id'])
      })

      it('leaves no turn to wait for after that', async () => {
        lsquare.client.capture(input())
        expect(await settles(lsquare.client.flush())).toBe(true)
        expect(lsquare.calls).toHaveLength(1)
      })

      it('nor for shutdown()', async () => {
        expect(await settles(lsquare.client.shutdown())).toBe(true)
        expect(codes(lsquare.errors)).toEqual(['invalid_id'])
      })
    })
  })

  // On Node, flush() runs the turns still to come itself: from a microtask, since it may be called
  // inside capture() (from an input's getter) or from a callback, each in the context it was made
  // in, and once.
  it('flush() called from an input getter inside capture() runs no onError there', async () => {
    let capturing = false
    const during: boolean[] = []
    const { client } = setup(ack, { onError: () => void during.push(capturing) })
    client.capture(input({ orgId: 7 as unknown as string }))
    let flushed: Promise<unknown> | undefined
    const reading = {
      ...input(),
      get question() {
        flushed ??= client.flush()
        return 'q'
      },
    }
    capturing = true
    client.capture(reading)
    capturing = false
    expect(flushed).toBeDefined()
    await flushed
    expect(during).toEqual([false])
  })

  it("runs a report in the context it was made in, though another request's flush() delivers it", async () => {
    const request = new AsyncLocalStorage<string>()
    const seen: Array<string | undefined> = []
    const { client } = setup(ack, { onError: () => void seen.push(request.getStore()) })
    request.run('A', () => client.capture(input({ orgId: 7 as unknown as string })))
    await request.run('B', () => client.flush())
    expect(seen).toEqual(['A'])
  })

  it("an async onError that flush() started isn't started again by its own turn", async () => {
    const settle: Array<() => void> = []
    const { client } = setup(ack, { onError: () => new Promise<void>((resolve) => settle.push(resolve)) })
    client.capture(input({ orgId: 7 as unknown as string }))
    await client.flush()
    expect(settle).toHaveLength(1)
    await tick()
    expect(settle).toHaveLength(1)
    for (const resolve of settle) resolve()
  })

  it("flush() from onError runs none of the other reports: they keep their own turns", async () => {
    const seen: number[] = []
    let started = 0
    const { client } = setup(ack, {
      onError: async () => {
        const n = ++started
        if (n > 1) return
        await client.flush()
        seen.push(started)
      },
    })
    for (let i = 0; i < 3; i++) client.capture(input({ orgId: 7 as unknown as string }))
    await tick()
    expect(started).toBe(3)
    // Its flush() resolved before the second report's turn.
    expect(seen).toEqual([1])
  })

  it('a turn that has run keeps no event copy or request alive, though something else still holds it', async () => {
    // As an app's instrumentation might: it keeps every callback it was given.
    const kept: unknown[] = []
    const real = setImmediate
    vi.stubGlobal('setImmediate', (callback: () => void) => {
      kept.push(callback)
      return real(callback)
    })
    const { client, copies } = failing()
    const state = inRequest(() => {
      for (let turnId = 1; turnId <= 10; turnId++) client.capture(input({ turnId }))
    })
    await client.flush()
    expect(copies).toHaveLength(10)
    expect(kept.length).toBeGreaterThanOrEqual(10)
    expect(await survivors([...copies, state])).toBe(0)
  })

  it.each(['Node', 'Workers'])('on %s, a turn that has run keeps neither its client nor what that client holds alive, though something else still holds it', async (runtime) => {
    if (runtime === 'Workers') vi.stubGlobal('navigator', { userAgent: 'Cloudflare-Workers' })
    const kept: unknown[] = []
    const real = setImmediate
    vi.stubGlobal('setImmediate', (callback: () => void) => {
      kept.push(callback)
      return real(callback)
    })
    // A client made for one request, whose onError and logger hold that request.
    const made = async () => {
      const request = { body: Buffer.alloc(1024 * 1024) }
      const client = new LSquare({
        dryRun: true,
        onError: () => void request.body,
        logger: { info: () => void request.body, warn: () => void request.body },
      })
      // A dry-run line, and a report.
      client.capture(input())
      client.capture(input({ orgId: 7 as unknown as string }))
      await client.flush()
      return [new WeakRef(client), new WeakRef(request)]
    }
    const refs = await made()
    expect(kept.length).toBeGreaterThanOrEqual(2)
    expect(await survivors(refs)).toBe(0)
  })

  it('an async onError still running keeps neither its client nor what that client holds alive', async () => {
    // Its promise lives as long as the test keeps its resolver.
    const settle: Array<() => void> = []
    const made = async () => {
      const request = { body: Buffer.alloc(1024 * 1024) }
      const client = new LSquare({
        apiKey: 'lsq_test',
        endpoint: 'https://ingest.test/',
        fetch: (async () => json({})) as typeof fetch,
        onError: () => new Promise<void>((resolve) => settle.push(resolve)),
        logger: { info: () => void request.body, warn: () => void request.body },
      })
      client.capture(input({ orgId: 7 as unknown as string }))
      await client.flush()
      return [new WeakRef(client), new WeakRef(request)]
    }
    const refs = await made()
    expect(settle).toHaveLength(1)
    expect(await survivors(refs)).toBe(0)
    for (const resolve of settle) resolve()
  })
})
