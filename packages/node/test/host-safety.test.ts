import { AsyncLocalStorage } from 'node:async_hooks'
import { Readable } from 'node:stream'
import { describe, expect, it, vi } from 'vitest'
import { LSquare, type BeforeSend, type TurnEvent } from '../src/index.js'
import { jsonLength, toJson } from '../src/json.js'

// The SDK runs inside the customer's process: nothing it does, and nothing the customer's data or
// environment does to it, may crash, hang or bloat that process.

const silent = { info() {}, warn() {} }
const valid = (extra: Record<string, unknown> = {}) => ({ conversationId: 'c', turnId: `t${Math.random()}`, orgId: 'o', toolName: 't', question: 'q', ...extra })
const ack = (async (_url: string, init: RequestInit) => {
  const { events } = JSON.parse(String(init.body)) as { events: { eventId: string }[] }
  return new Response(JSON.stringify({ accepted: events.map((e) => e.eventId), duplicates: [], rejected: [] }))
}) as unknown as typeof fetch

async function unhandledDuring(run: () => Promise<void>): Promise<unknown[]> {
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
    const from = vi.spyOn(Buffer, 'from')
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
