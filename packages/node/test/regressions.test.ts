// Regression tests for bugs found in code review: re-entrancy, sentinels, work budgets, result
// lengths and non-2xx responses.
import { describe, expect, it, vi } from 'vitest'
import { LSquare, type BeforeSend, type TurnEvent } from '../src/index.js'
import { jsonLength, toJson } from '../src/json.js'
import { normalize } from '../src/normalize.js'

const base = { conversationId: 'conv1', orgId: 'org_1', toolName: 'ask_docs', question: 'q' }
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const ackFor = (init?: RequestInit) => {
  const events = JSON.parse(String(init?.body)).events as TurnEvent[]
  return new Response(JSON.stringify({ accepted: events.map((e) => e.eventId) }))
}

describe('re-entrant callbacks', () => {
  it('never sends an event after reporting it dropped, even if onError captures', async () => {
    const timeline: string[] = []
    const fetch = (async (_url: unknown, init?: RequestInit) => {
      const event = JSON.parse(String(init?.body)).events[0] as TurnEvent
      timeline.push(`send ${event.question}`)
      if (event.question === 'B') {
        return new Promise<Response>((_, reject) => init?.signal?.addEventListener('abort', () => reject(new Error('aborted'))))
      }
      return ackFor(init)
    }) as typeof globalThis.fetch
    const grow: BeforeSend = (event) => (event.question === 'A' ? { ...event, answer: 'x'.repeat(600) } : event)
    let client: LSquare
    client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      fetch,
      beforeSend: grow,
      maxQueueBytes: 1000,
      onError: (error, event) => {
        timeline.push(`dropped ${event?.question ?? error.eventId}`)
        if (event?.question === 'B') client.capture({ ...base, question: 'C' })
      },
    })
    client.capture({ ...base, question: 'B' })
    await sleep(5)
    client.capture({ ...base, question: 'A' })
    await sleep(50)
    await client.flush()
    for (const entry of timeline.filter((line) => line.startsWith('dropped '))) {
      const name = entry.slice('dropped '.length)
      const index = timeline.indexOf(entry)
      expect(timeline.slice(index + 1), `${name} sent after being reported dropped`).not.toContain(`send ${name}`)
    }
  })
})

describe('sentinels', () => {
  it('keeps the string "too_large" as data', () => {
    const result = normalize({ ...base, verification: 'too_large', latency: 'too_large', toolCalls: [{ name: 'x', input: 'too_large' }] })
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.value.body.verification).toBe('too_large')
      expect(result.value.body.toolCalls).toEqual([{ name: 'x', input: 'too_large' }])
    }
  })
})

describe('work budgets', () => {
  it('charges every array slot, including holes', () => {
    const started = performance.now()
    expect(toJson(new Array(1_000_000), { remaining: 20 })).toEqual({ ok: false, tooLarge: true })
    expect(performance.now() - started).toBeLessThan(20)
  })

  it('charges invalid tool calls', () => {
    const calls = Array.from({ length: 100_000 }, () => ({ tool: 'x' }))
    expect(normalize({ ...base, toolCalls: calls }, { budget: 100 })).toMatchObject({ ok: false, code: 'too_large' })
  })

  it('shares one measuring allowance across all tool results', () => {
    const big = Array.from({ length: 900_000 }, () => 0)
    const started = performance.now()
    const result = normalize({ ...base, toolCalls: Array.from({ length: 20 }, () => ({ name: 'x', result: big })) })
    expect(performance.now() - started).toBeLessThan(100)
    expect(result.ok).toBe(true)
  })
})

describe('jsonLength', () => {
  it('matches JSON.stringify, escapes included', () => {
    const values = [{ a: '\n' }, ['\\', '\u0000'], { 'k"ey': 'é\t' }, [1, undefined, null, true, -0, 1e21], 'plain']
    for (const value of values) expect(jsonLength(value, { remaining: 1_000_000 }), JSON.stringify(value)).toBe(JSON.stringify(value).length)
  })
})

describe('non-2xx responses', () => {
  it('acts on a 401 without waiting for its body', async () => {
    let calls = 0
    const fetch = (async (_url: unknown, init?: RequestInit) => {
      calls++
      const body = new ReadableStream({ start: (c) => init?.signal?.addEventListener('abort', () => c.error(new Error('aborted'))) })
      return new Response(body, { status: 401 })
    }) as typeof globalThis.fetch
    const errors: string[] = []
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch, requestTimeoutMs: 30, onError: (e) => errors.push(`${e.code}:${e.status}`) })
    client.capture(base)
    expect(await client.flush()).toMatchObject({ failed: 1 })
    await sleep(0)
    expect(calls).toBe(1)
    expect(errors).toEqual(['send_failed:401'])
  })
})

describe('third review', () => {
  // Each loop is capped inside the test, so a regression fails the test instead of hanging the runner.
  const CAP = 50

  it('onError capturing after shutdown cannot loop forever', async () => {
    let calls = 0
    let client: LSquare
    client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      onError: () => {
        if (++calls <= CAP) client.capture(base)
      },
    })
    await client.shutdown()
    client.capture(base)
    await sleep(10)
    expect(calls).toBeLessThanOrEqual(2)
  })

  it('onError capturing invalid events cannot loop forever', async () => {
    let calls = 0
    let client: LSquare
    client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      onError: () => {
        if (++calls <= CAP) client.capture({ ...base, orgId: '' })
      },
    })
    client.capture({ ...base, orgId: '' })
    await sleep(10)
    expect(calls).toBeLessThanOrEqual(2)
  })

  it('an async onError that captures after an await cannot loop forever', async () => {
    let calls = 0
    let client: LSquare
    client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      onError: async () => {
        await Promise.resolve()
        if (++calls <= CAP) client.capture({ ...base, orgId: '' })
      },
    })
    client.capture({ ...base, orgId: '' })
    await sleep(10)
    expect(calls).toBeLessThanOrEqual(2)
  })

  it('an onError that captures from a timer cannot loop forever', async () => {
    let calls = 0
    let client: LSquare
    client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      onError: () => {
        setTimeout(() => {
          if (++calls <= CAP) client.capture({ ...base, orgId: '' })
        }, 0)
      },
    })
    client.capture({ ...base, orgId: '' })
    await sleep(50)
    expect(calls).toBeLessThanOrEqual(2)
  })

  it('a failure later in the pipeline keeps the depth of the callback that captured it', async () => {
    let calls = 0
    let client: LSquare
    client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      beforeSend: () => {
        throw new Error('hook fails')
      },
      onError: () => {
        if (++calls <= CAP) client.capture(base)
      },
    })
    client.capture(base)
    await sleep(20)
    expect(calls).toBeLessThanOrEqual(2)
  })

  it('a dry-run logger that captures cannot loop forever', async () => {
    let lines = 0
    let client: LSquare
    client = new LSquare({
      dryRun: true,
      logger: {
        info: () => {
          if (++lines <= CAP) client.capture(base)
        },
        warn: () => {},
      },
    })
    client.capture(base)
    await sleep(20)
    expect(lines).toBeLessThanOrEqual(2)
  })

  it('refuses an event that cannot fit next to running hooks, without dropping others', async () => {
    const errors: Array<{ code: string; question: string | undefined }> = []
    const hanging: BeforeSend = (event) => (event.question === 'hooked' ? new Promise<TurnEvent>(() => {}) : event)
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      fetch: (async () => new Promise<Response>(() => {})) as typeof globalThis.fetch,
      beforeSend: hanging,
      maxConcurrentHooks: 1,
      beforeSendTimeoutMs: 5000,
      maxQueueBytes: 700,
      onError: (error) => errors.push({ code: error.code, question: error.eventId }),
    })
    client.capture({ ...base, turnId: 'hooked', question: 'hooked' })
    await sleep(0)
    client.capture({ ...base, turnId: 'waiting', question: 'waiting' })
    client.capture({ ...base, turnId: 'big', question: 'big', answer: 'x'.repeat(300) })
    await sleep(0)
    expect(errors).toEqual([{ code: 'queue_full', question: 'conv1/t/big' }])
  })

  it('copies events for onError only when a custom handler needs them', async () => {
    const clone = vi.spyOn(globalThis, 'structuredClone')
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      fetch: (async () => new Response('', { status: 400 })) as typeof globalThis.fetch,
      logger: { info: () => {}, warn: () => {} },
    })
    client.capture(base)
    await client.flush()
    expect(clone).not.toHaveBeenCalled()
    clone.mockRestore()
  })
})

describe('fourth review', () => {
  // Work handed to something set up outside the callback loses the callback's depth. These loops
  // must still neither starve the event loop nor run unbounded.
  const CAP = 10_000

  it('a worker queue fed from onError neither starves timers nor loops unbounded', async () => {
    let calls = 0
    let timerRan = false
    const queue: Array<() => void> = []
    let wake: () => void = () => {}
    let stop = false
    ;(async () => {
      while (!stop) {
        if (queue.length === 0) await new Promise<void>((resolve) => (wake = resolve))
        queue.shift()?.()
      }
    })()
    let client: LSquare
    client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      onError: () => {
        queue.push(() => {
          if (++calls <= CAP) client.capture({ ...base, orgId: '' })
        })
        wake()
      },
    })
    setTimeout(() => (timerRan = true), 5)
    client.capture({ ...base, orgId: '' })
    await sleep(100)
    stop = true
    wake()
    expect(timerRan).toBe(true)
    expect(calls).toBeLessThanOrEqual(110)
  })

  it('a promise armed outside onError and resolved inside it is bounded too', async () => {
    let calls = 0
    let resolveNext: () => void = () => {}
    let client: LSquare
    const arm = (): void => {
      new Promise<void>((resolve) => (resolveNext = resolve)).then(() => {
        if (++calls <= CAP) client.capture({ ...base, orgId: '' })
        arm()
      })
    }
    arm()
    client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', onError: () => resolveNext() })
    client.capture({ ...base, orgId: '' })
    await sleep(100)
    expect(calls).toBeLessThanOrEqual(110)
  })

  it('reports over the rate limit are counted on the next one delivered', async () => {
    const delivered: Array<{ code: string; suppressed: number | undefined }> = []
    const client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      maxQueueEvents: 1000,
      fetch: (async (_url: unknown, init?: RequestInit) =>
        new Promise<Response>((_, reject) => init?.signal?.addEventListener('abort', () => reject(new Error('aborted'))))) as typeof globalThis.fetch,
      onError: (error) => delivered.push({ code: error.code, suppressed: error.suppressed }),
    })
    for (let i = 0; i < 150; i++) client.capture(base)
    await client.flush({ timeoutMs: 20 })
    await sleep(50)
    client.capture({ ...base, orgId: '' })
    await sleep(10)
    expect(delivered.filter((d) => d.code === 'pending_after_flush')).toHaveLength(100)
    expect(delivered.at(-1)).toEqual({ code: 'missing_required', suppressed: 50 })
  })
})

describe('fifth review', () => {
  it("an event's reports depend on where it was captured, not on which hook freed its slot", async () => {
    let releaseX: () => void = () => {}
    const errors: string[] = []
    let missing = 0
    let client: LSquare
    client = new LSquare({
      apiKey: 'k',
      endpoint: 'https://x.test',
      fetch: (async (_url: unknown, init?: RequestInit) => ackFor(init)) as typeof globalThis.fetch,
      maxConcurrentHooks: 1,
      beforeSendTimeoutMs: 5000,
      beforeSend: (event) => {
        if (event.question === 'X') return new Promise<TurnEvent>((resolve) => (releaseX = () => resolve(event)))
        if (event.question === 'Y') throw new Error('Y fails')
        return event
      },
      onError: (error) => {
        errors.push(`${error.code}:${error.eventId ?? ''}`)
        // Second-level callback captures X, so X is captured two levels deep.
        if (error.code === 'missing_required' && ++missing === 1) client.capture({ ...base, orgId: '' })
        else if (error.code === 'missing_required') client.capture({ ...base, turnId: 'x', question: 'X' })
      },
    })
    client.capture({ ...base, orgId: '' })
    await sleep(20)
    client.capture({ ...base, turnId: 'y', question: 'Y' }) // ordinary code: waits behind X's hook
    await sleep(5)
    releaseX()
    await sleep(20)
    expect(errors).toContain('hook_failed:conv1/t/y')
  })

  it('the default log adds what the rate limit suppressed', async () => {
    vi.useFakeTimers({ toFake: ['performance'] })
    try {
      const warn: string[] = []
      const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', logger: { info: () => {}, warn: (m) => warn.push(m) } })
      for (let i = 0; i < 150; i++) client.capture({ ...base, orgId: '' })
      await sleep(10)
      vi.advanceTimersByTime(61_000)
      client.capture({ ...base, orgId: '' })
      await sleep(10)
      expect(warn).toHaveLength(2)
      expect(warn[1]).toContain('(149 more since the last report)')
    } finally {
      vi.useRealTimers()
    }
  })

  it('a beforeSend that captures cannot loop forever', async () => {
    let calls = 0
    let timerRan = false
    let client: LSquare
    client = new LSquare({
      dryRun: true,
      logger: { info: () => {}, warn: () => {} },
      beforeSend: (event) => {
        if (++calls <= 10_000) client.capture(base)
        return event
      },
    })
    setTimeout(() => (timerRan = true), 5)
    client.capture(base)
    await sleep(50)
    expect(timerRan).toBe(true)
    expect(calls).toBeLessThanOrEqual(3)
  })
})

describe('timeouts beyond Node\'s timer limit', () => {
  it('are capped, so Node never warns and a flush still waits', async () => {
    const warnings: string[] = []
    const onWarning = (warning: Error) => void warnings.push(warning.name)
    process.on('warning', onWarning)
    try {
      let answer!: () => void
      const client = new LSquare({
        apiKey: 'k',
        endpoint: 'https://x.test',
        flushTimeoutMs: 1e15,
        requestTimeoutMs: 1e15,
        beforeSendTimeoutMs: 1e15,
        logger: { info() {}, warn() {} },
        onError() {},
        fetch: (async (_url: string, init: RequestInit) => {
          await new Promise<void>((resolve) => (answer = resolve))
          const { events } = JSON.parse(String(init.body)) as { events: { eventId: string }[] }
          return new Response(JSON.stringify({ accepted: events.map((e) => e.eventId), duplicates: [], rejected: [] }))
        }) as unknown as typeof fetch,
      })
      client.capture({ conversationId: 'c', turnId: 't', orgId: 'o', toolName: 't', question: 'q' })
      const flushed = client.flush({ timeoutMs: 1e15 })
      await new Promise((resolve) => setTimeout(resolve, 50))
      answer()
      expect(await flushed).toMatchObject({ sent: 1 })
      await new Promise((resolve) => setImmediate(resolve))
      expect(warnings).not.toContain('TimeoutOverflowWarning')
      await client.shutdown()
    } finally {
      process.off('warning', onWarning)
    }
  })
})
