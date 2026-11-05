// The core on a stateless Worker, with module-level clients (core.ts) driven by concurrent requests,
// each its own request as in production: Workers runs nothing of one request from another, and a
// request that ends takes its fetches and timers with it. The test pool runs with
// no_handle_cross_request_promise_resolution, the stricter of Workers' two behaviours.
import { SELF } from 'cloudflare:test'
import { beforeEach, describe, expect, it } from 'vitest'
import { holding, lines, seen } from './core.js'

interface Kept {
  events: Array<{ conversationId: string }>
  answered: boolean
}

const ingest = (path: string) => fetch(`https://ingest.test${path}`)
const batches = async () => (await (await ingest('/test/batches')).json()) as Kept[]
/** Conversations the ingestion acknowledged. */
const sent = async () => (await batches()).filter((batch) => batch.answered).flatMap((batch) => batch.events.map((event) => event.conversationId))
const turn = (client: string, id: string, wait?: number, hold?: number) =>
  SELF.fetch(`https://example.com/core/turn?client=${client}&id=${id}${wait === undefined ? '' : `&wait=${wait}`}${hold === undefined ? '' : `&hold=${hold}`}`).then((reply) => reply.text())
const call = (client: string, id: string) => SELF.fetch(`https://example.com/core/call?client=${client}&id=${id}`).then((reply) => reply.text())
/** A call still unbuilt in its own request, until the test takes `id` out of `holding`. */
const unbuilt = (client: string, id: string) => SELF.fetch(`https://example.com/core/unbuilt?client=${client}&id=${id}`).then((reply) => reply.text())
const report = (client: string, id: string, count: number, hold = 0) =>
  SELF.fetch(`https://example.com/core/report?client=${client}&id=${id}&count=${count}&hold=${hold}`).then((reply) => reply.text())
/** Waits until the app's onError has seen `code` in the request of conversation `id`. */
const reported = (code: string, id: string) => eventually(async () => seen.some((record) => record.by === `onError ${code}` && record.tenant === `tenant-${id}`))
const flush = async (client: string, id: string, wait: number, linger = 0) =>
  (await (await SELF.fetch(`https://example.com/core/flush?client=${client}&id=${id}&wait=${wait}&linger=${linger}`)).json()) as { pending: number; ms: number }
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

async function eventually(check: () => Promise<boolean>, ms = 4_000): Promise<void> {
  const deadline = Date.now() + ms
  while (!(await check())) {
    if (Date.now() > deadline) throw new Error(`Timed out; acknowledged: ${JSON.stringify(await sent())}; seen: ${JSON.stringify(seen)}`)
    await sleep(25)
  }
}
const allSent = (...ids: string[]) => eventually(async () => {
  const done = await sent()
  return ids.every((id) => done.includes(id))
})

beforeEach(async () => {
  await ingest('/test/set')
  seen.length = 0
  lines.length = 0
})

describe('a module-level client on a stateless Worker', () => {
  it("sends through Workers' own fetch, which refuses redirect: 'error', and never follows a redirect", async () => {
    await turn('plain', 'a', 2_000)
    await allSent('a')
    // Answered with a 307 to elsewhere: retried as a network error, never sent there.
    await ingest('/test/set?redirect=1')
    await turn('plain', 'b', 3_000)
    await allSent('b')
    expect(await (await ingest('/test/followed')).json()).toBe(0)
  })

  it('sends the events of more concurrent requests than it has request slots', async () => {
    await ingest('/test/set?delay=150')
    await Promise.all(['b1', 'b2', 'b3', 'b4'].map((id) => turn('narrow', id, 5_000)))
    await allSent('b1', 'b2', 'b3', 'b4')
    expect(await flush('narrow', 'status', 0)).toMatchObject({ pending: 0 })
  })

  it('runs beforeSend for the events of more concurrent requests than it has hook slots', async () => {
    await Promise.all(['h1', 'h2', 'h3'].map((id) => turn('hooked', id, 5_000)))
    await allSent('h1', 'h2', 'h3')
    expect(await flush('hooked', 'status', 0)).toMatchObject({ pending: 0 })
  })

  it('takes back the slot of a request that ended before its answer, and its event goes with a later one', async () => {
    await ingest('/test/set?delay=1000&slow=1')
    await turn('short', 'lost', 100)
    // Past the lost request's deadline: the next request's own pump takes its slot back.
    await sleep(500)
    await turn('short', 'next', 3_000)
    await allSent('next')
    // The lost event, queued again once its retry is due (at most 250 ms on), can only go with a
    // batch a living request starts.
    await sleep(400)
    await turn('short', 'later', 3_000)
    await allSent('lost', 'later')
  })

  it("sends a retry whose timer went with the request that ended, with the next request's", async () => {
    // Retried a second on: after its request has stopped waiting (50 ms), taking the timer with it.
    await ingest('/test/set?fail=1&retryAfter=1')
    await turn('narrow', 'r1', 50)
    await sleep(500)
    expect(await sent()).toEqual([])
    await sleep(700)
    await turn('narrow', 'r2', 3_000)
    await allSent('r1', 'r2')
  })

  it('takes back a lost slot while a request waits, with nothing else coming', async () => {
    await ingest('/test/set?delay=1000&slow=1')
    await turn('short', 'gone', 100)
    // Waiting before the lost request's deadline (300 ms): its own watch takes the slot back.
    await turn('short', 'waiting', 2_000)
    await allSent('waiting')
  })

  it('lets two requests wait for an event neither can start, each ending at its deadline', async () => {
    await ingest('/test/set?delay=300&slow=1')
    await turn('narrow', 'busy', 2_000)
    // Queued behind the busy request, by a request that then ends without waiting.
    await turn('narrow', 'orphan')
    await allSent('busy')
    const [one, two] = await Promise.all([flush('narrow', 'w1', 400), flush('narrow', 'w2', 400)])
    for (const result of [one, two]) {
      expect(result.pending).toBe(1)
      expect(result.ms).toBeLessThan(1_000)
    }
    // The next batch that can start carries it.
    await turn('narrow', 'carrier', 2_000)
    await allSent('orphan', 'carrier')
  })

  it("gives a custom logger a report about an event only in that event's request", async () => {
    await ingest('/test/set?delay=1000&slow=1')
    // Its request ends without waiting, taking its request to the ingestion with it.
    await turn('logged', 'gone')
    // Another request waits for it, and finds it still pending: that report isn't its to log.
    expect(await flush('logged', 'w', 300, 100)).toMatchObject({ pending: 1 })
    expect(lines.filter((line) => line.tenant === 'tenant-w')).toEqual([])
  })

  it("reports a call still unbuilt only in the call's request, not in another that flushes", async () => {
    // The call is held unbuilt in its own request until B has been checked, by the test itself:
    // no deadline of the wrapper's, nor any timing, decides whether B finds it pending.
    holding.add('cA')
    try {
      expect(await unbuilt('tracked', 'cA')).toBe('ok cA')
      expect(await flush('tracked', 'B', 200, 100)).toMatchObject({ pending: 1 })
      expect(seen.filter((record) => record.tenant === 'tenant-B')).toEqual([])
    } finally {
      holding.delete('cA')
    }
    // Let go of in its own request: no flush finds it pending any more.
    await eventually(async () => (await flush('tracked', 'C', 0)).pending === 0)
  })

  it('takes back the slot of a hook that went with its request, and keeps it while the request lives', async () => {
    // Its request holds its reply 7 s: alive, its hook keeps beating and keeps the only slot, past
    // its deadline and past the 5 s after which a hook that stopped beating gives it up.
    const held = turn('stuck', 'stuck', undefined, 7_000)
    await sleep(6_000)
    await turn('stuck', 'blocked', 800)
    await held
    // Once the request has ended, its hook's beat stops: 5 s later the slot comes back.
    await sleep(6_000)
    await turn('stuck', 'late', 2_000)
    await allSent('late')
    expect(await sent()).not.toContain('blocked')
  }, 25_000)

  it("admits an event once a hook that went with its request no longer fills the queue", async () => {
    await turn('pinned', 'stuck', 200)
    await sleep(6_000)
    await turn('pinned', 'next', 2_000)
    await allSent('next')
  }, 15_000)

  describe('reports what a request waited for before that wait ends, and the request with it', () => {
    it("a call's event the ingestion refuses, with the wrapper's waitUntil", async () => {
      await ingest('/test/set?fail=1&status=400')
      expect(await call('plain', 'c400')).toBe('ok c400')
      await reported('send_failed', 'c400')
    })

    it("a call's event refused as it's queued, with the wrapper's waitUntil", async () => {
      await call('full', 'cfull')
      await reported('queue_full', 'cfull')
    })

    it('an event the ingestion refuses, with ctx.waitUntil(flush())', async () => {
      await ingest('/test/set?fail=1&status=400')
      await turn('plain', 'f400', 2_000)
      await reported('send_failed', 'f400')
    })

    it('an event still pending when flush(), handed to ctx.waitUntil, returns', async () => {
      await ingest('/test/set?delay=1000&slow=1')
      await turn('plain', 'fpend', 100)
      await reported('pending_after_flush', 'fpend')
    })
  })

  it('takes back the report slots of an async onError whose request ended, once its beat stops, and keeps those of one whose request lives', async () => {
    const started = (id: string) => seen.filter((record) => record.tenant === `tenant-${id}`)
    // Each fills its client's 100 report slots: one request ends with its reply, the other lives 8 s.
    const alive = report('alive', 'a1', 100, 8_000)
    await report('gone', 'g1', 100)
    expect(started('g1')).toHaveLength(100)
    await eventually(async () => started('a1').length === 100)
    // A second on, the rate allows more: the slots don't.
    await sleep(1_100)
    await Promise.all([report('gone', 'g2', 1), report('alive', 'a2', 1)])
    expect([...started('g2'), ...started('a2')]).toEqual([])
    // 5 s on, the ended request's handlers have stopped beating: their slots are back, with the
    // count they carried. The living request's handlers still beat, and keep theirs.
    await sleep(5_000)
    await Promise.all([report('gone', 'g3', 1), report('alive', 'a3', 1)])
    expect(started('g3')).toEqual([{ by: 'onError invalid_id', tenant: 'tenant-g3', about: 'suppressed 1' }])
    expect(started('a3')).toEqual([])
    await alive
  }, 20_000)

  it("runs the app's callbacks only in the request of the event they are about", async () => {
    await Promise.all([
      turn('tenanted', 't1', 1_500),
      turn('tenanted', 't2', 1_500),
      turn('tenanted', 'slow', 1_500),
      // A flush in another request, waiting for all three.
      flush('tenanted', 't3', 1_000),
    ])
    await allSent('t1', 't2')
    await eventually(async () => seen.some((record) => record.by === 'abort'))
    const about = seen.filter((record) => record.about !== 'undefined')
    expect(about.filter((record) => record.by === 'beforeSend').map((record) => record.about).sort()).toEqual(['slow', 't1', 't2'])
    for (const record of about) expect(record, JSON.stringify(record)).toMatchObject({ tenant: `tenant-${record.about}` })
    expect(seen.filter((record) => record.tenant === 'tenant-t3')).toEqual([])
  })
})
