import { AsyncLocalStorage } from 'node:async_hooks'
import { Readable } from 'node:stream'
import vm from 'node:vm'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { LSquare, type BeforeSend, type LSquareError, type LSquareOptions, type TurnEvent } from '../src/index.js'
import { jsonLength, readFields, readSettings, silence, toJson } from '../src/json.js'

// The SDK runs inside the customer's process: nothing it does, and nothing the customer's data or
// environment does to it, may crash, hang or bloat that process.

const silent = { info() {}, warn() {} }
const valid = (extra: Record<string, unknown> = {}) => ({ conversationId: 'c', turnId: `t${Math.random()}`, orgId: 'o', toolName: 't', question: 'q', ...extra })
const ack = (async (_url: string, init: RequestInit) => {
  const { events } = JSON.parse(String(init.body)) as { events: { eventId: string }[] }
  return new Response(JSON.stringify({ accepted: events.map((e) => e.eventId), duplicates: [], rejected: [] }))
}) as unknown as typeof fetch

/** Polls until `check()` holds, for up to two seconds. */
async function until(check: () => boolean): Promise<void> {
  for (let i = 0; i < 400 && !check(); i++) await new Promise((resolve) => setTimeout(resolve, 5))
}

async function unhandledDuring(run: () => Promise<void> | void): Promise<unknown[]> {
  const seen: unknown[] = []
  const listener = (reason: unknown) => void seen.push(reason)
  process.on('unhandledRejection', listener)
  try {
    await run()
    await new Promise((resolve) => setTimeout(resolve, 50))
  } finally {
    process.off('unhandledRejection', listener)
  }
  return seen
}

describe('promises the SDK gets from the app’s data', () => {
  it('never become unhandled rejections: async toJSON, getters, tool-call slots, metadata', async () => {
    const reject = () => Promise.reject(new Error('secret content'))
    const sent: unknown[] = []
    const seen = await unhandledDuring(async () => {
      const client = new LSquare({
        apiKey: 'k',
        endpoint: 'https://x.test',
        logger: silent,
        onError() {},
        fetch: (async (url: string, init: RequestInit) => {
          sent.push(...(JSON.parse(String(init.body)) as { events: unknown[] }).events)
          return ack(url, init)
        }) as unknown as typeof fetch,
      })
      const getter = (key: string) => Object.defineProperty(valid(), key, { enumerable: true, get: reject })
      client.capture(valid({ verification: { async toJSON() { throw new Error('secret content') } } }))
      client.capture(valid({ latency: { nested: { toJSON: reject } } }))
      for (const key of ['answer', 'turnId', 'toolCalls', 'usage', 'client', 'error', 'metadata', 'latency']) client.capture(getter(key))
      const slot: unknown[] = [{ name: 'ok' }]
      Object.defineProperty(slot, 1, { enumerable: true, get: reject })
      client.capture(valid({ toolCalls: slot }))
      client.capture(valid({ toolCalls: [Object.defineProperty({}, 'name', { enumerable: true, get: reject })] }))
      client.capture(valid({ usage: Object.defineProperty({}, 'inputTokens', { enumerable: true, get: reject }) }))
      client.capture(valid({ client: Object.defineProperty({ name: 'x' }, 'version', { enumerable: true, get: reject }) }))
      client.capture(valid({ error: Object.defineProperty({ message: 'm' }, 'type', { enumerable: true, get: reject }) }))
      client.capture(valid({ metadata: Object.defineProperty({}, 'k', { enumerable: true, get: reject }) }))
      await client.flush({ timeoutMs: 2000 })
      await client.shutdown()
    })
    expect(seen).toEqual([])
    expect(sent.length).toBeGreaterThanOrEqual(14)
    expect(JSON.stringify(sent)).not.toContain('secret content')
  })

  it('nor do toJSON getters, proxies, the slot read as the budget runs out, or beforeSend output', async () => {
    const reject = () => Promise.reject(new Error('secret content'))
    // Every slot a getter, so whichever one is read as the work budget runs out is one too.
    const everySlot = (length: number) => {
      const slots: unknown[] = new Array(length)
      for (let i = 0; i < length; i++) Object.defineProperty(slots, i, { enumerable: true, get: reject })
      return slots
    }
    const seen = await unhandledDuring(async () => {
      const client = new LSquare({
        apiKey: 'k',
        endpoint: 'https://x.test',
        fetch: ack,
        logger: silent,
        onError() {},
        // A field the SDK doesn't keep: never read.
        beforeSend: (event) => Object.defineProperty({ ...event }, 'extra', { enumerable: true, get: reject }),
      })
      client.capture(valid({ verification: Object.defineProperty({}, 'toJSON', { get: reject }) }))
      client.capture(valid({ latency: new Proxy({}, { get: reject }) }))
      client.capture(valid({ verification: everySlot(140_000) }))
      // Object keys too: one read just as the budget runs out, and a `__proto__` key, which is skipped.
      const getterKey = (base: object, key: string) => Object.defineProperty(base, key, { enumerable: true, get: reject })
      client.capture(valid({ verification: getterKey({ notes: 'x'.repeat(260_000) }, 'k'.repeat(5_000)) }))
      client.capture(valid({ latency: getterKey({}, '__proto__') }))
      toJson(everySlot(100), { remaining: 50 })
      jsonLength(everySlot(100), { remaining: 50 })
      toJson(getterKey({}, 'k'.repeat(60)), { remaining: 50 })
      jsonLength(getterKey({}, 'k'.repeat(60)), { remaining: 50 })
      await client.flush({ timeoutMs: 2000 })
      await client.shutdown()
    })
    expect(seen).toEqual([])
  })

  it("nor does a Proxy's getPrototypeOf that answers with one each time: a captured input, an error, a field's object, a tool call", async () => {
    let traps = 0
    /** `target` behind a Proxy whose getPrototypeOf, the app's code, returns a new rejected promise each time. */
    const rejecting = <T extends object>(target: T) => new Proxy(target, { getPrototypeOf: () => (traps++, Promise.reject(new Error('secret content'))) })
    const sent: unknown[] = []
    const seen = await unhandledDuring(async () => {
      const client = new LSquare({
        apiKey: 'k',
        endpoint: 'https://x.test',
        logger: silent,
        onError() {},
        fetch: (async (url: string, init: RequestInit) => {
          sent.push(...(JSON.parse(String(init.body)) as { events: unknown[] }).events)
          return ack(url, init)
        }) as unknown as typeof fetch,
      })
      // Not a plain object, so dropped as invalid_input.
      client.capture(rejecting(valid()))
      // An error's fields are read from its prototypes too: here the promise, then Promise.prototype.
      client.capture(valid({ error: rejecting(new Error('lookup failed')) }))
      client.capture(valid({ client: rejecting({ name: 'c' }), usage: rejecting({ inputTokens: 1 }), metadata: rejecting({ k: 1 }), toolCalls: [rejecting({ name: 'n' })], verification: { nested: rejecting({}) } }))
      await client.flush({ timeoutMs: 2000 })
      await client.shutdown()
    })
    // Each Proxy asked at least once, to tell a plain object, the error too, to find its class's
    // fields (Codex's sixth review: a Proxy reads as what it wraps, its traps asked as JavaScript
    // asks them; round 5 asked the error nothing).
    expect(traps).toBeGreaterThanOrEqual(8)
    expect(seen).toEqual([])
    expect(sent).toHaveLength(2)
    expect(JSON.stringify(sent)).not.toContain('secret content')
  })

  describe('a promise from another realm, or with its prototype changed', () => {
    // A vm context's, or Node's own seen from a test runner's context: not `instanceof` this
    // realm's Promise.
    const foreign = () => vm.runInNewContext('Promise.reject(new Error("secret content"))') as Promise<never>

    it('is not instanceof Promise, so these tests mean something', () => {
      const promise = foreign()
      expect(promise instanceof Promise).toBe(false)
      Promise.prototype.then.call(promise, undefined, () => {})
    })

    it('read by silence(), toJson(), jsonLength(), readFields() and readSettings(): never an unhandled rejection', async () => {
      const seen = await unhandledDuring(() => {
        silence(foreign())
        toJson({ k: foreign() })
        toJson([foreign()])
        jsonLength({ k: foreign() }, { remaining: 1000 })
        readFields(Object.defineProperty({}, 'answer', { enumerable: true, get: foreign }), ['answer'], true)
        readSettings(Object.defineProperty({}, 'endpoint', { enumerable: true, get: foreign }), ['endpoint'])
        const reset = Promise.reject(new Error('secret content'))
        Object.setPrototypeOf(reset, Object.prototype)
        silence(reset)
        const nulled = Promise.reject(new Error('secret content'))
        Object.setPrototypeOf(nulled, null)
        silence(nulled)
      })
      expect(seen).toEqual([])
    })

    it("from a getter on a captured field: dropped as wrong_type, never an unhandled rejection", async () => {
      const events: TurnEvent[] = []
      const seen = await unhandledDuring(async () => {
        const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch: ack, logger: silent, onError() {}, beforeSend: (event) => (events.push(event as TurnEvent), null) })
        client.capture(Object.defineProperty(valid(), 'answer', { enumerable: true, get: foreign }))
        client.capture(valid({ metadata: Object.defineProperty({}, 'k', { enumerable: true, get: foreign }) }))
        expect(await client.flush({ timeoutMs: 2000 })).toMatchObject({ skipped: 2 })
        await client.shutdown({ timeoutMs: 0 })
      })
      expect(seen).toEqual([])
      expect(events[0]?.dropped).toEqual({ answer: 'wrong_type' })
    })

    it("runs none of the app's code to tell: no Proxy trap, and no other thenable's then (a query builder runs its query)", async () => {
      let traps = 0
      const proxy = new Proxy({}, { getPrototypeOf: () => (traps++, Promise.reject(new Error('from a trap'))) })
      let called = 0
      const thenable = { then: () => void called++ }
      const seen = await unhandledDuring(() => {
        silence(proxy)
        silence(thenable)
        toJson({ k: thenable })
      })
      expect(traps).toBe(0)
      expect(called).toBe(0)
      expect(seen).toEqual([])
    })
  })

  describe('redactKeys', () => {
    const reject = () => Promise.reject(new Error('bad redact key'))
    const construct = (redactKeys: unknown, logger: LSquareOptions['logger'] = silent) => new LSquare({ dryRun: true, logger, redactKeys: redactKeys as string[] })

    it('a rejected promise read from an entry, a generator, an iterator or its results: none unhandled, each entry skipped', async () => {
      const warnings: string[] = []
      const logger = { info() {}, warn: (m: string) => void warnings.push(m) }
      const seen = await unhandledDuring(() => {
        construct(Object.defineProperty(['token', 'name'], 0, { enumerable: true, get: reject }), logger)
        // Each promise made only as the SDK reads on.
        construct(
          (function* () {
            yield reject()
            yield 'name'
          })(),
          logger,
        )
        const results = [reject, () => ({ done: false, value: 'name' }), () => ({ done: true, value: undefined })]
        construct({ [Symbol.iterator]: () => ({ next: () => results.shift()?.() }) }, logger)
      })
      expect(seen).toEqual([])
      expect(warnings.filter((m) => m.includes("Part of redactKeys isn't a key name"))).toHaveLength(3)
    })

    it('the rest of what it reads: the iterator method, the iterator, next, return, done, an array’s length', async () => {
      const seen = await unhandledDuring(() => {
        construct(Object.defineProperty({}, Symbol.iterator, { get: reject }))
        // An iterator method made async by mistake: the iterator is a rejected promise.
        construct({ async [Symbol.iterator]() { throw new Error('bad redact key') } })
        construct({ [Symbol.iterator]: () => Object.defineProperty({}, 'next', { get: reject }) })
        let n = 0
        const done = { [Symbol.iterator]: () => ({ next: () => (n++ === 0 ? { get done() { return reject() }, value: 'name' } : { done: true }) }) }
        construct(done)
        // Endless, so let go of at the limit: its return is read, called, and what it returns too.
        const endless = (close: () => unknown) => ({ [Symbol.iterator]: () => Object.defineProperty({ next: () => ({ done: false, value: 'name' }) }, 'return', { get: close }) })
        construct(endless(reject))
        construct(endless(() => reject))
        construct(new Proxy(['name'], { get: (target, key) => (key === 'length' ? reject() : Reflect.get(target, key)) }))
      })
      expect(seen).toEqual([])
    })

    it('reads Symbol.iterator once, as JavaScript does', () => {
      let reads = 0
      const keys = Object.defineProperty({}, Symbol.iterator, {
        get: () => (
          reads++,
          function* () {
            yield 'name'
          }
        ),
      })
      construct(keys)
      expect(reads).toBe(1)
    })

    it('the names after a skipped entry still mask', async () => {
      const events: TurnEvent[] = []
      const seen = await unhandledDuring(async () => {
        const redactKeys = Object.defineProperty(['token', 'name'], 0, { enumerable: true, get: reject })
        const client = new LSquare({ dryRun: true, logger: silent, redactKeys, beforeSend: (event) => (events.push(event as TurnEvent), null) })
        client.capture(valid({ metadata: { name: 'Ada Lovelace' } }))
        await client.flush({ timeoutMs: 2000 })
      })
      expect(seen).toEqual([])
      expect(events).toHaveLength(1)
      expect(JSON.stringify(events)).not.toContain('Ada Lovelace')
    })
  })
})

/** A 2xx answer whose body never yields, from a fetch that ignores the abort, as some wrappers do. */
function stalled() {
  const bodies: Array<{ pulled: boolean; cancelled: boolean }> = []
  const respond = () => {
    const body = { pulled: false, cancelled: false }
    bodies.push(body)
    const stream = new ReadableStream<Uint8Array>({
      pull() {
        body.pulled = true
        return new Promise<void>(() => {})
      },
      cancel() {
        body.cancelled = true
      },
    })
    return new Response(stream, { status: 200 })
  }
  return { bodies, respond }
}

/** A client whose fetch, ignoring the abort, answers with `respond`. */
function ignoring(respond: () => Response | Promise<Response>, options: LSquareOptions) {
  const errors: LSquareError[] = []
  const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch: (async () => respond()) as unknown as typeof fetch, logger: silent, onError: (error) => void errors.push(error), ...options })
  return { client, errors }
}

describe('a 2xx body still being read, from a fetch that ignores the abort', () => {
  // Left alone, its read and socket would keep a short-lived process alive.
  it("is cancelled at the request's timeout", async () => {
    const { bodies, respond } = stalled()
    const { client, errors } = ignoring(respond, { requestTimeoutMs: 50, maxAttempts: 1, maxConcurrentRequests: 1 })
    for (let i = 0; i < 3; i++) {
      client.capture(valid({ question: `q${i}` }))
      expect(await client.flush({ timeoutMs: 1_000 })).toMatchObject({ failed: 1, pending: 0 })
    }
    await until(() => bodies.every((body) => body.cancelled))
    expect(bodies).toEqual(Array(3).fill({ pulled: true, cancelled: true }))
    expect(errors.map((error) => [error.code, error.message])).toEqual(Array(3).fill(['send_failed', 'Network error after 1 attempts.']))
  })

  it('is cancelled when shutdown() drops its event, long before the timeout', async () => {
    const { bodies, respond } = stalled()
    const { client } = ignoring(respond, { requestTimeoutMs: 60_000 })
    client.capture(valid())
    await until(() => bodies[0]?.pulled === true)
    expect(bodies).toEqual([{ pulled: true, cancelled: false }])
    expect(await client.shutdown({ timeoutMs: 0 })).toMatchObject({ failed: 1 })
    await until(() => bodies[0]?.cancelled === true)
    expect(bodies).toEqual([{ pulled: true, cancelled: true }])
  })

  it("is cancelled when its response comes only after the request's timeout", async () => {
    const { bodies, respond } = stalled()
    let answer = () => {}
    const held = new Promise<void>((resolve) => (answer = resolve))
    // Answers only once the test lets it, after the event has failed.
    const { client, errors } = ignoring(() => held.then(respond), { requestTimeoutMs: 50, maxAttempts: 1 })
    client.capture(valid())
    expect(await client.flush({ timeoutMs: 1_000 })).toMatchObject({ failed: 1, pending: 0 })
    expect(errors.map((error) => error.code)).toEqual(['send_failed'])
    expect(bodies).toEqual([])
    answer()
    await until(() => bodies[0]?.cancelled === true)
    expect(bodies.map((body) => body.cancelled)).toEqual([true])
  })

  it("as a Node stream, as some fetch polyfills return, is destroyed at the request's timeout", async () => {
    const streams: Readable[] = []
    const respond = () => {
      // Never pushes: read() does nothing.
      const body = new Readable({ read() {} })
      streams.push(body)
      return { status: 200, type: 'basic', headers: new Headers(), body } as unknown as Response
    }
    const { client, errors } = ignoring(respond, { requestTimeoutMs: 50, maxAttempts: 1 })
    client.capture(valid())
    expect(await client.flush({ timeoutMs: 1_000 })).toMatchObject({ failed: 1, pending: 0 })
    await until(() => streams[0]?.destroyed === true)
    expect(streams.map((stream) => stream.destroyed)).toEqual([true])
    expect(errors.map((error) => error.code)).toEqual(['send_failed'])
  })
})

describe('response bodies from fetch polyfills (Node streams)', () => {
  it('one that errors while being discarded never reaches the app as an uncaught exception', async () => {
    const thrown: unknown[] = []
    const listener = (error: unknown) => void thrown.push(error)
    process.on('uncaughtException', listener)
    try {
      const body = new Readable({
        read() {},
        destroy(_error, callback) {
          callback(new Error('body cleanup failed'))
        },
      })
      const client = new LSquare({
        apiKey: 'k',
        endpoint: 'https://x.test',
        logger: silent,
        onError() {},
        fetch: (async () => ({ status: 401, headers: new Headers(), body })) as unknown as typeof fetch,
      })
      client.capture(valid())
      expect(await client.flush()).toMatchObject({ failed: 1 })
      await new Promise((resolve) => setTimeout(resolve, 50))
    } finally {
      process.off('uncaughtException', listener)
    }
    expect(thrown).toEqual([])
  })

  it('an oversized string chunk is refused before it is copied', async () => {
    // 0.2 reads bodies with TextEncoder, which Workers have too: that's where a chunk would be copied.
    const from = vi.spyOn(TextEncoder.prototype, 'encode')
    try {
      const client = new LSquare({
        apiKey: 'k',
        endpoint: 'https://x.test',
        logger: silent,
        onError() {},
        fetch: (async () => ({ status: 200, headers: new Headers(), body: Readable.from(['x'.repeat(8 * 1024 * 1024)]) })) as unknown as typeof fetch,
      })
      client.capture(valid())
      expect(await client.flush()).toMatchObject({ failed: 1 })
      expect(from.mock.calls.filter(([value]) => typeof value === 'string' && value.length > 64 * 1024)).toEqual([])
    } finally {
      from.mockRestore()
    }
  })
})

describe('async context of a finished event', () => {
  // It holds the app's request, tenant or trace data: it must not stay reachable through the slot
  // handoffs that start later events while the queue stays busy.
  it.each(['request', 'hook'])('is released through %s slot handoffs', async (slot) => {
    const gc = (globalThis as { gc?: () => void }).gc
    if (gc === undefined) throw new Error('run with --expose-gc')
    const turns: Array<() => void> = []
    const waitTurn = () => new Promise<void>((resolve) => turns.push(resolve))
    const until = async (check: () => boolean) => {
      for (let i = 0; i < 200 && !check(); i++) await new Promise((resolve) => setTimeout(resolve, 5))
    }
    const hook: BeforeSend = async (event) => {
      await waitTurn()
      return event
    }
    const slowAck = (async (url: string, init: RequestInit) => {
      await waitTurn()
      return ack(url, init)
    }) as unknown as typeof fetch
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      logger: silent,
      onError() {},
      maxConcurrentRequests: 1,
      maxConcurrentHooks: 1,
      ...(slot === 'hook' ? { beforeSend: hook, fetch: ack } : { fetch: slowAck }),
    })
    const request = new AsyncLocalStorage<object>()
    let first!: WeakRef<object>
    ;(() => {
      const store = { requestData: Buffer.alloc(1024) }
      first = new WeakRef(store)
      request.run(store, () => client.capture(valid({ turnId: 'A' })))
    })()
    client.capture(valid({ turnId: 'B' }))
    client.capture(valid({ turnId: 'C' }))
    for (let handoff = 0; handoff < 2; handoff++) {
      await until(() => turns.length > 0)
      turns.shift()?.() // A, then B, finishes; the next starts from its completion
    }
    await until(() => turns.length > 0) // C is in progress
    for (let i = 0; i < 10; i++) {
      await new Promise((resolve) => setTimeout(resolve, 5))
      gc()
    }
    expect(first.deref()).toBeUndefined()
    turns.shift()?.()
    await client.flush()
    await client.shutdown()
    request.disable()
  })
})

describe('binary data', () => {
  it('is dropped as not_json without being copied', async () => {
    const gc = (globalThis as { gc?: () => void }).gc
    if (gc === undefined) throw new Error('run with --expose-gc')
    const sent: TurnEvent[] = []
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      logger: silent,
      onError() {},
      fetch: (async (url: string, init: RequestInit) => {
        sent.push(...(JSON.parse(String(init.body)) as { events: TurnEvent[] }).events)
        return ack(url, init)
      }) as unknown as typeof fetch,
    })
    // Buffer's toJSON would copy every byte into an array: 8 times the size, before any budget check.
    const file = Buffer.alloc(64 * 1024 * 1024)
    gc()
    const before = process.memoryUsage().heapUsed
    const started = performance.now()
    client.capture(valid({ verification: file, toolCalls: [{ name: 'upload', input: { file, name: 'a.pdf' } }] }))
    expect(performance.now() - started).toBeLessThan(100)
    expect((process.memoryUsage().heapUsed - before) / 1e6).toBeLessThan(20)
    expect(await client.flush()).toMatchObject({ sent: 1 })
    expect(sent[0]?.verification).toBeUndefined()
    expect(sent[0]?.toolCalls).toEqual([{ name: 'upload', input: { name: 'a.pdf' } }])
    expect(sent[0]?.dropped).toEqual({ verification: 'not_json', 'toolCalls[0].input': 'not_json' })
    await client.shutdown()
  })
})

describe('a detached capture()', () => {
  it('never throws, even passed as a callback', async () => {
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch: ack, logger: silent, onError() {} })
    const { capture } = client
    expect(() => capture(valid())).not.toThrow()
    expect(() => [valid()].forEach(client.capture)).not.toThrow()
    // From a .then(), a throw would be a rejection: unhandled, it ends the app's process.
    await expect(Promise.resolve(valid()).then(client.capture)).resolves.toBeUndefined()
    await client.shutdown()
  })
})

describe('memory', () => {
  it('releases dropped events at once, even inside one synchronous burst', () => {
    const gc = (globalThis as { gc?: () => void }).gc
    if (gc === undefined) throw new Error('run with --expose-gc')
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      maxQueueBytes: 300 * 1024,
      fetch: (() => new Promise(() => {})) as unknown as typeof fetch,
      logger: silent,
      onError() {},
    })
    gc()
    const before = process.memoryUsage().heapUsed
    // 300 events of 200 KB in one synchronous burst: 60 MB if dropped events were retained.
    for (let i = 0; i < 300; i++) client.capture(valid({ question: `${i}`.padEnd(200 * 1024, 'x') }))
    gc()
    const grownMb = (process.memoryUsage().heapUsed - before) / 1e6
    expect(grownMb).toBeLessThan(15)
    void client.shutdown({ timeoutMs: 10 })
  })
})

describe('timeouts', () => {
  it('ends an attempt at its deadline even if a custom fetch ignores the abort', async () => {
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      requestTimeoutMs: 50,
      maxAttempts: 1,
      fetch: (() => new Promise(() => {})) as unknown as typeof fetch,
      logger: silent,
      onError() {},
    })
    client.capture(valid())
    const started = performance.now()
    expect(await client.flush({ timeoutMs: 2000 })).toMatchObject({ failed: 1, pending: 0 })
    expect(performance.now() - started).toBeLessThan(1000)
  })
})

describe('a missing AbortController', () => {
  it('drops that event without using up a beforeSend slot', async () => {
    const real = globalThis.AbortController
    const hooks: string[] = []
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      fetch: ack,
      maxConcurrentHooks: 1,
      beforeSend: (event) => (hooks.push(event.eventId), event),
      logger: silent,
      onError() {},
    })
    try {
      ;(globalThis as { AbortController?: unknown }).AbortController = undefined
      client.capture(valid())
      expect(await client.flush({ timeoutMs: 500 })).toMatchObject({ failed: 1 })
    } finally {
      globalThis.AbortController = real
    }
    client.capture(valid())
    expect(await client.flush({ timeoutMs: 500 })).toMatchObject({ sent: 1 })
    expect(hooks).toHaveLength(1)
    await client.shutdown()
  })
})

describe("a hook's abort listener", () => {
  it('finds the client with the finished event gone: capturing from it counts the hook once', async () => {
    const codes: string[] = []
    const sent: string[] = []
    let client: LSquare | undefined
    client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      maxQueueEvents: 2,
      beforeSendTimeoutMs: 50,
      fetch: (async (url: string, init: RequestInit) => {
        for (const event of JSON.parse(String(init.body)).events as { conversationId: string }[]) sent.push(event.conversationId)
        return (ack as unknown as (url: string, init: RequestInit) => Promise<Response>)(url, init)
      }) as unknown as typeof fetch,
      logger: silent,
      onError: (error) => void codes.push(error.code),
      beforeSend: (event, { signal }) => {
        if (event.conversationId !== 'a') return event
        // Ignores its deadline; its abort listener captures another event.
        signal.addEventListener('abort', () => client?.capture(valid({ conversationId: 'b' })))
        return new Promise(() => {})
      },
    })
    client.capture(valid({ conversationId: 'a' }))
    await new Promise((resolve) => setTimeout(resolve, 100))
    await client.flush({ timeoutMs: 1000 })
    expect(sent).toEqual(['b'])
    await new Promise((resolve) => setImmediate(resolve))
    expect(codes).toEqual(['hook_timeout'])
  })
})

describe('a capture whose own admission shuts the client down', () => {
  it('drops its event: nothing is sent after shutdown()', async () => {
    const sent: string[] = []
    const codes: string[] = []
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      fetch: (async (url: string, init: RequestInit) => {
        for (const event of JSON.parse(String(init.body)).events as { conversationId: string }[]) sent.push(event.conversationId)
        return (ack as unknown as (url: string, init: RequestInit) => Promise<Response>)(url, init)
      }) as unknown as typeof fetch,
      logger: silent,
      onError: (error) => void codes.push(error.code),
    })
    // The app's code read while the event is built, as an abort listener can run while room is made.
    client.capture({
      ...valid({ conversationId: 'late' }),
      get question() {
        void client.shutdown({ timeoutMs: 0 })
        return 'q'
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(sent).toEqual([])
    expect(codes).toEqual(['shutdown'])
  })

  it('takes no room from an event shutdown() is still sending: with the queue full, that one is sent', async () => {
    const sent: string[] = []
    const codes: string[] = []
    let release = () => {}
    const gate = new Promise<void>((resolve) => (release = resolve))
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      maxQueueEvents: 1,
      fetch: (async (url: string, init: RequestInit) => {
        for (const event of JSON.parse(String(init.body)).events as { question: string }[]) sent.push(event.question)
        await gate
        return (ack as unknown as (url: string, init: RequestInit) => Promise<Response>)(url, init)
      }) as unknown as typeof fetch,
      logger: silent,
      onError: (error) => void codes.push(error.code),
    })
    client.capture(valid({ question: 'A' }))
    let closing: Promise<unknown> | undefined
    client.capture({
      ...valid(),
      get question() {
        closing = client.shutdown({ timeoutMs: 2_000 })
        return 'B'
      },
    })
    await until(() => sent.length === 1)
    release()
    expect(await closing).toEqual({ sent: 1, skipped: 0, failed: 0, pending: 0 })
    expect(sent).toEqual(['A'])
    expect(codes).toEqual(['shutdown'])
  })
})

describe('on Cloudflare Workers, a hook that never settles', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('beats a minute past its deadline, then gives its slot back once, however late it settles', async () => {
    vi.stubGlobal('navigator', { userAgent: 'Cloudflare-Workers' })
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'performance'] })
    const started: string[] = []
    const settle = new Map<string, (out: null) => void>()
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      maxConcurrentHooks: 1,
      beforeSendTimeoutMs: 100,
      fetch: ack,
      logger: silent,
      onError() {},
      beforeSend: (event) => {
        const id = String(event.conversationId)
        started.push(id)
        if (id === 'later') return event
        return new Promise((resolve) => settle.set(id, resolve))
      },
    })
    client.capture(valid({ conversationId: 'stuck' }))
    await vi.advanceTimersByTimeAsync(10)
    // Past its deadline it is finished, but still beats: the slot stays its own.
    await vi.advanceTimersByTimeAsync(30_000)
    client.capture(valid({ conversationId: 'early' }))
    await vi.advanceTimersByTimeAsync(200)
    // Its beat stops a minute past its deadline (60.1 s, at the beat of 61 s)...
    await vi.advanceTimersByTimeAsync(31_000)
    client.capture(valid({ conversationId: 'soon' }))
    await vi.advanceTimersByTimeAsync(10)
    expect(started).toEqual(['stuck'])
    // ...and 5 s after its last beat, the next capture finds the slot free.
    await vi.advanceTimersByTimeAsync(6_000)
    client.capture(valid({ conversationId: 'later' }))
    await vi.advanceTimersByTimeAsync(10)
    expect(started).toEqual(['stuck', 'later'])
    // Settling at last releases nothing twice: one slot still runs one hook at a time (on Workers,
    // the newest first).
    settle.get('stuck')!(null)
    await vi.advanceTimersByTimeAsync(10)
    client.capture(valid({ conversationId: 'one' }))
    client.capture(valid({ conversationId: 'two' }))
    await vi.advanceTimersByTimeAsync(10)
    expect(started).toEqual(['stuck', 'later', 'two'])
  })
})
