import { afterEach, describe, expect, it, vi } from 'vitest'
import { INTERNAL } from '../src/client.js'
import type { CallRecord } from '../src/event.js'
import { LSquare, type LSquareError, type LSquareEvent, type LSquareOptions, type TurnEvent } from '../src/index.js'

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
/** onError and loggers run in a later event-loop turn (setImmediate): let them run. */
const tick = () => new Promise<void>((resolve) => setImmediate(() => setImmediate(resolve)))
const until = async (check: () => boolean) => {
  for (let i = 0; i < 200 && !check(); i++) await sleep(5)
}
const prose = (chars: number) => 'lorem ipsum '.repeat(Math.ceil(chars / 12)).slice(0, chars)

interface Request {
  events: LSquareEvent[]
  bytes: number
}
type Reply = (request: Request, n: number, signal: AbortSignal | undefined) => Response | Promise<Response>

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const ack: Reply = ({ events }) => json({ accepted: events.map((e) => e.eventId), duplicates: [], rejected: [] })
const hanging: Reply = (_request, _n, signal) =>
  new Promise((_, reject) => signal?.addEventListener('abort', () => reject(new Error('aborted'))))

function setup(reply: Reply = ack, options: LSquareOptions = {}) {
  const requests: Request[] = []
  const errors: LSquareError[] = []
  const fetch = (async (_url: unknown, init?: RequestInit) => {
    const body = String(init?.body)
    const request = { events: JSON.parse(body).events, bytes: new TextEncoder().encode(body).length }
    requests.push(request)
    return reply(request, requests.length, init?.signal ?? undefined)
  }) as typeof globalThis.fetch
  const client = new LSquare({
    apiKey: 'lsq_test',
    endpoint: 'https://ingest.test',
    fetch,
    onError: (error) => errors.push(error),
    logger: { info() {}, warn() {} },
    ...options,
  })
  return { client, requests, errors }
}

const turn = (i: number, extra: Record<string, unknown> = {}) => ({ conversationId: 'c', turnId: i, question: `q${i}`, ...extra })

function call(client: LSquare, callId: string): void {
  const record: CallRecord = {
    callId,
    capturedAt: new Date().toISOString(),
    toolName: 'search_orders',
    status: 'completed',
    durationMs: 1,
    ids: {},
    diagnostics: false,
  }
  client[INTERNAL].record(record)
}

afterEach(() => {
  vi.useRealTimers()
})

describe('batches', () => {
  it('carry every event ready when a slot frees up, at most 100', async () => {
    const { client, requests } = setup()
    for (let i = 0; i < 250; i++) client.capture(turn(i))
    expect(await client.flush()).toMatchObject({ sent: 250, failed: 0 })
    expect(requests.map((r) => r.events.length)).toEqual([100, 100, 50])
  })

  it('stay within 1 MB of body', async () => {
    const { client, requests } = setup()
    // About 200 KB each: twelve tool calls with a 16,000-character input each.
    const toolCalls = Array.from({ length: 12 }, (_, i) => ({ name: `t${i}`, input: { q: prose(16_000) } }))
    for (let i = 0; i < 10; i++) client.capture(turn(i, { toolCalls }))
    expect(await client.flush()).toMatchObject({ sent: 10 })
    expect(requests.length).toBeGreaterThanOrEqual(2)
    for (const request of requests) expect(request.bytes).toBeLessThanOrEqual(1024 * 1024)
    expect(requests.flatMap((r) => r.events)).toHaveLength(10)
  })

  it('keep their acknowledgement under 60 KiB, even if every event came back over its daily limit', async () => {
    const { client, requests } = setup()
    // IDs that JSON doubles in size, the longest an acknowledgement can quote: 100 of them need ~120 KB.
    for (let i = 0; i < 100; i++) call(client, `${String(i).padStart(3, '0')}${'"'.repeat(509)}`)
    expect(await client.flush()).toMatchObject({ sent: 100 })
    expect(requests.length).toBeGreaterThanOrEqual(2)
    for (const { events } of requests) {
      const longest = { accepted: [], duplicates: [], rejected: events.map((e) => ({ eventId: e.eventId, reason: 'quota_exceeded', resetsAt: '2000-01-01T00:00:00.000Z' })) }
      expect(JSON.stringify(longest).length).toBeLessThanOrEqual(60 * 1024)
    }
  })

  it('go at most 4 at a time by default', async () => {
    const answers: Array<() => void> = []
    let inFlight = 0
    let peak = 0
    const { client, requests, errors } = setup(
      (request, n, signal) =>
        new Promise((resolve) => {
          peak = Math.max(peak, ++inFlight)
          answers.push(() => {
            inFlight--
            resolve(ack(request, n, signal) as Response)
          })
        }),
    )
    for (let i = 0; i < 10; i++) {
      client.capture(turn(i))
      await tick()
    }
    expect(requests.map((r) => r.events.length)).toEqual([1, 1, 1, 1])
    answers.shift()?.()
    await until(() => requests.length === 5)
    // The six that waited go in one request.
    expect(requests[4]?.events).toHaveLength(6)
    while (answers.length > 0) answers.shift()?.()
    expect(await client.flush()).toMatchObject({ failed: 0, pending: 0 })
    expect(errors).toEqual([])
    expect(requests.flatMap((r) => r.events)).toHaveLength(10)
    expect(peak).toBe(4)
  })

  it('a retried batch carries only the events still waiting to be sent', async () => {
    // Retry-After fixes the wait, so the retry can't go before the third event is captured.
    const unavailable = () => new Response('{}', { status: 503, headers: { 'retry-after': '1' } })
    const { client, requests } = setup((request, n, signal) => (n === 1 ? unavailable() : ack(request, n, signal)), { maxQueueEvents: 2 })
    client.capture(turn(1))
    client.capture(turn(2))
    await until(() => requests.length === 1)
    await tick()
    // While both wait to retry, a third pushes out the oldest.
    client.capture(turn(3))
    expect(await client.flush({ timeoutMs: 6_000 })).toMatchObject({ pending: 0 })
    const later = requests.slice(1).flatMap((r) => r.events.map((e) => (e as { question?: string }).question))
    expect(later.sort()).toEqual(['q2', 'q3'])
  })

  it('a request is aborted only once none of its events still needs it', async () => {
    const aborted: number[] = []
    const { client, requests, errors } = setup((_request, n, signal) => {
      signal?.addEventListener('abort', () => aborted.push(n))
      return new Promise(() => {})
    }, { maxQueueEvents: 2 })
    client.capture(turn(1))
    client.capture(turn(2))
    await until(() => requests.length === 1)
    // A full queue drops the oldest event; its request still carries the other.
    client.capture(turn(3))
    await tick()
    expect(errors.map((e) => e.code)).toEqual(['queue_full'])
    expect(aborted).toEqual([])
    // Once both of its events are gone, the request is aborted.
    client.capture(turn(4))
    await tick()
    expect(errors.map((e) => e.code)).toEqual(['queue_full', 'queue_full'])
    expect(aborted).toEqual([1])
  })
})

describe('daily limits', () => {
  const RESETS_AT = '2026-10-29T00:00:00.000Z'
  const NOW = Date.parse('2026-10-28T10:00:00.000Z')
  let overLimit = true
  const limited: Reply = (request, n, signal) =>
    overLimit
      ? json({ accepted: [], duplicates: [], rejected: request.events.map((e) => ({ eventId: e.eventId, reason: 'quota_exceeded', resetsAt: RESETS_AT })) })
      : ack(request, n, signal)

  function clock() {
    vi.useFakeTimers({ toFake: ['Date', 'performance'], now: NOW })
    return (ms: number) => vi.advanceTimersByTime(ms)
  }

  it('are final: never retried, and reported once when holding back starts', async () => {
    overLimit = true
    clock()
    const { client, requests, errors } = setup(limited)
    client.capture(turn(1))
    client.capture(turn(2))
    expect(await client.flush()).toEqual({ sent: 0, skipped: 0, failed: 2, pending: 0 })
    await tick()
    expect(requests).toHaveLength(1)
    expect(errors).toEqual([
      { code: 'quota_exceeded', message: "The project's daily limit was reached; events are dropped until 00:00 UTC.", resetsAt: RESETS_AT },
    ])
  })

  it('hold back until the limit resets: events are dropped as they are captured, but one every 15 minutes checks', async () => {
    overLimit = true
    const advance = clock()
    const hook = vi.fn((event: LSquareEvent) => event)
    const { client, requests, errors } = setup(limited, { beforeSend: hook })
    client.capture(turn(1))
    await client.flush()
    expect(hook).toHaveBeenCalledTimes(1)
    // Holding back: dropped at capture, before their hook, and never sent.
    client.capture(turn(2))
    client.capture(turn(3))
    expect(await client.flush()).toEqual({ sent: 0, skipped: 0, failed: 0, pending: 0 })
    expect(hook).toHaveBeenCalledTimes(1)
    expect(requests).toHaveLength(1)
    // 15 minutes later, the next event checks on its own, and finds the limit still reached.
    advance(15 * 60_000)
    client.capture(turn(4))
    client.capture(turn(5))
    await client.flush()
    expect(requests.map((r) => r.events.length)).toEqual([1, 1])
    // The limit is raised: the next check gets through, and sending resumes with a count of what was lost.
    overLimit = false
    advance(15 * 60_000)
    client.capture(turn(6))
    expect(await client.flush()).toMatchObject({ sent: 1 })
    client.capture(turn(7))
    expect(await client.flush()).toMatchObject({ sent: 1 })
    await tick()
    expect(requests).toHaveLength(4)
    expect(errors.map((e) => e.code)).toEqual(['quota_exceeded', 'quota_dropped'])
    expect(errors[1]).toMatchObject({ count: 5, message: 'Sending resumed after the daily limit; 5 events were dropped.' })
  })

  it('a check that gets no valid acknowledgement keeps holding back', async () => {
    overLimit = true
    const advance = clock()
    let reply: 'limited' | 'empty' = 'limited'
    const { client, requests } = setup((request, n, signal) => (reply === 'empty' ? json({}) : limited(request, n, signal)))
    client.capture(turn(1))
    await client.flush()
    reply = 'empty'
    advance(15 * 60_000)
    client.capture(turn(2))
    await client.flush()
    // Still holding back: dropped, not sent.
    client.capture(turn(3))
    await client.flush()
    expect(requests).toHaveLength(2)
  })

  it("a check's answer ends only the hold-back it was sent for, never a later one", async () => {
    overLimit = true
    const advance = clock()
    let answerCheck: (response: Response) => void = () => {}
    // Over the limit, with the next 00:00 UTC as the ingestion works it out.
    const nextMidnight = () => {
      const next = new Date(Date.now())
      next.setUTCHours(24, 0, 0, 0)
      return next.toISOString()
    }
    // The check (second request) hangs until after midnight: its timeout is longer still.
    const { client, requests } = setup(
      (request, n) => {
        if (n === 2) return new Promise((resolve) => (answerCheck = resolve))
        return json({ rejected: request.events.map((e) => ({ eventId: e.eventId, reason: 'quota_exceeded', resetsAt: nextMidnight() })) })
      },
      { requestTimeoutMs: 20 * 3_600_000 },
    )
    client.capture(turn(1))
    await client.flush()
    advance(15 * 60_000)
    client.capture(turn(2))
    await until(() => requests.length === 2)
    // Midnight passes: the next event goes ahead, and finds the new day's limit reached too.
    advance(14 * 3_600_000)
    client.capture(turn(3))
    await client.flush({ timeoutMs: 100 })
    expect(requests).toHaveLength(3)
    // The old check is accepted at last: the new hold-back stays.
    const checked = requests[1]!.events[0]!.eventId
    answerCheck(json({ accepted: [checked], duplicates: [], rejected: [] }))
    await tick()
    client.capture(turn(4))
    await client.flush()
    expect(requests).toHaveLength(3)
  })

  it('a late answer from a limit that has reset since starts no hold-back, and each event is reported', async () => {
    const advance = clock()
    let answer: (response: Response) => void = () => {}
    // The first request is answered 15 hours on: its timeout is longer still.
    const { client, requests, errors } = setup(
      (request, n) => (n === 1 ? new Promise((resolve) => (answer = resolve)) : ack(request, n, undefined)),
      { requestTimeoutMs: 20 * 3_600_000 },
    )
    client.capture(turn(1))
    await until(() => requests.length === 1)
    // Past midnight, the answer to a request sent before it arrives, naming the reset that has passed.
    advance(15 * 3_600_000)
    answer(json({ rejected: [{ eventId: requests[0]!.events[0]!.eventId, reason: 'quota_exceeded', resetsAt: RESETS_AT }] }))
    await client.flush()
    client.capture(turn(2))
    expect(await client.flush()).toMatchObject({ sent: 1 })
    await tick()
    expect(requests).toHaveLength(2)
    expect(errors.map((e) => `${e.code}: ${e.message}`)).toEqual(['rejected: LSquare rejected the event: quota_exceeded.'])
  })

  it('replace a check unanswered for a whole interval: on Workers, it may have gone with its request', async () => {
    const advance = clock()
    const { client, requests } = setup(
      (request, n) => {
        if (n === 1) return json({ rejected: request.events.map((e) => ({ eventId: e.eventId, reason: 'quota_exceeded', resetsAt: RESETS_AT })) })
        // The check: never answered, its timeout longer still.
        if (n === 2) return new Promise(() => {})
        return ack(request, n, undefined)
      },
      { requestTimeoutMs: 20 * 3_600_000 },
    )
    client.capture(turn(1))
    await client.flush()
    advance(15 * 60_000)
    client.capture(turn(2))
    await until(() => requests.length === 2)
    // A whole interval on: the next event checks instead, and its answer ends the hold-back.
    advance(15 * 60_000)
    client.capture(turn(3))
    await until(() => requests.length === 3)
    expect(requests[2]!.events.map((e) => (e as TurnEvent).turnId)).toEqual(['3'])
    await tick()
    client.capture(turn(4))
    await until(() => requests.length === 4)
    expect(requests[3]!.events.map((e) => (e as TurnEvent).turnId)).toEqual(['4'])
  })

  it('end at resetsAt without a check', async () => {
    overLimit = true
    const advance = clock()
    const { client, requests, errors } = setup(limited)
    client.capture(turn(1))
    await client.flush()
    overLimit = false
    advance(14 * 3_600_000)
    client.capture(turn(2))
    expect(await client.flush()).toMatchObject({ sent: 1 })
    await tick()
    expect(requests).toHaveLength(2)
    expect(errors.map((e) => e.code)).toEqual(['quota_exceeded', 'quota_dropped'])
  })

  it('without a usable resetsAt, hold back until the next 00:00 UTC', async () => {
    overLimit = true
    clock()
    const { client, errors } = setup((request) =>
      json({ rejected: request.events.map((e) => ({ eventId: e.eventId, reason: 'quota_exceeded', resetsAt: 'tomorrow' })) }),
    )
    client.capture(turn(1))
    await client.flush()
    await tick()
    expect(errors[0]).toMatchObject({ code: 'quota_exceeded', resetsAt: RESETS_AT })
  })
})

describe('acknowledgements in a batch', () => {
  it('settle each event on its own: accepted, duplicate, rejected, or missing', async () => {
    const { client, errors } = setup(({ events }) => {
      const [a, b, c] = events.map((e) => e.eventId)
      return json({ accepted: [a], duplicates: [b], rejected: [{ eventId: c, reason: 'invalid_field' }] })
    })
    for (let i = 0; i < 4; i++) client.capture(turn(i))
    expect(await client.flush()).toEqual({ sent: 2, skipped: 0, failed: 2, pending: 0 })
    await tick()
    expect(errors.map((e) => e.code).sort()).toEqual(['rejected', 'send_failed'])
  })
})

describe('fetch', () => {
  it('is never called with the client as `this`: a custom one gets none, the default is bound to globalThis', async () => {
    const receivers: unknown[] = []
    const answer = function (this: unknown, _url: unknown, init?: RequestInit) {
      receivers.push(this)
      const { events } = JSON.parse(String(init?.body)) as { events: LSquareEvent[] }
      return Promise.resolve(json({ accepted: events.map((e) => e.eventId), duplicates: [], rejected: [] }))
    }
    const options = { apiKey: 'lsq_test', endpoint: 'https://ingest.test', logger: { info() {}, warn() {} } }
    const custom = new LSquare({ ...options, fetch: answer as typeof fetch })
    custom.capture(turn(1))
    expect(await custom.flush()).toMatchObject({ sent: 1 })
    // Workers' fetch throws "Illegal invocation" given any other `this`.
    vi.stubGlobal('fetch', answer)
    try {
      const plain = new LSquare(options)
      plain.capture(turn(2))
      expect(await plain.flush()).toMatchObject({ sent: 1 })
    } finally {
      vi.unstubAllGlobals()
    }
    expect(receivers).toEqual([undefined, globalThis])
  })
})

describe('redirects', () => {
  it.each([304, 307])('a %i is never followed: retried as a network error, then failed', async (status) => {
    const { client, requests, errors } = setup(() => new Response(null, { status, headers: { location: 'https://elsewhere.example.com/v1/turns' } }), { maxAttempts: 2 })
    client.capture(turn(1))
    expect(await client.flush()).toMatchObject({ failed: 1 })
    await tick()
    expect(requests).toHaveLength(2)
    expect(errors.map((e) => `${e.code}: ${e.message}`)).toEqual(['send_failed: Network error after 2 attempts.'])
  })

  it("a browser's opaque redirect is refused the same way", async () => {
    const opaque = { status: 0, type: 'opaqueredirect', headers: new Headers(), body: null } as unknown as Response
    const { client, errors } = setup(() => opaque, { maxAttempts: 1 })
    client.capture(turn(1))
    expect(await client.flush()).toMatchObject({ failed: 1 })
    await tick()
    expect(errors.map((e) => e.message)).toEqual(['Network error after 1 attempts.'])
  })
})

// On Workers a request that ends takes its fetches and timers with it, so a request past its
// deadline may never be ended by its own timer, and a retry's timer may never fire. Here, the clock
// moves on while those timers wait (as they would in a blocked event loop).
describe('what a request that ended left behind', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('a request past its deadline is taken back by the next pump: its slot returns once, and its late answer changes nothing', async () => {
    vi.useFakeTimers({ toFake: ['performance'] })
    vi.spyOn(Math, 'random').mockReturnValue(0)
    let answerFirst: (response: Response) => void = () => {}
    let answerSecond: (response: Response) => void = () => {}
    let inFlight = 0
    let most = 0
    const { client, requests, errors } = setup(
      (request, n) => {
        if (n === 1) return new Promise((resolve) => (answerFirst = resolve))
        if (n === 2) return new Promise((resolve) => (answerSecond = resolve))
        most = Math.max(most, ++inFlight)
        return sleep(20).then(() => (inFlight--, ack(request, n, undefined)))
      },
      { maxConcurrentRequests: 1 },
    )
    client.capture(turn(1))
    await until(() => requests.length === 1)
    vi.advanceTimersByTime(10_001)
    client.capture(turn(2))
    await until(() => requests.length === 2)
    // The first request is answered at last, over a daily limit, while its event is on its way
    // again: it finishes nothing, and nothing is held back.
    const first = requests[0]!.events[0]!.eventId
    answerFirst(json({ rejected: [{ eventId: first, reason: 'quota_exceeded', resetsAt: new Date(Date.now() + 3_600_000).toISOString() }] }))
    await tick()
    answerSecond(ack(requests[1]!, 2, undefined) as Response)
    expect(await client.flush()).toEqual({ sent: 2, skipped: 0, failed: 0, pending: 0 })
    // One slot still: two events ready apart go one request after the other.
    client.capture(turn(3))
    await sleep(1)
    client.capture(turn(4))
    expect(await client.flush()).toMatchObject({ sent: 2, failed: 0 })
    expect(most).toBe(1)
    await tick()
    expect(errors).toEqual([])
  })

  it("a retry's earlier timer, firing after the event was retried again, doesn't send it early", async () => {
    vi.useFakeTimers({ toFake: ['performance'] })
    // As in workerd for a timer of another request: clearing it does nothing.
    vi.stubGlobal('clearTimeout', () => {})
    const at: number[] = []
    // Retried 2 s after the first attempt, then 3 s after the second.
    const { client, requests } = setup(
      (request, n) => {
        at.push(Date.now())
        return n <= 2 ? new Response(null, { status: 503, headers: { 'retry-after': String(n + 1) } }) : ack(request, n, undefined)
      },
      { maxConcurrentRequests: 1 },
    )
    try {
      client.capture(turn(1))
      await until(() => requests.length === 1)
      await tick()
      // Its retry is due by the clock before its timer has run: the next pump sends it, and it fails again.
      vi.advanceTimersByTime(2_100)
      client.capture(turn(2))
      await until(() => requests.length === 2)
      // The first timer fires 2 s on, and leaves the second retry, 3 s on, alone.
      expect(await client.flush({ timeoutMs: 6_000 })).toMatchObject({ sent: 2 })
      expect(at[2]! - at[1]!).toBeGreaterThanOrEqual(2_500)
    } finally {
      vi.unstubAllGlobals()
    }
  }, 10_000)
})
