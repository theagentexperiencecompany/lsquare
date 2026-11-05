// Smoke checks of the built @lsquare/node, on each Node version it supports (run.sh, in CI's core
// job): promises the app's code gives the SDK, response bodies left behind, and beforeSend's copy of
// the event. No unhandledRejection or uncaughtException listener: a failure there ends the process,
// as in an app. Usage: node esm.mjs|cjs.cjs promises|streams|hooks, from a directory where the
// package is installed.
const vm = require('node:vm')
const { Readable } = require('node:stream')

const tick = () => new Promise((resolve) => setImmediate(() => setImmediate(resolve)))
const silent = { info() {}, warn() {} }
const valid = (extra = {}) => ({ conversationId: 'c', turnId: `t${Math.random()}`, orgId: 'o', toolName: 't', question: 'q', ...extra })
const acknowledging = (sent) => async (_url, init) => {
  const { events } = JSON.parse(String(init.body))
  sent.push(...events)
  return new Response(JSON.stringify({ accepted: events.map((e) => e.eventId), duplicates: [], rejected: [] }))
}

const sections = {
  async promises(LSquare, check) {
    const sent = []
    const foreign = () => vm.runInNewContext('Promise.reject(new Error("smoke: foreign"))')
    const options = { apiKey: 'k', endpoint: 'https://x.test', fetch: acknowledging(sent), logger: silent, onError() {} }
    Object.defineProperty(options, 'maxAttempts', { enumerable: true, get: foreign })
    const client = new LSquare(options)
    client.capture(Object.defineProperty(valid(), 'verification', { enumerable: true, get: foreign }))
    const length = () => new Proxy([], { get: (t, k) => (k === 'length' ? Promise.reject(new Error('smoke: length')) : Reflect.get(t, k)) })
    client.capture(valid({ toolCalls: length(), latency: length() }))
    let asked = 0
    const proto = new Proxy({ a: 1 }, { getPrototypeOf: () => (asked++ === 0 ? Object.prototype : Promise.reject(new Error('smoke: prototype'))) })
    client.capture(valid({ metadata: proto }))
    const result = await client.flush(Object.defineProperty({}, 'timeoutMs', { enumerable: true, get: foreign }))
    check('3 events sent', result.sent === 3)
    check('length dropped', JSON.stringify(sent[1]?.dropped) === JSON.stringify({ toolCalls: 'wrong_type', latency: 'not_json' }))
    check('metadata kept', sent[2]?.metadata?.a === 1)
    await client.shutdown()
  },

  async streams(LSquare, check) {
    // A late Node stream, from a fetch that ignores the abort.
    let answer
    const answered = new Promise((resolve) => (answer = resolve))
    let late
    const lateClient = new LSquare({
      apiKey: 'k', endpoint: 'https://x.test', logger: silent, onError() {}, requestTimeoutMs: 20, maxAttempts: 1,
      fetch: async () => {
        await answered
        late = new Readable({ read() {} })
        return { status: 200, headers: new Headers(), body: late }
      },
    })
    lateClient.capture(valid())
    check('late: failed', (await lateClient.flush({ timeoutMs: 5000 })).failed === 1)
    answer()
    await tick()
    check('late: destroyed', late?.destroyed === true)
    let threw = false
    try { late.emit('error', new Error('smoke: socket reset')) } catch { threw = true }
    check('late: later error caught', !threw)

    // A stalled web body, cancelled at the timeout.
    let cancelled = false
    const stalled = new LSquare({
      apiKey: 'k', endpoint: 'https://x.test', logger: silent, onError() {}, requestTimeoutMs: 20, maxAttempts: 1,
      fetch: async () => new Response(new ReadableStream({ pull: () => new Promise(() => {}), cancel: () => void (cancelled = true) }), { status: 200 }),
    })
    stalled.capture(valid())
    check('stalled: failed', (await stalled.flush({ timeoutMs: 5000 })).failed === 1)
    check('stalled: cancelled', cancelled)

    // Headers that throw before a Node body is read.
    const body = new Readable({ read() {} })
    const broken = new LSquare({
      apiKey: 'k', endpoint: 'https://x.test', logger: silent, onError() {}, maxAttempts: 1,
      fetch: async () => ({ status: 200, headers: { get() { throw new Error('smoke: headers') } }, body }),
    })
    broken.capture(valid())
    check('headers: failed', (await broken.flush()).failed === 1)
    check('headers: destroyed', body.destroyed === true)
    threw = false
    try { body.emit('error', new Error('smoke: socket reset')) } catch { threw = true }
    check('headers: later error caught', !threw)
  },

  async hooks(LSquare, check) {
    // A vm realm's structuredClone, as jest-environment-node has.
    const sent = []
    const real = globalThis.structuredClone
    globalThis.structuredClone = vm.runInNewContext('(value) => JSON.parse(JSON.stringify(value))')
    try {
      const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch: acknowledging(sent), logger: silent, onError() {}, beforeSend: (event) => event })
      client.capture(valid({ usage: { inputTokens: 1 }, toolCalls: [{ name: 'find', input: { x: 1 } }] }))
      check('realm: sent', (await client.flush()).sent === 1)
      check('realm: nothing dropped', sent[0]?.dropped === undefined && sent[0]?.toolCalls?.[0]?.input?.x === 1)
    } finally {
      globalThis.structuredClone = real
    }
    // An inherited toJSON never reaches the hook's copy.
    const privacy = []
    const client = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch: acknowledging(privacy), logger: silent, onError() {}, beforeSend: (event) => ({ ...event, question: 'scrubbed' }) })
    Object.defineProperty(Object.prototype, 'toJSON', {
      value() { return Object.prototype.hasOwnProperty.call(this, 'eventId') && this.question === 'private question' ? { ...this, answer: this.question } : this },
      configurable: true, writable: true,
    })
    try {
      client.capture(valid({ question: 'private question' }))
      await client.flush()
    } finally {
      delete Object.prototype.toJSON
    }
    check('privacy: scrubbed, no answer', privacy[0]?.question === 'scrubbed' && privacy[0]?.answer === undefined)
    // -0 stays -0.
    let seen
    const zero = new LSquare({ apiKey: 'k', endpoint: 'https://x.test', fetch: acknowledging([]), logger: silent, onError() {}, beforeSend: (event) => ((seen = event.usage?.inputTokens), null) })
    zero.capture(valid({ usage: { inputTokens: -0 } }))
    await zero.flush()
    check('-0 kept', Object.is(seen, -0))
  },
}

module.exports = async function smoke(LSquare, format, section) {
  const failed = []
  await sections[section](LSquare, (name, ok) => void (ok || failed.push(name)))
  await tick()
  console.log(`${format} ${section}: ${failed.length === 0 ? 'ok' : `FAILED ${failed.join(', ')}`}`)
  process.exitCode = failed.length === 0 ? 0 : 1
}
